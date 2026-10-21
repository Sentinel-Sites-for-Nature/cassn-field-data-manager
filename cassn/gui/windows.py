"""Independent deployment windows with exclusive ownership of shared resources."""

from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from functools import wraps
from pathlib import Path

from PySide6.QtCore import QIODevice, QLockFile, QSaveFile, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QDialog

from cassn.config import CONFIG_JSON
from cassn.gui.wizard import FieldDataWizard
from cassn.gui.dialogs import WindowMessageBox as QMessageBox
from cassn.lookups import normalize_deployment_event_metadata


class ResourceBusy(RuntimeError):
    pass


class ResourceLease:
    """Crash-recoverable locks outside deployment folders, also honored by other processes."""

    def __init__(self, directory: Path, keys):
        self.keys = tuple(sorted(set(keys)))
        self._locks = []
        directory.mkdir(parents=True, exist_ok=True)
        try:
            for key in self.keys:
                name = hashlib.sha256(key.encode("utf-8")).hexdigest() + ".lock"
                lock = QLockFile(str(directory / name))
                # A long upload is not stale. Qt can still recover dead-process locks.
                lock.setStaleLockTime(0)
                if not lock.tryLock(0):
                    if lock.error() != QLockFile.LockFailedError:
                        raise OSError(f"Cannot create an application lock in {directory}")
                    raise ResourceBusy(key)
                self._locks.append(lock)
        except Exception:
            self.release()
            raise

    def release(self):
        for lock in reversed(self._locks):
            lock.unlock()
        self._locks.clear()


def deployment_keys(folder: Path, event_id: str, box_root: str | None):
    return (
        "deployment-path:" + os.path.normcase(str(folder.resolve())),
        f"deployment-event:{box_root or 'default'}:{event_id}",
    )


def _guard_callback(handler):
    """Keep ownership through queued callbacks, including their nested dialogs."""
    @wraps(handler)
    def guarded(self, *args, **kwargs):
        self._callback_depth += 1
        try:
            return handler(self, *args, **kwargs)
        finally:
            self._callback_depth -= 1
            self._release_idle_cloud()
    return guarded


class DeploymentWindows:
    """Own top-level windows and give each one its own mutable lookup snapshot."""

    def __init__(self, *, lookups, box_config, lock_directory=None):
        self.lookups = deepcopy(lookups)
        self.box_config = box_config
        self.lock_directory = Path(lock_directory or CONFIG_JSON.parent / "window-locks")
        self.windows = []
        self.box_authenticated = None

    def new_window(self, *, open_sessions=False):
        window = DeploymentWindow(self)
        window.setAttribute(Qt.WA_DeleteOnClose)
        window.closed.connect(self._forget_window)
        self.windows.append(window)
        window.show()
        if open_sessions:
            window._session_picker_timer.start(0)
        return window

    def _forget_window(self, window):
        self.windows.remove(window)

    def activate_window(self):
        """Raise an existing window without changing its deployment or active jobs."""
        if not self.windows:
            self.new_window()
            return
        window = next((item for item in self.windows if item.isActiveWindow()), self.windows[-1])
        window.setWindowState(window.windowState() & ~Qt.WindowMinimized)
        window.show()
        window.raise_()
        window.activateWindow()
        dialogs = [dialog for dialog in window.findChildren(QDialog) if dialog.isVisible() and dialog.isModal()]
        if dialogs:
            dialogs[-1].raise_()
            dialogs[-1].activateWindow()

    def close_all(self):
        for window in list(self.windows):
            if not window.close():
                return


