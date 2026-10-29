"""Finished card panels follow mounted volumes, not leftover directories."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from cassn.box.auth import BoxConfig
from cassn.gui import wizard as w
from cassn.lookups import LookupTables


@pytest.fixture
def window(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(w.FieldDataWizard, "load_config", lambda self: None)
    monkeypatch.setattr(w.FieldDataWizard, "check_box_auth", lambda self: True)
    monkeypatch.setattr(w.FieldDataWizard, "find_all_sessions", lambda self: [])
    monkeypatch.setattr(w.FieldDataWizard, "save_session", lambda self: True)
    monkeypatch.setattr(w, "write_metadata_outputs", lambda *args, **kwargs: None)
    monkeypatch.setattr(w.QMessageBox, "critical", lambda *args: None)
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
