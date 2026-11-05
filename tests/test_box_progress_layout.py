"""The Box panel must reflow live updates as the Review window changes size."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from cassn.box.auth import hosted_config
from cassn.gui.wizard import FieldDataWizard
from cassn.lookups import LookupTables


@pytest.mark.parametrize("zoom", [100, 130, 200])
def test_box_progress_fits_wide_narrow_and_restored_review_window(monkeypatch, zoom):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(FieldDataWizard, "load_config", lambda self: None)
    monkeypatch.setattr(FieldDataWizard, "check_box_auth", lambda self: False)
    monkeypatch.setattr(FieldDataWizard, "find_all_sessions", lambda self: [])
    window = FieldDataWizard(lookups=LookupTables(), box_config=hosted_config("uc"))
    monkeypatch.setattr(window, "_box_callback_is_current", lambda _: True)
    try:
        window.tabs.setCurrentIndex(2)
        window.workspace.set_zoom(zoom)
        window.upload_group.show()
        window.cancel_upload_btn.show()
        window.hash_group.show()
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
            sizes.append((window.upload_progress_bar.width(), status.height()))
            if width == 930:
                page.ensureWidgetVisible(window.cancel_upload_btn)
                assert page.verticalScrollBar().value() > 0

        assert sizes[0] == sizes[2]
        assert sizes[1][0] < sizes[0][0]
        assert sizes[1][1] > sizes[0][1]
    finally:
        window.current_deployment_folder = None
        window.close()
        app.processEvents()
