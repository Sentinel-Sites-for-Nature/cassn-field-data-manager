"""Exercise the Review layout with every kind of progress panel visible."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPushButton

from cassn.box.auth import hosted_config
from cassn.gui.wizard import FieldDataWizard
from cassn.gui.appearance import PlatformPanel
from cassn.lookups import LookupTables


def test_simultaneous_progress_preserves_overview_and_titles(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(FieldDataWizard, "load_config",
                        lambda window: setattr(window, "wi_upload_helper", "sample"))
    monkeypatch.setattr(FieldDataWizard, "check_box_auth", lambda self: False)
    monkeypatch.setattr(FieldDataWizard, "find_all_sessions", lambda self: [])
    window = FieldDataWizard(lookups=LookupTables(), box_config=hosted_config("uc"))
    try:
        window.tabs.setCurrentIndex(2)
        window.show()
        page = window.tabs.widget(2)
        assert not page.progress_heading.isHidden()
        assert window.summary_group.content.isHidden()
        window.summary_group.disclosure.setChecked(True)
        groups = [window.upload_group, window.hash_group, window.soundhub_group, window.wi_control.group]
        for group in groups:
            group.show()
            group.disclosure.setChecked(True)
        wi = window.wi_control
        wi.jobs = {"sample": {"scopes": [{"deployment": "p1_ML", "images": 40}],
                              "rows": [{"deployment": "p1_ML", "stage": "uploading", "confirmed": 28}]}}
        wi.render()
        wi.timer.stop()
        wi_bar = wi.bar_for_item(wi.tree.topLevelItem(0))
        window.soundhub_status_label.setText("Preparing " + "recording" * 35)
        for width, zoom in ((1280, 100), (940, 130), (940, 200)):
            window.workspace.set_zoom(zoom)
            window.resize(width, 740)
            for _ in range(3):
                app.processEvents()
            assert page.horizontalScrollBar().maximum() == 0
            assert wi_bar.height() >= wi_bar.fontMetrics().height()
            assert window.summary_text.viewport().height() >= 150
            for group in groups:
                if isinstance(group, PlatformPanel):
                    header = group.header.geometry()
                    assert 0 <= header.top() < header.bottom() < group._body_area.geometry().top()
                    assert header.width() >= group.width() - 4
                    assert group._heading.geometry().height() >= group._heading.fontMetrics().height()
                    continue
            page.ensureWidgetVisible(wi.group.disclosure)
            assert page.verticalScrollBar().value() > 0
            assert {b.text() for b in page.findChildren(QPushButton)}.isdisjoint({"Summary ↑", "Progress ↓"})
        for group in groups:
            group.hide()
        assert not page.progress_heading.isHidden()
    finally:
        window.close()
        app.processEvents()


def test_summary_defaults_collapsed_and_finder_card_stays_visible(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(FieldDataWizard, "load_config", lambda w: setattr(w, "wi_upload_helper", "sample"))
    monkeypatch.setattr(FieldDataWizard, "check_box_auth", lambda _: False)
    monkeypatch.setattr(FieldDataWizard, "find_all_sessions", lambda _: [])
    monkeypatch.setattr("cassn.gui.wizard.platform.system", lambda: "Darwin")
    opened = []
    monkeypatch.setattr("cassn.gui.wizard.subprocess.run", lambda command: opened.append(command))
    window = FieldDataWizard(lookups=LookupTables(), box_config=hosted_config("uc"))
    try:
        window.tabs.setCurrentIndex(2)
        window.show()
        window.summary_text.setPlainText("Keep this summary")
        event = tmp_path / "UC_PinonRidge_20261021"
        event.mkdir()
        window.current_deployment_folder = event
        window.update_review_tab()
        assert "Before you finish" not in window.summary_text.toPlainText()
        window.summary_text.setPlainText("Keep this summary")
        assert window.staged_event_label.text() == "Open UC PinonRidge 2026-10-21 in Finder to review its files."
        assert window.open_event_folder_btn.isEnabled()
        window.open_event_folder_btn.click()
        assert opened == [["open", str(event)]]
        assert not hasattr(window, "staged_event_tree")
        assert window.summary_group.content.isHidden()
        assert not hasattr(window.staged_event_group, "disclosure")
        window.summary_group.disclosure.click()
        for _ in range(3):
            app.processEvents()
        for card in (window.summary_group,):
            app.processEvents()
            expanded_height = window.review_overview.height()
            card.disclosure.click()
            for _ in range(3):
                app.processEvents()
            assert card.content.isHidden()
            assert card.disclosure.arrowType() == Qt.RightArrow
            assert window.review_overview.height() < expanded_height
            card.disclosure.click()
            for _ in range(3):
                app.processEvents()
            assert not card.content.isHidden()
            assert card.disclosure.arrowType() == Qt.DownArrow
        assert window.summary_text.toPlainText() == "Keep this summary"
        window.current_deployment_folder = tmp_path / "missing"
        window._update_staged_event_folder()
        assert not window.open_event_folder_btn.isEnabled()
        assert "unavailable" in window.staged_event_label.text()
        window.current_deployment_folder = None
        window._update_staged_event_folder()
        assert not window.open_event_folder_btn.isEnabled()
    finally:
        window.current_deployment_folder = None
        window.close()
        app.processEvents()
