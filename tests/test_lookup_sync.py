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
    validate_deployment_lookups,
    validate_lookup_directory,
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
        root / "deployment_events.csv",
        [
            "deployment_event_id", "site_short_name", "site_name",
            "deployment_event_start_date", "deployment_event_end_date",
        ],
        [{
            "deployment_event_id": "UC_TestSite_20260201",
            "site_short_name": "TestSite", "site_name": "Test Reserve",
            "deployment_event_start_date": "2026-01-01",
            "deployment_event_end_date": "2026-02-01",
        }],
    )
    _write_csv(
        root / "deployments.csv",
        [
            "deployment_id", "deployment_event_id", "deployment_sequence",
            "site_short_name", "plot_number", "device_type", "device_id",
            "asset_tag",
            "deployment_start_date", "deployment_end_date", "identifier_policy",
            "feature_type", "mounted_on", "sensor_height_meters",
        ],
        [{
            "deployment_id": "UC_TestSite_plot1_ML_20260201", "deployment_event_id": "UC_TestSite_20260201",
            "deployment_sequence": "0",
            "site_short_name": "TestSite", "plot_number": "1", "device_type": "ML",
            "device_id": "CAM1",
            "asset_tag": "",
            "deployment_start_date": "2026-01-01", "deployment_end_date": "2026-02-01",
            "identifier_policy": "", "feature_type": "Trail game",
            "mounted_on": "", "sensor_height_meters": "",
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


def test_validation_rejects_noncanonical_prospective_deployment_id(tmp_path):
    _canonical_files(tmp_path)
    path = tmp_path / "deployments.csv"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("UC_TestSite_plot1_ML_20260201", "S123_guid_ML"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="do not match the prospective naming contract"):
        validate_deployment_lookups(path, tmp_path / "deployment_events.csv")


def test_validation_accepts_explicit_already_filed_legacy_identifier(tmp_path):
    _canonical_files(tmp_path)
    path = tmp_path / "deployments.csv"
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    fields = list(rows[0])
    rows[0]["deployment_id"] = "EXACT_ID_ALREADY_FILED_EXTERNALLY"
    rows[0]["identifier_policy"] = "filed_legacy"
    _write_csv(path, fields, rows)

    result = validate_deployment_lookups(path, tmp_path / "deployment_events.csv")
    assert result.deployments == 1


def test_pair_validation_allows_closed_placement_awaiting_event_assignment(tmp_path):
    _canonical_files(tmp_path)
    path = tmp_path / "deployments.csv"
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    fields = list(rows[0])
    rows[0]["deployment_event_id"] = ""
    _write_csv(path, fields, rows)

    result = validate_deployment_lookups(path, tmp_path / "deployment_events.csv")
    assert result.deployments == 1


def test_pair_validation_rejects_unknown_event_and_site_mismatch(tmp_path):
    _canonical_files(tmp_path)
    path = tmp_path / "deployments.csv"
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    fields = list(rows[0])

    rows[0]["deployment_event_id"] = "UC_TestSite_20260202"
    _write_csv(path, fields, rows)
    with pytest.raises(ValueError, match="unknown deployment_event_id"):
        validate_deployment_lookups(path, tmp_path / "deployment_events.csv")

    rows[0]["deployment_event_id"] = "UC_TestSite_20260201"
    rows[0]["site_short_name"] = "OtherSite"
    _write_csv(path, fields, rows)
    with pytest.raises(ValueError, match="site_short_name disagrees"):
        validate_deployment_lookups(path, tmp_path / "deployment_events.csv")


def test_event_dates_do_not_have_to_equal_device_placement_dates(tmp_path):
    _canonical_files(tmp_path)
    path = tmp_path / "deployments.csv"
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    fields = list(rows[0])
    rows[0]["deployment_start_date"] = "2026-01-05"
    rows[0]["deployment_end_date"] = "2026-01-31"
    _write_csv(path, fields, rows)

    rows[0]["deployment_id"] = "UC_TestSite_plot1_ML_20260131"
    _write_csv(path, fields, rows)
    result = validate_deployment_lookups(path, tmp_path / "deployment_events.csv")
    assert result.deployment_events == 1


def test_validation_allows_deployment_dates_outside_event_window(tmp_path):
    _canonical_files(tmp_path)
    path = tmp_path / "deployments.csv"
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    fields = list(rows[0])
    rows[0]["deployment_start_date"] = "2025-12-31"
    _write_csv(path, fields, rows)

    result = validate_deployment_lookups(path, tmp_path / "deployment_events.csv")
    assert result.deployments == 1


def test_directory_validation_rejects_event_site_name_mismatch(tmp_path):
    _canonical_files(tmp_path)
    path = tmp_path / "deployment_events.csv"
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    fields = list(rows[0])
    rows[0]["site_name"] = "Wrong Reserve"
    _write_csv(path, fields, rows)

    with pytest.raises(ValueError, match="site_name disagrees with sites.csv"):
        validate_lookup_directory(tmp_path)


def test_validation_allows_sequential_deployments_but_rejects_duplicate_sequence(tmp_path):
    _canonical_files(tmp_path)
    path = tmp_path / "deployments.csv"
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    fields = list(rows[0])
    successor = {
        **rows[0],
        "deployment_id": "UC_TestSite_plot1_ML_20260201-seq01",
        "deployment_sequence": "1",
        "deployment_start_date": "2026-02-01",
    }
    _write_csv(path, fields, [rows[0], successor])
    result = validate_deployment_lookups(path, tmp_path / "deployment_events.csv")
    assert result.deployments == 2

    successor["deployment_sequence"] = "0"
    successor["deployment_id"] = rows[0]["deployment_id"]
    _write_csv(path, fields, [rows[0], successor])
    with pytest.raises(ValueError, match="duplicate deployment_id"):
        validate_deployment_lookups(path, tmp_path / "deployment_events.csv")


def test_validation_rejects_one_audiomoth_in_overlapping_event_slots(tmp_path):
    _canonical_files(tmp_path)
    path = tmp_path / "deployments.csv"
    fields = [
        "deployment_id", "deployment_event_id", "deployment_sequence",
        "site_short_name", "plot_number", "device_type", "device_id",
        "asset_tag", "deployment_start_date", "deployment_end_date",
        "identifier_policy", "feature_type", "mounted_on",
        "sensor_height_meters",
    ]
    common = {
        "deployment_event_id": "UC_TestSite_20260201",
        "deployment_sequence": "0",
        "site_short_name": "TestSite",
        "device_id": "24F3190464890001",
        "deployment_start_date": "2026-01-01",
        "deployment_end_date": "2026-02-01",
        "identifier_policy": "",
        "feature_type": "",
        "mounted_on": "metal_pole",
        "sensor_height_meters": "2.5",
    }
    rows = [
        {
            **common,
            "deployment_id": "UC_TestSite_plot1_BD_20260201",
            "plot_number": "1",
            "device_type": "BD",
            "asset_tag": "0001",
        },
        {
            **common,
            "deployment_id": "UC_TestSite_plot2_BT_20260201",
            "plot_number": "2",
            "device_type": "BT",
            "asset_tag": "0002",
        },
    ]
    _write_csv(path, fields, rows)

    with pytest.raises(ValueError, match="overlapping device slots"):
        validate_deployment_lookups(path, tmp_path / "deployment_events.csv")


def test_sequence_suffix_is_not_used_when_end_dates_are_unique(tmp_path):
    _canonical_files(tmp_path)
    path = tmp_path / "deployments.csv"
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    fields = list(rows[0])
    rows[0]["deployment_end_date"] = "2026-01-31"
    rows[0]["deployment_id"] = "UC_TestSite_plot1_ML_20260131"
    successor = {
        **rows[0],
        "deployment_id": "UC_TestSite_plot1_ML_20260201",
        "deployment_sequence": "1",
        "deployment_start_date": "2026-01-31",
        "deployment_end_date": "2026-02-01",
    }
    _write_csv(path, fields, [rows[0], successor])

    result = validate_deployment_lookups(path, tmp_path / "deployment_events.csv")
    assert result.deployments == 2

    successor["deployment_id"] = "UC_TestSite_plot1_ML_20260201-seq01"
    _write_csv(path, fields, [rows[0], successor])
    with pytest.raises(ValueError, match="do not match the prospective naming contract"):
        validate_deployment_lookups(path, tmp_path / "deployment_events.csv")


def test_pair_validation_rejects_reversed_interval(tmp_path):
    _canonical_files(tmp_path)
    path = tmp_path / "deployments.csv"
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    fields = list(rows[0])
    rows[0]["deployment_end_date"] = "2025-12-31"
    _write_csv(path, fields, rows)

    with pytest.raises(ValueError, match="end date before its start date"):
        validate_deployment_lookups(path, tmp_path / "deployment_events.csv")


def test_pair_validation_allows_sequenced_but_unnamed_open_placements(tmp_path):
    _canonical_files(tmp_path)
    path = tmp_path / "deployments.csv"
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    fields = list(rows[0])
    closed = rows[0]
    open_row = {**closed}
    open_row["deployment_start_date"] = "2026-02-01"
    open_row["deployment_end_date"] = ""
    open_row["deployment_id"] = ""
    open_row["deployment_event_id"] = ""
    open_row["deployment_sequence"] = "1"
    _write_csv(path, fields, [closed, open_row])

    result = validate_deployment_lookups(path, tmp_path / "deployment_events.csv")
    assert result.deployments == 2

    open_row["deployment_id"] = "UC_TestSite_plot1_ML_OPEN_20260101"
    _write_csv(path, fields, [closed, open_row])
    with pytest.raises(ValueError, match="identifier to an open deployment"):
        validate_deployment_lookups(path, tmp_path / "deployment_events.csv")

    open_row["deployment_id"] = ""
    open_row["deployment_event_id"] = "INFERRED_TestSite_20260101"
    _write_csv(path, fields, [closed, open_row])
    with pytest.raises(ValueError, match="identifier to an open deployment"):
        validate_deployment_lookups(path, tmp_path / "deployment_events.csv")


def test_fresh_authenticated_installation_bootstraps_complete_box_snapshot(tmp_path):
    box_source = tmp_path / "box"
    files = _canonical_files(box_source)
    cache = tmp_path / "cache"
    cache.mkdir()
    for legacy_name in ("devices.csv", "cameras.csv", "ARUs.csv"):
        (cache / legacy_name).write_text("retired\n", encoding="utf-8")

    result = bootstrap_lookup_tables(
        BoxConfig(app_config_folder_id="folder-1"),
        cache,
        client_factory=lambda _config: FakeBoxClient(files),
    )

    assert result.source == "box"
    assert result.warning == ""
    assert result.lookups.site_names == ["Test Reserve"]
    assert result.lookups.deployment_events[0]["deployment_event_id"] == (
        "UC_TestSite_20260201"
    )
    assert (cache / "deployments.csv").read_bytes() == files["deployments.csv"]
    assert (cache / "deployment_events.csv").read_bytes() == files["deployment_events.csv"]
    for legacy_name in ("devices.csv", "cameras.csv", "ARUs.csv"):
        assert not (cache / legacy_name).exists()


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
