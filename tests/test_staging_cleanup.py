"""Tests for fail-closed, metadata-only staging cleanup."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cassn.box.client import BoxStorage
from cassn.core.hashing import sha1
from cassn.core.staging_cleanup import (
    StagingCleanupError,
    clear_verified_deployment,
    configured_staging_root,
    discover_staged_deployments,
    inspect_deployment_for_cleanup,
)


def _folder(name: str, item_id: str):
    return SimpleNamespace(type="folder", name=name, id=item_id)


def _file(name: str, digest: str):
    return SimpleNamespace(type="file", name=name, id=name, sha1=digest)


class FakeStorage(BoxStorage):
    def __init__(self, tree):
        super().__init__(client=None)
        self.tree = tree

    def iter_folder_items(self, folder_id, *, fields=None):
        del fields
        yield from self.tree.get(folder_id, [])


def _build_event(tmp_path: Path):
    staging = tmp_path / "staging"
    event = staging / "UC_TestSite_20260424"
    raw_file = event / "raw_data" / "p1_ML" / "image.jpg"
    raw_file.parent.mkdir(parents=True)
    raw_file.write_bytes(b"captured image")

    image_csv = event / "image_file_metadata.csv"
    image_csv.write_text(
        "filename,is_uploaded_to_box\nimage.jpg,True\n", encoding="utf-8"
    )
    event_record = event / "deployment_event_record.json"
    event_record.write_text('{"event":"UC_TestSite_20260424"}', encoding="utf-8")
    qc = event / "qc"
    qc.mkdir()
    upload_manifest = qc / "box_upload_manifest.json"
    upload_manifest.write_text('{"file_count":1}', encoding="utf-8")
    qc_report = qc / "qc_report.json"
    qc_report.write_text('{"current_state":[]}', encoding="utf-8")
    (qc / "box_upload_verification.json").write_text(
        json.dumps({"verified": True, "verified_at": "2026-09-01T12:00:00"}),
        encoding="utf-8",
    )

    session = {
        "schema_version": 1,
        "saved_at": "2026-09-01T12:00:00",
        "metadata": {
            "site_name": "Test Reserve",
            "deployment_event_id": event.name,
            "deployment_event_end_date": "2026-04-24",
        },
        "devices": [[1, "One", "ML", "p1_ML"]],
        "device_statuses": {
            "p1_ML": {"status": "Complete", "files_copied": "1"}
        },
        "file_inventory": [
            {
                "device_label": "p1_ML",
                "new_filename": "image.jpg",
                "storage_relpath": "raw_data/p1_ML/image.jpg",
                "file_hash_sha1": sha1(raw_file),
                "file_size_bytes": raw_file.stat().st_size,
            }
        ],
        "deployment_folder": str(event),
    }
    (event / "session.json").write_text(json.dumps(session), encoding="utf-8")

    tree = {
        "field-root": [_folder("2026", "year")],
        "year": [_folder("Test Reserve", "reserve")],
        "reserve": [_folder(event.name, "event")],
        "event": [
            _folder("raw_data", "raw"),
            _folder("qc", "qc"),
            _file(image_csv.name, sha1(image_csv)),
            _file(event_record.name, sha1(event_record)),
        ],
        "raw": [_folder("p1_ML", "device")],
        "device": [_file(raw_file.name, sha1(raw_file))],
        "qc": [
            _file(upload_manifest.name, sha1(upload_manifest)),
            _file(qc_report.name, sha1(qc_report)),
        ],
    }
    return staging, event, FakeStorage(tree)


def test_configured_staging_root_uses_app_setting_and_default(tmp_path, monkeypatch):
    configured = tmp_path / "selected"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"staging_root": str(configured)}))
    assert configured_staging_root(config) == configured

    missing = tmp_path / "missing.json"
    monkeypatch.setattr(
        "cassn.core.staging_cleanup.DEFAULT_STAGING_ROOT", tmp_path / "default"
    )
    assert configured_staging_root(missing) == tmp_path / "default"


def test_discovery_selects_only_direct_session_folders(tmp_path):
    staging, event, _storage = _build_event(tmp_path)
    (staging / "unrelated").mkdir()
    assert discover_staged_deployments(staging) == [event]


def test_live_box_hash_match_makes_completed_event_clearable(tmp_path):
    staging, event, storage = _build_event(tmp_path)
    plan = inspect_deployment_for_cleanup(
        event, staging, storage, "field-root"
    )

    assert plan.clearable
    assert plan.reasons == []
    assert plan.box_folder_id == "event"
    assert plan.box_path == f"2026/Test Reserve/{event.name}"
    assert plan.file_count == 1


def test_missing_box_media_blocks_cleanup_without_reading_media(tmp_path):
    staging, event, storage = _build_event(tmp_path)
    storage.tree["device"] = []

    plan = inspect_deployment_for_cleanup(
        event, staging, storage, "field-root"
    )

    assert not plan.clearable
    assert any("missing from Box" in reason for reason in plan.reasons)
    assert any("raw_data/p1_ML/image.jpg" in reason for reason in plan.reasons)


def test_historical_local_only_box_manifest_does_not_block_cleanup(tmp_path):
    staging, event, storage = _build_event(tmp_path)
    storage.tree["qc"] = [
        item for item in storage.tree["qc"] if item.name != "box_upload_manifest.json"
    ]

    plan = inspect_deployment_for_cleanup(
        event, staging, storage, "field-root"
    )

    assert plan.clearable


def test_missing_uploaded_qc_report_still_blocks_cleanup(tmp_path):
    staging, event, storage = _build_event(tmp_path)
    storage.tree["qc"] = [
        item for item in storage.tree["qc"] if item.name != "qc_report.json"
    ]

    plan = inspect_deployment_for_cleanup(
        event, staging, storage, "field-root"
    )

    assert not plan.clearable
    assert any("qc/qc_report.json" in reason for reason in plan.reasons)


def test_unstamped_metadata_blocks_before_box_calls(tmp_path):
    staging, event, storage = _build_event(tmp_path)
    (event / "image_file_metadata.csv").write_text(
        "filename,is_uploaded_to_box\nimage.jpg,False\n", encoding="utf-8"
    )
    storage.tree.clear()

    plan = inspect_deployment_for_cleanup(
        event, staging, storage, "field-root"
    )

    assert not plan.clearable
    assert any("not stamped" in reason for reason in plan.reasons)


def test_apply_deletes_only_preflighted_event_without_creating_receipt(tmp_path):
    staging, event, storage = _build_event(tmp_path)
    plan = inspect_deployment_for_cleanup(
        event, staging, storage, "field-root"
    )
    result = clear_verified_deployment(plan)

    assert not event.exists()
    assert staging.exists()
    assert result.file_count == 1
    assert not (tmp_path / "receipts").exists()


def test_apply_refuses_event_changed_after_preflight(tmp_path):
    staging, event, storage = _build_event(tmp_path)
    plan = inspect_deployment_for_cleanup(
        event, staging, storage, "field-root"
    )
    (event / "new-untracked-file.txt").write_text("changed")

    with pytest.raises(StagingCleanupError, match="changed after cleanup preflight"):
        clear_verified_deployment(plan)

    assert event.is_dir()
