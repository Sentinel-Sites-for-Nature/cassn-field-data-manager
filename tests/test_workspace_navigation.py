"""The workspace shell must preserve workflow gates and fit a laptop window."""

import json
import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

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
    from PySide6.QtWidgets import QPushButton, QToolButton

    _app, window = workspace_window
    uc = window.box_config.profile != "csu"
    assert window.soundhub_stage_btn.isVisible() == uc
    assert window.open_soundhub_btn.isVisible() == uc
    window.tabs.setCurrentIndex(2)
    assert window.soundhub_group.isHidden() != uc
    assert window.soundhub_panel_button.text() == "Stage bird audio"
    assert not any(button.menu() for button in window.tabs.widget(2).findChildren(QToolButton))
    actions = window.soundhub_group.findChildren(QPushButton)
    assert not any("Upload Bird Data to SoundHub" in button.text() or "SoundHub Landing Zone" in button.text()
                   for button in actions)


@pytest.mark.parametrize("workspace_window", [hosted_config("uc")], indirect=True)
def test_hosted_uc_staging_starts_only_local_worker(workspace_window, tmp_path, monkeypatch):
    from cassn.gui import wizard

    _app, window = workspace_window
    window.current_deployment_folder = tmp_path / "event"
    monkeypatch.setattr(window, "_soundhub_staging_root", lambda: tmp_path / "soundhub")
    monkeypatch.setattr(wizard, "flac_available", lambda: True)
    monkeypatch.setattr(wizard.SoundHubStageDialog, "exec", lambda self: self.DialogCode.Accepted)
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
    assert not window.soundhub_stage_btn.isEnabled()
    assert window.soundhub_stage_btn.text() == "Bird Audio Already Staged"
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
    from PySide6.QtWidgets import QTreeWidget
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
        assert tree.topLevelItem(tree.topLevelItemCount() - 1).text(0).startswith("Pending")
        for expected in ("p1_ML", "Small image", "Invalid offset", "Missing latitude", "Missing elevation"):
            assert expected in text
        if accept:
            for i, finding in enumerate(findings):
                if finding["severity"] in {"error", "warning"}:
                    tree.setCurrentItem(tree.topLevelItem(i))
                    dialog.acknowledge.click()
        target = "Continue to review" if accept else "Back to card ingest"
        next(button for button in dialog.findChildren(QPushButton) if button.text() == target).click()
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
        from cassn.gui.workspace import DeviceGridScroll
        matrix = window.findChild(DeviceGridScroll)
        previous = matrix.viewport().height()
        window.resize(1380, 1070)
        for _ in range(3):
            app.processEvents()
        assert matrix.viewport().height() > previous
        assert matrix.viewport().height() >= matrix.widget().sizeHint().height()
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


def test_review_groups_only_empty_pending_devices(workspace_window, tmp_path):
    from PySide6.QtWidgets import QLabel, QTreeWidgetItem
    from cassn.gui.qc_review import CollectionReviewDialog

    _app, window = workspace_window
    window.current_deployment_folder = tmp_path
    window.file_inventory = [{"device_label": "p3_SA", "new_filename": "one.jpg"}]
    for device, status, count in [
        ("p1_ML", "Pending", "0"), ("p2_ML", "Pending", "0"),
        ("p3_SA", "Pending", "0"), ("p3_ML", "Incomplete", "1"),
    ]:
        row = QTreeWidgetItem(window.device_tree, [device, "Camera", status, count])
        row.setData(0, Qt.UserRole, device)
    dialog = CollectionReviewDialog(window._collection_review_findings([]), window.file_inventory, tmp_path)
    try:
        assert dialog.findChild(QLabel, "qcSummary") is None
        tree = dialog.tree
        assert tree.currentItem().text(1) == "p3_ML"
        assert tree.topLevelItem(1).text(1) == "p3_SA"
        parent = tree.topLevelItem(2)
        assert parent.childCount() == 2 and not parent.isExpanded()
        assert [parent.child(i).text(1) for i in range(2)] == ["p1_ML", "p2_ML"]
        assert len({tree.topLevelItem(i).background(0).color().name() for i in range(3)}) == 3
        for i in range(3):
            item = tree.topLevelItem(i)
            assert item.background(0) == item.background(1)
            assert item.data(0, Qt.DecorationRole) is None
        parent.setExpanded(True)
        tree.setCurrentItem(parent.child(0))
        assert dialog.details.toPlainText() == "p1_ML has no staged files."
    finally:
        dialog.close()


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
    def cancel(dialog):
        text = [label.text() for label in dialog.findChildren(QLabel)]
        assert "Current batch: 2 deployments · 3 recordings" in text
        assert "event" in text
        assert {b.text() for b in dialog.findChildren(QPushButton)} == {"Cancel", "Stage bird audio"}
        return dialog.DialogCode.Rejected

    monkeypatch.setattr(wizard.SoundHubStageDialog, "exec", cancel)
    window.stage_for_soundhub()
    validate.assert_not_called()
    check_box.assert_not_called()
    stage.assert_not_called()

    monkeypatch.setattr(wizard.SoundHubStageDialog, "exec", lambda self: self.DialogCode.Accepted)
    window.stage_for_soundhub()
    worker.start.assert_called_once()
    assert window.soundhub_status_label.text() == "Checking existing SoundHub batch…"
    validate.assert_not_called()
    check_box.assert_not_called()
    window.soundhub_stage_thread = None


