"""Tests for Survey123 OAuth authentication helpers."""

from __future__ import annotations

import csv
import json
import ssl
import stat
import urllib.parse
from pathlib import Path

import pytest

from utils import sync_survey123_lookups as sync
from utils.sync_survey123_lookups import (
    AuthenticationError,
    ConfigError,
    OAuthSettings,
    SurveySource,
    SurveySyncConfig,
    SnapshotError,
    build_authorization_url,
    create_verified_ssl_context,
    fetch_dataset_pages,
    load_config,
    parse_callback_url,
    pkce_challenge,
    redact_auth_data,
    refresh_access_token,
    save_token_record,
    select_related_service,
    validate_feature_service_url,
)


RFC_7636_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
RFC_7636_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def make_config(tmp_path: Path) -> SurveySyncConfig:
    return SurveySyncConfig(
        portal_url="https://example.maps.arcgis.com",
        oauth=OAuthSettings(
            client_id="public-client-id",
            redirect_uri="http://127.0.0.1:8765/oauth/callback",
        ),
        surveys=(
            SurveySource(
                role="ml_camera",
                label="ML Camera Deployment",
                item_id="0123456789abcdef0123456789abcdef",
            ),
        ),
        config_path=tmp_path / "sources.json",
    )


def test_pkce_challenge_matches_rfc_7636_vector():
    assert pkce_challenge(RFC_7636_VERIFIER) == RFC_7636_CHALLENGE


def test_tls_context_requires_certificate_and_hostname_verification():
    context = create_verified_ssl_context()

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.get_ca_certs()


def test_authorization_url_uses_pkce_without_client_secret(tmp_path):
    url = build_authorization_url(
        make_config(tmp_path),
        state_value="random-state",
        code_challenge=RFC_7636_CHALLENGE,
    )
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)

    assert query["client_id"] == ["public-client-id"]
    assert query["redirect_uri"] == ["http://127.0.0.1:8765/oauth/callback"]
    assert query["response_type"] == ["code"]
    assert query["state"] == ["random-state"]
    assert query["code_challenge"] == [RFC_7636_CHALLENGE]
    assert query["code_challenge_method"] == ["S256"]
    assert "client_secret" not in query


def test_callback_returns_code_when_state_and_path_match():
    code = parse_callback_url(
        "/oauth/callback?code=one-time-code&state=expected-state",
        expected_path="/oauth/callback",
        expected_state="expected-state",
    )
    assert code == "one-time-code"


@pytest.mark.parametrize(
    "callback_url",
    [
        "/wrong/path?code=code&state=expected-state",
        "/oauth/callback?code=code&state=wrong-state",
        "/oauth/callback?code=code",
        "/oauth/callback?state=expected-state",
    ],
)
def test_callback_rejects_invalid_or_incomplete_response(callback_url):
    with pytest.raises(AuthenticationError):
        parse_callback_url(
            callback_url,
            expected_path="/oauth/callback",
            expected_state="expected-state",
        )


def test_callback_surfaces_oauth_error_without_accepting_code():
    with pytest.raises(AuthenticationError, match="access_denied"):
        parse_callback_url(
            "/oauth/callback?error=access_denied&state=expected-state",
            expected_path="/oauth/callback",
            expected_state="expected-state",
        )


def test_redaction_covers_nested_oauth_fields():
    redacted = redact_auth_data(
        {
            "client_id": "public",
            "access_token": "secret-access",
            "nested": {"refresh_token": "secret-refresh", "status": "ok"},
        }
    )
    assert redacted == {
        "client_id": "public",
        "access_token": "<redacted>",
        "nested": {"refresh_token": "<redacted>", "status": "ok"},
    }


def test_token_file_is_written_with_owner_only_permissions(tmp_path):
    token_path = tmp_path / "private" / "tokens.json"
    save_token_record(
        token_path,
        {
            "schema_version": 1,
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_at": 1234,
        },
    )

    assert json.loads(token_path.read_text())["refresh_token"] == "refresh"
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_path.parent.stat().st_mode) == 0o700


def test_refresh_uses_client_id_and_refresh_token_without_client_secret(tmp_path):
    config = make_config(tmp_path)
    captured = {}

    def fake_post(url, data):
        captured["url"] = url
        captured["data"] = dict(data)
        return {"access_token": "new-access", "expires_in": 1800}

    refreshed = refresh_access_token(
        config,
        {"refresh_token": "existing-refresh"},
        post_json=fake_post,
        now=1000,
    )

    assert captured["url"].endswith("/sharing/rest/oauth2/token/")
    assert captured["data"] == {
        "f": "json",
        "grant_type": "refresh_token",
        "client_id": "public-client-id",
        "refresh_token": "existing-refresh",
    }
    assert "client_secret" not in captured["data"]
    assert refreshed["refresh_token"] == "existing-refresh"
    assert refreshed["access_token"] == "new-access"
    assert refreshed["expires_at"] == 2800


