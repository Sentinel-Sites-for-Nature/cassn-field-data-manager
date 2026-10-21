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
