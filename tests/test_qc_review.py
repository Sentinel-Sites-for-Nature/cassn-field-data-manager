"""Unusual findings keep evidence; expected offsets stay bounded and non-actionable."""
import json

from cassn.core.image_metadata import CAMERA_TIMESTAMP_SOURCE
from cassn.core.qc_review import collection_review_findings, small_file_findings
from cassn.core.quality_control import (
    append_qc_report, check_camera_serial, check_camera_timezones, check_sequence_integrity,
)


def image(name="one.jpg", device="p4_ML", **changes):
    return dict({
        "file_type": "image", "device_type": "ML", "device_label": device,
        "new_filename": name, "camera_datetime_original": "2026:08:25 12:00:00",
        "camera_offset_time_original": "-08:00", "camera_timestamp_source": CAMERA_TIMESTAMP_SOURCE,
        "camera_serial_exif": "4LPXKT08021269", "camera_id": "08021269",
        "file_size_bytes": 700_000,
    }, **changes)


def report_entries(rows, *, structured=True):
    entries = []
    for device in dict.fromkeys(row["device_label"] for row in rows):
        device_rows = [row for row in rows if row["device_label"] == device]
        for check, run in (("camera_timezone_consistency", check_camera_timezones),
                           ("camera_serial_match", check_camera_serial),
                           ("sequence_gap", check_sequence_integrity)):
            evidence = []
            if check == "camera_timezone_consistency":
                severity, message = run(device_rows, findings=evidence)
            else:
                messages = run(device_rows, device, findings=evidence)
                severity, message = ("warning", "\n".join(messages)) if messages else ("pass", "OK")
            entry = {"check": check, "_device": device, "severity": severity, "message": message}
            if structured:
                entry["findings"] = evidence
            entries.append(entry)
        evidence = small_file_findings(device_rows)
        entries.append({"check": "file_size_floor", "_device": device,
            "severity": "warning" if evidence else "pass", "message": "Small files" if evidence else "OK",
            **({"findings": evidence} if structured else {})})
    return entries


def test_three_symptoms_combine_only_for_the_same_device_and_filename():
    rows = [image("bad.jpg", camera_datetime_original="", camera_serial_exif="", file_size_bytes=10),
            image("bad.jpg", device="p1_ML")]
    findings = collection_review_findings(report_entries(rows), rows)
    errors = [f for f in findings if f["severity"] == "error"]
    assert len(errors) == 1
    combined = errors[0]
    assert combined["files"] == [{"device_label": "p4_ML", "filename": "bad.jpg"}]
    for symptom in ("capture time", "camera serial", "small file"):
        assert symptom in combined["title"]
    assert "10 bytes" in combined["message"]
    notices = [f for f in findings if f["severity"] == "info"]
    assert len(notices) == 1 and not notices[0]["files"]
    assert "1 photo" in notices[0]["affected"]


def test_distinct_files_are_not_assumed_to_share_symptoms():
    rows = [image("no-time.jpg", camera_datetime_original=""),
            image("no-serial.jpg", camera_serial_exif=""),
            image("small.jpg", file_size_bytes=10)]
    findings = collection_review_findings(report_entries(rows), rows)
    assert len([f for f in findings if f["files"]]) == 3
    assert all(" + " not in f["title"] for f in findings)


def test_legacy_reports_are_enriched_from_retained_metadata_without_media(tmp_path):
    rows = [image("bad.jpg", camera_datetime_original="", camera_serial_exif="", file_size_bytes=10)]
    findings = collection_review_findings(report_entries(rows, structured=False), rows)
    assert len(findings) == 1
    assert "Unreadable camera serial" in findings[0]["title"]
    assert "Unusually small file" in findings[0]["title"]
    assert not list(tmp_path.iterdir())


def test_resolved_offsets_retain_counts_without_unbounded_file_lists():
    rows = [image(f"expected-{i}.jpg", device=f"p{i % 2 + 1}_ML") for i in range(2000)]
    entries = report_entries(rows)
    notices = [entry for entry in entries if entry["check"] == "camera_timezone_consistency"]
    for entry in notices:
        assert len(entry["findings"]) == 1
        assert entry["findings"][0]["files"] == []
        assert entry["findings"][0]["count"] == 1000
        assert len(json.dumps(entry)) < 1600
    findings = collection_review_findings(entries, rows)
    assert len(findings) == 1
    assert findings[0]["severity"] == "info"
    assert findings[0]["affected"] == "2,000 photos · 2 devices"
    assert "expected-" not in findings[0]["message"]
    assert "clock accuracy has not been verified" in findings[0]["action"]


