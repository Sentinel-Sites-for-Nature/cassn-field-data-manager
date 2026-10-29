"""The workspace shell must preserve workflow gates and fit a laptop window."""

import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from cassn.box.auth import BoxConfig, hosted_config
from cassn.gui.wizard import FieldDataWizard
from cassn.lookups import LookupTables


@pytest.fixture
def workspace_window(monkeypatch, request):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(FieldDataWizard, "load_config", lambda self: None)
    monkeypatch.setattr(FieldDataWizard, "check_box_auth", lambda self: False)
    monkeypatch.setattr(FieldDataWizard, "find_all_sessions", lambda self: [])
    window = FieldDataWizard(lookups=LookupTables(), box_config=getattr(request, "param", BoxConfig()))
    window._card_ejection_timer.stop()
    yield app, window
    window.card_ingest_threads.clear()
    window._box_operation_active = False
    window.current_deployment_folder = None
    window.close()
    app.processEvents()


@pytest.mark.parametrize("workspace_window", [hosted_config("uc"), hosted_config("csu"), BoxConfig()], indirect=True)
def test_review_exposes_uc_staging_without_soundhub_upload(workspace_window):
    from PySide6.QtWidgets import QToolButton

    _app, window = workspace_window
    uc = window.box_config.profile != "csu"
    assert window.soundhub_stage_btn.isVisible() == uc
    assert window.open_soundhub_btn.isVisible() == uc
    buttons = [button for button in window.tabs.widget(2).findChildren(QToolButton) if button.menu()]
    # Qt requires && to render a literal ampersand instead of a mnemonic.
    prepare = next(button for button in buttons if button.text() == "Prepare && Upload ▾")
    assert window.upload_now_btn in prepare.menu().actions()
    assert window.soundhub_stage_btn in prepare.menu().actions()
    menus = [button.menu() for button in buttons]
    visible = [action for menu in menus for action in menu.actions() if action.isVisible()]
    assert (window.soundhub_stage_btn in visible) == uc
    assert (window.open_soundhub_btn in visible) == uc
    assert not any("Upload Bird Data to SoundHub" in action.text() or "SoundHub Landing Zone" in action.text()
                   for action in visible)


@pytest.mark.parametrize("workspace_window", [hosted_config("uc")], indirect=True)
def test_hosted_uc_staging_starts_only_local_worker(workspace_window, tmp_path, monkeypatch):
    from cassn.gui import wizard

    _app, window = workspace_window
    window.current_deployment_folder = tmp_path / "event"
    monkeypatch.setattr(window, "_soundhub_staging_root", lambda: tmp_path / "soundhub")
    monkeypatch.setattr(wizard, "flac_available", lambda: True)
    monkeypatch.setattr(wizard.QMessageBox, "question", lambda *args: wizard.QMessageBox.Yes)
    worker = MagicMock()
    stage = MagicMock(return_value=worker)
    upload = MagicMock(side_effect=AssertionError("Staging must not start an upload"))
    monkeypatch.setattr(wizard, "SoundHubStageThread", stage)
    monkeypatch.setattr(wizard, "SoundHubUploadThread", upload)
    notice = MagicMock()
    monkeypatch.setattr(wizard.QMessageBox, "information", notice)
    window.soundhub_stage_btn.trigger()
    stage.assert_called_once_with(window.current_deployment_folder, tmp_path / "soundhub", window.lookups)
    worker.start.assert_called_once()
    assert not window.soundhub_stage_btn.isEnabled()
    window.on_soundhub_stage_complete(True, False, "Prepared local batch", {})
    assert window.soundhub_stage_btn.isEnabled()
    assert "nothing was uploaded" in notice.call_args.args[2]
    assert "Upload Bird Data to SoundHub" not in notice.call_args.args[2]
    upload.assert_not_called()
    window.soundhub_stage_thread = None


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


