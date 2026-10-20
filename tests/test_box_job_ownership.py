"""Real queued Qt signals must not change another deployment's state."""

from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication

from cassn.gui import wizard as w


class Worker(QObject):
    result = Signal(bool, str, list)
    upload = Signal(bool, str)
    prepared = Signal(bool, bool, str)
    progress = Signal(int, int, str)

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.finished = self.result
        self.completed = self.prepared
        self.deploy_folder_id = "synthetic-box-folder"
        self.started = False

    def start(self):
        assert self._box_owner_folder is not None
        self.started = True

    def isRunning(self):
        return False  # Exercise the gap before the queued callback is handled.


class Harness(QObject):
    # QObject-bound slots preserve the same dispatch and sender semantics as the GUI.
    for name in (
        '_box_callback_is_current', '_on_wi_split_progress', '_on_wi_split_completed',
        'on_upload_progress', 'on_upload_finished', '_write_upload_provenance',
        '_on_provenance_upload_finished', '_start_post_upload_verification',
        '_on_auto_box_verify_finished', '_on_auto_fixity_progress',
        '_on_auto_fixity_finished', '_finish_post_upload_verification',
        '_on_fixity_progress', '_on_fixity_finished', '_on_box_verify_finished',
        'run_fixity_check', 'run_box_verify', 'open_different_deployment',
        'start_new_deployment', 'closeEvent',
    ):
        locals()[name] = getattr(w.FieldDataWizard, name)

    def __init__(self, folder):
        super().__init__()
        self.current_deployment_folder = folder
        self.file_inventory = [{'new_filename': 'image.jpg'}]
        self.metadata = {'observer': 'Test'}
        self.devices = []
        self.box_config = {}
        self.box_authenticated = True
        self._box_operation_active = True
        self.box_upload_complete = False
        self.fixity_check_run = False
        self.card_ingest_threads = {}
        self._post_upload_box_summary = ''
        self._post_upload_box_issues = []
        for name in ('wi_split_thread', 'upload_thread', 'provenance_thread',
                     'box_verify_thread', 'fixity_thread'):
            setattr(self, name, None)
        for name in ('open_btn', 'switch_deployment_btn', 'new_btn', 'exit_btn',
                     'upload_now_btn', 'box_verify_btn', 'fixity_btn', 'cancel_upload_btn',
                     'upload_progress_bar', 'upload_status_label', 'hash_group',
                     'hash_progress_bar', 'hash_status_label', 'log', 'save_session',
                     '_update_copy_action', '_start_box_upload_thread',
                     '_format_post_upload_issues', 'show_pre_departure_checklist',
                     'find_all_sessions', 'offer_resume_session'):
            setattr(self, name, MagicMock())
        self._valid_site_names = lambda: None