class DeploymentWindow(FieldDataWizard):
    """Independent deployment, cloud connection, and local SoundHub batch ownership."""

    closed = Signal(object)

    def __init__(self, manager):
        self.manager = manager
        self._deployment_lease = None
        self._card_leases = {}
        self._cloud_lease = None
        self._starting_cloud = False
        self._soundhub_pending = False
        self._staging_pending = False
        self._staging_lease = None
        self._closed = False
        self._callback_depth = 0
        self._initializing = True
        super().__init__(lookups=deepcopy(manager.lookups), box_config=manager.box_config)
        self._initializing = False
        self._session_picker_timer = QTimer(self)
        self._session_picker_timer.setSingleShot(True)
        self._session_picker_timer.timeout.connect(self._open_saved_sessions)
        file_menu = self.menuBar().addMenu("File")
        self.menuBar().insertMenu(self.menuBar().actions()[0], file_menu)
        self.new_window_action = QAction("New Window", self)
        self.new_window_action.setShortcut(QKeySequence.New)
        self.new_window_action.triggered.connect(lambda: manager.new_window())
        file_menu.addAction(self.new_window_action)
        close_action = QAction("Close Window", self)
        close_action.setShortcut(QKeySequence.Close)
        close_action.triggered.connect(self.close)
        file_menu.addAction(close_action)
        file_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.setMenuRole(QAction.QuitRole)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(manager.close_all)
        file_menu.addAction(quit_action)
        self.switch_deployment_btn.setText("Open Deployment in New Window…")
        self._lease_timer = QTimer(self)
        self._lease_timer.setInterval(250)
        self._lease_timer.timeout.connect(self._release_idle_cloud)
        self._lease_timer.start()

    def check_box_auth(self):
        # New windows reuse the initial connection result instead of touching
        # the shared OAuth token file while a worker may be refreshing it.
        if self.manager.box_authenticated is None:
            self.manager.box_authenticated = super().check_box_auth()
        return self.manager.box_authenticated

    def find_all_sessions(self):
        return [] if self._initializing else super().find_all_sessions()

    def _open_saved_sessions(self):
        sessions = self.find_all_sessions()
        if sessions:
            self.offer_resume_session(sessions)

    def _refresh_workspace_context(self, *_args):
        super()._refresh_workspace_context()
        event = self.metadata.get("deployment_event_id") if self.current_deployment_folder else None
        self.setWindowTitle(f"{event or 'New deployment'} — CA-SSN Field Data Manager")

    def _navigate_workspace(self, index):
        if index != self.tabs.currentIndex() and self._work_running():
            QMessageBox.information(self, "Operation Running", "Wait for this window’s jobs to finish before changing pages. Use File → New Window to work on another deployment.")
            self.workspace._sync_page(self.tabs.currentIndex())
            return
        super()._navigate_workspace(index)

    def validate_and_next_collection(self):
        if self._cloud_lease or self._staging_lease or self._starting_cloud or self._soundhub_pending:
            QMessageBox.information(self, "Operation Running", "Wait for this window’s cloud operation to finish before regenerating deployment files.")
            return
        super().validate_and_next_collection()

    def _claim_deployment(self, folder, event_id):
        keys = tuple(sorted(deployment_keys(folder, event_id, self.box_config.field_data_folder_id)))
        if self._deployment_lease:
            if self._deployment_lease.keys == keys:
                return True
            QMessageBox.information(
                self, "Deployment Already Selected",
                "Use File → New Window to work on another deployment, or start a new deployment in this window.",
            )
            return False
        try:
            self._deployment_lease = ResourceLease(self.manager.lock_directory, keys)
            return True
        except ResourceBusy:
            QMessageBox.information(
                self, "Deployment Already Open",
                "That deployment is already open in another window. Each deployment can be edited in one window at a time.",
            )
        except OSError as exc:
            QMessageBox.warning(self, "Cannot Reserve Deployment", str(exc))
        return False

    def validate_and_next(self):
        if self._work_running():
            QMessageBox.information(self, "Operation Running", "Wait for this window’s active jobs to finish before changing deployment details.")
            return
        event = self.deploy_event_combo.currentData()
        claimed = False
        if event and event.get("deployment_event_id") and event.get("deployment_event_end_date"):
            folder = self.staging_root / event["deployment_event_id"]
            if not self._claim_deployment(folder, event["deployment_event_id"]):
                return
            claimed = self.current_deployment_folder is None
        try:
            super().validate_and_next()
        finally:
            if claimed and self.current_deployment_folder is None:
                self._release_deployment()

    def restore_session(self, session_data):
        folder = Path(session_data["deployment_folder"])
        metadata = normalize_deployment_event_metadata(session_data.get("metadata", {}))
        if self._claim_deployment(folder, metadata.get("deployment_event_id") or folder.name):
            super().restore_session(session_data)

    def _release_deployment(self):
        if self._deployment_lease:
            self._deployment_lease.release()
            self._deployment_lease = None

    def open_different_deployment(self):
        self.manager.new_window(open_sessions=True)

    def start_new_deployment(self):
        if self._work_running():
            QMessageBox.information(self, "Operation Running", "Wait for this window’s active jobs to finish before resetting it.")
            return
        super().start_new_deployment()
        if self.current_deployment_folder is None:
            self._release_deployment()

    def _claim_card_source(self, source_dir, device_label):
        source = next(
            (path for path in (source_dir, *source_dir.parents) if path.parent != path and path.is_mount()),
            source_dir,
        )
        try:
            lease = ResourceLease(self.manager.lock_directory, [f"source-card:{source.resolve()}"])
        except ResourceBusy:
            QMessageBox.information(self, "Card Already In Use", "Another window is copying from that card. Wait for that copy to finish.")
            return False
        except OSError as exc:
            QMessageBox.warning(self, "Cannot Reserve Card", str(exc))
            return False
        self._card_leases[device_label] = (source_dir, lease)
        return True

    def copy_sd_card_data(self):
        if self._cloud_lease or self._staging_lease:
            QMessageBox.information(self, "Operation Running", "Wait for this window’s cloud operation to finish before copying cards.")
            return
        try:
            super().copy_sd_card_data()
        finally:
            active = set(self.card_ingest_threads) | {thread.device_label for thread in self._retiring_card_threads}
            for label in set(self._card_leases) - active:
                source, lease = self._card_leases.pop(label)
                lease.release()
                self._active_card_sources.discard(source)

    def _release_finished_card_thread(self, worker):
        claim = self._card_leases.pop(worker.device_label, None)
        if claim:
            claim[1].release()
        super()._release_finished_card_thread(worker)

    def _cloud_workers_running(self):
        return any(worker is not None and worker.isRunning() for worker in (
            self.wi_split_thread, self.upload_thread, self.provenance_thread,
            self.box_verify_thread, self.fixity_thread, self.soundhub_upload_thread,
        ))

    def _workers_running(self):
        stage = self.soundhub_stage_thread
        return self._cloud_workers_running() or bool(stage and stage.isRunning())

    def _work_running(self):
        return bool(
            self.card_ingest_threads or self._retiring_card_threads
            or self._box_operation_active or self._soundhub_pending or self._starting_cloud
            or self._staging_pending
            or self._workers_running() or self._callback_depth
        )

    _on_card_completed = _guard_callback(FieldDataWizard._on_card_completed)
    _on_wi_split_completed = _guard_callback(FieldDataWizard._on_wi_split_completed)
    on_upload_finished = _guard_callback(FieldDataWizard.on_upload_finished)
    _on_provenance_upload_finished = _guard_callback(FieldDataWizard._on_provenance_upload_finished)
    _on_auto_box_verify_finished = _guard_callback(FieldDataWizard._on_auto_box_verify_finished)
    _on_auto_fixity_finished = _guard_callback(FieldDataWizard._on_auto_fixity_finished)
    _on_box_verify_finished = _guard_callback(FieldDataWizard._on_box_verify_finished)
    _on_fixity_finished = _guard_callback(FieldDataWizard._on_fixity_finished)

    def _release_idle_cloud(self):
        if self._cloud_lease and not self._cloud_work_running():
            self._cloud_lease.release()
            self._cloud_lease = None
        stage = self.soundhub_stage_thread
        if self._staging_lease and not self._staging_pending and not (stage and stage.isRunning()):
            self._staging_lease.release()
            self._staging_lease = None

    def _cloud_work_running(self):
        """Local SoundHub conversion does not occupy the cloud connection."""
        return bool(
            self.card_ingest_threads or self._retiring_card_threads
            or self._box_operation_active or self._soundhub_pending or self._starting_cloud
            or self._callback_depth
            or self._cloud_workers_running()
        )

    def _soundhub_batch_key(self):
        return f"soundhub-batch:{self._soundhub_staging_root().resolve()}"

    def _run_cloud_operation(self, handler, *, soundhub=False, soundhub_batch=False):
        if self._cloud_work_running() or self._cloud_lease:
            QMessageBox.information(self, "Operation Running", "Wait for this window’s active jobs to finish before starting another cloud operation.")
            return
        try:
            keys = ["cloud-operations"]
            if soundhub or soundhub_batch:
                keys.append(self._soundhub_batch_key())
            self._cloud_lease = ResourceLease(self.manager.lock_directory, keys)
        except ResourceBusy:
            QMessageBox.information(
                self, "Cloud Connection or SoundHub Batch in Use",
                "A Box or SoundHub operation is using the connection or this staging batch. Wait for it to finish before starting this operation.",
            )
            return
        except OSError as exc:
            QMessageBox.warning(self, "Cannot Reserve Cloud Connection", str(exc))
            return
        prior = (self.soundhub_stage_thread, self.soundhub_upload_thread)
        self._starting_cloud = True
        self._soundhub_pending = soundhub
        try:
            handler()
        except Exception as exc:
            if not self._cloud_workers_running():
                self._soundhub_pending = False
                FieldDataWizard._set_box_controls_busy(self, False)
                if not self._staging_pending:
                    self._set_soundhub_actions_enabled(True)
                self.cancel_upload_btn.hide()
                if not self._staging_pending:
                    self.cancel_soundhub_btn.hide()
            QMessageBox.critical(self, "Operation Could Not Start", str(exc))
        finally:
            self._starting_cloud = False
            if soundhub and prior == (self.soundhub_stage_thread, self.soundhub_upload_thread):
                self._soundhub_pending = False
            self._release_idle_cloud()

    def upload_to_box(self):
        self._run_cloud_operation(super().upload_to_box)

    def run_box_verify(self):
        self._run_cloud_operation(super().run_box_verify)

    def run_fixity_check(self):
        self._run_cloud_operation(super().run_fixity_check)

    def stage_for_soundhub(self):
        self._run_staging_operation(super().stage_for_soundhub)

    def _run_staging_operation(self, handler):
        if self._staging_pending or self._soundhub_pending or self._soundhub_busy() or self.card_ingest_threads or self._retiring_card_threads:
            QMessageBox.information(self, "Operation Running", "Wait for card ingest or the current SoundHub operation to finish before staging.")
            return
        try:
            self._staging_lease = ResourceLease(self.manager.lock_directory, [self._soundhub_batch_key()])
        except ResourceBusy:
            QMessageBox.information(self, "SoundHub Batch in Use", "Another operation is staging or submitting this SoundHub batch. Wait for it to finish.")
            return
        except OSError as exc:
            QMessageBox.warning(self, "Cannot Reserve SoundHub Staging", str(exc))
            return
        prior = self.soundhub_stage_thread
        self._staging_pending = True
        try:
            handler()
        except Exception as exc:
            if self.soundhub_stage_thread is None or not self.soundhub_stage_thread.isRunning():
                self._staging_pending = False
                self._set_soundhub_actions_enabled(True)
                self.cancel_soundhub_btn.hide()
            QMessageBox.critical(self, "SoundHub Staging Could Not Start", str(exc))
        finally:
            if prior is self.soundhub_stage_thread:
                self._staging_pending = False
            self._release_idle_cloud()

    def upload_to_soundhub(self):
        self._run_cloud_operation(super().upload_to_soundhub, soundhub=True)

    def verify_soundhub(self):
        self._run_cloud_operation(super().verify_soundhub, soundhub_batch=True)

    def on_soundhub_stage_complete(self, *args):
        try:
            super().on_soundhub_stage_complete(*args)
        finally:
            self._staging_pending = False
            self._release_idle_cloud()

    def on_soundhub_upload_complete(self, *args):
        try:
            super().on_soundhub_upload_complete(*args)
        finally:
            self._soundhub_pending = False
            self._release_idle_cloud()

    def save_config(self):
        """Merge roots from other windows and atomically retain credentials/settings."""
        try:
            lease = ResourceLease(self.manager.lock_directory, ["gui-settings"])
            try:
                config = json.loads(self.config_file.read_text()) if self.config_file.exists() else {}
                if not isinstance(config, dict):
                    raise ValueError("The existing settings file is not a JSON object; it was left unchanged.")
                self._remember_staging_root(self.staging_root)
                existing = config.get("known_staging_roots", [])
                existing = [root for root in existing if isinstance(root, str)] if isinstance(existing, list) else []
                roots = list(dict.fromkeys(existing + self.known_staging_roots))
                config.update(staging_root=str(self.staging_root), known_staging_roots=roots,
                              card_concurrency_limit=self.card_concurrency_limit)
                self.config_file.parent.mkdir(parents=True, exist_ok=True)
                output = QSaveFile(str(self.config_file))
                payload = json.dumps(config, indent=2).encode("utf-8")
                if not output.open(QIODevice.WriteOnly):
                    raise OSError(output.errorString())
                if output.write(payload) != len(payload) or not output.commit():
                    raise OSError(output.errorString())
                self.known_staging_roots = roots
            finally:
                lease.release()
        except (OSError, ValueError, ResourceBusy) as exc:
            QMessageBox.warning(self, "Settings Could Not Be Saved", str(exc))

    def closeEvent(self, event):
        if self._work_running():
            QMessageBox.information(self, "Operation Running", "Finish or cancel this window’s active jobs before closing it. Other windows can stay open.")
            event.ignore()
            return
        super().closeEvent(event)
        if event.isAccepted() and not self._closed:
            self._closed = True
            self._lease_timer.stop()
            self._session_picker_timer.stop()
            self._card_ejection_timer.stop()
            for _source, lease in self._card_leases.values():
                lease.release()
            self._card_leases.clear()
            self._release_idle_cloud()
            self._release_deployment()
            self.closed.emit(self)
