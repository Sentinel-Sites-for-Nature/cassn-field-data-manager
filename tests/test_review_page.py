"""Exercise the Review layout with every kind of progress panel visible."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPushButton, QStyle, QStyleOptionGroupBox

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
            assert window.review_splitter.orientation() == (Qt.Horizontal if width == 1280 else Qt.Vertical)
            for view in (window.summary_text, window.staged_event_tree):
                assert view.viewport().height() >= 150
            for group in [window.review_splitter.widget(i) for i in range(2)] + groups:
                if isinstance(group, PlatformPanel):
                    header = group.header.geometry()
                    assert 0 <= header.top() < header.bottom() < group._body_area.geometry().top()
                    assert header.width() >= group.width() - 4
                    assert group._heading.geometry().height() >= group._heading.fontMetrics().height()
                    continue
                option = QStyleOptionGroupBox()
                group.initStyleOption(option)
                title = group.style().subControlRect(QStyle.CC_GroupBox, option, QStyle.SC_GroupBoxLabel, group)
                assert 0 < title.top() < title.bottom() < group.layout().itemAt(0).geometry().top()
            page.ensureWidgetVisible(wi.group.disclosure)
            assert page.verticalScrollBar().value() > 0
            assert {b.text() for b in page.findChildren(QPushButton)}.isdisjoint({"Summary ↑", "Progress ↓"})
        for group in groups:
            group.hide()
        assert not page.progress_heading.isHidden()
    finally:
        window.close()
        app.processEvents()
