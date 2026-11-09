"""Reopening an event must preserve saved inventory and per-device progress."""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from cassn.box.auth import BoxConfig
from cassn.gui.preview import sample_lookups
from cassn.gui.wizard import FieldDataWizard


@pytest.fixture
def windows(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(FieldDataWizard, "load_config", lambda self: None)
    monkeypatch.setattr(FieldDataWizard, "check_box_auth", lambda self: False)
    monkeypatch.setattr(FieldDataWizard, "find_all_sessions", lambda self: [])
    opened = []

    def create():
        window = FieldDataWizard(lookups=sample_lookups(), box_config=BoxConfig())
        window._card_ejection_timer.stop()
        window.staging_root = tmp_path
        opened.append(window)
        return window

    yield create
    for window in opened:
        window.current_deployment_folder = None
        window.close()
    app.processEvents()


def test_resume_progress_shows_active_verification_then_returns_to_copying(windows, tmp_path):
    window = windows()
    window._create_card_panel("p1_BT", tmp_path)
    panel = window.card_ingest_panels["p1_BT"]
    window._on_card_progress("p1_BT", 21, 100, "Checking saved card file 1/21: recording.WAV\n64 / 3721 MB read")
    assert panel["progress"].minimum() == panel["progress"].maximum() == 0
    assert "Checking saved files" in panel["status"].text()
    assert "64 / 3721 MB read" in panel["detail"].text()
    window._on_card_progress("p1_BT", 22, 100, "next.WAV")
    assert panel["progress"].maximum() == 100
    assert panel["progress"].value() == 22
    assert panel["status"].text() == "Copying and verifying…"


def saved_work(window):
    window.validate_and_next()
    labels = [device[3] for device in window.devices]
    window.file_inventory = [
        {"device_label": labels[0], "new_filename": f"image-{i}.jpg"}
        for i in range(3)
    ] + [{"device_label": labels[1], "new_filename": "partial.jpg"}]
    window._device_item(labels[0]).setText(2, "Complete")
    window._device_item(labels[1]).setText(2, "Copying")
    window._device_item(labels[2]).setText(2, "Skipped")
    assert window.save_session()
    return labels, window.current_deployment_folder / "session.json"


def test_new_window_selecting_existing_event_restores_without_overwriting(windows):
    labels, session_path = saved_work(windows())
    original = session_path.read_bytes()
    reopened = windows()
    reopened.validate_and_next()
    assert reopened.tabs.currentIndex() == 1
    assert reopened._device_item(labels[0]).text(2) == "Complete"
    assert reopened._device_item(labels[0]).text(3) == "3"
    assert reopened._device_item(labels[1]).text(2) == "Incomplete"
    assert reopened._device_item(labels[1]).text(3) == "1"
    assert reopened._device_item(labels[2]).text(2) == "Skipped"
    assert reopened._device_item(labels[3]).text(2) == "Pending"
    assert len(reopened.file_inventory) == 4
    assert session_path.read_bytes() == original

    # Returning through setup must not reset the restored rows or save zeros.
    reopened.tabs.setCurrentIndex(0)
    reopened.validate_and_next()
    saved = json.loads(session_path.read_text())
    assert saved["device_statuses"][labels[0]] == {"status": "Complete", "files_copied": "3"}
    assert len(saved["file_inventory"]) == 4

    reopened.devices.reverse()
    reopened.populate_collection_list()
    assert reopened._device_item(labels[0]).text(2) == "Complete"
    assert reopened._device_item(labels[0]).text(3) == "3"
    assert reopened._device_item(labels[2]).text(2) == "Skipped"


def test_direct_resume_uses_inventory_count_and_does_not_infer_completion(windows):
    labels, session_path = saved_work(windows())
    session = json.loads(session_path.read_text())
    session["device_statuses"].pop(labels[0])
    session["device_statuses"][labels[1]]["files_copied"] = "999"
    reopened = windows()
    reopened.restore_session(session)
    assert reopened._device_item(labels[0]).text(2) == "Incomplete"
    assert reopened._device_item(labels[0]).text(3) == "3"
    assert reopened._device_item(labels[1]).text(3) == "1"


def test_unreadable_existing_session_is_never_overwritten(windows, monkeypatch):
    window = windows()
    folder = window.staging_root / window.deploy_event_combo.currentData()["deployment_event_id"]
    folder.mkdir()
    path = folder / "session.json"
    path.write_text('{"interrupted":')
    errors = []
    monkeypatch.setattr("cassn.gui.wizard.QMessageBox.critical", lambda *args: errors.append(args))
    window.validate_and_next()
    assert errors
    assert window.current_deployment_folder is None
    assert window.tabs.currentIndex() == 0
    assert path.read_text() == '{"interrupted":'


def test_box_completion_state_matches_current_proof_on_reopen(windows):
    from cassn.core.verification_evidence import verification_context, write_result

    source = windows()
    source.box_config = BoxConfig(field_data_folder_id="test-root")
    _, session = saved_work(source)
    folder = source.current_deployment_folder
    context = verification_context(folder, source.file_inventory, source.metadata, source.box_config)
    for kind in ("box", "hash"):
        write_result(folder, kind, context, ok=True, summary="passed")
    combined = folder / "qc/box_upload_verification.json"
    evidence = combined.read_bytes()
    reopened = windows()
    reopened.box_config = source.box_config
    def reopen():
        reopened.restore_session(json.loads(session.read_text()))

    reopen()
    assert reopened.box_upload_complete and not reopened.box_panel_button.isEnabled()
    assert not reopened.box_panel_button.isHidden()
    assert not reopened.upload_group.isHidden() and reopened.hash_group.isHidden()
    reopened.upload_group.disclosure.click()
    assert combined.read_bytes() == evidence
    write_result(folder, "hash", context, ok=False, summary="hash mismatch")
    reopen()
    assert not reopened.box_upload_complete and not reopened.upload_group.isHidden()
    assert reopened.upload_status_label.property("platformStatus") != "complete"


def test_review_acknowledgements_survive_reopen_and_expire_on_change(windows, monkeypatch):
    from cassn.gui.qc_review import CollectionReviewDialog

    window = windows()
    _, path = saved_work(window)
    finding = dict(title="Recording stopped early", message="One file", action="Check the folder",
                   severity="warning", files=[], affected="p1_BD", technical="low voltage", evidence_key="one-file")
    monkeypatch.setattr(FieldDataWizard, "_collection_review_findings", lambda self, _: [finding])

    def accept(dialog):
        assert not dialog.proceed.isEnabled()
        dialog.acknowledge.click()
        assert dialog.proceed.isEnabled()
        return dialog.DialogCode.Accepted

    monkeypatch.setattr(CollectionReviewDialog, "exec", accept)
    assert window._confirm_collection_review([])
    reopened = windows()
    reopened.restore_session(json.loads(path.read_text()))

    def inspect(dialog):
        return dialog.DialogCode.Accepted if dialog.proceed.isEnabled() else dialog.DialogCode.Rejected

    monkeypatch.setattr(CollectionReviewDialog, "exec", inspect)
    assert reopened._confirm_collection_review([])
    finding["message"] = "Two files"
    finding["evidence_key"] = "two-files"
    assert not reopened._confirm_collection_review([])
    assert json.loads(path.read_text())["qc_acknowledgements"] == []


def test_chooser_distinguishes_complete_skipped_and_unreadable_sessions(windows, tmp_path):
    from datetime import datetime
    from cassn.gui.session_chooser import SessionChooser
    window = windows()
    entries = [
        dict(data={"metadata": {"site_name": "San Jacinto Mountains", "observer": "James",
                                "deployment_start": "2026-06-10", "deployment_end": "2026-10-23"},
                   "device_statuses": {"p1_ML": {"status": "Complete"}, "p1_BD": {"status": "Skipped"}}},
             path=tmp_path / "sample-event" / "session.json", status="ok", saved_at=datetime(2026, 11, 5, 18)),
        dict(data=None, path=tmp_path / "damaged" / "session.json", status="corrupted", error_msg="Unreadable JSON")
    ]
    chooser = SessionChooser(entries, window)
    try:
        row = chooser.tree.currentItem()
        assert row.text(0) == "San Jacinto Mountains" and row.text(2) == "1 of 2"
        assert row.text(1) == "2026-06-10 → 2026-10-23"
        assert "James" in chooser.detail.text() and "Skipped devices: 1" in chooser.detail.text()
        assert chooser.selected_session() is entries[0]
        chooser.tree.setCurrentItem(chooser.tree.topLevelItem(1))
        assert not chooser.resume.isEnabled()
        assert "Unreadable JSON" in chooser.detail.text()
    finally:
        chooser.close()
