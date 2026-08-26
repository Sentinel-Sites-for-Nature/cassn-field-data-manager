"""Background execution for one SD-card ingest.

Each worker owns one source card, one device destination, and one ExifTool
process.  It never mutates Qt widgets or the wizard's shared inventory.  New
inventory rows are delivered to the GUI thread in checkpoints, where the
single session writer merges and persists them.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from cassn.core.image_metadata import ReconyxExtractor
from cassn.core.inventory import count_expected_files
from cassn.core.quality_control import append_qc_report


class IngestHashRegistry:
    """Atomically reserve media hashes across concurrent card workers."""

    def __init__(self, hashes=()):
        self._hashes = set(hashes)
        self._lock = threading.Lock()

    def reserve(self, file_hash: str, file_type: str) -> bool:
        if file_type == "config":
            return True
        with self._lock:
            if file_hash in self._hashes:
                return False
            self._hashes.add(file_hash)
            return True

    def release(self, file_hash: str, file_type: str) -> None:
        if file_type == "config":
            return
        with self._lock:
            self._hashes.discard(file_hash)

    def add_committed(self, hashes) -> None:
        with self._lock:
            self._hashes.update(hash_ for hash_ in hashes if hash_)


class _WorkerContext:
    """The non-Qt surface required by ``process_sd_card_files``."""

    def __init__(self, thread, *, deployment_folder, metadata, lookups, inventory, registry):
        self.thread = thread
        self.current_deployment_folder = Path(deployment_folder)
        self.metadata = dict(metadata)
        self.lookups = lookups
        self.file_inventory = [dict(entry) for entry in inventory]
        self._initial_count = len(self.file_inventory)
        self._emitted_count = self._initial_count
        self._last_session_save = time.monotonic()
        self._registry = registry
        self._uncommitted_hashes: set[str] = set()

    def log(self, message) -> None:
        self.thread.log_line.emit(self.thread.device_label, str(message))

    def save_session(self) -> bool:
        self.emit_checkpoint()
        self._last_session_save = time.monotonic()
        return not self.thread.checkpoint_failed()

    def emit_checkpoint(self) -> None:
        if len(self.file_inventory) <= self._emitted_count:
            return
        rows = [dict(row) for row in self.file_inventory[self._emitted_count :]]
        self._emitted_count = len(self.file_inventory)
        self.thread.rows_ready.emit(self.thread.device_label, rows)
        self._uncommitted_hashes.difference_update(
            row.get("file_hash_sha256", "")
            for row in rows
            if row.get("file_type") != "config"
        )

    def _reserve_ingest_hash(self, file_hash: str, file_type: str) -> bool:
        reserved = self._registry.reserve(file_hash, file_type)
        if reserved and file_type != "config":
            self._uncommitted_hashes.add(file_hash)
        return reserved

    def _release_ingest_hash(self, file_hash: str, file_type: str) -> None:
        self._registry.release(file_hash, file_type)
        self._uncommitted_hashes.discard(file_hash)

    def release_uncommitted_hashes(self) -> None:
        for file_hash in tuple(self._uncommitted_hashes):
            self._registry.release(file_hash, "media")
        self._uncommitted_hashes.clear()

    def _append_ingest_qc(
        self, deployment_folder, check, device, severity, message
    ) -> None:
        append_qc_report(deployment_folder, check, device, severity, message)

    def _ingest_cancelled(self) -> bool:
        return self.thread.is_cancelled()

    def _ingest_progress(self, _copied: int, filename: str) -> None:
        self.thread.progress.emit(
            self.thread.device_label,
            len(self.file_inventory),
            self.thread.expected_file_count or 0,
            filename,
        )


class CardIngestThread(QThread):
    """Run one existing device copy engine against isolated worker state."""

    log_line = Signal(str, str)  # device_label, message
    progress = Signal(str, int, int, str)  # device_label, copied, expected, filename
    rows_ready = Signal(str, object)  # device_label, list[dict]
    completed = Signal(str, object)  # device_label, result dict

    def __init__(
        self,
        *,
        processor,
        source_dir,
        deployment_folder,
        plot_num,
        plot_label,
        device_code,
        device_label,
        metadata,
        lookups,
        inventory,
        hash_registry,
    ):
        super().__init__()
        self.processor = processor
        self.source_dir = Path(source_dir)
        self.deployment_folder = Path(deployment_folder)
        self.plot_num = plot_num
        self.plot_label = plot_label
        self.device_code = device_code
        self.device_label = device_label
        self.metadata = dict(metadata)
        self.lookups = lookups
        self.inventory = [dict(entry) for entry in inventory]
        self.hash_registry = hash_registry
        self.expected_file_count: int | None = None
        self._cancel_event = threading.Event()
        self._checkpoint_failed = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def mark_checkpoint_failed(self) -> None:
        self._checkpoint_failed.set()

    def checkpoint_failed(self) -> bool:
        return self._checkpoint_failed.is_set()

    def run(self) -> None:
        context = _WorkerContext(
            self,
            deployment_folder=self.deployment_folder,
            metadata=self.metadata,
            lookups=self.lookups,
            inventory=self.inventory,
            registry=self.hash_registry,
        )
        reconyx = None
        result: dict
        try:
            self.log_line.emit(self.device_label, "Counting files on source card…")
            self.expected_file_count = count_expected_files(self.source_dir)
            self.progress.emit(
                self.device_label, 0, self.expected_file_count or 0, "Starting…"
            )
            if self.is_cancelled():
                raise InterruptedError("Card ingest cancelled")

            destination = (
                self.deployment_folder / "raw_data" / self.device_label
            )
            destination.mkdir(parents=True, exist_ok=True)
            reconyx = ReconyxExtractor().start()
            copied, duplicates, hash_mismatches = self.processor(
                context,
                self.source_dir,
                destination,
                self.plot_num,
                self.plot_label,
                self.device_code,
                self.device_label,
                reconyx,
            )
            context.emit_checkpoint()
            result = {
                "ok": True,
                "cancelled": False,
                "expected_file_count": self.expected_file_count,
                "files_copied": copied,
                "duplicates": duplicates,
                "hash_mismatches": hash_mismatches,
                "source_dir": str(self.source_dir),
            }
        except InterruptedError as exc:
            context.emit_checkpoint()
            result = {
                "ok": False,
                "cancelled": True,
                "error": str(exc),
                "expected_file_count": self.expected_file_count,
                "source_dir": str(self.source_dir),
            }
        except Exception as exc:
            context.emit_checkpoint()
            error = str(exc)
            if not self.source_dir.exists():
                error = (
                    f"Source card disconnected or unmounted while copying from "
                    f"{self.source_dir}. Reconnect it and retry; completed files "
                    f"have been saved. Technical details: {exc}"
                )
            result = {
                "ok": False,
                "cancelled": False,
                "error": error,
                "expected_file_count": self.expected_file_count,
                "source_dir": str(self.source_dir),
            }
        finally:
            if reconyx is not None:
                reconyx.close()
            context.release_uncommitted_hashes()
        self.completed.emit(self.device_label, result)