def test_config_rejects_client_secret(tmp_path):
    path = tmp_path / "sources.json"
    path.write_text(
        json.dumps(
            {
                "portal_url": "https://example.maps.arcgis.com",
                "oauth": {
                    "client_id": "public-client-id",
                    "client_secret": "must-not-be-stored",
                    "redirect_uri": "http://127.0.0.1:8765/oauth/callback",
                },
                "surveys": [
                    {
                        "role": "ml_camera",
                        "label": "ML Camera Deployment",
                        "item_id": "0123456789abcdef0123456789abcdef",
                    }
                ],
            }
        )
    )

    with pytest.raises(ConfigError, match="client_secret"):
        load_config(path)


def test_config_rejects_non_local_callback(tmp_path):
    path = tmp_path / "sources.json"
    path.write_text(
        json.dumps(
            {
                "portal_url": "https://example.maps.arcgis.com",
                "oauth": {
                    "client_id": "public-client-id",
                    "redirect_uri": "https://example.com/oauth/callback",
                },
                "surveys": [
                    {
                        "role": "ml_camera",
                        "label": "ML Camera Deployment",
                        "item_id": "0123456789abcdef0123456789abcdef",
                    }
                ],
            }
        )
    )

    with pytest.raises(ConfigError, match="localhost callback"):
        load_config(path)


def test_feature_service_url_validation_prevents_token_exfiltration():
    safe = "https://services9.arcgis.com/example/arcgis/rest/services/form/FeatureServer/"
    assert validate_feature_service_url(safe) == safe.rstrip("/")

    for unsafe in (
        "http://services9.arcgis.com/example/FeatureServer",
        "https://evil.example/example/FeatureServer",
        "https://services9.arcgis.com/example/FeatureServer?next=https://evil.example",
        "https://services9.arcgis.com/example/MapServer",
    ):
        with pytest.raises(SnapshotError):
            validate_feature_service_url(unsafe)


def test_select_related_service_requires_one_feature_service(tmp_path):
    survey = make_config(tmp_path).surveys[0]
    form = {"id": survey.item_id, "type": "Form"}
    service = {
        "id": "fedcba9876543210fedcba9876543210",
        "type": "Feature Service",
        "url": "https://services9.arcgis.com/example/arcgis/rest/services/form/FeatureServer",
    }

    selected, url = select_related_service(survey, form, {"relatedItems": [service]})
    assert selected["id"] == service["id"]
    assert url == service["url"]

    with pytest.raises(SnapshotError, match="found 0"):
        select_related_service(survey, form, {"relatedItems": []})
    with pytest.raises(SnapshotError, match="found 2"):
        select_related_service(survey, form, {"relatedItems": [service, service]})


def test_fetch_dataset_pages_chunks_ids_and_verifies_completeness():
    requests = []

    def fake_post(url, data):
        requests.append((url, dict(data)))
        if data.get("returnIdsOnly") == "true":
            return {"objectIdFieldName": "objectid", "objectIds": [3, 1, 2]}
        ids = [int(value) for value in data["objectIds"].split(",")]
        return {"features": [{"attributes": {"objectid": value}} for value in ids]}

    ids, pages = fetch_dataset_pages(
        "https://services9.arcgis.com/example/FeatureServer/0",
        {"objectIdField": "objectid", "type": "Feature Layer"},
        "secret-token",
        page_size=2,
        post_json=fake_post,
    )

    assert ids["objectIds"] == [3, 1, 2]
    assert len(pages) == 2
    assert requests[1][1]["objectIds"] == "3,1"
    assert requests[2][1]["objectIds"] == "2"
    assert all(request[1]["token"] == "secret-token" for request in requests)


def test_fetch_dataset_pages_rejects_incomplete_response():
    def fake_post(url, data):
        if data.get("returnIdsOnly") == "true":
            return {"objectIds": [1, 2]}
        return {"features": [{"attributes": {"objectid": 1}}]}

    with pytest.raises(SnapshotError, match="incomplete"):
        fetch_dataset_pages(
            "https://services9.arcgis.com/example/FeatureServer/0",
            {"objectIdField": "objectid", "type": "Feature Layer"},
            "secret-token",
            page_size=10,
            post_json=fake_post,
        )


