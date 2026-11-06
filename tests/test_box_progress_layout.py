"""Platform actions and progress must fit the Review window at each supported zoom."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QToolButton

from cassn.box.auth import hosted_config
from cassn.gui.wizard import FieldDataWizard
from cassn.lookups import LookupTables


def test_platform_buttons_use_guarded_actions_and_box_connection_state(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(FieldDataWizard, "load_config", lambda w: setattr(w, "wi_upload_helper", "/synthetic/helper"))
    monkeypatch.setattr(FieldDataWizard, "check_box_auth", lambda _: True)
    monkeypatch.setattr(FieldDataWizard, "find_all_sessions", lambda _: [])
    window = FieldDataWizard(lookups=LookupTables(), box_config=hosted_config("uc"))
    try:
        assert not window.connect_box_btn.isEnabled()
        window.set_box_connection_state(False)
        assert window.connect_box_btn.isEnabled()
        window.set_box_connection_state(True)
        assert not window.connect_box_btn.isEnabled()
        window.file_inventory = [{"filename": "test.jpg"}]
        window.wi_control.has_selection = True
        for group, action, label in (
            (window.upload_group, window.upload_now_btn, "Upload to Box"),
            (window.soundhub_group, window.soundhub_stage_btn, "Stage Bird Audio"),
            (window.wi_control.group, window.wi_control.action, "Upload to Wildlife Insights"),
        ):
            button = next(b for b in group.findChildren(QPushButton) if b.text() == label)
            action.triggered.disconnect()
            calls = []
            action.triggered.connect(lambda: calls.append(True))
            action.setEnabled(False)
            assert not button.isEnabled()
            button.click()
            assert not calls
            action.setEnabled(True)
            button.click()
            assert calls == [True]
            assert not group.disclosure.isChecked()
    finally:
        window.close()
        app.processEvents()


@pytest.mark.parametrize("zoom", [100, 130, 200])
def test_four_platform_panels_fit_wide_narrow_and_restored_review_window(monkeypatch, tmp_path, zoom):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(FieldDataWizard, "load_config", lambda self: setattr(self, "wi_upload_helper", "/synthetic/helper"))
    monkeypatch.setattr(FieldDataWizard, "check_box_auth", lambda self: False)
    monkeypatch.setattr(FieldDataWizard, "find_all_sessions", lambda self: [])
    window = FieldDataWizard(lookups=LookupTables(), box_config=hosted_config("uc"))
    monkeypatch.setattr(window, "_box_callback_is_current", lambda _: True)
    try:
        window.tabs.setCurrentIndex(2)
        window.workspace.set_zoom(zoom)
        window.current_deployment_folder = tmp_path
        monkeypatch.setattr(window, "_soundhub_staging_root", lambda: tmp_path / "audio")
        window.upload_group.show()
        window.upload_progress_bar.show()  # The transfer worker has started.
        window.cancel_upload_btn.show()
        window.hash_group.show()
        window.soundhub_group.show()
        window.soundhub_status_label.setText("Preparing recordings…")
        control = window.wi_control
        control.folder = tmp_path
        scopes = [
            {"deployment": "UC_Example_plot3_ML_20261023", "selection_sha256": "a"*64, "images": 8150},
            {"deployment": "UC_Example_plot3_SA_20261023", "selection_sha256": "b"*64, "images": 4044},
        ]
        rows = [
            {**scopes[0], "stage": "uploading", "confirmed": 2100},
            {**scopes[1], "stage": "verifying", "confirmed": 4044, "verified_count": 3130},
        ]
        control.jobs = {"synthetic": {"scopes": scopes, "rows": rows}}
        control.render()
        control.timer.stop()
        panels = [window.upload_group, window.hash_group, window.soundhub_group, control.group]
        for panel in panels:
            assert not panel.disclosure.isChecked()
            assert panel.disclosure.arrowType() == Qt.RightArrow
            panel.disclosure.click()
            assert panel.disclosure.arrowType() == Qt.DownArrow
        window.show()
        page = window.tabs.widget(2)
        path = "raw_data/p3_ML/p3_ML_1/" + "CameraImage" * 20 + ".jpg"
        status = window.upload_status_label
        sizes = []
        for count, width in enumerate((1440, 930, 1440), 1):
            window.resize(width, 740)
            window.on_upload_progress(count, 4, path)
            window.on_upload_metrics({"stage": "transferring", "files_per_second": count})
            window.hash_status_label.setText("Checking hashes for " + path)
            for _ in range(3):
                app.processEvents()

            assert window.upload_group.footer.isAncestorOf(window.cancel_upload_btn)
            if width == 1440 and zoom == 100:
                assert window.cancel_upload_btn.y() == window.box_panel_button.y()
                assert window.cancel_upload_btn.geometry().right() < window.box_panel_button.x()

            for panel in (window.upload_group, window.soundhub_group):
                assert panel._detail_area.geometry().bottom() < panel.footer.y()
                rects = []
                for button in panel.footer.findChildren(QPushButton):
                    if button.isVisible():
                        rect = button.rect().translated(button.mapTo(panel.footer, button.rect().topLeft()))
                        assert panel.footer.rect().contains(rect)
                        assert all(not other.intersects(rect) for other in rects)
                        rects.append(rect)
            assert page.widget().width() == page.viewport().width()
            assert page.horizontalScrollBar().maximum() == 0
            assert window.upload_progress_bar.text() == f"{count} of 4 files checked this transfer · {25 * count}%"
            assert window.box_history_label.isHidden()
            assert status.text() == path
            assert f"{count}.00 files/s" in window.upload_metrics_label.text()
            for label in (status, window.upload_metrics_label, window.hash_status_label):
                assert label.height() >= label.heightForWidth(label.width())
            assert len({p.header.height() for p in panels}) == 1
            assert len({p.logo.x() for p in panels}) == 1
            assert len({p._heading.x() for p in panels}) == 1
            for group, name, title in [
                (window.upload_group, "Box", "Upload event files to Box"),
                (window.hash_group, "Box", "Verify uploaded files against local copies"),
                (window.soundhub_group, "Wildlife Sound Hub", "Prepare and Stage Bird Audio Data for SoundHub Upload"),
                (control.group, "Wildlife Insights", "Upload camera deployments and photos"),
            ]:
                assert not group.isHidden()
                assert group.title() == title
                assert group.accessibleName() == f"{name} {title}"
                heading = group._heading
                bounds = heading.fontMetrics().boundingRect(heading.rect(), Qt.TextWordWrap, title)
                assert bounds.width() <= heading.width() and bounds.height() <= heading.height()
                logos = [label for label in group.findChildren(QLabel) if label.property("role") == "platformLogo"]
                assert [label.accessibleName() for label in logos] == [f"{name} logo"]
                assert not logos[0].pixmap().isNull()
                if name == "Wildlife Sound Hub":
                    assert logos[0].pixmap().deviceIndependentSize().width() >= 90
            for index, value, text in [(0, 2100, "Uploading 2,100 of 8,150 photos · 25%"),
                                       (1, 3130, "Verifying 3,130 of 4,044 photos · 77%")]:
                bar = control.bar_for_item(control.tree.topLevelItem(index))
                assert bar.value() == value
                assert bar.text() == text
                bounds = bar.fontMetrics().boundingRect(bar.rect(), Qt.AlignCenter | Qt.TextWordWrap, text)
                assert bounds.width() <= bar.width() and bounds.height() <= bar.height()
                assert bar.parentWidget().rect().contains(bar.geometry())
                assert control.tree.viewport().rect().contains(bar.mapTo(control.tree.viewport(), bar.rect().bottomRight()))
            sizes.append((window.upload_progress_bar.width(), status.height()))
            if width == 930:
                page.ensureWidgetVisible(window.cancel_upload_btn)
                assert page.verticalScrollBar().value() > 0

        assert sizes[0] == sizes[2]
        assert sizes[1][0] < sizes[0][0]
        assert sizes[1][1] > sizes[0][1]

        rows[0].update(stage="held", confirmed=0, hold_reason="Missing camera")
        control.render()
        assert "Missing camera" in control.tree.topLevelItem(0).text(1)
        for scope, row in zip(scopes, rows):
            row.update(stage="verified", confirmed=scope["images"], manifest_sha256="c"*64,
                       integrity={"kind": "integrity_verified_image_batch", "manifest_sha256": "c"*64,
                                  "receipt_sha256": "d"*64, "image_count": scope["images"]})
        control.render()
        window._show_saved_box_result(True)
        monkeypatch.setattr("cassn.gui.wizard.staging_progress", lambda *_: None)
        from cassn.soundhub.export import EventStagingSummary
        monkeypatch.setattr("cassn.gui.wizard.event_staging_summary", lambda *_: EventStagingSummary(1, 1, True))
        window._refresh_soundhub_resume()
        window.resize(930, 740)
        for _ in range(3):
            app.processEvents()
        for panel, main, open_label in (
            (window.upload_group, window.box_panel_button, "Open in Box"),
            (window.soundhub_group, window.soundhub_panel_button, "Open Staging Folder"),
            (control.group, control.panel_button, "Open Wildlife Insights"),
        ):
            footer_buttons = [b for b in panel.footer.findChildren(QPushButton) if b.isVisible()]
            assert len(footer_buttons) == 2
            opener = next(button for button in footer_buttons if button.text() == open_label)
            assert main.isVisible() and not main.isEnabled()
            assert opener.isVisible()
            panel.disclosure.setChecked(False)
            app.processEvents()
            positions = (opener.geometry().topLeft(), main.geometry().topLeft())
            panel.disclosure.setChecked(True)
            app.processEvents()
            assert positions == (opener.geometry().topLeft(), main.geometry().topLeft())
        for label in (window.upload_status_label, window.soundhub_status_label, control.verified_summary):
            assert label.property("platformStatus") == "complete"
            assert label.font().bold()
            assert label.palette().color(label.foregroundRole()) == window.upload_status_label.palette().color(window.upload_status_label.foregroundRole())
    finally:
        window.current_deployment_folder = None
        window.close()
        app.processEvents()

@pytest.mark.parametrize(("profile", "helper"), [("uc", "/test/helper"), ("uc", ""), ("csu", "")])
def test_platform_panels_are_present_before_work_and_match_the_project(monkeypatch, profile, helper):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(FieldDataWizard, "load_config", lambda w: setattr(w, "wi_upload_helper", helper))
    monkeypatch.setattr(FieldDataWizard, "check_box_auth", lambda _: False)
    monkeypatch.setattr(FieldDataWizard, "find_all_sessions", lambda _: [])
    window = FieldDataWizard(lookups=LookupTables(), box_config=hosted_config(profile))
    try:
        window.tabs.setCurrentIndex(2)
        window.show()
        app.processEvents()
        assert window.upload_group.isVisible()
        assert window.soundhub_group.isVisible() == (profile == "uc")
        assert not window.findChild(QToolButton, "prepareUpload")
        assert window.tabs.widget(2).progress_heading.text() == "Upload and Stage Data"
        if helper:
            assert window.wi_control.group.isVisible() and not window.wi_control.action.isEnabled()
        else:
            assert not getattr(window, "wi_control", None)
        assert window.switch_deployment_btn in window.file_menu.actions()
        assert window.new_btn in window.file_menu.actions()
    finally:
        window.close()
        app.processEvents()


def test_event_bars_count_each_new_file_once_and_keep_earlier_results(monkeypatch, tmp_path):
    from cassn.gui.event_progress import EventProgress
    from cassn.box.publication import STATE_PATH
    import json

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(FieldDataWizard, "load_config", lambda self: setattr(self, "wi_upload_helper", ""))
    monkeypatch.setattr(FieldDataWizard, "check_box_auth", lambda self: False)
    monkeypatch.setattr(FieldDataWizard, "find_all_sessions", lambda self: [])
    window = FieldDataWizard(lookups=LookupTables(), box_config=hosted_config("uc"))
    monkeypatch.setattr(window, "_box_callback_is_current", lambda *_: True)
    window.current_deployment_folder = tmp_path
    window.file_inventory = [{"new_filename": name, "device_label": "p1_ML", "deployment_id": "d"}
                             for name in ("old.jpg", "new.jpg")]
    monkeypatch.setattr("cassn.box.upload_history.published_file_keys", lambda *_: {("d", "old.jpg")})
    monkeypatch.setattr("cassn.gui.wizard.previously_uploaded", lambda *_: 1)
    try:
        state_path = tmp_path / STATE_PATH
        state_path.parent.mkdir()
        state = dict(status="needs_recovery", phase="initial", error="Legacy deployment mapping needs review for p1_ML",
                     files={"event": {"status": "read"}})
        state_path.write_text(json.dumps(state))
        window._show_saved_box_result(False)
        assert window.upload_progress_bar.text() == "1 of 2 files uploaded · 50%"
        assert "Ready to continue" in window.upload_status_label.text()
        assert "Legacy" not in window.upload_status_label.text()
        state["error"] = "A Box file changed during publication"
        state_path.write_text(json.dumps(state))
        window._show_saved_box_result(False)
        assert state["error"] in window.upload_status_label.text()

        window.upload_group.disclosure.setChecked(True)
        window._box_event_progress = EventProgress.box(tmp_path, window.file_inventory)
        for action in ("skipped: raw_data/p1_ML/old.jpg", "uploaded: qc/notes.json", "failed: raw_data/p1_ML/new.jpg"):
            window.on_upload_progress(1, 4, action)
            assert window.upload_progress_bar.value() == 1
        for _ in range(2):
            window.on_upload_progress(3, 4, "uploaded: raw_data/p1_ML/new.jpg")
            assert window.upload_progress_bar.text() == "2 of 2 event files · 100%"
            assert window.box_history_label.isHidden()
            assert window.upload_status_label.text() == "Uploaded: raw_data/p1_ML/new.jpg"
            assert not window.box_upload_complete

        window.upload_group.disclosure.setChecked(False)
        assert not window.box_history_label.isHidden()
        assert window.box_history_label.text() == window.upload_progress_bar.text()
        window._soundhub_event_progress = EventProgress(3, {"p2/new.flac", "p2/last.flac"},
                                                       {"p1": {"p1/old.flac"}, "p2": {"p2/new.flac", "p2/last.flac"}})
        window._soundhub_recording_prepared("p1", "old.wav")
        window._soundhub_recording_prepared("p2", "new.wav")
        assert window.soundhub_recorder_bars["p1"][1].text() == "1 of 1 recording · 100%"
        assert window.soundhub_recorder_bars["p2"][1].text() == "1 of 2 recordings · 50%"
        window._soundhub_recording_prepared("p2", "new.wav")
        assert window.soundhub_recorder_bars["p2"][1].value() == 1
        assert window.soundhub_recorder_layout.itemAtPosition(2, 1).widget() is window.soundhub_recorder_bars["p2"][1]
    finally:
        window.current_deployment_folder = None
        window.close()
        app.processEvents()
