"""Open an isolated, non-operational workspace preview: python -m cassn.gui.preview."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QPushButton, QToolButton

from cassn.box.auth import BoxConfig
from cassn.gui.wizard import FieldDataWizard
from cassn.lookups import LookupTables, Site, build_deployment_rounds


def sample_lookups() -> LookupTables:
    """Build a self-contained fixture without reading the live lookup cache."""
    event = {
        "deployment_event_id": "UC_SantaCruzIsland_20260808",
        "site_short_name": "SantaCruzIsland",
        "site_name": "Santa Cruz Island Reserve",
        "deployment_event_start_date": "2026-03-21",
        "deployment_event_end_date": "2026-08-08",
    }
    rows = [
        {
            "deployment_id": f"sample-{plot}-{code}",
            "deployment_event_id": event["deployment_event_id"],
            "deployment_sequence": 0,
            "site_short_name": event["site_short_name"],
            "plot_number": plot,
            "device_type": code,
            "device_id": f"{plot:016X}" if code in {"BD", "BT"} else f"SAMPLE-{plot}-{code}",
            "asset_tag": "",
            "deployment_start_date": event["deployment_event_start_date"],
            "deployment_end_date": event["deployment_event_end_date"],
            "identifier_policy": "filed_legacy",
        }
        for plot in range(1, 5) for code in ("ML", "SA", "BD", "BT")
    ]
    events, by_round = build_deployment_rounds([event], rows)
    return LookupTables(
        sites=[Site(event["site_name"], event["site_short_name"], "SCI")],
        plot_names={"SantaCruzIsland": {i: str(i) for i in range(1, 5)}},
        deployments=events, _deployment_rows_by_round=by_round,
    )


class WorkspacePreview(FieldDataWizard):
    """Use temporary sample data; never load credentials, resume, or run transfers."""

    def load_config(self):
        pass

    def save_config(self):
        pass

    def check_box_auth(self):
        return False

    def find_all_sessions(self):
        return []

    def save_session(self):
        return True

    def _navigate_workspace(self, index):
        self.tabs.setCurrentIndex(index)

    def _refresh_workspace_context(self, *_args):
        super()._refresh_workspace_context()
        self.workspace.header_status.setText("Preview · sample data · transfers disabled")

    def _update_copy_action(self, *_args):
        super()._update_copy_action()
        if hasattr(self, "copy_btn"):
            self.copy_btn.setEnabled(False)

    def closeEvent(self, event):
        # There are no workers or production sessions to finalize.
        event.accept()

    def __init__(self, sample_root: Path):
        super().__init__(lookups=sample_lookups(), box_config=BoxConfig())
        self.setWindowTitle("CA-SSN — Field Workspace Preview (sample data)")
        self._card_ejection_timer.stop()
        self.staging_root = sample_root
        self.staging_label.setText(str(sample_root))
        self.current_deployment_folder = sample_root / "UC_SantaCruzIsland_20260808"
        raw = self.current_deployment_folder / "raw_data"
        raw.mkdir(parents=True)
        self.metadata = {
            "organization": "UC", "site_name": "Santa Cruz Island Reserve",
            "site_short_name": "SantaCruzIsland", "site_code": "SCI",
            "deployment_event_id": "UC_SantaCruzIsland_20260808",
            "deployment_event_start_date": "2026-03-21",
            "deployment_event_end_date": "2026-08-08", "observer": "Sample observer",
        }
        self.devices = [
            (plot, str(plot), code, f"p{plot}_{code}")
            for plot in range(1, 5) for code in ("ML", "SA", "BD", "BT")
        ]
        self.populate_collection_list()
        for index, count in enumerate((1847, 1840, 120, 86)):
            label = self.devices[index][3]
            (raw / label).mkdir()
            self.file_inventory.extend(
                {
                    "device_label": label, "new_filename": f"sample_{number}.jpg" if index < 2 else f"sample_{number}.WAV",
                    "file_type": "image" if index < 2 else "audio",
                    "file_size_bytes": 7_000_000 if index < 2 else 1_000_000_000,
                }
                for number in range(count)
            )
            item = self.device_tree.topLevelItem(index)
            item.setText(2, "Copying" if index < 2 else "Complete")
            item.setText(3, str(count))
            if index < 2:
                source = sample_root / f"SAMPLE-1-{self.devices[index][2]}"
                source.mkdir()
                self._create_card_panel(label, source)
                self.card_ingest_panels[label]["started"] = time.monotonic() - count / (39.5 if index == 0 else 51.7)
                self._on_card_progress(label, count, 40564 if index == 0 else 2655, "sample_camera_image.jpg")
        self.observer_combo.setCurrentText("Sample observer")
        self.device_tree.setCurrentItem(self.device_tree.topLevelItem(4))
        self.update_review_tab()
        self.tabs.setCurrentIndex(1)
        # Keep navigation and disclosure controls usable. Operational controls,
        # source/destination pickers, session switching, and all actions are inert.
        for button in self.findChildren(QPushButton):
            if button not in self.workspace.navigation:
                button.setEnabled(False)
        for button in self.findChildren(QToolButton):
            if button.menu() is not None:
                button.setEnabled(False)
        for action in self.findChildren(QAction):
            action.setEnabled(False)
        self.copy_btn.setEnabled(False)
        self.card_concurrency_spin.setEnabled(False)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    with TemporaryDirectory(prefix="cassn-workspace-preview-") as directory:
        window = WorkspacePreview(Path(directory))
        window.show()
        return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
