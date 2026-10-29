"""Out-of-window camera dates require a durable decision before progression."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from cassn.core.quality_control import camera_date_discrepancies
from cassn.gui.wizard import FieldDataWizard


def image(name, timestamp, device="p3_SA"):
    return {
        "file_type": "image", "device_label": device,
        "new_filename": name, "recorded_datetime": timestamp,
    }


def test_camera_date_finding_is_stable_and_invalidates_when_evidence_changes():
    rows = [
        image("early.jpg", "2026-03-25T23:00:00-07:00"),
        image("normal.jpg", "2026-03-26T11:00:00-07:00"),
        image("late.jpg", "2026-06-12T15:26:00-07:00"),
        {"file_type": "audio", "device_label": "p3_BT", "recorded_datetime": "2026-06-12"},
    ]
    finding = camera_date_discrepancies(rows, "2026-03-26", "2026-06-10")[0]
    assert finding["before_count"] == finding["after_count"] == 1
    assert finding["device_label"] == "p3_SA"
    assert finding["examples"] == ["early.jpg", "late.jpg"]
    assert camera_date_discrepancies(list(reversed(rows)), "2026-03-26", "2026-06-10")[0]["fingerprint"] == finding["fingerprint"]
    revised = camera_date_discrepancies(rows, "2026-03-26", "2026-06-12")[0]
    assert revised["after_count"] == 0
    assert revised["fingerprint"] != finding["fingerprint"]


def test_review_is_required_saved_and_reopened_only_when_evidence_changes(tmp_path):
    finding = camera_date_discrepancies(
        [image("late.jpg", "2026-06-12T15:26:00-07:00")], "2026-03-26", "2026-06-10",
    )[0]
    context = SimpleNamespace(
        camera_date_reviews={}, current_deployment_folder=tmp_path,
        _camera_date_findings=MagicMock(return_value=[finding]),
        _prompt_camera_date_review=MagicMock(return_value=(
            "Camera clock/date was incorrect; retain original camera dates",
            "Setup and retrieval frames show a two-day clock error.",
        )),
        save_session=MagicMock(return_value=True),
    )
    context._camera_date_review_is_current = lambda finding: FieldDataWizard._camera_date_review_is_current(context, finding)
    assert FieldDataWizard._ensure_camera_dates_reviewed(context)
    assert context.camera_date_reviews["p3_SA"]["fingerprint"] == finding["fingerprint"]
    report = json.loads((tmp_path / "qc/qc_report.json").read_text())
    current = next(x for x in report["current_state"] if x["check"] == "camera_date_review")
    assert current["severity"] == "pass"
    assert "two-day clock error" in current["message"]

    assert FieldDataWizard._ensure_camera_dates_reviewed(context)
    context._prompt_camera_date_review.assert_called_once()
    changed = camera_date_discrepancies(
        [image("late.jpg", "2026-06-12T15:26:00-07:00"),
         image("new.jpg", "2026-06-12T15:27:00-07:00")],
        "2026-03-26", "2026-06-10",
    )[0]
    context._camera_date_findings.return_value = [changed]
    context._prompt_camera_date_review.return_value = None
    assert not FieldDataWizard._ensure_camera_dates_reviewed(context)
    report = json.loads((tmp_path / "qc/qc_report.json").read_text())
    current = next(x for x in report["current_state"] if x["check"] == "camera_date_review")
    assert current["severity"] == "error"


def test_review_transition_stops_before_generating_outputs():
    tree = MagicMock()
    tree.topLevelItemCount.return_value = 0
    context = SimpleNamespace(
        card_ingest_threads={}, device_tree=tree,
        _ensure_camera_dates_reviewed=MagicMock(return_value=False),
        generate_metadata_files=MagicMock(), update_review_tab=MagicMock(),
        tabs=MagicMock(),
    )
    FieldDataWizard.validate_and_next_collection(context)
    context.generate_metadata_files.assert_not_called()
    context.tabs.setCurrentIndex.assert_not_called()


def test_device_qc_records_blocking_error_until_reviewed(tmp_path):
    rows = [image("late.jpg", "2026-06-12T15:26:00-07:00")]
    context = SimpleNamespace(
        current_deployment_folder=tmp_path,
        metadata={"deployment_event_start_date": "2026-03-26",
                  "deployment_event_end_date": "2026-06-10"},
        camera_date_reviews={}, _append_card_message=MagicMock(), log=MagicMock(),
    )
    context._camera_date_review_is_current = lambda finding: FieldDataWizard._camera_date_review_is_current(context, finding)
    FieldDataWizard._run_device_qc_checks(context, rows, "p3_SA")
    report = json.loads((tmp_path / "qc/qc_report.json").read_text())
    finding = next(x for x in report["current_state"] if x["check"] == "camera_date_review")
    assert finding["severity"] == "error"
    assert "Operator review required" in finding["message"]


def test_review_does_not_clear_block_when_session_cannot_be_saved(tmp_path):
    finding = camera_date_discrepancies(
        [image("late.jpg", "2026-06-12T15:26:00-07:00")], "2026-03-26", "2026-06-10",
    )[0]
    context = SimpleNamespace(
        camera_date_reviews={}, current_deployment_folder=tmp_path,
        _camera_date_findings=MagicMock(return_value=[finding]),
        _prompt_camera_date_review=MagicMock(return_value=(
            "Camera clock/date was incorrect; original camera dates retained",
            "Installation and retrieval images support a two-day offset.",
        )),
        save_session=MagicMock(return_value=False),
    )
    context._camera_date_review_is_current = lambda item: FieldDataWizard._camera_date_review_is_current(context, item)
    assert not FieldDataWizard._ensure_camera_dates_reviewed(context)
    assert context.camera_date_reviews == {}
    report = json.loads((tmp_path / "qc/qc_report.json").read_text())
    assert next(x for x in report["current_state"] if x["check"] == "camera_date_review")["severity"] == "error"


def test_direct_box_upload_stops_before_preparation(tmp_path):
    context = SimpleNamespace(
        box_authenticated=True, current_deployment_folder=tmp_path,
        card_ingest_threads={}, _box_operation_active=False,
        box_verify_thread=None, fixity_thread=None,
        wi_split_thread=None, upload_thread=None,
        _ensure_camera_dates_reviewed=MagicMock(return_value=False),
    )
    FieldDataWizard.upload_to_box(context)
    assert not hasattr(context, "_box_operation_token")
