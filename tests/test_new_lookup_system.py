"""Contract tests for Survey123-derived device and deployment lookups."""

from __future__ import annotations

import csv
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from cassn.export.metadata_csv import build_metadata_rows
from cassn.config import AUDIO_FIELDS, IMAGE_FIELDS
from cassn.lookups import (
    LookupSchemaError,
    LookupTables,
    Site,
    build_deployment_rounds,
    load_device_deployments,
    load_plot_names,
    load_sites,
)


FIELDS = [
    "deployment_id",
    "deployment_event_id",
    "site_short_name",
    "plot_number",
    "device_type",
    "device_id",
    "deployment_start_date",
    "deployment_start_datetime",
    "deployment_end_date",
    "camera_id",
    "feature_type",
    "sensor_height",
    "sensor_orientation",
    "mounted_on",
    "sensor_height_meters",
    "ARU_status",
]


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def placement(**changes) -> dict:
    row = {
        "deployment_id": "dep-old-camera",
        "deployment_event_id": "source-event",
        "site_short_name": "TestSite",
        "plot_number": "1",
        "device_type": "ML",
        "device_id": "CAM-OLD",
        "deployment_start_date": "2026-01-08",
        "deployment_start_datetime": "2026-01-08T10:00:00-08:00",
        "deployment_end_date": "2026-04-24",
        "camera_id": "CAM-OLD",
        "feature_type": "Trail game",
        "sensor_height": "Knee height",
        "sensor_orientation": "Parallel",
        "mounted_on": "",
        "sensor_height_meters": "",
        "ARU_status": "",
    }
    row.update(changes)
    return row


def test_canonical_site_and_plot_loaders_join_on_short_name(tmp_path):
    sites_path = tmp_path / "sites.csv"
    plots_path = tmp_path / "plots.csv"
    write_csv(
        sites_path,
        ["site_name", "site_short_name", "site_code"],
        [{
            "site_name": "Cahill Riparian Reserve",
            "site_short_name": "Cahill",
            "site_code": "CRR",
        }],
    )
    write_csv(
        plots_path,
        ["site_short_name", "plot_number", "plot_name"],
        [{"site_short_name": "Cahill", "plot_number": "1", "plot_name": "One"}],
    )

    assert load_sites(sites_path) == [Site("Cahill Riparian Reserve", "Cahill", "CRR")]
    plot_names, _plot_metadata = load_plot_names(plots_path)
    assert plot_names == {"Cahill": {1: "One"}}


def test_plots_without_names_are_still_selectable(tmp_path):
    """Survey123-sourced plots.csv leaves plot_name blank for many sites; those
    plots must still reach the wizard's device grid (keyed on plot number)."""
    plots_path = tmp_path / "plots.csv"
    write_csv(
        plots_path,
        ["site_short_name", "plot_number", "plot_name"],
        [
            {"site_short_name": "StrathearnRanch", "plot_number": "1", "plot_name": ""},
            {"site_short_name": "StrathearnRanch", "plot_number": "2", "plot_name": ""},
            {"site_short_name": "Cahill", "plot_number": "1", "plot_name": "One"},
        ],
    )

    plot_names, plot_metadata = load_plot_names(plots_path)

    assert plot_names == {
        "StrathearnRanch": {1: "", 2: ""},
        "Cahill": {1: "One"},
    }
    assert plot_metadata[("StrathearnRanch", 1)]["plot_name"] == ""


def test_plot_elevation_is_read_when_present(tmp_path):
    plots_path = tmp_path / "plots.csv"
    write_csv(
        plots_path,
        ["site_short_name", "plot_number", "plot_name", "elevation_m"],
        [
            {"site_short_name": "Cahill", "plot_number": "1",
             "plot_name": "One", "elevation_m": "128"},
            # Present-but-empty is the common case in the hand-entered Box file
            # and must land as a blank, never as a KeyError downstream.
            {"site_short_name": "Cahill", "plot_number": "2",
             "plot_name": "Two", "elevation_m": ""},
        ],
    )

    _plot_names, plot_metadata = load_plot_names(plots_path)

    assert plot_metadata[("Cahill", 1)]["elevation_m"] == "128"
    assert plot_metadata[("Cahill", 2)]["elevation_m"] == ""


