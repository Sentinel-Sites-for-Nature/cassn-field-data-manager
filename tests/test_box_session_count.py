from types import SimpleNamespace

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication

from cassn.box.client import BoxStorage
from cassn.gui import wizard as gui
from cassn.gui.box_session_count import event_paths_on_box
from cassn.gui.event_progress import EventProgress
from cassn.box.auth import hosted_config
from cassn.lookups import LookupTables


def test_live_count_reads_all_raw_pages_and_split_folders(tmp_path):
    folder = tmp_path / "UC_Example"
    def item(name, kind):
        return SimpleNamespace(type=kind, id=name, name=name)

    def page(folder_id, **kwargs):
        hierarchy = {"root": "2026", "2026": "Example", "Example": folder.name,
                     folder.name: "raw_data", "raw_data": "p1_ML", "p1_ML": "part"}
        if folder_id in hierarchy:
            entries, next_marker = [item(hierarchy[folder_id], "folder")], None
        else:
            assert folder_id == "part" and kwargs["usemarker"]
            more = kwargs["marker"] is not None
            entries = [item("b.jpg" if more else "a.jpg", "file")]
            next_marker = None if more else "page2"
        return SimpleNamespace(entries=entries, next_marker=next_marker)

    storage = BoxStorage(SimpleNamespace(folders=SimpleNamespace(get_folder_items=page)))
    metadata = {"site_name": "Example", "deployment_event_end_date": "2026-10-23"}
    assert event_paths_on_box(storage, "root", folder, metadata, folder.name) == {
        "raw_data/p1_ML/part/a.jpg", "raw_data/p1_ML/part/b.jpg"}
    with pytest.raises(ValueError, match="destination"):
        event_paths_on_box(storage, "root", folder, metadata, "different-id")


def test_reopen_counts_partial_upload_without_publishing_or_verifying(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(gui.FieldDataWizard, "load_config", lambda self: None)
    monkeypatch.setattr(gui.FieldDataWizard, "check_box_auth", lambda self: False)
    monkeypatch.setattr(gui.FieldDataWizard, "find_all_sessions", lambda self: [])
    window = gui.FieldDataWizard(lookups=LookupTables(), box_config=hosted_config("uc"))
    first = tmp_path / "one"
    second = tmp_path / "two"
    for folder in (first, second):
        (folder / "qc").mkdir(parents=True)
        (folder / gui.STATE_PATH).write_text('{"status":"needs_recovery","folder_id":"123"}')
    inventory = [{"device_label":"p1_ML", "new_filename":n} for n in ("old.jpg","partial.jpg","next.jpg")]
    present = {"raw_data/p1_ML/old.jpg", "raw_data/p1_ML/partial.jpg", "raw_data/p1_ML/unrelated.jpg"}
    pending = []
    monkeypatch.setattr(QThreadPool, "start", lambda pool, reader: pending.append(reader))
    try:
        window.current_deployment_folder = first
        window.file_inventory = inventory
        window.box_authenticated = True
        window._restore_box_result()
        assert window.box_history_label.text() == "Checking files on Box…"
        window.current_deployment_folder = second
        window._restore_box_result()
        pending[0].signals.result.emit(pending[0], present, "")
        app.processEvents()
        assert window.box_history_label.text() == "Checking files on Box…"
        pending[1].signals.result.emit(pending[1], None, "Could not read the event folder on Box.")
        app.processEvents()
        assert window.upload_progress_bar.isHidden()
        assert not window.box_count_retry.isHidden()
        window.box_count_retry.click()
        pending[2].signals.result.emit(pending[2], present, "")
        app.processEvents()
        assert window.upload_progress_bar.text() == "2 of 3 event files on Box · 66%"
        assert window.box_history_label.text() == window.upload_progress_bar.text()
        assert not window.box_upload_complete
        assert EventProgress.from_box_paths(inventory, window._box_present_paths).remaining == {"raw_data/p1_ML/next.jpg"}
        assert not (second / "qc/box_upload_verification.json").exists()
        window.set_box_connection_state(False)
        assert "Connect to Box" in window.box_history_label.text()
        assert window.upload_progress_bar.isHidden()
    finally:
        window.current_deployment_folder = None
        window.close()
