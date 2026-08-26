"""Contract tests for curated device and deployment lookups."""

from __future__ import annotations

import csv
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from cassn.export.metadata_csv import build_deployment_event_record, build_metadata_rows
from cassn.export.wildlife_insights import SUBPROJECT_DESIGN
from cassn.config import AUDIO_FIELDS, IMAGE_FIELDS
from cassn.lookups import (
    LookupSchemaError,
    LookupTables,
    Site,
    build_deployment_rounds,
    deployment_storage_label,
    load_deployment_events,
    load_device_deployments,
    load_plot_names,
    load_sites,
    normalize_deployment_event_metadata,
)


FIELDS = [
    "deployment_id",
    "deployment_event_id",
    "deployment_sequence",
    "site_short_name",
    "plot_number",
    "device_type",
    "device_id",
    "deployment_start_date",
    "deployment_end_date",
    "identifier_policy",
    "feature_type",
    "mounted_on",
    "sensor_height_meters",
]


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def placement(**changes) -> dict:
    row = {
        "deployment_id": "dep-old-camera",
        "deployment_event_id": "UC_TestSite_20260424",
        "deployment_sequence": "0",
        "site_short_name": "TestSite",
        "plot_number": "1",
        "device_type": "ML",
        "device_id": "CAM-OLD",
        "deployment_start_date": "2026-01-08",
        "deployment_end_date": "2026-04-24",
        "identifier_policy": "filed_legacy",
        "feature_type": "Trail game",
        "mounted_on": "",
        "sensor_height_meters": "",
    }
    row.update(changes)
    return row


def deployment_event(**changes) -> dict:
    row = {
        "deployment_event_id": "UC_TestSite_20260424",
        "site_short_name": "TestSite",
        "site_name": "Test Reserve",
        "deployment_event_start_date": "2026-01-08",
        "deployment_event_end_date": "2026-04-24",
    }
    row.update(changes)
    return row


def test_deployment_event_loader_requires_iso_dates_unique_ids_and_matching_suffix(
    tmp_path,
):
    path = tmp_path / "deployment_events.csv"
    fields = [
        "deployment_event_id",
        "site_short_name",
        "site_name",
        "deployment_event_start_date",
        "deployment_event_end_date",
    ]
    write_csv(path, fields, [deployment_event()])
    assert load_deployment_events(path) == [deployment_event()]

    write_csv(
        path,
        fields,
        [deployment_event(deployment_event_end_date="4/24/26")],
    )
    with pytest.raises(LookupSchemaError, match="expected YYYY-MM-DD"):
        load_deployment_events(path)

    write_csv(
        path,
        fields,
        [deployment_event(), deployment_event()],
    )
    with pytest.raises(LookupSchemaError, match="duplicate deployment_event_id"):
        load_deployment_events(path)

    write_csv(
        path,
        fields,
        [deployment_event(deployment_event_id="UC_TestSite_20260423")],
    )
    with pytest.raises(LookupSchemaError, match="does not end with"):
        load_deployment_events(path)


def test_event_table_dates_override_device_placement_dates():
    events, _rows_by_round = build_deployment_rounds(
        [deployment_event(deployment_event_start_date="2026-01-01")],
        [
            placement(
                deployment_start_date="2026-01-08",
                deployment_end_date="2026-04-23",
            )
        ],
    )

    event = events["TestSite"][0]
    assert event["deployment_event_start_date"] == "2026-01-01"
    assert event["deployment_event_end_date"] == "2026-04-24"


def test_historical_event_metadata_is_normalized_at_ingress():
    normalized = normalize_deployment_event_metadata(
        {
            "deployment_start": "2026-01-01",
            "deployment_end": "2026-04-24",
        }
    )
    assert normalized == {
        "deployment_event_start_date": "2026-01-01",
        "deployment_event_end_date": "2026-04-24",
    }


def test_sequential_deployments_remain_individually_addressable():
    first = placement(
        deployment_id="UC_TestSite_plot1_BD_20260424",
        device_type="BD",
        device_id="ARU-OLD",
    )
    successor = placement(
        deployment_id="UC_TestSite_plot1_BD_20260424-seq01",
        deployment_sequence="1",
        device_type="BD",
        device_id="ARU-NEW",
        deployment_start_date="2026-04-23",
    )
    events, rows_by_round = build_deployment_rounds(
        [deployment_event()], [first, successor]
    )
    event = events["TestSite"][0]
    lookups = LookupTables(
        deployments=events,
        _deployment_rows_by_round=rows_by_round,
        deployments_by_id={
            first["deployment_id"]: first,
            successor["deployment_id"]: successor,
        },
    )
    lookups.activate_deployment_round(event["deployment_round_id"])

    rows = lookups.active_rows_for_slot("TestSite", 1, "BD")
    assert [row["deployment_sequence"] for row in rows] == ["0", "1"]
    assert [deployment_storage_label(row) for row in rows] == ["p1_BD", "p1_BD_seq01"]
    assert lookups.active_deployment_for_label("p1_BD_seq01")["device_id"] == "ARU-NEW"