def test_plot_elevation_absent_from_schema_loads_blank(tmp_path):
    """Caches synced from Box before the column existed must keep loading."""
    plots_path = tmp_path / "plots.csv"
    write_csv(
        plots_path,
        ["site_short_name", "plot_number", "plot_name"],
        [{"site_short_name": "Cahill", "plot_number": "1", "plot_name": "One"}],
    )

    _plot_names, plot_metadata = load_plot_names(plots_path)

    assert plot_metadata[("Cahill", 1)]["elevation_m"] == ""


def test_plot_number_zero_is_excluded(tmp_path):
    plots_path = tmp_path / "plots.csv"
    write_csv(
        plots_path,
        ["site_short_name", "plot_number", "plot_name"],
        [
            {"site_short_name": "Cahill", "plot_number": "0", "plot_name": ""},
            {"site_short_name": "Cahill", "plot_number": "1", "plot_name": "One"},
        ],
    )

    plot_names, _plot_metadata = load_plot_names(plots_path)

    assert plot_names == {"Cahill": {1: "One"}}


def test_legacy_site_schema_is_rejected(tmp_path):
    sites_path = tmp_path / "sites.csv"
    write_csv(
        sites_path,
        ["site_name", "site_code", "label_code"],
        [{"site_name": "Cahill Riparian Reserve", "site_code": "Cahill", "label_code": "CRR"}],
    )

    with pytest.raises(LookupSchemaError, match="required columns"):
        load_sites(sites_path)


def test_metadata_schemas_expose_only_canonical_site_fields():
    for fields in (IMAGE_FIELDS, AUDIO_FIELDS):
        assert "site_name" in fields
        assert "site_short_name" in fields
        assert "site_code" in fields
        assert "site" not in fields
        assert "site_full_name" not in fields
        assert "label_code" not in fields


def test_rounds_follow_card_return_date_and_activate_historical_devices(tmp_path):
    rows = [
        placement(),
        placement(
            deployment_id="dep-aru",
            device_type="BD",
            device_id="ARU-1",
            deployment_start_date="2026-03-04",
            deployment_start_datetime="2026-03-04T10:00:00-08:00",
            camera_id="",
            mounted_on="Tree",
            sensor_height_meters="1.5",
        ),
        placement(
            deployment_id="dep-new-camera",
            device_id="CAM-NEW",
            camera_id="CAM-NEW",
            deployment_start_date="2026-04-24",
            deployment_start_datetime="2026-04-24T10:00:00-07:00",
            deployment_end_date="2026-05-06",
        ),
        placement(
            deployment_id="dep-current-camera",
            device_id="CAM-CURRENT",
            camera_id="CAM-CURRENT",
            deployment_start_date="2026-05-06",
            deployment_start_datetime="2026-05-06T10:00:00-07:00",
            deployment_end_date="",
        ),
    ]
    path = tmp_path / "deployments.csv"
    write_csv(path, FIELDS, rows)

    deployments = load_device_deployments(path)
    events, rows_by_round = build_deployment_rounds(deployments)

    april = next(event for event in events["TestSite"] if event["deployment_end"] == "2026-04-24")
    may = next(event for event in events["TestSite"] if event["deployment_end"] == "2026-05-06")
    assert april["deployment_start"] == "2026-01-08"
    assert april["device_count"] == 2
    assert april["deployment_event_id"] == "UC_TestSite_20260424"
    assert may["device_count"] == 1

    lookups = LookupTables(
        deployments=events,
        _deployment_rows_by_round=rows_by_round,
    )
    assert [event["deployment_end"] for event in lookups.returned_rounds("TestSite")] == [
        "2026-05-06",
        "2026-04-24",
    ]
    assert [event["deployment_start"] for event in lookups.current_rounds("TestSite")] == [
        "2026-05-06"
    ]
    assert lookups.current_rounds("TestSite")[0]["deployment_event_id"] == ""
    lookups.activate_deployment_round(april["deployment_round_id"])
    assert lookups.cameras[("TestSite", 1, "ML")]["camera_id"] == "CAM-OLD"
    assert lookups.arus[("TestSite", 1, "BD")]["device_id"] == "ARU-1"

    lookups.activate_deployment_round(may["deployment_round_id"])
    assert lookups.cameras[("TestSite", 1, "ML")]["camera_id"] == "CAM-NEW"
    assert not lookups.arus


