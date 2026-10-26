"""The deployment summary must exist before the Box uploader snapshots files."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from cassn.gui import wizard as wizard_module
from cassn.gui.wizard import FieldDataWizard


def test_box_upload_writes_summary_before_uploader_starts(tmp_path, monkeypatch):
    deployment = tmp_path / "UC_Test_20260902"
    (deployment / "qc").mkdir(parents=True)
    summary = deployment / "qc" / "deployment_summary.txt"
    seen_at_construction = []

    class _FakeUploadThread:
        def __init__(self, folder, *_args, **_kwargs):
            seen_at_construction.append(summary.is_file())
            self.progress = MagicMock()
            self.finished = MagicMock()

        def start(self):
            pass

    monkeypatch.setattr(wizard_module, "BoxUploadThread", _FakeUploadThread)
    context = SimpleNamespace(
        current_deployment_folder=deployment,
        metadata={"site_name": "Test Reserve", "observer": "Tester"},
        file_inventory=[],
        devices=[],
        box_config={},
        upload_progress_bar=MagicMock(),
        upload_status_label=MagicMock(),
        cancel_upload_btn=MagicMock(),
        log=MagicMock(),
        _update_copy_action=MagicMock(),
        _valid_site_names=lambda: None,
        on_upload_progress=MagicMock(),
        on_upload_finished=MagicMock(),
    )

    FieldDataWizard._start_box_upload_thread(context)

    assert seen_at_construction == [True]
    assert "CA-SSN DEPLOYMENT SUMMARY" in summary.read_text()


def test_box_upload_does_not_start_while_ingest_is_active(tmp_path, monkeypatch):
    context = SimpleNamespace(
        box_authenticated=True, current_deployment_folder=tmp_path,
        card_ingest_threads={"p1_ML": object()}, _box_operation_active=False,
    )
    message = MagicMock()
    preparation = MagicMock()
    monkeypatch.setattr(wizard_module.QMessageBox, "information", message)
    monkeypatch.setattr(wizard_module, "WISplitThread", preparation)
    FieldDataWizard.upload_to_box(context)
    message.assert_called_once()
    preparation.assert_not_called()
    assert not context._box_operation_active


def test_card_ingest_is_blocked_during_entire_box_cycle(monkeypatch):
    context = SimpleNamespace(_box_operation_active=True)
    message = MagicMock()
    chooser = MagicMock()
    monkeypatch.setattr(wizard_module.QMessageBox, "information", message)
    monkeypatch.setattr(wizard_module.QFileDialog, "getExistingDirectory", chooser)
    FieldDataWizard.copy_sd_card_data(context)
    message.assert_called_once()
    chooser.assert_not_called()


def test_copy_button_tracks_box_cycle():
    selected = object()
    tree = MagicMock()
    tree.currentItem.return_value = selected
    tree.indexOfTopLevelItem.return_value = 0
    concurrency = MagicMock()
    concurrency.value.return_value = 2
    context = SimpleNamespace(
        copy_btn=MagicMock(), device_tree=tree, devices=[(1, "Plot 1", "ML", "p1_ML")],
        card_ingest_threads={}, card_concurrency_spin=concurrency, _box_operation_active=True,
    )
    FieldDataWizard._update_copy_action(context)
    context.copy_btn.setEnabled.assert_called_with(False)
    context._box_operation_active = False
    FieldDataWizard._update_copy_action(context)
    context.copy_btn.setEnabled.assert_called_with(True)


def test_box_operation_latch_blocks_between_threads(tmp_path):
    context = SimpleNamespace(
        box_authenticated=True, current_deployment_folder=tmp_path,
        card_ingest_threads={}, _box_operation_active=True,
        wi_split_thread=None, upload_thread=None,
    )
    FieldDataWizard.upload_to_box(context)  # No UI or thread creation allowed.
    assert context._box_operation_active


def test_box_lock_is_kept_through_successful_upload_provenance():
    context = MagicMock()
    context._box_operation_active = True
    FieldDataWizard.on_upload_finished(context, True, "complete")
    assert context._box_operation_active is True
    context._write_upload_provenance.assert_called_once()


def test_box_lock_releases_after_failed_upload(monkeypatch):
    monkeypatch.setattr(wizard_module.QMessageBox, "warning", MagicMock())
    context = MagicMock()
    context._box_operation_active = True
    FieldDataWizard.on_upload_finished(context, False, "failed")
    assert context._box_operation_active is False
    context._update_copy_action.assert_called_once()


def test_box_lock_releases_after_cancelled_preparation(tmp_path, monkeypatch):
    monkeypatch.setattr(wizard_module.QMessageBox, "information", MagicMock())
    context = MagicMock()
    context.current_deployment_folder = tmp_path
    context._box_operation_active = True
    FieldDataWizard._on_wi_split_completed(context, False, True, "cancelled")
    assert context._box_operation_active is False
    context._update_copy_action.assert_called_once()
    context._start_box_upload_thread.assert_not_called()


def test_box_lock_releases_when_verification_has_no_files(tmp_path):
    context = MagicMock()
    context.current_deployment_folder = tmp_path
    context.file_inventory = []
    context._box_operation_active = True
    FieldDataWizard._start_post_upload_verification(context)
    assert context._box_operation_active is False
    context._update_copy_action.assert_called_once()


def test_box_lock_releases_after_verification(tmp_path, monkeypatch):
    monkeypatch.setattr(wizard_module.QMessageBox, "information", MagicMock())
    monkeypatch.setattr(wizard_module.QMessageBox, "warning", MagicMock())
    context = MagicMock()
    context.current_deployment_folder = tmp_path
    context._post_upload_box_issues = []
    context._post_upload_box_summary = "ok"
    context._box_operation_active = True
    FieldDataWizard._finish_post_upload_verification(context, True, "ok", [])
    assert context._box_operation_active is False
    context._update_copy_action.assert_called_once()


def test_box_preparation_acquires_guard_before_worker_starts(tmp_path, monkeypatch):
    context = MagicMock()
    context.current_deployment_folder = tmp_path
    context.box_authenticated = True
    context.card_ingest_threads = {}
    context._box_operation_active = False
    context.wi_split_thread = None
    context.upload_thread = None
    context.box_verify_thread = None
    context.fixity_thread = None
    context.box_upload_complete = False
    context.file_inventory = []
    preparation = MagicMock()

    def start():
        assert context._box_operation_active is True
        context._update_copy_action.assert_called_once()

    preparation.start.side_effect = start
    monkeypatch.setattr(wizard_module, "WISplitThread", lambda *_args: preparation)
    FieldDataWizard.upload_to_box(context)
    preparation.start.assert_called_once()
    assert context._box_operation_active is True


def test_manual_checks_cannot_interrupt_box_cycle():
    context = SimpleNamespace(_box_operation_active=True)
    FieldDataWizard.run_fixity_check(context)
    FieldDataWizard.run_box_verify(context)


def test_upload_waits_for_running_manual_check(tmp_path, monkeypatch):
    check = MagicMock()
    check.isRunning.return_value = True
    context = SimpleNamespace(
        box_authenticated=True, current_deployment_folder=tmp_path,
        card_ingest_threads={}, _box_operation_active=False,
        box_verify_thread=check, fixity_thread=None,
    )
    message = MagicMock()
    monkeypatch.setattr(wizard_module.QMessageBox, "information", message)
    FieldDataWizard.upload_to_box(context)
    message.assert_called_once()
    assert context._box_operation_active is False


def test_failed_preparation_save_blocks_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(wizard_module.QMessageBox, 'warning', MagicMock())
    context = MagicMock()
    context.current_deployment_folder = tmp_path
    context._box_operation_active = True
    context.save_session.return_value = False
    FieldDataWizard._on_wi_split_completed(context, True, False, 'prepared')
    context._start_box_upload_thread.assert_not_called()
    assert not context._box_operation_active
    assert 'could not be saved' in context.upload_status_label.setText.call_args.args[0]


def test_successful_upload_keeps_deployment_controls_locked():
    context = MagicMock()
    FieldDataWizard.on_upload_finished(context, True, 'uploaded')
    context.open_btn.setEnabled.assert_not_called()
    context.switch_deployment_btn.setEnabled.assert_not_called()
    context.exit_btn.setEnabled.assert_not_called()
    context._write_upload_provenance.assert_called_once()


def test_active_box_operation_prevents_switch_reset_and_close(monkeypatch):
    monkeypatch.setattr(wizard_module.QMessageBox, 'information', MagicMock())
    context = SimpleNamespace(_box_operation_active=True)
    FieldDataWizard.open_different_deployment(context)
    FieldDataWizard.start_new_deployment(context)
    event = MagicMock()
    FieldDataWizard.closeEvent(context, event)
    event.ignore.assert_called_once()
    event.accept.assert_not_called()


def test_ingest_hold_blocks_upload_before_authentication(tmp_path, monkeypatch):
    from cassn.box import threads
    from cassn.core.quality_control import append_qc_report
    append_qc_report(tmp_path, "ingest_safety", "p1_ML", "error", "Original source needs review")
    authenticate = MagicMock(side_effect=AssertionError("Must stop before Box access"))
    monkeypatch.setattr(threads, "get_box_client", authenticate)
    worker = threads.BoxUploadThread(tmp_path, {}, box_config={}, valid_site_names=set())
    outcomes = []
    worker.finished.connect(lambda ok, message: outcomes.append((ok, message)))
    worker.run()
    authenticate.assert_not_called()
    assert len(outcomes) == 1
    assert outcomes[0][0] is False
    assert "Original source needs review" in outcomes[0][1]
