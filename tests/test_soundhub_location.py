"""Choosing the SoundHub staging location, its saved setting, and its status."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from cassn.box.auth import BoxConfig, hosted_config
from cassn.config import SOUNDHUB_PROJECT_SHORT_NAME
from cassn.soundhub import staging as staging_module
from cassn.soundhub.location import (
    CLOSED_BATCH, EMPTY, NOT_STAGING, OPEN_BATCH, UNAVAILABLE,
    save_soundhub_staging_root, staging_internal_folder, staging_root_status,
)
from cassn.soundhub.staging import fragments_root, project_root, recover_staging, work_root
from cassn.soundhub.upload import load_soundhub_config

SETTINGS = {"bucket": "casoundhub", "upload_prefix": "upload", "project_short_name": SOUNDHUB_PROJECT_SHORT_NAME}
DESTINATION = f"s3://casoundhub/upload/{SOUNDHUB_PROJECT_SHORT_NAME}/"
PATHS = ("UC_A_plot1_BD_20260714/a.flac", "UC_A_plot1_BD_20260714/b.flac")


def open_batch(root: Path) -> Path:
    project = project_root(root)
    for relative in PATHS:
        (project / relative).parent.mkdir(parents=True, exist_ok=True)
        (project / relative).write_bytes(b"x" * 1000)
        (project / relative).with_name("._" + Path(relative).name).write_bytes(b"apple")
    (project / "recording.csv").write_text("deployment_id,path\n" + "".join(
        f"UC_A_plot1_BD_20260714,{relative}\n" for relative in PATHS))
    (fragments_root(root) / "UC_A_plot1_BD_20260714").mkdir(parents=True)
    return root


def transfer_record(root: Path, paths, *, ok=True, destination=DESTINATION):
    directory = root.parent / "transfers" / "2026-10-22_161016Z_5f0d9d89"
    directory.mkdir(parents=True)
    (directory / "transfer.json").write_text(json.dumps({
        "destination": destination,
        "status": "failed",
        "verification": {"ok": ok},
        "planned": {"objects": [{"relative": path, "key": "k", "size": 1000} for path in paths]},
    }))


def test_settings_helper_keeps_other_keys_and_is_read_back(tmp_path):
    config = tmp_path / "config.json"
    original = {
        "staging_root": "/raw/data/staging",
        "known_staging_roots": ["/raw/data/staging"],
        "box": {"client_id": "kept"},
        "soundhub": {"staging_root": "/old", "bucket": "other-bucket"},
    }
    config.write_text(json.dumps(original))
    config.chmod(0o600)

    saved = save_soundhub_staging_root(tmp_path / "drive" / ".." / "drive", config)

    written = json.loads(config.read_text())
    assert saved == tmp_path / "drive"
    assert written["soundhub"] == {"staging_root": str(tmp_path / "drive"), "bucket": "other-bucket"}
    assert {key: written[key] for key in ("staging_root", "known_staging_roots", "box")} == {
        key: original[key] for key in ("staging_root", "known_staging_roots", "box")}
    assert load_soundhub_config(config)["staging_root"] == str(tmp_path / "drive")
    assert load_soundhub_config(config)["bucket"] == "other-bucket"
    assert config.stat().st_mode & 0o777 == 0o600
    assert sorted(path.name for path in tmp_path.iterdir()) == ["config.json"]


def test_settings_helper_creates_block_and_refuses_unreadable_config(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"card_concurrency_limit": 2}))
    save_soundhub_staging_root(tmp_path / "drive", config)
    assert json.loads(config.read_text()) == {
        "card_concurrency_limit": 2, "soundhub": {"staging_root": str(tmp_path / "drive")}}

    config.write_text("{not json")
    with pytest.raises(ValueError):
        save_soundhub_staging_root(tmp_path / "other", config)
    assert config.read_text() == "{not json"


def test_wizard_save_config_keeps_the_soundhub_block(tmp_path):
    from cassn.gui.windows import DeploymentWindow
    from cassn.gui.wizard import FieldDataWizard

    QApplication.instance() or QApplication([])
    config = tmp_path / "config.json"
    block = {"staging_root": "/Volumes/Expansion/s3_upload_staging", "aws_profile": "soundhub"}
    for save in (FieldDataWizard.save_config, DeploymentWindow.save_config):
        config.write_text(json.dumps({"soundhub": block, "box": {"client_id": "kept"}}))
        window = SimpleNamespace(
            config_file=config, staging_root=tmp_path / "raw", known_staging_roots=[],
            card_concurrency_limit=3, _remember_staging_root=lambda root: None,
            manager=SimpleNamespace(lock_directory=tmp_path / "locks"),
        )
        save(window)
        written = json.loads(config.read_text())
        assert written["soundhub"] == block
        assert written["box"] == {"client_id": "kept"}
        assert written["staging_root"] == str(tmp_path / "raw")


def test_status_has_five_states(tmp_path):
    missing = tmp_path / "Volumes" / "Expansion" / "s3_upload_staging"
    assert staging_root_status(missing, SETTINGS).state == UNAVAILABLE
    assert not (tmp_path / "Volumes").exists()

    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / ".DS_Store").write_bytes(b"finder")
    (empty / ".cassn_backups").mkdir()
    assert staging_root_status(empty, SETTINGS).state == EMPTY

    other = tmp_path / "other"
    (other / "transfers").mkdir(parents=True)
    assert staging_root_status(other, SETTINGS).state == NOT_STAGING

    batch = open_batch(tmp_path / "laptop" / "s3_upload_staging")
    status = staging_root_status(batch, SETTINGS)
    assert (status.state, status.recordings, status.files) == (OPEN_BATCH, 2, 2)
    assert status.bytes == 2000 + (project_root(batch) / "recording.csv").stat().st_size
    assert status.describe() == "Open batch: 2 recordings, 0.0 GB"

    transfer_record(batch, PATHS[:1])
    assert staging_root_status(batch, SETTINGS).state == OPEN_BATCH

    closed = open_batch(tmp_path / "drive" / "s3_upload_staging")
    transfer_record(closed, PATHS)
    assert staging_root_status(closed, SETTINGS).state == CLOSED_BATCH
    assert staging_root_status(closed, {**SETTINGS, "bucket": "elsewhere"}).state == OPEN_BATCH


def test_status_open_batch_without_csv_and_unverified_transfer(tmp_path):
    started = tmp_path / "started"
    work_root(started).mkdir(parents=True)
    status = staging_root_status(started, SETTINGS)
    assert status.state == OPEN_BATCH
    assert "no recordings listed yet" in status.describe()

    failed = open_batch(tmp_path / "failed" / "s3_upload_staging")
    transfer_record(failed, PATHS, ok=False)
    assert staging_root_status(failed, SETTINGS).state == OPEN_BATCH


def test_default_folder_is_empty_then_created_on_first_use(tmp_path, monkeypatch):
    default = tmp_path / "home" / "cassn" / "soundhub" / "s3_upload_staging"
    monkeypatch.setattr(staging_module, "SOUNDHUB_STAGING_DEFAULT", default)
    assert staging_root_status(default, SETTINGS).state == EMPTY
    assert not default.exists()
    assert recover_staging(default) is None
    assert default.is_dir()


def test_internal_staging_folders_are_named(tmp_path):
    batch = tmp_path / "s3_upload_staging"
    assert staging_internal_folder(project_root(batch) / "UC_A_plot1_BD_20260714") == SOUNDHUB_PROJECT_SHORT_NAME
    assert staging_internal_folder(work_root(batch)) == ".cassn_work"
    assert staging_internal_folder(batch) is None


# ---------------------------------------------------------------------------
# SoundHub panel
# ---------------------------------------------------------------------------

@pytest.fixture
def panel(tmp_path, monkeypatch):
    from cassn.gui import wizard
    from cassn.gui.wizard import FieldDataWizard
    from cassn.lookups import LookupTables

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(FieldDataWizard, "load_config", lambda self: None)
    monkeypatch.setattr(FieldDataWizard, "check_box_auth", lambda self: False)
    monkeypatch.setattr(FieldDataWizard, "find_all_sessions", lambda self: [])
    window = FieldDataWizard(lookups=LookupTables(), box_config=hosted_config("uc"))
    config = tmp_path / "config.json"
    current = open_batch(tmp_path / "laptop" / "s3_upload_staging")
    config.write_text(json.dumps({"staging_root": "/raw", "soundhub": {"staging_root": str(current)}}))
    window.config_file = config
    warnings = []
    answer = {"value": wizard.QMessageBox.Cancel}

    def warning(*args):
        warnings.append(args)
        return answer["value"]

    monkeypatch.setattr(wizard.QMessageBox, "warning", warning)

    def choose(folder):
        monkeypatch.setattr(wizard.QFileDialog, "getExistingDirectory", lambda *args: str(folder))
        window.change_soundhub_staging_root()

    yield SimpleNamespace(window=window, config=config, current=current, warnings=warnings,
                          answer=answer, choose=choose, wizard=wizard)
    window.current_deployment_folder = None
    window.close()
    app.processEvents()


def saved_root(config):
    return json.loads(config.read_text())["soundhub"]["staging_root"]


def location_text(window):
    return window.soundhub_location_label.text().replace("​", "")


def test_change_row_shows_path_and_cancelled_new_batch_warning_keeps_setting(panel, tmp_path):
    window = panel.window
    window._refresh_soundhub_resume()
    assert f"Staging location: {panel.current}" in location_text(window)
    assert "Open batch: 2 recordings" in location_text(window)
    assert window.soundhub_location_btn.text() == "Change…"
    assert window.soundhub_location_btn.isEnabled()

    empty = tmp_path / "drive" / "s3_upload_staging"
    empty.mkdir(parents=True)
    panel.choose(empty)
    assert len(panel.warnings) == 1
    text = panel.warnings[0][2]
    assert "starts a new SoundHub batch" in text
    assert str(panel.current) in text and "can replace the earlier batch's CSVs" in text
    assert saved_root(panel.config) == str(panel.current)
    assert json.loads(panel.config.read_text())["staging_root"] == "/raw"

    panel.answer["value"] = panel.wizard.QMessageBox.Yes
    panel.choose(empty)
    assert saved_root(panel.config) == str(empty)
    assert f"Staging location: {empty}" in location_text(window)
    assert "Empty. Staging here starts a new batch." in location_text(window)


def test_change_refuses_internal_other_and_submitted_folders(panel, tmp_path):
    other = tmp_path / "other"
    (other / "photos").mkdir(parents=True)
    closed = open_batch(tmp_path / "submitted" / "s3_upload_staging")
    transfer_record(closed, PATHS)
    for folder, words in ((project_root(panel.current), "inside UCNature-SSN"),
                          (other, "not a SoundHub staging folder"),
                          (closed, "cannot be added to a submitted batch")):
        panel.warnings.clear()
        panel.choose(folder)
        assert len(panel.warnings) == 1 and words in panel.warnings[0][2]
        assert saved_root(panel.config) == str(panel.current)


def test_change_to_open_batch_needs_no_warning(panel, tmp_path):
    drive = open_batch(tmp_path / "Expansion" / "s3_upload_staging")
    panel.choose(drive)
    assert panel.warnings == []
    assert saved_root(panel.config) == str(drive)


def test_unavailable_location_disables_stage_and_open_but_not_change(panel, tmp_path, monkeypatch):
    window = panel.window
    missing = tmp_path / "Volumes" / "Expansion" / "s3_upload_staging"
    save_soundhub_staging_root(missing, panel.config)
    window.current_deployment_folder = tmp_path / "event"
    window._refresh_soundhub_resume()
    assert "Not available" in location_text(window)
    assert "location is not available" in window.soundhub_status_label.text()
    assert not window.soundhub_stage_btn.isEnabled()
    assert not window.open_soundhub_btn.isEnabled()
    assert window.soundhub_location_btn.isEnabled()

    run = MagicMock()
    monkeypatch.setattr(panel.wizard.subprocess, "run", run)
    window.open_soundhub_folder()
    run.assert_not_called()
    assert "not available" in panel.warnings[-1][2]
    assert not (tmp_path / "Volumes").exists()


def test_change_is_disabled_while_soundhub_work_runs(panel):
    panel.window._set_soundhub_actions_enabled(False)
    assert not panel.window.soundhub_location_btn.isEnabled()
    panel.window._set_soundhub_actions_enabled(True)
    assert panel.window.soundhub_location_btn.isEnabled()


def test_location_row_is_hidden_for_csu(monkeypatch):
    from cassn.gui.wizard import FieldDataWizard
    from cassn.lookups import LookupTables

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(FieldDataWizard, "load_config", lambda self: None)
    monkeypatch.setattr(FieldDataWizard, "check_box_auth", lambda self: False)
    monkeypatch.setattr(FieldDataWizard, "find_all_sessions", lambda self: [])
    window = FieldDataWizard(lookups=LookupTables(), box_config=hosted_config("csu"))
    try:
        assert window.soundhub_location_row.isHidden()
        assert window._show_soundhub_location() is None
    finally:
        window.close()
        app.processEvents()


def test_other_window_using_the_batch_blocks_the_change(tmp_path, monkeypatch):
    from cassn.gui.dialogs import WindowMessageBox
    from cassn.gui.preview import sample_lookups
    from cassn.gui.windows import DeploymentWindows, ResourceLease
    from cassn.gui.wizard import FieldDataWizard

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(FieldDataWizard, "load_config", lambda self: None)
    monkeypatch.setattr(FieldDataWizard, "find_all_sessions", lambda self: [])
    information = MagicMock()
    monkeypatch.setattr(WindowMessageBox, "information", information)
    manager = DeploymentWindows(lookups=sample_lookups(), box_config=BoxConfig(), lock_directory=tmp_path / "locks")
    manager.box_authenticated = False
    window = manager.new_window(open_sessions=False)
    try:
        config = tmp_path / "config.json"
        current = open_batch(tmp_path / "laptop" / "s3_upload_staging")
        config.write_text(json.dumps({"soundhub": {"staging_root": str(current)}}))
        window.config_file = config
        held = ResourceLease(manager.lock_directory, [window._soundhub_batch_key()])
        try:
            assert not window._save_soundhub_staging_root(tmp_path / "drive")
        finally:
            held.release()
        assert "Another window" in information.call_args[0][2]
        assert saved_root(config) == str(current)
        assert window._save_soundhub_staging_root(tmp_path / "drive")
        assert saved_root(config) == str(tmp_path / "drive")
    finally:
        window.close()
