"""Camera timezone policy, source evidence, and app QC integration."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cassn.core.image_metadata import validate_camera_timezone
from cassn.core.inventory import build_inventory_record
from cassn.core.quality_control import check_camera_timezones
from cassn.gui.wizard import FieldDataWizard


@pytest.mark.parametrize("clock,offset,severity,expected", [
    ("2026:07:01 11:14:07", "-07:00", "pass", ("-07:00",)),
    ("2026:01:01 11:14:07", "-08:00", "pass", ("-08:00",)),
    ("2026:09:05 11:14:07", "-08:00", "warning", ("-07:00",)),
    ("2026:01:01 11:14:07", "-07:00", "warning", ("-08:00",)),
    ("2026:03:08 01:59:59", "-08:00", "pass", ("-08:00",)),
    ("2026:03:08 03:00:00", "-07:00", "pass", ("-07:00",)),
    ("2026:03:08 02:30:00", "-08:00", "error", ()),
    ("2026:11:01 01:30:00", "-07:00", "error", ("-07:00", "-08:00")),
    ("2026:11:01 01:30:00", "-08:00", "error", ("-07:00", "-08:00")),
    ("2026:11:01 01:30:00", "-06:00", "error", ("-07:00", "-08:00")),
    ("2026:11:01 02:00:00", "-08:00", "pass", ("-08:00",)),
    ("2026:07:01 11:14:07", "", "warning", ("-07:00",)),
    ("2026:07:01 11:14:07", "-8:00", "warning", ("-07:00",)),
    ("2026:07:01 11:14:07", "-08:60", "warning", ("-07:00",)),
    ("not a date", "-07:00", "error", ()),
    ("", "", "error", ()),
])
def test_camera_timezone_rules(clock, offset, severity, expected):
    exif = {"DateTimeOriginal": clock, "OffsetTimeOriginal": offset}
    before = dict(exif)
    result = validate_camera_timezone(exif)
    assert result.severity == severity
    assert result.expected_offsets == expected
    assert exif == before


def test_timezone_is_reusable_outside_pacific():
    result = validate_camera_timezone(
        {"DateTimeOriginal": "2026:07:01 11:14:07", "OffsetTimeOriginal": "+05:30"},
        "Asia/Kolkata",
    )
    assert result.severity == "pass"


def image(clock="2026:07:01 11:14:07", offset="-08:00", name="one.jpg"):
    return {
        "file_type": "image", "device_label": "p3_SA", "new_filename": name,
        "camera_datetime_original": clock, "camera_offset_time_original": offset,
        "recorded_datetime": "2026-07-01T12:14:07-07:00",
    }


def test_errors_are_not_hidden_by_missing_offset_or_legacy_warning():
    rows = [image(), image(offset="", name="missing.jpg"),
            {"file_type": "image", "new_filename": "legacy.jpg"},
            {"file_type": "audio"}]
    severity, summary = check_camera_timezones(rows)
    assert severity == "error"
    assert "3 image(s); 0 passed, 1 errors, 2 warnings" in summary
    assert "reported -08:00; expected -07:00" in summary
    assert "one.jpg" in summary


def test_aggregate_bounds_examples_and_ignores_converted_timestamp():
    severity, summary = check_camera_timezones([image(name=f"{n}.jpg") for n in range(100)])
    assert severity == "error"
    assert "100 image(s): Camera timezone offset mismatch" in summary
    assert "2.jpg" in summary and "3.jpg" not in summary


def test_inventory_retains_original_exif_independently_of_conversion():
    row = build_inventory_record(
        original_filename="RCNX0001.JPG", new_filename="one.jpg", plot_num=3,
        plot_label="plot3", dev_code="SA", device_label="p3_SA", device_id="CAM",
        deployment_id="UC_Test_plot3_SA_20260901", file_type="image", file_size_bytes=1,
        file_hash_sha256="hash256", file_hash_sha1="hash1",
        recorded_datetime="2026-07-01T12:14:07-07:00", source_path="/card/RCNX0001.JPG",
        plot_metadata={}, exif_data={"DateTimeOriginal": "2026:07:01 11:14:07", "OffsetTimeOriginal": "-08:00"},
        config_data={}, wav_data={}, reconyx_data={}, trigger_type=None,
        seq_pos=None, seq_total=None, event_num=None, date_installed="2026-04-01",
        soundhub_config={},
    )
    restored = json.loads(json.dumps(row))
    assert restored["camera_datetime_original"] == "2026:07:01 11:14:07"
    assert restored["camera_offset_time_original"] == "-08:00"
    assert restored["recorded_datetime"] == "2026-07-01T11:14:07-07:00"
    assert restored["recorded_datetime_utc"] == "2026-07-01T18:14:07Z"
    assert restored["camera_timestamp_status"] == "offset_mismatch_corrected"
    assert check_camera_timezones([restored])[0] == "warning"


def test_device_qc_persists_error_and_readiness_fails_after_reload(tmp_path):
    context = SimpleNamespace(
        current_deployment_folder=tmp_path,
        metadata={"deployment_event_start_date": "2026-04-01", "deployment_event_end_date": "2026-09-01"},
        file_inventory=[image()], _append_card_message=MagicMock(), log=MagicMock(),
        device_tree=MagicMock(), fixity_check_run=True, box_upload_complete=True,
    )
    context.device_tree.topLevelItemCount.return_value = 0
    FieldDataWizard._run_device_qc_checks(context, context.file_inventory, "p3_SA")
    report = json.loads((tmp_path / "qc/qc_report.json").read_text())
    finding = next(r for r in report["current_state"] if r["check"] == "camera_timezone_consistency")
    assert finding["severity"] == "error"
    assert not any("All checks passed" in str(call) for call in context._append_card_message.call_args_list)
    context.file_inventory = json.loads(json.dumps(context.file_inventory))
    items = FieldDataWizard._build_checklist_items(context)
    assert not next(i for i in items if i["label"] == "Camera timezone QC has no errors")["ok"]
    assert not next(i for i in items if i["label"] == "No QC errors recorded")["ok"]


def test_rerun_replaces_current_error_without_erasing_history(tmp_path):
    context = SimpleNamespace(
        current_deployment_folder=tmp_path,
        metadata={"deployment_event_start_date": "2026-04-01", "deployment_event_end_date": "2026-09-01"},
        _append_card_message=MagicMock(), log=MagicMock(),
    )
    FieldDataWizard._run_device_qc_checks(context, [image()], "p3_SA")
    FieldDataWizard._run_device_qc_checks(context, [image(offset="-07:00")], "p3_SA")
    report = json.loads((tmp_path / "qc/qc_report.json").read_text())
    assert next(r for r in report["current_state"] if r["check"] == "camera_timezone_consistency")["severity"] == "pass"
    history = [r for r in report["history"]["devices"]["p3_SA"] if r["check"] == "camera_timezone_consistency"]
    assert [r["severity"] for r in history] == ["error", "pass"]


def test_unresolved_new_timestamp_is_an_error_after_session_reload(tmp_path):
    from cassn.core.image_metadata import camera_timestamp_fields

    row = {**image(), **camera_timestamp_fields({
        'DateTimeOriginal': '2026:11:01 01:30:00', 'OffsetTimeOriginal': '-08:00'})}
    restored = json.loads(json.dumps(row))
    assert restored['recorded_datetime'] == restored['recorded_datetime_utc'] == ''
    assert restored['camera_timestamp_status'] == 'ambiguous_capture_time'
    context = SimpleNamespace(
        current_deployment_folder=tmp_path,
        metadata={'deployment_event_start_date': '2026-01-01', 'deployment_event_end_date': '2026-12-01'},
        file_inventory=[restored], _append_card_message=MagicMock(), log=MagicMock(),
        device_tree=MagicMock(), fixity_check_run=True, box_upload_complete=True,
    )
    context.device_tree.topLevelItemCount.return_value = 0
    FieldDataWizard._run_device_qc_checks(context, [restored], 'p3_SA')
    report = json.loads((tmp_path / 'qc/qc_report.json').read_text())
    assert next(r for r in report['current_state'] if r['check'] == 'camera_timezone_consistency')['severity'] == 'error'
    items = FieldDataWizard._build_checklist_items(context)
    assert not next(i for i in items if i['label'] == 'Camera timezone QC has no errors')['ok']


def test_sequence_elapsed_time_across_spring_transition():
    from cassn.core.image_metadata import camera_timestamp_fields
    from cassn.core.quality_control import check_sequence_integrity

    entries = []
    for position, clock in enumerate(['2026:03:08 01:59:59', '2026:03:08 03:00:00'], 1):
        entries.append({**image(), **camera_timestamp_fields({
            'DateTimeOriginal': clock, 'OffsetTimeOriginal': '-08:00'}),
            'sequence_event_num': 1, 'sequence_position': position, 'sequence_total': 2})
    assert check_sequence_integrity(entries, 'p3_SA') == []