@pytest.fixture
def setup(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    for name in ('information', 'warning', 'question'):
        monkeypatch.setattr(w.QMessageBox, name, MagicMock())
    # Avoid modal UI for failure branches while retaining real queued dispatch.
    dialog = MagicMock()
    dialog.Critical, dialog.Warning = 3, 2
    monkeypatch.setattr(w, 'QMessageBox', dialog)
    monkeypatch.setattr(w, 'BoxVerifyThread', Worker)
    monkeypatch.setattr(w, 'FixityCheckThread', Worker)
    qc = MagicMock()
    monkeypatch.setattr(w, 'append_qc_report', qc)
    folder = tmp_path / 'A'
    (folder / 'qc').mkdir(parents=True)
    return app, Harness(folder), qc


CALLBACKS = [
    ('wi_split_thread', '_on_wi_split_progress', 'progress', (1, 2, 'file')),
    ('wi_split_thread', '_on_wi_split_completed', 'prepared', (True, False, 'prepared')),
    ('upload_thread', 'on_upload_progress', 'progress', (1, 2, 'file')),
    ('upload_thread', 'on_upload_finished', 'upload', (True, 'uploaded')),
    ('provenance_thread', '_on_provenance_upload_finished', 'upload', (True, 'stamped')),
    ('box_verify_thread', '_on_auto_box_verify_finished', 'result', (True, 'verified', [])),
    ('fixity_thread', '_on_auto_fixity_progress', 'progress', (1, 2, 'file')),
    ('fixity_thread', '_on_auto_fixity_finished', 'result', (True, 'hashed', [])),
    ('fixity_thread', '_on_fixity_progress', 'progress', (1, 2, 'file')),
    ('fixity_thread', '_on_fixity_finished', 'result', (True, 'hashed', [])),
    ('box_verify_thread', '_on_box_verify_finished', 'result', (True, 'verified', [])),
]


@pytest.mark.parametrize('attribute,callback,signal,args', CALLBACKS)
@pytest.mark.parametrize('stale', ['changed_deployment', 'replaced_worker', 'new_operation'])
def test_queued_stale_callback_has_no_effect(setup, attribute, callback, signal, args, stale):
    app, context, qc = setup
    worker = Worker()
    setattr(context, attribute, worker)
    w.FieldDataWizard._start_owned_box_worker(context, worker)
    getattr(worker, signal).connect(getattr(context, callback), Qt.QueuedConnection)
    getattr(worker, signal).emit(*args)
    if stale == 'changed_deployment':
        context.current_deployment_folder = context.current_deployment_folder.parent / 'B'
    elif stale == 'replaced_worker':
        setattr(context, attribute, Worker())
    else:
        context._box_operation_token = object()
    app.processEvents()
    qc.assert_not_called()
    context.log.assert_not_called()
    context.save_session.assert_not_called()
    context._start_box_upload_thread.assert_not_called()
    context.hash_progress_bar.setValue.assert_not_called()
    context.upload_progress_bar.setValue.assert_not_called()
    assert context._box_operation_active
    assert not context.fixity_check_run
    assert not context.box_upload_complete


@pytest.mark.parametrize('method,attribute', [('run_box_verify', 'box_verify_thread'),
                                           ('run_fixity_check', 'fixity_thread')])
@pytest.mark.parametrize('ok', [True, False])
def test_manual_check_locks_lifecycle_until_queued_completion(setup, method, attribute, ok):
    app, context, qc = setup
    context._box_operation_active = False
    getattr(context, method)()
    worker = getattr(context, attribute)
    assert worker.started
    assert context._box_operation_active
    assert worker._box_owner_folder == context.current_deployment_folder
    context.open_different_deployment()
    context.start_new_deployment()
    event = MagicMock()
    context.closeEvent(event)
    event.ignore.assert_called_once()
    context.find_all_sessions.assert_not_called()
    context.show_pre_departure_checklist.assert_not_called()
    for button in (context.open_btn, context.switch_deployment_btn, context.exit_btn):
        button.setEnabled.assert_called_with(False)
    # Explicit queued connection to simulate completed thread awaiting GUI delivery.
    worker.result.disconnect()
    callback = context._on_box_verify_finished if attribute == 'box_verify_thread' else context._on_fixity_finished
    worker.result.connect(callback, Qt.QueuedConnection)
    worker.result.emit(ok, 'result', [])
    assert context._box_operation_active
    app.processEvents()
    assert not context._box_operation_active
    assert qc.call_args.args[0] == context.current_deployment_folder
    for button in (context.open_btn, context.switch_deployment_btn, context.exit_btn):
        button.setEnabled.assert_called_with(True)


def test_automatic_pipeline_keeps_lock_until_final_verification(setup):
    app, context, qc = setup
    upload = Worker()
    context.upload_thread = upload
    w.FieldDataWizard._start_owned_box_worker(context, upload)
    upload.upload.connect(context.on_upload_finished, Qt.QueuedConnection)
    upload.upload.emit(True, 'uploaded')
    app.processEvents()
    assert context._box_operation_active
    verify = context.box_verify_thread
    assert verify.started
    verify.result.emit(True, 'listed', [])
    assert context._box_operation_active
    fixity = context.fixity_thread
    assert fixity.started
    fixity.result.emit(True, 'hashed', [])
    assert not context._box_operation_active
    assert context.fixity_check_run
    assert all(call.args[0] == context.current_deployment_folder for call in qc.call_args_list)
    assert (context.current_deployment_folder / 'qc/box_upload_verification.json').is_file()


def test_provenance_phase_keeps_original_owner_and_lock(setup, monkeypatch):
    app, context, qc = setup
    path = context.current_deployment_folder / 'image_file_metadata.csv'
    path.write_text('filename\nimage.jpg\n')

    class ProvenanceWorker(Worker):
        def __init__(self, paths, folder_id, **kwargs):
            super().__init__()
            self.finished = self.upload
            assert paths == [path]
            assert folder_id == 'synthetic-box-folder'

    monkeypatch.setattr(w, 'ProvenanceUploadThread', ProvenanceWorker)
    context.upload_thread = Worker()
    w.FieldDataWizard._start_owned_box_worker(context, context.upload_thread)
    context.upload_thread.upload.connect(context.on_upload_finished, Qt.QueuedConnection)
    context.upload_thread.upload.emit(True, 'uploaded')
    app.processEvents()
    provenance = context.provenance_thread
    assert provenance.started and context._box_operation_active
    assert context.box_verify_thread is None
    provenance.upload.emit(False, 'synthetic provenance upload failure')
    assert context.box_verify_thread.started and context._box_operation_active
    context.box_verify_thread.result.emit(False, 'synthetic list check failure', [])
    assert not context._box_operation_active
    assert context.fixity_thread is None
    assert all(call.args[0] == context.current_deployment_folder for call in qc.call_args_list)


def test_cancelled_preparation_releases_controls_without_starting_upload(setup):
    app, context, qc = setup
    context.wi_split_thread = Worker()
    w.FieldDataWizard._start_owned_box_worker(context, context.wi_split_thread)
    context.wi_split_thread.prepared.connect(context._on_wi_split_completed, Qt.QueuedConnection)
    context.wi_split_thread.prepared.emit(False, True, 'cancelled')
    app.processEvents()
    assert not context._box_operation_active
    context._start_box_upload_thread.assert_not_called()
    assert qc.call_args.args[0] == context.current_deployment_folder
    context.switch_deployment_btn.setEnabled.assert_called_with(True)


def test_worker_start_failure_releases_lifecycle_lock(setup):
    _, context, _ = setup
    worker = Worker()
    worker.start = MagicMock(side_effect=RuntimeError('synthetic start failure'))
    with pytest.raises(RuntimeError, match='synthetic start failure'):
        w.FieldDataWizard._start_owned_box_worker(context, worker)
    assert not context._box_operation_active
    context.switch_deployment_btn.setEnabled.assert_called_with(True)


@pytest.mark.parametrize('method', ['run_fixity_check', 'run_box_verify'])
def test_manual_check_waits_for_card_jobs(setup, method):
    _, context, _ = setup
    context._box_operation_active = False
    context.card_ingest_threads = {'camera': object()}
    getattr(context, method)()
    assert not context._box_operation_active
    assert context.fixity_thread is None and context.box_verify_thread is None
