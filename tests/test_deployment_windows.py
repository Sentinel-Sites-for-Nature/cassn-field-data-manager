"""Independent windows must not share mutable state or concurrent writers."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication

from cassn.box.auth import BoxConfig
from cassn.gui.dialogs import WindowDialog, WindowMessageBox
from cassn.gui.preview import sample_lookups
from cassn.gui.windows import DeploymentWindows, ResourceBusy, ResourceLease
from cassn.gui.wizard import FieldDataWizard


@pytest.fixture
def manager(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(FieldDataWizard, "load_config", lambda self: None)
    monkeypatch.setattr(FieldDataWizard, "find_all_sessions", lambda self: [])
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(WindowMessageBox, name, MagicMock())
    windows = DeploymentWindows(lookups=sample_lookups(), box_config=BoxConfig(), lock_directory=tmp_path / "locks")
    windows.box_authenticated = False
    yield windows
    for window in list(windows.windows):
        window.card_ingest_threads.clear()
        window._retiring_card_threads.clear()
        window._box_operation_active = False
        window._soundhub_pending = False
        window._starting_cloud = False
        for attr in ("upload_thread", "wi_split_thread", "provenance_thread", "box_verify_thread", "fixity_thread", "soundhub_stage_thread", "soundhub_upload_thread"):
            setattr(window, attr, None)
        window.current_deployment_folder = None
        window.close()
    app.processEvents()


def test_new_window_action_creates_independent_lookup_state(manager):
    first = manager.new_window()
    round_id = first.lookups.active_deployment_round_id
    assert first.lookups.active_deployment_rows
    first.new_window_action.trigger()
    assert len(manager.windows) == 2
    second = manager.windows[1]
    first.lookups.clear_active_deployment_round()
    first.metadata["observer"] = "First window"
    first.file_inventory.append({"sample": True})
    assert second.lookups.active_deployment_round_id == round_id
    assert second.lookups.active_deployment_rows
    assert second.metadata == {}
    assert second.file_inventory == []
    first.workspace.zoom_in_action.trigger()
    assert first.workspace.zoom == 110
    assert second.workspace.zoom == 100


def test_duplicate_deployment_blocked_before_create_or_restore(manager, tmp_path, monkeypatch):
    first, second = manager.new_window(), manager.new_window()
    first.staging_root = second.staging_root = tmp_path / "staging"
    first.validate_and_next()
    folder = first.current_deployment_folder
    assert folder is not None
    before = (folder / "session.json").read_bytes()
    second.validate_and_next()
    assert second.current_deployment_folder is None
    assert (folder / "session.json").read_bytes() == before
    restore = MagicMock()
    monkeypatch.setattr(FieldDataWizard, "restore_session", restore)
    second.restore_session(json.loads(before))
    restore.assert_not_called()
    # A second staging root still refers to the same Box deployment event.
    second.staging_root = tmp_path / "other-root"
    second.validate_and_next()
    assert second.current_deployment_folder is None
    assert not second.staging_root.exists()


def test_distinct_deployments_and_card_sources_can_be_claimed_independently(manager, tmp_path):
    first, second = manager.new_window(), manager.new_window()
    assert first._claim_deployment(tmp_path / "first", "event-one")
    assert second._claim_deployment(tmp_path / "second", "event-two")
    source = tmp_path / "card-one"
    source.mkdir()
    assert first._claim_card_source(source, "p1_ML")
    assert not second._claim_card_source(source, "p2_ML")
    other = tmp_path / "card-two"
    other.mkdir()
    assert second._claim_card_source(other, "p2_ML")
    # These claims normally release at worker finished; release explicit test claims.
    for window in (first, second):
        for _source, lease in window._card_leases.values():
            lease.release()
        window._card_leases.clear()


def test_closing_idle_window_preserves_upload_in_another(manager, monkeypatch):
    first, second = manager.new_window(), manager.new_window()
    first._run_cloud_operation(lambda: setattr(first, "_box_operation_active", True))
    assert first._cloud_lease is not None
    blocked = MagicMock()
    second._run_cloud_operation(blocked)
    blocked.assert_not_called()
    copy = MagicMock()
    monkeypatch.setattr(FieldDataWizard, "copy_sd_card_data", copy)
    second.copy_sd_card_data()
    copy.assert_called_once()
    assert second.close()
    assert manager.windows == [first]
    assert first._box_operation_active
    assert not first.close()
    first._box_operation_active = False
    first._release_idle_cloud()
    assert first._cloud_lease is None


def test_cloud_lease_survives_finished_thread_before_queued_completion(manager):
    first, second = manager.new_window(), manager.new_window()

    def start():
        first._box_operation_active = True
        first.upload_thread = MagicMock()
        first.upload_thread.isRunning.return_value = False

    first._run_cloud_operation(start)
    first._release_idle_cloud()
    handler = MagicMock()
    second._run_cloud_operation(handler)
    handler.assert_not_called()
    # The operation flag owns the gap between upload and verification callbacks.
    first._box_operation_active = False
    first._release_idle_cloud()
    second._run_cloud_operation(handler)
    handler.assert_called_once()
    assert second._cloud_lease is None


def test_declined_and_failed_cloud_start_release_lane(manager):
    first, second = manager.new_window(), manager.new_window()
    first._run_cloud_operation(lambda: None)
    assert first._cloud_lease is None

    def fail():
        first.new_btn.setEnabled(False)
        first.exit_btn.setEnabled(False)
        first._box_operation_active = True
        raise RuntimeError("synthetic start failure")

    first._run_cloud_operation(fail)
    assert first._cloud_lease is None
    assert first.new_btn.isEnabled()
    assert first.exit_btn.isEnabled()
    handler = MagicMock()
    second._run_cloud_operation(handler)
    handler.assert_called_once()


def test_queued_upload_callback_retains_ownership_inside_dialog(manager, monkeypatch):
    first, second = manager.new_window(), manager.new_window()

    class Worker(QObject):
        finished = Signal(bool, str)

        def isRunning(self):
            return False

    worker = Worker()
    worker._box_owner_folder = first.current_deployment_folder
    first._box_operation_token = worker._box_operation_token = object()
    first.upload_thread = worker
    first._run_cloud_operation(lambda: setattr(first, "_box_operation_active", True))
    attempted = MagicMock()

    def failure_dialog(*args):
        assert first._callback_depth == 1
        first._release_idle_cloud()
        second._run_cloud_operation(attempted)
        attempted.assert_not_called()

    monkeypatch.setattr(WindowMessageBox, "warning", failure_dialog)
    worker.finished.connect(first.on_upload_finished, Qt.QueuedConnection)
    worker.finished.emit(False, "synthetic upload failed")
    QApplication.processEvents()
    assert "synthetic upload failed" in first.upload_status_label.text()
    assert first._cloud_lease is None
    second._run_cloud_operation(attempted)
    attempted.assert_called_once()


def test_settings_merge_other_window_roots_without_losing_credentials(manager, tmp_path):
    first, second = manager.new_window(), manager.new_window()
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"box": {"sample": "preserve"}, "known_staging_roots": ["old"]}))
    first.config_file = second.config_file = config
    first.staging_root, second.staging_root = tmp_path / "one", tmp_path / "two"
    first.save_config()
    second.save_config()
    state = json.loads(config.read_text())
    assert state["box"] == {"sample": "preserve"}
    assert state["known_staging_roots"] == ["old", str(first.staging_root), str(second.staging_root)]


def test_window_dialogs_do_not_make_other_windows_modal(manager, monkeypatch):
    window = manager.new_window()
    dialog = WindowDialog(window)
    assert dialog.windowModality() == Qt.WindowModal
    seen = []

    def fake_exec(message):
        seen.append((message.parentWidget(), message.windowModality()))
        return WindowMessageBox.Yes

    monkeypatch.setattr(WindowMessageBox, "exec", fake_exec)
    assert WindowMessageBox.question(window, "Question", "Sample?") == WindowMessageBox.Yes
    assert seen == [(window, Qt.WindowModal)]


def test_failed_multi_resource_claim_releases_partial_locks(tmp_path):
    held = ResourceLease(tmp_path, ["b"])
    try:
        with pytest.raises(ResourceBusy):
            ResourceLease(tmp_path, ["a", "b"])
        available = ResourceLease(tmp_path, ["a"])
        available.release()
    finally:
        held.release()


def test_resource_lock_blocks_other_process_and_recovers_after_crash(tmp_path):
    script = """
import sys
from pathlib import Path
from cassn.gui.windows import ResourceLease
lease = ResourceLease(Path(sys.argv[1]), ['deployment'])
print('locked', flush=True)
sys.stdin.readline()
"""
    child = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(ResourceBusy):
            ResourceLease(tmp_path, ["deployment"])
    finally:
        child.terminate()
        child.communicate(timeout=10)
    recovered = ResourceLease(tmp_path, ["deployment"])
    recovered.release()


def test_open_event_does_not_claim_staging_or_create_session(manager, tmp_path, monkeypatch):
    window = manager.new_window()
    event = dict(window.deploy_event_combo.currentData())
    event.update(deployment_event_id="", deployment_event_end_date="")
    window.deploy_event_combo.addItem("Open deployments · In field", event)
    window.deploy_event_combo.setCurrentIndex(window.deploy_event_combo.count() - 1)
    window.staging_root = tmp_path / "staging"
    claim = MagicMock()
    monkeypatch.setattr(window, "_claim_deployment", claim)
    window.validate_and_next()
    claim.assert_not_called()
    assert window.current_deployment_folder is None
    assert not window.staging_root.exists()