def test_create_snapshot_writes_private_atomic_layout(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    survey = config.surveys[0]
    resolved = sync.ResolvedSurveyService(
        survey=survey,
        form_item={"id": survey.item_id, "type": "Form", "title": survey.label},
        service_item={
            "id": "fedcba9876543210fedcba9876543210",
            "type": "Feature Service",
        },
        service_url="https://services9.arcgis.com/example/FeatureServer",
        service_schema={"layers": [{"id": 0, "name": "Responses"}], "tables": []},
    )
    monkeypatch.setattr(sync, "authenticated_user", lambda config, token: {"username": "user"})
    monkeypatch.setattr(sync, "resolve_all_survey_services", lambda config, token: [resolved])
    monkeypatch.setattr(
        sync,
        "request_json",
        lambda url, data: {
            "id": 0,
            "name": "Responses",
            "type": "Feature Layer",
            "objectIdField": "objectid",
            "maxRecordCount": 1000,
        },
    )
    monkeypatch.setattr(
        sync,
        "fetch_dataset_pages",
        lambda url, schema, token, page_size: (
            {"objectIdFieldName": "objectid", "objectIds": [1]},
            [{"features": [{"attributes": {"objectid": 1, "value": "raw"}}]}],
        ),
    )

    path, manifest = sync.create_snapshot(
        config,
        "secret-token",
        output_root=tmp_path / "snapshots",
        snapshot_id="20260809T120000Z",
    )

    assert path.name == "20260809T120000Z"
    assert manifest["sources"][0]["datasets"][0]["record_count"] == 1
    assert (path / "ml_camera" / "layers" / "0" / "schema.json").is_file()
    assert (path / "ml_camera" / "layers" / "0" / "pages" / "page-0001.json").is_file()
    assert "secret-token" not in (path / "manifest.json").read_text()
    if sync.os.name != "nt":
        assert all(
            stat.S_IMODE(directory.stat().st_mode) == 0o700
            for directory in (path, *[item for item in path.rglob("*") if item.is_dir()])
        )
        assert all(
            stat.S_IMODE(file.stat().st_mode) == 0o600
            for file in path.rglob("*")
            if file.is_file()
        )


def _write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _make_refresh_fixture(tmp_path: Path) -> tuple[Path, Path]:
    lookup_dir = tmp_path / "lookups"
    lookup_dir.mkdir()
    _write_csv(
        lookup_dir / "plots.csv",
        ["site_code", "plot_number", "plot_name"],
        [{"site_code": "TestSite", "plot_number": "1", "plot_name": "One"}],
    )
    (lookup_dir / "devices.csv").write_text("old devices\n", encoding="utf-8")
    (lookup_dir / "deployments.csv").write_text("old deployments\n", encoding="utf-8")

    candidate = tmp_path / "candidates" / "20260810T120000Z"
    candidate.mkdir(parents=True)
    _write_csv(
        candidate / "devices.csv",
        ["device_record_id", "device_id", "device_type"],
        [{"device_record_id": "ML:CAM1", "device_id": "CAM1", "device_type": "ML"}],
    )
    _write_csv(
        candidate / "deployments.csv",
        [
            "deployment_id",
            "deployment_event_id",
            "site_code",
            "plot_number",
            "device_type",
            "device_record_id",
            "device_id",
            "deployment_start_date",
            "deployment_end_date",
        ],
        [
            {
                "deployment_id": "deployment-1",
                "deployment_event_id": "UC_TestSite_20260201",
                "site_code": "TestSite",
                "plot_number": "1",
                "device_type": "ML",
                "device_record_id": "ML:CAM1",
                "device_id": "CAM1",
                "deployment_start_date": "2026-01-01",
                "deployment_end_date": "2026-02-01",
            }
        ],
    )
    _write_csv(
        candidate / "issues.csv",
        ["severity", "code", "source_role", "source_globalid", "source_objectid", "message"],
        [],
    )
    (candidate / "manifest.json").write_text(
        json.dumps(
            {
                "source_snapshot_id": candidate.name,
                "counts": {"devices": 1, "deployments": 1},
            }
        ),
        encoding="utf-8",
    )
    return candidate, lookup_dir


def test_refresh_installer_validates_backs_up_and_hash_verifies(tmp_path):
    candidate, lookup_dir = _make_refresh_fixture(tmp_path)
    old_devices = (lookup_dir / "devices.csv").read_bytes()
    old_deployments = (lookup_dir / "deployments.csv").read_bytes()

    validation = sync.validate_candidate_lookup_set(candidate, lookup_dir)
    receipt = sync.install_candidate_lookup_set(candidate, lookup_dir, validation)

    assert validation["devices"] == 1
    assert validation["deployments"] == 1
    assert validation["rounds"] == 1
    assert (lookup_dir / "devices.csv").read_bytes() == (candidate / "devices.csv").read_bytes()
    assert (lookup_dir / "deployments.csv").read_bytes() == (candidate / "deployments.csv").read_bytes()
    backup = Path(receipt["backup_path"])
    assert (backup / "devices.csv").read_bytes() == old_devices
    assert (backup / "deployments.csv").read_bytes() == old_deployments
    assert json.loads((lookup_dir / sync.REFRESH_RECEIPT_FILENAME).read_text())["snapshot_id"] == candidate.name
    assert not (lookup_dir / sync.REFRESH_LOCK_FILENAME).exists()


def test_refresh_installer_rolls_back_both_files_if_second_replace_fails(tmp_path, monkeypatch):
    candidate, lookup_dir = _make_refresh_fixture(tmp_path)
    old_devices = (lookup_dir / "devices.csv").read_bytes()
    old_deployments = (lookup_dir / "deployments.csv").read_bytes()
    validation = sync.validate_candidate_lookup_set(candidate, lookup_dir)
    real_replace = sync.os.replace
    failed = False

    def fail_once(source, destination):
        nonlocal failed
        destination = Path(destination)
        if destination == lookup_dir / "deployments.csv" and not failed:
            failed = True
            raise OSError("simulated replacement failure")
        return real_replace(source, destination)

    monkeypatch.setattr(sync.os, "replace", fail_once)

    with pytest.raises(sync.RefreshError, match="Could not install"):
        sync.install_candidate_lookup_set(candidate, lookup_dir, validation)

    assert (lookup_dir / "devices.csv").read_bytes() == old_devices
    assert (lookup_dir / "deployments.csv").read_bytes() == old_deployments
    assert not (lookup_dir / sync.REFRESH_LOCK_FILENAME).exists()


def test_refresh_installer_rolls_back_if_receipt_cannot_be_written(tmp_path, monkeypatch):
    candidate, lookup_dir = _make_refresh_fixture(tmp_path)
    old_devices = (lookup_dir / "devices.csv").read_bytes()
    old_deployments = (lookup_dir / "deployments.csv").read_bytes()
    validation = sync.validate_candidate_lookup_set(candidate, lookup_dir)
    monkeypatch.setattr(
        sync,
        "_private_json_write",
        lambda path, payload: (_ for _ in ()).throw(OSError("simulated receipt failure")),
    )

    with pytest.raises(sync.RefreshError, match="rolled back"):
        sync.install_candidate_lookup_set(candidate, lookup_dir, validation)

    assert (lookup_dir / "devices.csv").read_bytes() == old_devices
    assert (lookup_dir / "deployments.csv").read_bytes() == old_deployments
    assert not (lookup_dir / sync.REFRESH_LOCK_FILENAME).exists()


def test_refresh_validation_failure_never_changes_live_files(tmp_path):
    candidate, lookup_dir = _make_refresh_fixture(tmp_path)
    old_devices = (lookup_dir / "devices.csv").read_bytes()
    old_deployments = (lookup_dir / "deployments.csv").read_bytes()
    (candidate / "deployments.csv").write_text("wrong,schema\n", encoding="utf-8")

    with pytest.raises(sync.RefreshError, match="wrong schema"):
        sync.validate_candidate_lookup_set(candidate, lookup_dir)

    assert (lookup_dir / "devices.csv").read_bytes() == old_devices
    assert (lookup_dir / "deployments.csv").read_bytes() == old_deployments


def test_refresh_command_runs_complete_pipeline(tmp_path, monkeypatch):
    candidate, lookup_dir = _make_refresh_fixture(tmp_path)
    snapshot = tmp_path / "snapshots" / candidate.name
    snapshot.mkdir(parents=True)
    config = make_config(tmp_path)
    args = sync.argparse.Namespace(
        config=config.config_path,
        snapshot_root=snapshot.parent,
        candidate_root=candidate.parent,
        lookup_dir=lookup_dir,
        page_size=500,
    )
    monkeypatch.setattr(sync, "load_config", lambda path: config)
    monkeypatch.setattr(
        sync,
        "usable_token_record",
        lambda loaded: ({"access_token": "secret"}, False),
    )
    monkeypatch.setattr(
        sync,
        "create_snapshot",
        lambda loaded, token, output_root, page_size: (
            snapshot,
            {
                "sources": [
                    {
                        "role": "ml_camera",
                        "datasets": [{"record_count": 1}],
                    }
                ]
            },
        ),
    )
    monkeypatch.setattr(
        sync,
        "transform_legacy_snapshot",
        lambda source, lookups, output: (candidate, {"counts": {}}),
    )

    assert sync.command_refresh(args) == 0
    assert (lookup_dir / "devices.csv").read_bytes() == (candidate / "devices.csv").read_bytes()
    assert (lookup_dir / sync.REFRESH_RECEIPT_FILENAME).is_file()
