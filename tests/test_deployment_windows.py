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
    monkeypatch.setattr(FieldDataWizard, "_soundhub_staging_root", lambda self: tmp_path / "soundhub")
    windows = DeploymentWindows(lookups=sample_lookups(), box_config=BoxConfig(), lock_directory=tmp_path / "locks")
    windows.box_authenticated = False
    yield windows
    for window in list(windows.windows):
        window.card_ingest_threads.clear()
        window._retiring_card_threads.clear()
        window._box_operation_active = False
        window._soundhub_pending = False
        window._staging_pending = False
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


@pytest.mark.parametrize("box_first", [True, False])
@pytest.mark.parametrize("same_window", [True, False])
def test_box_and_local_soundhub_staging_run_together(manager, monkeypatch, box_first, same_window):
    box = manager.new_window()
    stage = box if same_window else manager.new_window()
    worker = MagicMock()
    worker.isRunning.return_value = True
    stage_start = MagicMock(side_effect=lambda self: setattr(self, "soundhub_stage_thread", worker))
    box_start = MagicMock(side_effect=lambda self: setattr(self, "_box_operation_active", True))
    monkeypatch.setattr(FieldDataWizard, "stage_for_soundhub", lambda self: stage_start(self))
    monkeypatch.setattr(FieldDataWizard, "upload_to_box", lambda self: box_start(self))
    operations = [box.upload_to_box, stage.stage_for_soundhub]
    for operation in operations if box_first else reversed(operations):
        operation()
    stage_start.assert_called_once()
    box_start.assert_called_once()
    assert box._cloud_lease is not None
    assert stage._staging_lease is not None
    assert stage._staging_pending

    box._box_operation_active = False
    box._release_idle_cloud()
    assert box._cloud_lease is None
    assert stage._staging_lease is not None
    assert stage._work_running()


def test_staging_keeps_batch_owned_until_completion_and_blocks_submission(manager, monkeypatch):
    stage, other = manager.new_window(), manager.new_window()
    worker = MagicMock()
    worker.isRunning.return_value = False  # Worker ended; completion is still queued.
    stage._run_staging_operation(lambda: setattr(stage, "soundhub_stage_thread", worker))
    stage._release_idle_cloud()
    assert stage._staging_lease is not None
    attempted = MagicMock()
    other._run_staging_operation(attempted)
    monkeypatch.setattr(FieldDataWizard, "upload_to_soundhub", attempted)
    monkeypatch.setattr(FieldDataWizard, "verify_soundhub", attempted)
    other.upload_to_soundhub()
    other.verify_soundhub()
    attempted.assert_not_called()
    assert other._cloud_lease is None

    # A Box job can start in the same window despite the queued staging callback.
    stage._run_cloud_operation(lambda: setattr(stage, "_box_operation_active", True))
    stage.on_soundhub_stage_complete(True, False, "Staged", {})
    assert stage._staging_lease is None
    assert stage._cloud_lease is not None
    assert not stage.exit_btn.isEnabled()
    assert not stage.new_btn.isEnabled()
    FieldDataWizard._set_box_controls_busy(stage, False)
    stage._release_idle_cloud()
    assert stage._cloud_lease is None
    assert stage.exit_btn.isEnabled()
    other._run_staging_operation(attempted)
    attempted.assert_called_once()


def test_declined_or_failed_staging_does_not_release_box_ownership(manager):
    window = manager.new_window()
    window._run_cloud_operation(lambda: setattr(window, "_box_operation_active", True))
    window._run_staging_operation(lambda: None)
    assert window._staging_lease is None
    assert window._cloud_lease is not None

    def fail():
        window._set_soundhub_actions_enabled(False)
        raise RuntimeError("synthetic encoder start failure")

    window._run_staging_operation(fail)
    assert not window._staging_pending
    assert window._staging_lease is None
    assert window._cloud_lease is not None
    assert not window.exit_btn.isEnabled()
    assert window.soundhub_stage_btn.isEnabled()


def test_stage_progress_title_and_box_finish_do_not_unlock_active_staging(manager, monkeypatch, tmp_path):
    import cassn.gui.wizard as wizard_module
    window = manager.new_window()
    window.current_deployment_folder = tmp_path / "deployment"
    window._box_operation_active = True
    worker = MagicMock()
    worker.isRunning.return_value = False
    worker.start.side_effect = lambda: setattr(worker.isRunning, "return_value", True)
    monkeypatch.setattr(wizard_module, "flac_available", lambda: True)
    monkeypatch.setattr(wizard_module, "SoundHubStageThread", lambda *args: worker)
    monkeypatch.setattr(WindowMessageBox, "question", lambda *args: WindowMessageBox.Yes)
    window.soundhub_group.setTitle("SoundHub upload progress")
    window.stage_for_soundhub()
    assert window.soundhub_group.title() == "SoundHub staging progress"
    assert window._staging_pending
    FieldDataWizard._set_box_controls_busy(window, False)
    assert not window.exit_btn.isEnabled()
    assert not window.new_btn.isEnabled()
    assert window.cancel_soundhub_btn.isEnabled()


def test_failed_box_start_keeps_soundhub_staging_and_its_controls(manager):
    window = manager.new_window()
    worker = MagicMock()
    worker.isRunning.return_value = True

    def stage():
        window.soundhub_stage_thread = worker
        window._set_soundhub_actions_enabled(False)
        window.cancel_soundhub_btn.show()

    window._run_staging_operation(stage)

    def fail():
        window._box_operation_active = True
        raise RuntimeError("synthetic Box start failure")

    window._run_cloud_operation(fail)
    assert window._cloud_lease is None
    assert window._staging_lease is not None
    assert window._staging_pending
    assert not window.soundhub_stage_btn.isEnabled()
    assert not window.cancel_soundhub_btn.isHidden()
    assert not window.exit_btn.isEnabled()


def test_soundhub_submission_reserves_batch_against_staging(manager):
    upload, stage = manager.new_window(), manager.new_window()
    worker = MagicMock()
    worker.isRunning.return_value = True
    upload._run_cloud_operation(lambda: setattr(upload, "soundhub_upload_thread", worker), soundhub=True)
    assert upload._soundhub_pending
    attempted = MagicMock()
    stage._run_staging_operation(attempted)
    attempted.assert_not_called()
    assert stage._staging_lease is None


def test_connect_box_updates_all_windows_under_cloud_lease(manager, monkeypatch):
    from cassn.gui.box_connect import BoxConnectDialog
    first, second = manager.new_window(), manager.new_window()
    config = BoxConfig("new-client", "secret", "folder", "lookup-folder")
    def login(dialog):
        assert first._cloud_lease is not None
        dialog.connected_config = config
        return BoxConnectDialog.Accepted
    monkeypatch.setattr(BoxConnectDialog, "exec", login)
    first.connect_to_box()
    assert manager.box_authenticated
    assert manager.box_config == config
    for window in (first, second):
        assert window.box_authenticated
        assert window.box_config == config
        assert window.box_connection_label.text() == "✓ Connected"
    assert first._cloud_lease is None


def test_connect_box_blocked_during_another_windows_upload(manager, monkeypatch):
    from cassn.gui.box_connect import BoxConnectDialog
    first, second = manager.new_window(), manager.new_window()
    login = MagicMock()
    monkeypatch.setattr(BoxConnectDialog, "exec", login)
    held = ResourceLease(manager.lock_directory, ["cloud-operations"])
    try:
        second.connect_to_box()
        login.assert_not_called()
    finally:
        held.release()
