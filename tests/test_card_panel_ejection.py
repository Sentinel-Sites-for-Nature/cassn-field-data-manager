"""Finished card panels follow mounted volumes, not leftover directories."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from cassn.box.auth import BoxConfig
from cassn.gui import wizard as w
from cassn.gui.card_eject import CardIdentity
from cassn.lookups import LookupTables

RUN_DISK_OPERATION = w.FieldDataWizard._run_card_disk_operation


@pytest.fixture
def window(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(w.FieldDataWizard, "_soundhub_staging_root", lambda self: tmp_path / "soundhub")
    monkeypatch.setattr(w.FieldDataWizard, "load_config", lambda self: None)
    monkeypatch.setattr(w.FieldDataWizard, "check_box_auth", lambda self: True)
    monkeypatch.setattr(w.FieldDataWizard, "find_all_sessions", lambda self: [])
    monkeypatch.setattr(w.FieldDataWizard, "save_session", lambda self: True)
    monkeypatch.setattr(w, "write_metadata_outputs", lambda *args, **kwargs: None)
    monkeypatch.setattr(w.QMessageBox, "critical", lambda *args: None)
    monkeypatch.setattr(w.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(w, "card_attachment", lambda volume, device: ("disk8s1", "disk8", (1, 2)))
    monkeypatch.setattr(w, "identify_card", lambda volume, device, attachment: CardIdentity(
        volume, device, "disk8s1", "disk8", "original", "CAMERA"))
    def run_operation(self, operation, callback):
        try:
            result = operation()
        except Exception as exc:
            callback(None, str(exc))
        else:
            callback(result, "")
    monkeypatch.setattr(w.FieldDataWizard, "_run_card_disk_operation", run_operation)
    widget = w.FieldDataWizard(lookups=LookupTables(), box_config=BoxConfig())
    widget.current_deployment_folder = tmp_path
    widget._card_ejection_timer.stop()
    yield widget
    widget.current_deployment_folder = None
    widget.close()
    app.processEvents()


@pytest.mark.parametrize("result", [
    {"ok": True},
    {"ok": False, "error": "Source disconnected"},
    {"ok": False, "cancelled": True, "error": "Cancelled"},
])
def test_ejection_clears_finished_job_with_mount_folder_remaining(
    window, tmp_path, monkeypatch, result
):
    volume = tmp_path / "card"
    source = volume / "DCIM"
    source.mkdir(parents=True)
    mounted = True
    monkeypatch.setattr(Path, "is_mount", lambda path: path == volume and mounted)
    monkeypatch.setattr(window, "_finalize_completed_card", lambda *args: None)
    window._create_card_panel("finished", source)
    window._create_card_panel("other-card", tmp_path)
    window._on_card_completed("finished", {**result, "source_dir": str(source)})

    window._remove_ejected_completed_card_panels()
    assert "finished" in window.card_ingest_panels

    mounted = False
    assert source.exists()  # Reproduce the old existence-only check's blind spot.
    window._remove_ejected_completed_card_panels()
    assert set(window.card_ingest_panels) == {"other-card"}


def test_replaced_volume_clears_finished_panel_but_preserves_running_job(
    window, tmp_path, monkeypatch
):
    source = tmp_path / "card"
    source.mkdir()
    monkeypatch.setattr(Path, "is_mount", lambda path: path == source)
    window._create_card_panel("finished", source)
    window._create_card_panel("running", source)
    window.card_ingest_panels["finished"]["finished"] = True
    # A different filesystem now occupies the same mounted path.
    device = window.card_ingest_panels["finished"]["source_volume_device"] + 1
    monkeypatch.setattr(Path, "stat", lambda path, **kwargs: SimpleNamespace(st_dev=device))
    window._remove_ejected_completed_card_panels()
    assert set(window.card_ingest_panels) == {"running"}


def make_card(window, tmp_path, monkeypatch):
    volume = tmp_path / "CARD"
    volume.mkdir()
    monkeypatch.setattr(Path, "is_mount", lambda path: path == volume)
    monkeypatch.setattr(window, "_finalize_completed_card", lambda *args: None)
    window._create_card_panel("card", volume)
    return volume, window.card_ingest_panels["card"]


@pytest.mark.parametrize("result,save_ok,visible", [
    ({"ok": True}, True, True),
    ({"ok": False, "error": "Copy failed"}, True, False),
    ({"ok": False, "cancelled": True}, True, False),
    ({"ok": True}, False, False),
])
def test_eject_button_requires_safe_completion(window, tmp_path, monkeypatch, result, save_ok, visible):
    volume, panel = make_card(window, tmp_path, monkeypatch)
    assert panel["eject"].isHidden()
    monkeypatch.setattr(window, "save_session", lambda: save_ok)
    window._on_card_completed("card", {**result, "source_dir": str(volume)})
    assert panel["eject"].isHidden() is not visible
    assert panel["eject"].text() == "Eject CAMERA"
    assert panel["eject"].toolTip() == str(volume)


def test_eject_visible_with_timezone_warning(window, tmp_path, monkeypatch):
    volume, panel = make_card(window, tmp_path, monkeypatch)
    monkeypatch.setattr(w, "check_camera_timezones", lambda rows: ("error", "bad timezone"))
    window._on_card_completed("card", {"ok": True, "source_dir": str(volume)})
    assert "timezone QC error" in panel["status"].text()
    assert not panel["eject"].isHidden()


def test_ordinary_folder_has_no_eject_button(window, tmp_path):
    window._create_card_panel("folder", tmp_path)
    window._on_card_completed("folder", {"ok": True, "source_dir": str(tmp_path)})
    assert window.card_ingest_panels["folder"]["eject"].isHidden()


def test_eject_pending_failure_retry_and_success(window, tmp_path, monkeypatch):
    volume, panel = make_card(window, tmp_path, monkeypatch)
    window._on_card_completed("card", {"ok": True, "source_dir": str(volume)})
    pending = []
    monkeypatch.setattr(window, "_run_card_disk_operation", lambda op, callback: pending.append(callback))
    panel["eject"].click()
    assert panel["eject"].text() == "Ejecting…"
    assert not panel["eject"].isEnabled()
    window._eject_card("card")
    assert len(pending) == 1
    monkeypatch.setattr(Path, "is_mount", lambda path: False)
    window._remove_ejected_completed_card_panels()
    assert "card" in window.card_ingest_panels
    pending.pop()(None, "Disk is busy")
    assert panel["eject"].isEnabled()
    assert "Disk is busy" in panel["messages"].toPlainText()
    panel["eject"].click()
    pending.pop()(None, "")
    assert "card" not in window.card_ingest_panels


def test_disk_operation_runs_in_background_and_delivers_on_gui_thread(window):
    import threading
    import time
    from PySide6.QtCore import QThread
    from PySide6.QtTest import QTest

    release = threading.Event()
    results = []
    def operation():
        assert QThread.currentThread() != window.thread()
        assert release.wait(2)
        return "done"
    def callback(result, error):
        results.append((result, error, QThread.currentThread() == window.thread()))
    RUN_DISK_OPERATION(window, operation, callback)
    try:
        assert window._card_disk_threads
        assert not results
    finally:
        release.set()
        deadline = time.monotonic() + 3
        while window._card_disk_threads and time.monotonic() < deadline:
            QTest.qWait(10)
    assert results == [("done", "", True)]
    assert not window._card_disk_threads


def test_discovery_timeout_recovers_after_copy(window, tmp_path, monkeypatch):
    identify = w.identify_card
    def timeout(*args):
        raise RuntimeError("Disk check timed out")
    monkeypatch.setattr(w, "identify_card", timeout)
    volume, panel = make_card(window, tmp_path, monkeypatch)
    window._on_card_completed("card", {"ok": True, "source_dir": str(volume)})
    assert panel["eject"].text() == "Retry card check"
    assert "timed out" in panel["messages"].toPlainText()
    monkeypatch.setattr(w, "identify_card", identify)
    panel["eject"].click()
    assert panel["eject"].text() == "Eject CAMERA"
    assert not panel["eject"].isHidden()


def test_completed_card_retries_once_and_refuses_replacement(window, tmp_path, monkeypatch):
    def timeout(*args):
        raise RuntimeError("Disk check timed out")
    monkeypatch.setattr(w, "identify_card", timeout)
    volume, panel = make_card(window, tmp_path, monkeypatch)
    monkeypatch.setattr(w, "card_attachment", lambda *args: ("disk8s1", "disk8", (3, 4)))
    window._on_card_completed("card", {"ok": True, "source_dir": str(volume)})
    assert not panel.get("card_identity")
    assert "original card is no longer connected" in panel["messages"].toPlainText()


def test_late_initial_failure_retries_automatically_after_completion(window, tmp_path, monkeypatch):
    pending = []
    monkeypatch.setattr(window, "_run_card_disk_operation", lambda op, done: pending.append((op, done)))
    volume, panel = make_card(window, tmp_path, monkeypatch)
    window._on_card_completed("card", {"ok": True, "source_dir": str(volume)})
    assert panel["eject"].text() == "Checking card…"
    op, done = pending.pop()
    attachment, _identity, _error = op()
    done((attachment, None, "First check timed out"), "")
    op, done = pending.pop()
    done(op(), "")
    assert panel["eject"].text() == "Eject CAMERA"
    assert panel["eject"].isEnabled()
    assert not pending
