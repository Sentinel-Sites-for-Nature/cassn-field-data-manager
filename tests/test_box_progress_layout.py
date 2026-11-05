"""Platform actions and progress must fit the Review window at each supported zoom."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QAccessible
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QToolButton

from cassn.box.auth import hosted_config
from cassn.gui.wizard import FieldDataWizard
from cassn.lookups import LookupTables


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
        assert control.dismiss_button.isHidden()
        assert window.dismiss_box_btn.isHidden()
        assert window.dismiss_soundhub_btn.isHidden()
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

            assert page.widget().width() == page.viewport().width()
            assert page.horizontalScrollBar().maximum() == 0
            assert window.upload_progress_bar.value() == 25 * count
            assert status.text().endswith(f"{path} ({count}/4)")
            assert f"{count}.00 files/s" in window.upload_metrics_label.text()
            for label in (status, window.upload_metrics_label, window.hash_status_label):
                assert label.height() >= label.heightForWidth(label.width())
            for group, name in [
                (window.upload_group, "Box"), (window.hash_group, "Box"),
                (window.soundhub_group, "Wildlife Sound Hub"), (control.group, "Wildlife Insights"),
            ]:
                assert not group.isHidden()
                logos = [label for label in group.findChildren(QLabel) if label.property("role") == "platformLogo"]
                assert [label.accessibleName() for label in logos] == [f"{name} logo"]
                assert not logos[0].pixmap().isNull()
            for index, value, text in [(0, 2100, "Uploading 2,100 of 8,150 photos"),
                                       (1, 3130, "Verifying 3,130 of 4,044 photos")]:
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
        assert control.dismiss_button.isHidden()
        assert "Missing camera" in control.tree.topLevelItem(0).text(1)
        for scope, row in zip(scopes, rows):
            row.update(stage="verified", confirmed=scope["images"], manifest_sha256="c"*64,
                       integrity={"kind": "integrity_verified_image_batch", "manifest_sha256": "c"*64,
                                  "receipt_sha256": "d"*64, "image_count": scope["images"]})
        control.render()
        window._show_saved_box_result(True)
        monkeypatch.setattr("cassn.gui.wizard.staging_progress", lambda *_: None)
        monkeypatch.setattr("cassn.gui.wizard.event_audio_is_staged", lambda *_: True)
        window._refresh_soundhub_resume()
        window.resize(930, 740)
        for _ in range(3):
            app.processEvents()
        for button in (window.dismiss_box_btn, window.dismiss_soundhub_btn, control.dismiss_button):
            assert button.isVisible()
            page.ensureWidgetVisible(button)
            assert page.viewport().rect().contains(button.mapTo(page.viewport(), button.rect().center()))
    finally:
        window.current_deployment_folder = None
        window.close()
        app.processEvents()

def test_platform_menu_keeps_native_actions_and_accessible_logos(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(FieldDataWizard, "load_config", lambda self: setattr(self, "wi_upload_helper", "/synthetic/helper"))
    monkeypatch.setattr(FieldDataWizard, "check_box_auth", lambda self: False)
    monkeypatch.setattr(FieldDataWizard, "find_all_sessions", lambda self: [])
    window = FieldDataWizard(lookups=LookupTables(), box_config=hosted_config("uc"))
    try:
        window.tabs.setCurrentIndex(2)
        window.show()
        prepare = window.findChild(QToolButton, "prepareUpload")
        menu = prepare.menu()
        menu.popup(prepare.mapToGlobal(prepare.rect().bottomLeft()))
        app.processEvents()
        actions = menu.actions()
        assert actions == [window.upload_now_btn, window.soundhub_stage_btn, window.wi_control.action]
        assert [action.text() for action in actions] == [
            "Upload to Box now", "Prepare and stage bird audio for Wildlife Sound Hub",
            "Upload images to Wildlife Insights",
        ]
        logos = menu.findChildren(QLabel)
        assert [QAccessible.queryAccessibleInterface(logo).text(QAccessible.Name) for logo in logos] == [
            "Box logo", "Wildlife Sound Hub logo", "Wildlife Insights logo",
        ]
        for action, logo in zip(actions, logos):
            assert logo.isVisible() and not logo.pixmap().isNull()
            assert menu.actionGeometry(action).contains(logo.geometry())
            assert logo.testAttribute(Qt.WA_TransparentForMouseEvents)
        window.soundhub_stage_btn.setEnabled(False)
        assert not logos[1].isEnabled()
        window.soundhub_stage_btn.setText("Bird Audio Already Staged")
        assert menu.actions()[1].text() == "Bird Audio Already Staged"
        # Activating through the logo uses the original action exactly once.
        clicked = []
        window.upload_now_btn.triggered.disconnect()
        window.upload_now_btn.triggered.connect(lambda: clicked.append(True))
        window.upload_now_btn.setEnabled(True)
        QTest.mouseClick(menu, Qt.LeftButton, pos=logos[0].geometry().center())
        assert clicked == [True]
    finally:
        window.current_deployment_folder = None
        window.close()
        app.processEvents()
