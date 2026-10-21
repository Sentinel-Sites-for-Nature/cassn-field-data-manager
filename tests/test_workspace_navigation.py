"""The workspace shell must preserve workflow gates and fit a laptop window."""

import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from cassn.box.auth import BoxConfig
from cassn.gui.wizard import FieldDataWizard
from cassn.lookups import LookupTables


@pytest.fixture
def workspace_window(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(FieldDataWizard, "load_config", lambda self: None)
    monkeypatch.setattr(FieldDataWizard, "check_box_auth", lambda self: False)
    monkeypatch.setattr(FieldDataWizard, "find_all_sessions", lambda self: [])
    window = FieldDataWizard(lookups=LookupTables(), box_config=BoxConfig())
    window._card_ejection_timer.stop()
    yield app, window
    window.card_ingest_threads.clear()
    window._box_operation_active = False
    window.current_deployment_folder = None
    window.close()
    app.processEvents()


def test_sidebar_cannot_bypass_missing_deployment(workspace_window, monkeypatch):
    _app, window = workspace_window
    warning = MagicMock()
    monkeypatch.setattr("cassn.gui.wizard.QMessageBox.warning", warning)
    window.workspace.navigation[1].click()
    assert window.tabs.currentIndex() == 0
    assert window.workspace.navigation[0].isChecked()
    window.workspace.navigation[2].click()
    assert window.tabs.currentIndex() == 0
    assert window.workspace.navigation[0].isChecked()
    assert warning.call_count == 2
    assert window.current_deployment_folder is None


def test_sidebar_review_respects_validation_refusal(workspace_window, tmp_path, monkeypatch):
    _app, window = workspace_window
    window.current_deployment_folder = tmp_path
    window.tabs.setCurrentIndex(1)
    validate = MagicMock()
    monkeypatch.setattr(window, "validate_and_next_collection", validate)
    window.workspace.navigation[2].click()
    assert window.tabs.currentIndex() == 1
    assert window.workspace.navigation[1].isChecked()
    validate.assert_called_once()


@pytest.mark.parametrize("operation", ["card", "box"])
def test_sidebar_cannot_change_deployment_during_operation(
    workspace_window, tmp_path, monkeypatch, operation
):
    _app, window = workspace_window
    window.current_deployment_folder = tmp_path
    window.tabs.setCurrentIndex(1)
    if operation == "card":
        window.card_ingest_threads["p1_ML"] = object()
    else:
        window._box_operation_active = True
    message = MagicMock()
    monkeypatch.setattr("cassn.gui.wizard.QMessageBox.information", message)
    window.workspace.navigation[0].click()
    assert window.tabs.currentIndex() == 1
    assert window.workspace.navigation[1].isChecked()
    message.assert_called_once()


def test_four_jobs_fit_small_window_and_restore_empty_state(workspace_window, tmp_path):
    app, window = workspace_window
    window.tabs.setCurrentIndex(1)
    for i in range(4):
        window._create_card_panel(f"p{i + 1}_ML", tmp_path)
    window.resize(1000, 740)
    window.show()
    app.processEvents()
    assert window.width() == 1000
    assert window.collection_splitter.orientation() == Qt.Vertical
    assert window.card_jobs_scroll.verticalScrollBar().maximum() > 0
    assert window.workspace.navigation[1].isVisible()
    assert window.copy_btn.isVisible()
    assert not window.card_jobs_empty.isVisible()
    window.resize(1380, 950)
    app.processEvents()
    assert window.collection_splitter.orientation() == Qt.Horizontal
    devices = window.collection_splitter.widget(0)
    jobs = window.card_jobs_container
    assert devices.geometry().top() == jobs.geometry().top()
    assert devices.contentsRect().top() == jobs.contentsRect().top()
    for label in list(window.card_ingest_panels):
        window._remove_card_panel(label)
    app.processEvents()
    assert window.card_jobs_empty.isVisible()


def test_review_summary_escapes_metadata_and_counts_each_device(workspace_window, tmp_path):
    _app, window = workspace_window
    window.current_deployment_folder = tmp_path
    window.metadata = {"site_name": "Reserve <test>", "observer": "A & B"}
    window.devices = [(1, "One", "ML", "p1_ML")]
    window.populate_collection_list()
    window.file_inventory = [
        {"device_label": "p1_ML", "file_type": "image", "file_size_bytes": 1000},
    ]
    window.update_review_tab()
    text = window.summary_text.toPlainText()
    assert "Reserve <test>" in text
    assert "A & B" in text
    assert "1 files" in text
    assert "0 of 1" in text


def test_zoom_actions_resize_text_and_preserve_page_and_selection(workspace_window):
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QStyle, QStyleOptionComboBox

    app, window = workspace_window
    window.resize(1000, 740)
    window.show()
    app.processEvents()
    combo = window.site_name_combo
    initial_size = combo.font().pixelSize()
    initial_text = combo.currentText()
    window.workspace.zoom_in_action.trigger()
    app.processEvents()
    assert window.workspace.zoom == 110
    assert combo.font().pixelSize() > initial_size
    assert combo.currentText() == initial_text
    assert window.tabs.currentIndex() == 0
    window.workspace.changeEvent(QEvent(QEvent.ApplicationPaletteChange))
    assert "font-size: 14px" in window.workspace.styleSheet()
    window.workspace.set_zoom(200)
    app.processEvents()
    assert not window.workspace.zoom_in_action.isEnabled()
    assert combo.font().pixelSize() == 26
    window.workspace.zoom_reset_action.trigger()
    app.processEvents()
    assert combo.font().pixelSize() == initial_size
    assert window.workspace.zoom == 100
    option = QStyleOptionComboBox()
    combo.initStyleOption(option)
    arrow = combo.style().subControlRect(QStyle.CC_ComboBox, option, QStyle.SC_ComboBoxArrow, combo)
    assert arrow.width() >= 16


@pytest.mark.parametrize("connected", [False, True])
def test_deployment_page_fits_desktop_and_stacks_on_laptop(monkeypatch, connected):
    from PySide6.QtWidgets import QBoxLayout, QScrollArea
    from cassn.gui.preview import sample_lookups

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(FieldDataWizard, "load_config", lambda self: None)
    monkeypatch.setattr(FieldDataWizard, "check_box_auth", lambda self: connected)
    monkeypatch.setattr(FieldDataWizard, "find_all_sessions", lambda self: [])
    lookups = sample_lookups()
    lookups.plot_names["SantaCruzIsland"][5] = "Five"
    window = FieldDataWizard(lookups=lookups, box_config=BoxConfig())
    window._card_ejection_timer.stop()
    try:
        window.resize(1380, 900)
        window.show()
        for _ in range(3):
            app.processEvents()
        scroll = window.tabs.widget(0).findChild(QScrollArea)
        assert window.metadata_columns.direction() == QBoxLayout.LeftToRight
        assert scroll.verticalScrollBar().maximum() == 0
        assert scroll.horizontalScrollBar().maximum() == 0
        window.resize(1000, 740)
        for _ in range(3):
            app.processEvents()
        assert window.metadata_columns.direction() == QBoxLayout.TopToBottom
        assert window.storage_columns.direction() == QBoxLayout.TopToBottom
        assert scroll.verticalScrollBar().maximum() > 0
        assert scroll.horizontalScrollBar().maximum() == 0
    finally:
        window.close()
        app.processEvents()