def test_event_record_adds_exact_id_only_for_prospective_inventory():
    devices = [(1, "One", "BD", "p1_BD_seq01")]
    prospective = build_deployment_event_record(
        {}, devices, 1, {"p1_BD_seq01": "UC_TestSite_plot1_BD_20260424-seq01"}
    )
    assert prospective["devices"][0]["deployment_id"].endswith("-seq01")

    historical = build_deployment_event_record({}, devices, 1)
    assert "deployment_id" not in historical["devices"][0]


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
    """Curated plots.csv leaves plot_name blank for many sites; those
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


def test_events_follow_curated_id_and_activate_historical_devices(tmp_path):
    rows = [
        placement(),
        placement(
            deployment_id="dep-aru",
            device_type="BD",
            device_id="ARU-1",
            deployment_start_date="2026-03-04",
            mounted_on="Tree",
            sensor_height_meters="1.5",
        ),
        placement(
            deployment_id="dep-new-camera",
            deployment_event_id="UC_TestSite_20260506",
            device_id="CAM-NEW",
            deployment_start_date="2026-04-24",
            deployment_end_date="2026-05-06",
        ),
        placement(
            deployment_id="",
            deployment_event_id="",
            device_id="CAM-CURRENT",
            deployment_start_date="2026-05-06",
            deployment_end_date="",
        ),
    ]
    path = tmp_path / "deployments.csv"
    write_csv(path, FIELDS, rows)

    deployments = load_device_deployments(path)
    events, rows_by_round = build_deployment_rounds(
        [
            deployment_event(),
            deployment_event(
                deployment_event_id="UC_TestSite_20260506",
                deployment_event_start_date="2026-04-24",
                deployment_event_end_date="2026-05-06",
            ),
        ],
        deployments,
    )

    april = next(
        event
        for event in events["TestSite"]
        if event["deployment_event_end_date"] == "2026-04-24"
    )
    may = next(
        event
        for event in events["TestSite"]
        if event["deployment_event_end_date"] == "2026-05-06"
    )
    assert april["deployment_event_start_date"] == "2026-01-08"
    assert april["device_count"] == 2
    assert april["deployment_event_id"] == "UC_TestSite_20260424"
    assert may["device_count"] == 1
    assert may["deployment_event_id"] == "UC_TestSite_20260506"

    lookups = LookupTables(
        deployments=events,
        _deployment_rows_by_round=rows_by_round,
    )
    assert [
        event["deployment_event_end_date"]
        for event in lookups.returned_rounds("TestSite")
    ] == [
        "2026-05-06",
        "2026-04-24",
    ]
    assert [
        event["deployment_event_start_date"]
        for event in lookups.current_rounds("TestSite")
    ] == [
        "2026-05-06"
    ]
    assert lookups.current_rounds("TestSite")[0]["deployment_event_id"] == ""
    lookups.activate_deployment_round(april["deployment_round_id"])
    assert lookups.cameras[("TestSite", 1, "ML")]["camera_id"] == "CAM-OLD"
    assert lookups.arus[("TestSite", 1, "BD")]["device_id"] == "ARU-1"

    lookups.activate_deployment_round(may["deployment_round_id"])
    assert lookups.cameras[("TestSite", 1, "ML")]["camera_id"] == "CAM-NEW"
    assert not lookups.arus


def test_adjacent_dates_do_not_merge_distinct_curated_events(tmp_path):
    rows = [
        placement(
            deployment_id="curated-one",
            deployment_event_id="UC_TestSite_20260424",
            deployment_end_date="2026-04-24",
        ),
        placement(
            deployment_id="curated-two",
            deployment_event_id="UC_TestSite_20260425",
            device_type="BD",
            device_id="ARU-1",
            deployment_end_date="2026-04-25",
        ),
    ]
    path = tmp_path / "deployments.csv"
    write_csv(path, FIELDS, rows)

    events, rows_by_round = build_deployment_rounds(
        [
            deployment_event(),
            deployment_event(
                deployment_event_id="UC_TestSite_20260425",
                deployment_event_end_date="2026-04-25",
            ),
        ],
        load_device_deployments(path),
    )

    assert {event["deployment_event_id"] for event in events["TestSite"]} == {
        "UC_TestSite_20260424",
        "UC_TestSite_20260425",
    }
    assert sorted(len(rows) for rows in rows_by_round.values()) == [1, 1]


def test_all_open_placements_at_a_site_form_one_current_field_set():
    original = placement(
        deployment_id="",
        deployment_event_id="",
        deployment_end_date="",
        deployment_start_date="2026-04-08",
    )
    later_bird_redeployment = placement(
        deployment_id="",
        deployment_event_id="",
        deployment_end_date="",
        deployment_start_date="2026-06-11",
        plot_number="2",
        device_type="BD",
        device_id="ARU-CURRENT",
    )

    events, rows_by_round = build_deployment_rounds(
        [], [original, later_bird_redeployment]
    )

    assert len(events["TestSite"]) == 1
    current = events["TestSite"][0]
    assert current["deployment_round_id"] == "TestSite:open"
    assert current["deployment_event_start_date"] == "2026-04-08"
    assert current["latest_open_deployment_start_date"] == "2026-06-11"
    assert current["device_count"] == 2
    assert rows_by_round["TestSite:open"] == [original, later_bird_redeployment]


def test_metadata_uses_each_device_placement_interval(tmp_path):
    rows = [
        placement(),
        placement(
            deployment_id="dep-aru",
            device_type="BD",
            device_id="ARU-1",
            deployment_start_date="2026-03-04",
            mounted_on="Tree",
            sensor_height_meters="1.5",
        ),
    ]
    path = tmp_path / "deployments.csv"
    write_csv(path, FIELDS, rows)
    events, rows_by_round = build_deployment_rounds(
        [deployment_event()],
        load_device_deployments(path),
    )
    event = events["TestSite"][0]
    lookups = LookupTables(
        sites=[Site("Test Reserve", "TestSite", "TST")],
        soundhub_config={},
        wi_config={},
        deployments=events,
        _deployment_rows_by_round=rows_by_round,
        deployments_by_id={row["deployment_id"]: row for row in rows},
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
            "deployment_id": "dep-old-camera",
            "plot_number": 1,
            "device_type": "ML",
            "file_type": "image",
            "camera_id": "CAM-OLD",
        },
        {
            **common,
            "device_label": "p1_BD",
            "deployment_id": "dep-aru",
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
            "deployment_event_start_date": event["deployment_event_start_date"],
            "deployment_event_end_date": event["deployment_event_end_date"],
            "deployment_event_id": event["deployment_event_id"],
            "observer": "Tester",
        },
        inventory,
        lookups,
    )

    assert images[0]["start_date"] == "2026-01-08 00:00:00"
    assert images[0]["end_date"] == "2026-04-24 23:59:59"
    assert images[0]["event_name"] == "UC_TestSite_20260424"
    assert images[0]["subproject_design"] == SUBPROJECT_DESIGN
    assert images[0]["site_name"] == "Test Reserve"
    assert images[0]["site_short_name"] == "TestSite"
    assert images[0]["site_code"] == "TST"
    assert audio[0]["date_installed"] == "2026-03-04"
    assert audio[0]["subproject_design"] == SUBPROJECT_DESIGN
    assert audio[0]["mounted_on"] == "Tree"
    assert images[0]["elevation_m"] == "128"
    assert audio[0]["elevation_m"] == "128"


def test_gui_lists_only_returned_rounds_and_shows_current_read_only(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from cassn.box.auth import BoxConfig
    from cassn.gui.wizard import FieldDataWizard

    rows = [
        placement(),
        placement(
            deployment_id="UC_TestSite_plot1_ML_20260424-seq01",
            deployment_sequence="1",
            device_id="CAM-REPLACEMENT",
            deployment_start_date="2026-04-23",
        ),
        placement(
            deployment_id="",
            deployment_event_id="",
            device_id="CAM-CURRENT",
            deployment_start_date="2026-04-24",
            deployment_end_date="",
        ),
    ]
    path = tmp_path / "deployments.csv"
    write_csv(path, FIELDS, rows)
    events, rows_by_round = build_deployment_rounds(
        [deployment_event()],
        load_device_deployments(path),
    )
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
        form = window.deploy_event_combo.parentWidget().layout()
        assert form.labelForField(window.deploy_event_combo).text() == "Deployment Event:"
        assert (
            form.labelForField(window.current_deployment_status_label).text()
            == "Currently Deployed:"
        )
        assert form.labelForField(window.deploy_start_date).text() == "Start Date:"
        assert form.labelForField(window.deploy_end_date).text() == "End Date:"
        assert window.deploy_event_combo.count() == 1
        assert (
            window.deploy_event_combo.currentData()["deployment_event_end_date"]
            == "2026-04-24"
        )
        assert window.deploy_start_date.isReadOnly()
        assert window.deploy_end_date.isReadOnly()
        checks = window.device_checkboxes[1]["ML"]
        assert [checkbox.text() for _row, checkbox in checks] == ["seq00", "seq01"]
        assert all(checkbox.isChecked() for _row, checkbox in checks)
        assert window.observer_combo.currentText() == "Imperato, John"
        assert "1 device in the field" in window.current_deployment_status_label.text()
        assert "deployed 2026-04-24" in window.current_deployment_status_label.text()
    finally:
        window.close()
        app.processEvents()


def test_gui_runs_two_card_jobs_concurrently_and_frees_both_slots(
    tmp_path, monkeypatch
):
    import threading
    import time

    from PySide6.QtWidgets import QApplication, QFileDialog

    import cassn.gui.card_ingest_thread as card_thread_module
    import cassn.gui.wizard as wizard_module
    from cassn.box.auth import BoxConfig
    from cassn.gui.wizard import FieldDataWizard

    class DummyReconyx:
        def start(self):
            return self

        def parse(self, _path):
            return {}

        def close(self):
            return None

    rows = [
        placement(
            deployment_id="UC_TestSite_plot1_ML_20260424",
            identifier_policy="prospective",
        ),
        placement(
            deployment_id="UC_TestSite_plot2_ML_20260424",
            identifier_policy="prospective",
            plot_number="2",
            device_id="CAM-2",
        ),
    ]
    path = tmp_path / "deployments.csv"
    write_csv(path, FIELDS, rows)
    events, rows_by_round = build_deployment_rounds(
        [deployment_event()], load_device_deployments(path)
    )
    lookups = LookupTables(
        sites=[Site("Test Reserve", "TestSite", "TST")],
        plot_names={"TestSite": {1: "One", 2: "Two"}},
        deployments=events,
        _deployment_rows_by_round=rows_by_round,
    )
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(FieldDataWizard, "find_all_sessions", lambda self: [])
    monkeypatch.setattr(FieldDataWizard, "check_box_auth", lambda self: False)
    monkeypatch.setattr(card_thread_module, "ReconyxExtractor", DummyReconyx)
    monkeypatch.setattr(wizard_module, "EXIF_AVAILABLE", False)

    source_paths = []
    for plot in (1, 2):
        source = tmp_path / f"card{plot}"
        source.mkdir()
        (source / f"image{plot}.jpg").write_bytes(f"unique-{plot}".encode())
        source_paths.append(str(source))
    selected_sources = iter(source_paths)
    monkeypatch.setattr(
        QFileDialog,
        "getExistingDirectory",
        lambda *_args, **_kwargs: next(selected_sources),
    )

    original_processor = FieldDataWizard.process_sd_card_files
    both_started = threading.Barrier(2)

    def gated_processor(context, *args):
        both_started.wait(timeout=5)
        return original_processor(context, *args)

    monkeypatch.setattr(FieldDataWizard, "process_sd_card_files", gated_processor)

    window = FieldDataWizard(lookups=lookups, box_config=BoxConfig())
    try:
        event_root = tmp_path / "event"
        (event_root / "raw_data").mkdir(parents=True)
        window.current_deployment_folder = event_root
        window.metadata = {
            "organization": "UC",
            "site_name": "Test Reserve",
            "site_short_name": "TestSite",
            "site_code": "TST",
            "deployment_event_id": "UC_TestSite_20260424",
            "deployment_round_id": "TestSite|2026-01-08|2026-04-24",
            "deployment_event_start_date": "2026-01-08",
            "deployment_event_end_date": "2026-04-24",
            "observer": "Imperato, John",
        }
        window.devices = [
            (1, "One", "ML", "p1_ML"),
            (2, "Two", "ML", "p2_ML"),
        ]
        window.populate_collection_list()

        window.device_tree.setCurrentItem(window.device_tree.topLevelItem(0))
        window.copy_sd_card_data()
        window.device_tree.setCurrentItem(window.device_tree.topLevelItem(1))
        window.copy_sd_card_data()
        assert len(window.card_ingest_threads) == 2

        deadline = time.monotonic() + 10
        while (window.card_ingest_threads or window._retiring_card_threads) and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        app.processEvents()

        assert not window.card_ingest_threads
        assert not window._retiring_card_threads
        assert [
            window.device_tree.topLevelItem(index).text(2) for index in range(2)
        ] == ["Complete", "Complete"]
        assert {row["device_label"] for row in window.file_inventory} == {
            "p1_ML",
            "p2_ML",
        }
        assert all(
            panel["status"].text() == "Complete — safe to eject card"
            for panel in window.card_ingest_panels.values()
        )
        assert window.copy_btn.isEnabled()
    finally:
        window.current_deployment_folder = None
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
