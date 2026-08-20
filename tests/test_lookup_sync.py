"""Tests for Box-first lookup bootstrap and offline-cache fallback."""

from __future__ import annotations

import csv
from pathlib import Path
from types import SimpleNamespace

import pytest

from cassn.box.auth import BoxConfig
from cassn.lookup_sync import (
    LookupBootstrapError,
    bootstrap_lookup_tables,
    validate_device_lookup_pair,
)


def _write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _canonical_files(root: Path) -> dict[str, bytes]:
    _write_csv(
        root / "sites.csv",
        ["site_name", "site_short_name", "site_code"],
        [{"site_name": "Test Reserve", "site_short_name": "TestSite", "site_code": "TST"}],
    )
    _write_csv(
        root / "plots.csv",
        ["site_short_name", "plot_number", "plot_name"],
        [{"site_short_name": "TestSite", "plot_number": "1", "plot_name": "One"}],
    )
    _write_csv(
        root / "devices.csv",
        ["device_record_id", "device_id", "device_type"],
        [{"device_record_id": "ML:CAM1", "device_id": "CAM1", "device_type": "ML"}],
    )
    _write_csv(
        root / "deployments.csv",
        [
            "deployment_id", "deployment_event_id", "site_short_name", "plot_number",
            "device_type", "device_record_id", "device_id", "deployment_start_date",
            "deployment_end_date",
        ],
        [{
            "deployment_id": "UC_TestSite_plot1_ML_20260201", "deployment_event_id": "UC_TestSite_20260201",
            "site_short_name": "TestSite", "plot_number": "1", "device_type": "ML",
            "device_record_id": "ML:CAM1", "device_id": "CAM1",
            "deployment_start_date": "2026-01-01", "deployment_end_date": "2026-02-01",
        }],
    )
    (root / "soundhub_config.json").write_text("{}", encoding="utf-8")
    (root / "wi_config.json").write_text("{}", encoding="utf-8")
    return {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}


class FakeBoxClient:
    def __init__(self, files: dict[str, bytes]):
        self.content = {f"id-{name}": value for name, value in files.items()}
        self.items = [
            SimpleNamespace(type="file", name=name, id=f"id-{name}")
            for name in files
        ]
        self.folders = SimpleNamespace(get_folder_items=self.get_folder_items)
        self.downloads = SimpleNamespace(download_file=self.download_file)

    def get_folder_items(self, _folder_id, **_kwargs):
        return SimpleNamespace(entries=self.items)

    def download_file(self, file_id):
        return [self.content[file_id]]


def test_pair_validation_preserves_curated_deployment_id(tmp_path):
    _canonical_files(tmp_path)
    path = tmp_path / "deployments.csv"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("UC_TestSite_plot1_ML_20260201", "S123_guid_ML"),
        encoding="utf-8",
    )

    result = validate_device_lookup_pair(tmp_path / "devices.csv", path)
    assert result.deployments == 1


def test_pair_validation_requires_event_id_for_closed_placement(tmp_path):
    _canonical_files(tmp_path)
    path = tmp_path / "deployments.csv"
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    fields = list(rows[0])
    rows[0]["deployment_event_id"] = ""
    _write_csv(path, fields, rows)

    with pytest.raises(ValueError, match="closed placement.*without"):
        validate_device_lookup_pair(tmp_path / "devices.csv", path)


def test_pair_validation_rejects_duplicate_event_slot(tmp_path):
    _canonical_files(tmp_path)
    path = tmp_path / "deployments.csv"
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    fields = list(rows[0])
    duplicate = {**rows[0], "deployment_id": "another-curated-id"}
    _write_csv(path, fields, [rows[0], duplicate])

    with pytest.raises(ValueError, match="duplicate plot/device slot"):
        validate_device_lookup_pair(tmp_path / "devices.csv", path)


def test_pair_validation_rejects_reversed_interval(tmp_path):
    _canonical_files(tmp_path)
    path = tmp_path / "deployments.csv"
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    fields = list(rows[0])
    rows[0]["deployment_end_date"] = "2025-12-31"
    _write_csv(path, fields, rows)

    with pytest.raises(ValueError, match="date precedes"):
        validate_device_lookup_pair(tmp_path / "devices.csv", path)


def test_pair_validation_allows_only_unnamed_open_placements(tmp_path):
    _canonical_files(tmp_path)
    path = tmp_path / "deployments.csv"
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    fields = list(rows[0])
    rows[0]["deployment_end_date"] = ""
    rows[0]["deployment_id"] = ""
    rows[0]["deployment_event_id"] = ""
    _write_csv(path, fields, rows)

    result = validate_device_lookup_pair(tmp_path / "devices.csv", path)
    assert result.deployments == 1

    rows[0]["deployment_id"] = "UC_TestSite_plot1_ML_OPEN_20260101"
    _write_csv(path, fields, rows)
    with pytest.raises(ValueError, match="open placement"):
        validate_device_lookup_pair(tmp_path / "devices.csv", path)

    rows[0]["deployment_id"] = ""
    rows[0]["deployment_event_id"] = "INFERRED_TestSite_20260101"
    _write_csv(path, fields, rows)
    with pytest.raises(ValueError, match="deployment_event_id to an open placement"):
        validate_device_lookup_pair(tmp_path / "devices.csv", path)


def test_fresh_authenticated_installation_bootstraps_complete_box_snapshot(tmp_path):
    box_source = tmp_path / "box"
    files = _canonical_files(box_source)
    cache = tmp_path / "cache"

    result = bootstrap_lookup_tables(
        BoxConfig(app_config_folder_id="folder-1"),
        cache,
        client_factory=lambda _config: FakeBoxClient(files),
    )

    assert result.source == "box"
    assert result.warning == ""
    assert result.lookups.site_names == ["Test Reserve"]
    assert (cache / "devices.csv").read_bytes() == files["devices.csv"]
    assert (cache / "deployments.csv").read_bytes() == files["deployments.csv"]


def test_valid_offline_cache_continues_with_clear_warning(tmp_path):
    cache = tmp_path / "cache"
    _canonical_files(cache)

    result = bootstrap_lookup_tables(
        BoxConfig(app_config_folder_id="folder-1"),
        cache,
        client_factory=lambda _config: None,
    )

    assert result.source == "offline-cache"
    assert "last validated local cache" in result.warning


def test_invalid_downloaded_pair_never_replaces_valid_cache(tmp_path):
    cache = tmp_path / "cache"
    before = _canonical_files(cache)
    box_source = tmp_path / "box"
    downloaded = _canonical_files(box_source)
    downloaded["deployments.csv"] = b"site_short_name,deployment_start\nTestSite,2026-01-01\n"

    result = bootstrap_lookup_tables(
        BoxConfig(app_config_folder_id="folder-1"),
        cache,
        client_factory=lambda _config: FakeBoxClient(downloaded),
    )

    assert result.source == "offline-cache"
    assert "wrong schema" in result.warning
    for name, content in before.items():
        assert (cache / name).read_bytes() == content


def test_no_box_and_no_valid_cache_stops_with_actionable_error(tmp_path):
    with pytest.raises(LookupBootstrapError, match="repair the curated lookup files"):
        bootstrap_lookup_tables(
            BoxConfig(app_config_folder_id="folder-1"),
            tmp_path / "missing-cache",
            client_factory=lambda _config: None,
        )
