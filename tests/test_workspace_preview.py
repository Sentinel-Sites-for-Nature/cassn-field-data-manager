"""Previewing a redesign cannot resume a real session or enable transfers."""

import os
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication

from cassn.gui.preview import WorkspacePreview
from cassn.gui.wizard import FieldDataWizard


def test_preview_avoids_live_startup_and_keeps_copy_disabled(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])

    def forbidden(*args, **kwargs):
        raise AssertionError("Live app startup was called")

    for method in ("load_config", "check_box_auth", "find_all_sessions"):
        monkeypatch.setattr(FieldDataWizard, method, forbidden)
    copy = MagicMock()
    monkeypatch.setattr(FieldDataWizard, "copy_sd_card_data", copy)
    window = WorkspacePreview(tmp_path)
    try:
        assert not window._card_ejection_timer.isActive()
        assert not window.card_ingest_threads
        for i in (0, 4, 8, 15):
            window.device_tree.setCurrentItem(window.device_tree.topLevelItem(i))
            assert not window.copy_btn.isEnabled()
            window.copy_btn.click()
        copy.assert_not_called()
        assert all(not action.isEnabled() for action in window.findChildren(QAction))
    finally:
        window.close()
        app.processEvents()
    assert not list(tmp_path.rglob("session.json"))


def test_preview_navigation_never_runs_export_or_upload(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = WorkspacePreview(tmp_path)
    operations = []
    for method in ("validate_and_next", "validate_and_next_collection", "generate_metadata_files", "upload_to_box"):
        operation = MagicMock()
        monkeypatch.setattr(window, method, operation)
        operations.append(operation)
    try:
        for page in (0, 1, 2, 0, 2):
            window.workspace.navigation[page].click()
            assert window.tabs.currentIndex() == page
            assert "Preview" in window.workspace.header_status.text()
        for operation in operations:
            operation.assert_not_called()
    finally:
        window.close()
        app.processEvents()