def test_historical_offset_errors_and_ambiguous_times_remain_errors():
    rows = [image("historical.jpg", camera_timestamp_source="old"),
            image("ambiguous.jpg", camera_datetime_original="2026:11:01 01:30:00")]
    findings = collection_review_findings(report_entries(rows), rows)
    assert not any(f["severity"] == "info" for f in findings)
    assert sum(f["severity"] == "error" for f in findings) == 2
    assert any("Historical timestamp" in f["title"] for f in findings)
    assert any("Ambiguous capture time" in f["title"] for f in findings)


def test_missing_original_timestamp_fallback_requires_review():
    row = image("fallback.jpg", camera_datetime_source="DateTime", camera_offset_time_original="-07:00")
    findings = collection_review_findings(report_entries([row]), [row])
    assert len(findings) == 1
    assert findings[0]["severity"] == "warning"
    assert findings[0]["files"][0]["filename"] == "fallback.jpg"


def test_unknown_and_malformed_findings_fall_back_without_losing_errors():
    entries = [
        {"check": "future_check", "severity": "warning", "message": "New warning"},
        {"check": "camera_timezone_consistency", "severity": "error", "message": "Original error", "findings": "bad"},
        {"check": "file_size_floor", "severity": "warning", "message": "Missing fields", "findings": [{}]},
    ]
    findings = collection_review_findings(entries, [])
    assert len(findings) == 3 and findings[0]["severity"] == "error"
    assert "Original error" in findings[0]["message"]
    assert any("New warning" in f["message"] for f in findings)
    assert all("not retained" in f["message"] for f in findings)


def test_legacy_error_is_not_downgraded_by_new_inventory_interpretation():
    row = image()
    entry = {"check": "camera_timezone_consistency", "_device": "p4_ML", "severity": "error",
             "message": "Historical error requires separate correction"}
    finding, = collection_review_findings([entry], [row])
    assert finding["severity"] == "error"
    assert "Historical error requires separate correction" in finding["message"]


def test_all_burst_findings_and_observed_file_context_survive_report_reload(tmp_path):
    rows = [image("burst1.jpg", sequence_event_num=1, sequence_total=3, sequence_position=1),
            image("burst2.jpg", sequence_event_num=2, sequence_total=3, sequence_position=1)]
    evidence = []
    messages = check_sequence_integrity(rows, "p4_ML", findings=evidence)
    assert len(messages) == 2
    append_qc_report(tmp_path, "sequence_gap", "p4_ML", "warning", "\n".join(messages), findings=evidence)
    report = json.loads((tmp_path / "qc/qc_report.json").read_text())
    findings = collection_review_findings(report["current_state"], rows)
    assert len(findings) == 1
    assert "Burst #1" in findings[0]["message"] and "Burst #2" in findings[0]["message"]
    assert {f["filename"] for f in findings[0]["files"]} == {"burst1.jpg", "burst2.jpg"}
    # A later pass supersedes the entire finding, while history retains the evidence.
    append_qc_report(tmp_path, "sequence_gap", "p4_ML", "pass", "OK")
    report = json.loads((tmp_path / "qc/qc_report.json").read_text())
    assert collection_review_findings(report["current_state"], rows) == []
    assert report["history"]["devices"]["p4_ML"][0]["findings"] == evidence


def test_device_completion_persists_all_bursts_and_mixed_timestamp_evidence(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from cassn.gui.wizard import FieldDataWizard

    rows = [image("bad.jpg", camera_datetime_original="", camera_serial_exif="", sequence_event_num=1, sequence_total=3),
            image("good-time.jpg", sequence_event_num=2, sequence_total=3)]
    context = SimpleNamespace(current_deployment_folder=tmp_path, metadata={},
                              _append_card_message=MagicMock(), log=MagicMock())
    FieldDataWizard._run_device_qc_checks(context, rows, "p4_ML")
    report = json.loads((tmp_path / "qc/qc_report.json").read_text())
    bursts = next(e for e in report["current_state"] if e["check"] == "sequence_gap")
    assert "Burst #1" in bursts["message"] and "Burst #2" in bursts["message"]
    time = next(e for e in report["current_state"] if e["check"] == "camera_timezone_consistency")
    assert time["severity"] == "error"
    assert {f["severity"] for f in time["findings"]} == {"error", "info"}
    serial = next(e for e in report["current_state"] if e["check"] == "camera_serial_match")
    assert serial["findings"][0]["files"][0]["filename"] == "bad.jpg"