def test_soundhub_continue_action_restores_saved_counts(workspace_window, tmp_path, monkeypatch):
    from cassn.gui import wizard
    from cassn.soundhub.staging import work_root

    _app, window = workspace_window
    event = tmp_path / "event"
    event.mkdir()
    deployment_id = "UC_RanchoMarino_plot2_BD_20260803"
    (event / "audio_file_metadata.csv").write_text(
        "filename,deployment_id,device_type,file_type\n"
        f"{deployment_id}_00001.wav,{deployment_id},BD,audio\n"
        f"{deployment_id}_00002.wav,{deployment_id},BD,audio\n"
    )
    staging = tmp_path / "soundhub"
    folder = work_root(staging) / deployment_id
    folder.mkdir(parents=True)
    (folder / f"{deployment_id}_00001.flac").write_bytes(b"completed")
    monkeypatch.setattr(window, "_soundhub_staging_root", lambda: staging)
    window.current_deployment_folder = event
    window.tabs.setCurrentIndex(2)
    assert window.soundhub_stage_btn.text() == "Continue SoundHub Staging…"
    assert "1 completed, 1 remaining" in window.soundhub_status_label.text()
    monkeypatch.setattr(wizard, "flac_available", lambda: True)
    def cancel(dialog):
        text = [label.text() for label in dialog.findChildren(QLabel)]
        assert "1 completed · 1 remaining" in text
        assert "Continue staging" in [b.text() for b in dialog.findChildren(QPushButton)]
        return dialog.DialogCode.Rejected

    monkeypatch.setattr(wizard.SoundHubStageDialog, "exec", cancel)
    window.stage_for_soundhub()

def test_activity_log_shows_device_severity_and_complete_diagnostic(workspace_window):
    _, window = workspace_window
    log = window.log_text
    log.clear()
    window.log("[p1_BD] Warning: Check the recorder.")
    window.log("QC [p2_ML] pass: Camera dates match.")
    window.log("✗ [p3_BD] Copy stopped.\nDetails: <card unavailable>")
    assert [log.topLevelItem(i).text(1) for i in range(3)] == ["Warning", "Passed", "Error"]
    entry = log.topLevelItem(2)
    assert entry.text(0) == "p3_BD · Copy stopped."
    assert entry.child(0).text(0) == "Copy stopped.\nDetails: <card unavailable>"


def test_soundhub_dismiss_and_failed_restore(workspace_window, tmp_path, monkeypatch):
    from cassn.gui import wizard

    _, window = workspace_window
    event = tmp_path / "event"
    event.mkdir()
    window.current_deployment_folder = event
    window.metadata["site_short_name"] = "example"
    window.save_session()
    monkeypatch.setattr(window, "_soundhub_staging_root", lambda: tmp_path / "staging")
    from cassn.soundhub.export import EventStagingSummary
    monkeypatch.setattr(wizard, "event_staging_summary", lambda *_: EventStagingSummary(1, 1, True))
    monkeypatch.setattr(wizard.QMessageBox, "information", lambda *args: None)
    monkeypatch.setattr(wizard.QMessageBox, "warning", lambda *args: None)

    window.on_soundhub_stage_complete(True, False, "Bird audio staged.", {})
    session = event / "session.json"
    before = session.read_bytes()
    window.dismiss_soundhub_btn.click()
    window._refresh_soundhub_resume()
    assert not window.soundhub_group.isHidden() and not window.soundhub_group.disclosure.isChecked()
    assert session.read_bytes() == before
    window.restore_session(json.loads(before))
    window.tabs.setCurrentIndex(2)
    assert not window.soundhub_group.isHidden() and not window.dismiss_soundhub_btn.isHidden()

    window.on_soundhub_stage_complete(False, False, "Disk full", {})
    window.restore_session(json.loads(session.read_text()))
    window.tabs.setCurrentIndex(2)
    assert not window.soundhub_group.isHidden() and window.dismiss_soundhub_btn.isHidden()
    assert "Disk full" in window.soundhub_status_label.text()
    window._dismiss_soundhub_progress()
    assert not window.soundhub_group.isHidden()


def test_device_filter_keeps_card_history_after_panel_removal(workspace_window, tmp_path):
    _, window = workspace_window
    window._create_card_panel("p1_BD", tmp_path)
    window._on_card_log("p1_BD", "Warning: Low battery")
    window.log("Error: [p2_BD] Card unavailable")
    window._show_device_activity("p1_BD")
    window._remove_card_panel("p1_BD")
    log = window.log_text
    assert log.device_filter.currentText() == "p1_BD"
    rows = [log.topLevelItem(i) for i in range(log.topLevelItemCount())]
    visible = [r for r in rows if not r.isHidden()]
    assert [r.text(0) for r in visible] == ["p1_BD · Low battery"]
    assert visible[0].background(0) == visible[0].background(2)
    log.device_filter.setCurrentIndex(0)
    assert all(not row.isHidden() for row in rows)
