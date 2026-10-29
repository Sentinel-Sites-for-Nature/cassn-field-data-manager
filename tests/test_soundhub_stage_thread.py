"""Staging checks run in the worker and must finish before any data changes."""

import os
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from cassn.gui import soundhub_thread as module
from cassn.soundhub.staging import project_root


@pytest.fixture
def batch(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    root = project_root(tmp_path)
    root.mkdir()
    (root / "deployment.csv").write_text("deployment_id\nexisting\n")
    validate = MagicMock()
    lifecycle = MagicMock(return_value=SimpleNamespace(
        closed=False, errors=[], provenance=object(),
    ))
    monkeypatch.setattr(module, "validate_staging_manifests", validate)
    monkeypatch.setattr(module, "plan_completed_batch_cleanup", lifecycle)
    read = MagicMock(return_value=[])
    stage = MagicMock()
    monkeypatch.setattr(module, "read_bd_audio_rows", read)
    monkeypatch.setattr(module, "enrich_audio_rows", lambda rows, lookups: rows)
    monkeypatch.setattr(module, "stage_deployment", stage)
    worker = module.SoundHubStageThread(tmp_path / "event", tmp_path, None)
    results = []
    worker.completed.connect(lambda *args: results.append(args))
    return app, worker, validate, lifecycle, read, stage, results


@pytest.mark.parametrize("failure", ["invalid", "closed", "unsafe"])
def test_existing_batch_failure_prevents_source_reads_and_conversion(batch, failure):
    _app, worker, validate, lifecycle, read, stage, results = batch
    if failure == "invalid":
        validate.side_effect = ValueError("cumulative rows differ")
        expected = "cumulative rows differ"
    elif failure == "closed":
        lifecycle.return_value = SimpleNamespace(closed=True, errors=[], recording_count=12)
        expected = "already recorded as submitted"
    else:
        lifecycle.return_value = SimpleNamespace(closed=False, errors=["unsafe layout"], provenance=None)
        expected = "unsafe layout"
    worker.run()
    assert results[0][:2] == (False, False)
    assert expected in results[0][2]
    read.assert_not_called()
    stage.assert_not_called()
    if failure == "invalid":
        lifecycle.assert_not_called()


def test_box_errors_keep_existing_local_extension_policy(batch):
    _app, worker, validate, lifecycle, read, _stage, results = batch
    lifecycle.return_value.errors = ["Box unavailable"]
    worker.run()
    validate.assert_called_once()
    read.assert_called_once()
    assert "No bird" in results[0][2]


def test_slow_check_runs_off_main_thread_and_cancel_prevents_conversion(batch):
    app, worker, _validate, lifecycle, read, stage, results = batch
    entered = threading.Event()
    release = threading.Event()
    main_thread = threading.get_ident()
    thread_ids = []

    def slow_check(*args):
        thread_ids.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        return SimpleNamespace(closed=False, errors=[], provenance=object())

    lifecycle.side_effect = slow_check
    worker.start()
    try:
        assert entered.wait(5)
        assert worker.isRunning()
        app.processEvents()
        worker.cancel()
    finally:
        release.set()
        assert worker.wait(5000)
    app.processEvents()
    assert thread_ids[0] != main_thread
    assert results[0][:2] == (False, True)
    read.assert_not_called()
    stage.assert_not_called()


def test_empty_batch_skips_validation_and_box_checks(batch):
    _app, worker, validate, lifecycle, read, _stage, _results = batch
    (project_root(worker.staging_root) / "deployment.csv").unlink()
    worker.run()
    validate.assert_not_called()
    lifecycle.assert_not_called()
    read.assert_called_once()
