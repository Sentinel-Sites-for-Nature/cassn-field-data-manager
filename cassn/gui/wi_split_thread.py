"""Background wrapper for first-upload Wildlife Insights preparation."""

from __future__ import annotations

import threading

from PySide6.QtCore import QThread, Signal

from cassn.core.wi_split import WI_UPLOAD_LIMIT, prepare_deployment_for_wi


class WISplitThread(QThread):
    """Plan, move, verify, and inventory-sync oversized camera folders."""

    PROGRESS_INTERVAL = 100
    progress = Signal(int, int, str)
    completed = Signal(bool, bool, str)  # success, cancelled, message

    def __init__(self, deployment_folder, file_inventory):
        super().__init__()
        self.deployment_folder = deployment_folder
        self.file_inventory = file_inventory
        self._cancel_event = threading.Event()
        self._last_progress = 0

    def cancel(self):
        self._cancel_event.set()

    def _emit_progress(self, current: int, total: int, path: str):
        """Keep the GUI responsive instead of queueing one signal per image."""
        if (
            total <= 0
            or current == total
            or current - self._last_progress >= self.PROGRESS_INTERVAL
        ):
            self._last_progress = current
            self.progress.emit(current, total, path)

    def run(self):
        try:
            result = prepare_deployment_for_wi(
                self.deployment_folder,
                self.file_inventory,
                limit=WI_UPLOAD_LIMIT,
                progress=self._emit_progress,
                is_cancelled=self._cancel_event.is_set,
            )
        except Exception as exc:
            self.completed.emit(False, False, f"WI image preparation failed: {exc}")
            return

        if result["cancelled"]:
            self.completed.emit(
                False,
                True,
                "WI image preparation cancelled after "
                f"{result['images_moved']}/{result['total_moves']} move(s). "
                "No Box upload was started; re-run upload to resume.",
            )
            return

        if result["devices_split"]:
            message = (
                "WI image preparation complete: "
                f"{result['devices_split']} camera folder(s) organized into "
                f"{result['parts']} part(s); {result['images_moved']} image(s) moved."
            )
        else:
            message = (
                "WI image preparation complete: no camera folder exceeds "
                f"{WI_UPLOAD_LIMIT:,} images."
            )
        self.completed.emit(True, False, message)