def test_metadata_uses_each_device_placement_interval(tmp_path):
    rows = [
        placement(),
        placement(
            deployment_id="dep-aru",
            device_type="BD",
            device_id="ARU-1",
            deployment_start_date="2026-03-04",
            deployment_start_datetime="2026-03-04T10:00:00-08:00",
            camera_id="",
            mounted_on="Tree",
            sensor_height_meters="1.5",
        ),
    ]
    path = tmp_path / "deployments.csv"
    write_csv(path, FIELDS, rows)
    events, rows_by_round = build_deployment_rounds(load_device_deployments(path))
    event = events["TestSite"][0]
    lookups = LookupTables(
        sites=[Site("Test Reserve", "TestSite", "TST")],
        soundhub_config={},
        wi_config={},
        deployments=events,
        _deployment_rows_by_round=rows_by_round,
    )
    lookups.activate_deployment_round(event["deployment_round_id"])
    common = {
        "original_filename": "source",
        "new_filename": "new",
        "file_size_bytes": 1,
        "file_hash_sha256": "sha256",
        "file_hash_sha1": "sha1",
        "recorded_datetime": "2026-04-01T12:00:00-07:00",
        "latitude": "38",
        "longitude": "-122",
        "elevation_m": "128",
    }
    inventory = [
        {
            **common,
            "device_label": "p1_ML",
            "plot_number": 1,
            "device_type": "ML",
            "file_type": "image",
            "camera_id": "CAM-OLD",
        },
        {
            **common,
            "device_label": "p1_BD",
            "plot_number": 1,
            "device_type": "BD",
            "file_type": "audio",
            "device_id": "ARU-1",
        },
    ]

    images, audio = build_metadata_rows(
        {
            "organization": "UC",
            "site_name": "Test Reserve",
            "site_short_name": "TestSite",
            "site_code": "TST",
            "deployment_start": event["deployment_start"],
            "deployment_end": event["deployment_end"],
            "deployment_event_id": event["deployment_event_id"],
            "observer": "Tester",
        },
        inventory,
        lookups,
    )

    assert images[0]["start_date"] == "2026-01-08 00:00:00"
    assert images[0]["end_date"] == "2026-04-24 23:59:59"
    assert images[0]["event_name"] == "2026JAN-2026APR"
    assert images[0]["site_name"] == "Test Reserve"
    assert images[0]["site_short_name"] == "TestSite"
    assert images[0]["site_code"] == "TST"
    assert audio[0]["date_installed"] == "2026-03-04"
    assert images[0]["elevation_m"] == "128"
    assert audio[0]["elevation_m"] == "128"


def test_gui_lists_only_returned_rounds_and_shows_current_read_only(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from cassn.box.auth import BoxConfig
    from cassn.gui.wizard import FieldDataWizard

    rows = [
        placement(),
        placement(
            deployment_id="dep-current-camera",
            device_id="CAM-CURRENT",
            camera_id="CAM-CURRENT",
            deployment_start_date="2026-04-24",
            deployment_start_datetime="2026-04-24T10:00:00-07:00",
            deployment_end_date="",
        ),
    ]
    path = tmp_path / "deployments.csv"
    write_csv(path, FIELDS, rows)
    events, rows_by_round = build_deployment_rounds(load_device_deployments(path))
    lookups = LookupTables(
        sites=[Site("Test Reserve", "TestSite", "TST")],
        plot_names={"TestSite": {1: "One"}},
        deployments=events,
        _deployment_rows_by_round=rows_by_round,
    )
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(FieldDataWizard, "find_all_sessions", lambda self: [])
    monkeypatch.setattr(FieldDataWizard, "check_box_auth", lambda self: False)

    window = FieldDataWizard(lookups=lookups, box_config=BoxConfig())
    try:
        assert window.site_name_combo.currentText() == "Test Reserve"
        assert window.site_short_name_edit.text() == "TestSite"
        assert window.site_code_edit.text() == "TST"
        assert window.deploy_event_combo.count() == 1
        assert window.deploy_event_combo.currentData()["deployment_end"] == "2026-04-24"
        assert "Since 2026-04-24" in window.current_deployment_status_label.text()
        assert "1 device in the field" in window.current_deployment_status_label.text()
    finally:
        window.close()
        app.processEvents()


def test_event_only_legacy_deployments_schema_is_rejected(tmp_path):
    path = tmp_path / "deployments.csv"
    write_csv(
        path,
        ["site_code", "deployment_start", "deployment_end", "deployment_event_id"],
        [{
            "site_code": "TestSite",
            "deployment_start": "2026-01-01",
            "deployment_end": "2026-02-01",
            "deployment_event_id": "UC_TestSite_20260201",
        }],
    )

    with pytest.raises(LookupSchemaError, match="wrong schema"):
        load_device_deployments(path)