@pytest.mark.parametrize("accept", [False, True])
@pytest.mark.parametrize("sidebar", [False, True])
def test_collection_checks_require_explicit_continue(
    workspace_window, tmp_path, monkeypatch, accept, sidebar
):
    from PySide6.QtWidgets import QDialogButtonBox, QTreeWidget
    from cassn.core.quality_control import append_qc_report
    from cassn.gui import wizard

    _app, window = workspace_window
    window.current_deployment_folder = tmp_path
    window.tabs.setCurrentIndex(1)
    window.devices = [(1, "One", "ML", "p1_ML")]
    window.populate_collection_list()
    append_qc_report(tmp_path, "copy", "p1_ML", "error", "Old copy failure")
    append_qc_report(tmp_path, "copy", "p1_ML", "pass", "Copy recovered")
    append_qc_report(tmp_path, "file_size_floor", "p1_ML", "warning", "Small image")
    append_qc_report(tmp_path, "camera_timezone", "p1_ML", "error", "Invalid offset")
    monkeypatch.setattr(window, "_ensure_camera_dates_reviewed", lambda: True)
    monkeypatch.setattr(wizard, "validate_coordinates", lambda rows: ["Missing latitude", "Missing elevation"])
    export = MagicMock()
    upload = MagicMock()
    monkeypatch.setattr(window, "generate_metadata_files", export)
    monkeypatch.setattr(window, "update_review_tab", MagicMock())
    monkeypatch.setattr(window, "upload_to_box", upload)
    dialogs = []

    def respond(dialog):
        dialogs.append(dialog.windowTitle())
        tree = dialog.findChild(QTreeWidget)
        findings = [tree.topLevelItem(i).data(0, Qt.UserRole) for i in range(tree.topLevelItemCount())]
        text = "\n".join(finding["message"] for finding in findings)
        assert "Old copy failure" not in text
        for expected in ("p1_ML", "Small image", "Invalid offset", "Pending", "Missing latitude", "Missing elevation"):
            assert expected in text
        buttons = dialog.findChild(QDialogButtonBox)
        target = "Continue to review" if accept else "Back to card ingest"
        next(button for button in buttons.buttons() if button.text() == target).click()
        return dialog.result()

    monkeypatch.setattr(wizard.QDialog, "exec", respond)
    if sidebar:
        window.workspace.navigation[2].click()
    else:
        window.validate_and_next_collection()
    assert dialogs == ["Card ingest checks"]
    assert window.tabs.currentIndex() == (2 if accept else 1)
    assert export.call_count == int(accept)
    upload.assert_not_called()


def test_clean_collection_still_shows_checks(workspace_window, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QLabel, QTextEdit
    from cassn.gui import wizard

    _app, window = workspace_window
    window.current_deployment_folder = tmp_path

    def respond(dialog):
        assert "Card ingest checks passed" in [label.text() for label in dialog.findChildren(QLabel)]
        assert dialog.findChild(QTextEdit).toPlainText() == "No findings to address."
        return wizard.QDialog.Accepted

    monkeypatch.setattr(wizard.QDialog, "exec", respond)
    assert window._confirm_collection_review([])


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
    assert jobs.width() > devices.width()
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


@pytest.mark.parametrize("contents", ["not json", '[]', '{"current_state": [null]}',
                                     '{"current_state": [{"check": [], "severity": "error"}]}'])
def test_unreadable_collection_qc_report_remains_visible(workspace_window, tmp_path, contents):
    _app, window = workspace_window
    window.current_deployment_folder = tmp_path
    (tmp_path / "qc").mkdir()
    (tmp_path / "qc/qc_report.json").write_text(contents)
    findings = window._collection_review_findings([])
    assert any(f["severity"] == "error" and f["title"] == "QC report could not be read" for f in findings)


def test_soundhub_dialog_uses_local_counts_before_background_checks(workspace_window, tmp_path, monkeypatch):
    from cassn.gui import soundhub_thread, wizard
    from cassn.soundhub.staging import project_root

    _app, window = workspace_window
    window.current_deployment_folder = tmp_path / "event"
    staging = tmp_path / "soundhub"
    root = project_root(staging)
    root.mkdir(parents=True)
    (root / "deployment.csv").write_text("deployment_id\nfirst\nsecond\n")
    (root / "recording.csv").write_text("path\nfirst/a.flac\nsecond/b.flac\nsecond/c.flac\n")
    monkeypatch.setattr(window, "_soundhub_staging_root", lambda: staging)
    monkeypatch.setattr(wizard, "flac_available", lambda: True)
    validate = MagicMock(side_effect=AssertionError("Validation must run in the worker"))
    check_box = MagicMock(side_effect=AssertionError("Box checks must run in the worker"))
    monkeypatch.setattr(soundhub_thread, "validate_staging_manifests", validate)
    monkeypatch.setattr(soundhub_thread, "plan_completed_batch_cleanup", check_box)
    worker = MagicMock()
    stage = MagicMock(return_value=worker)
    monkeypatch.setattr(wizard, "SoundHubStageThread", stage)
    question = MagicMock(return_value=wizard.QMessageBox.No)
    monkeypatch.setattr(wizard.QMessageBox, "question", question)

    window.stage_for_soundhub()
    assert "2 SoundHub deployment(s), 3 recording(s)" in question.call_args.args[2]
    validate.assert_not_called()
    check_box.assert_not_called()
    stage.assert_not_called()

    question.return_value = wizard.QMessageBox.Yes
    window.stage_for_soundhub()
    worker.start.assert_called_once()
    assert window.soundhub_status_label.text() == "Checking existing SoundHub batch…"
    validate.assert_not_called()
    check_box.assert_not_called()
    window.soundhub_stage_thread = None
