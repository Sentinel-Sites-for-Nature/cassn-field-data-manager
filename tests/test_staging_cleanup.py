"""Tests for fail-closed, metadata-only staging cleanup."""

from __future__ import annotations

import csv
import hashlib
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
    digest = hashlib.sha256(raw_file.read_bytes()).hexdigest()
    camera = dict(filename="image.jpg", is_uploaded_to_box="True", deployment_id="camera",
                  file_size_bytes=str(raw_file.stat().st_size), file_hash_sha256=digest,
                  file_type="image", device_type="ML")
    with image_csv.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(camera))
        writer.writeheader()
        writer.writerow(camera)
    from cassn.core.wi_requests import originals_digest, save_requests, scope_key
    scope = dict(deployment="camera", images=1, selection_sha256="f"*64)
    row = dict(deployment="camera", stage="verified", confirmed=1, selection_sha256="f"*64,
               manifest_sha256="1"*64, integrity=dict(kind="integrity_verified_image_batch",
               manifest_sha256="1"*64, image_count=1, receipt_sha256="2"*64,
               originals_sha256=originals_digest([("image.jpg", digest)])))
    save_requests(event, {scope_key(event.name, [scope]): {"scopes": [scope], "rows": [row]}})
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
                "file_hash_sha1": sha1(raw_file), "file_hash_sha256": digest,
                "file_type": "image", "deployment_id": "camera", "device_type": "ML",
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


def test_missing_local_media_requires_force(tmp_path):
    staging, event, storage = _build_event(tmp_path)
    (event / "raw_data/p1_ML/image.jpg").unlink()

    plan = inspect_deployment_for_cleanup(event, staging, storage, "field-root")

    assert not plan.clearable
    assert any("missing locally" in reason for reason in plan.reasons)

    plan = inspect_deployment_for_cleanup(
        event, staging, storage, "field-root", force=True
    )
    assert plan.clearable
    assert plan.file_count > 0
    assert plan.local_bytes > 0
    assert event.exists()  # Inspection remains read-only, even with force.
    clear_verified_deployment(plan)
    assert not event.exists()


@pytest.mark.parametrize("box_digest", [None, "", "0" * 40])
def test_force_bypasses_box_copy_checks_for_missing_local_media(
    tmp_path, box_digest
):
    staging, event, storage = _build_event(tmp_path)
    (event / "raw_data/p1_ML/image.jpg").unlink()
    storage.tree["device"] = (
        [] if box_digest is None else [_file("image.jpg", box_digest)]
    )

    plan = inspect_deployment_for_cleanup(
        event, staging, storage, "field-root", force=True
    )

    assert plan.clearable
    assert plan.force
    assert not plan.box_folder_id
    clear_verified_deployment(plan)
    assert not event.exists()


def test_force_bypasses_changed_remaining_media(tmp_path):
    staging, event, storage = _build_event(tmp_path)
    (event / "raw_data/p1_ML/image.jpg").write_bytes(b"changed")

    plan = inspect_deployment_for_cleanup(
        event, staging, storage, "field-root", force=True
    )

    assert plan.clearable
    assert plan.force


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


def test_apply_deletes_only_preflighted_event_and_saves_receipt(tmp_path):
    staging, event, storage = _build_event(tmp_path)
    plan = inspect_deployment_for_cleanup(
        event, staging, storage, "field-root"
    )
    result = clear_verified_deployment(plan)

    assert not event.exists()
    assert staging.exists()
    assert result.file_count == 1
    assert json.loads(result.report_path.read_text())["outcome"] == "cleared"


@pytest.mark.parametrize("force", [False, True])
def test_apply_refuses_event_changed_after_preflight(tmp_path, force):
    staging, event, storage = _build_event(tmp_path)
    plan = inspect_deployment_for_cleanup(
        event, staging, storage, "field-root", force=force
    )
    (event / "new-untracked-file.txt").write_text("changed")

    with pytest.raises(StagingCleanupError, match="changed after cleanup preflight"):
        clear_verified_deployment(plan)

    assert event.is_dir()


@pytest.mark.parametrize('target', ['root', 'outside', 'symlink', 'nested_symlink'])
def test_force_preserves_path_boundaries(tmp_path, target):
    staging, event, _ = _build_event(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'keep').write_text('keep')
    candidate = event
    if target == 'root':
        candidate = staging
    elif target == 'outside':
        candidate = outside
    elif target == 'symlink':
        candidate = staging / 'linked'
        candidate.symlink_to(outside, target_is_directory=True)
    else:
        (event / 'linked').symlink_to(outside, target_is_directory=True)
    plan = inspect_deployment_for_cleanup(candidate, staging, None, '', force=True)
    assert not plan.clearable
    with pytest.raises(StagingCleanupError):
        clear_verified_deployment(plan)
    assert (outside / 'keep').read_text() == 'keep'


def test_force_cli_needs_no_box_or_session_and_requires_apply(tmp_path, monkeypatch, capsys):
    from utils import clear_box_verified_staging as cli
    staging, event, _ = _build_event(tmp_path)
    (event / 'session.json').unlink()
    def unavailable(*args):
        raise AssertionError('force must not contact Box')
    monkeypatch.setattr(cli, 'load_box_config', unavailable)
    args = ['--staging-root', str(staging), '--event', event.name, '--force']
    assert cli.main(args) == 0
    assert event.exists()
    assert 'UNVERIFIED' in capsys.readouterr().out
    assert cli.main(args + ['--apply']) == 0
    assert not event.exists()


@pytest.mark.parametrize('change,accepted', [
    ('submission', True), ('filename', False), ('row_removed', False),
    ('other_field', False), ('bad_timestamp', False), ('regression', False),
])
def test_normal_cleanup_only_accepts_forward_soundhub_updates(tmp_path, monkeypatch, change, accepted):
    import csv
    import io
    from cassn.soundhub.box_metadata import BoxMetadataStore
    staging, event, storage = _build_event(tmp_path)
    fields = ['filename', 'deployment_id', 'is_uploaded_to_box', 'notes',
              'is_submitted_to_soundhub', 'soundhub_submitter', 'soundhub_submission_datetime']
    before = dict(zip(fields, ['audio.wav', 'deployment', 'True', 'original', 'False', '', '']))
    raw = event / 'raw_data/p1_BT/audio.wav'
    raw.parent.mkdir()
    raw.write_bytes(b'recording')
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()
    before.update(file_type='audio', device_type='BT', file_size_bytes=str(raw.stat().st_size), file_hash_sha256=digest)
    fields = list(before)
    session = json.loads((event/'session.json').read_text())
    session['devices'].append([1, "One", "BT", "p1_BT"])
    session['device_statuses']['p1_BT'] = {"status": "Complete"}
    session['file_inventory'].append(dict(device_label="p1_BT", new_filename="audio.wav", storage_relpath="raw_data/p1_BT/audio.wav",
                                         deployment_id="deployment", file_type="audio", file_hash_sha1=sha1(raw),
                                         file_hash_sha256=digest, file_size_bytes=raw.stat().st_size))
    (event/'session.json').write_text(json.dumps(session))
    storage.tree['raw'].append(_folder('p1_BT','bat'))
    storage.tree['bat'] = [_file('audio.wav', sha1(raw))]
    after = dict(before, is_submitted_to_soundhub='True', soundhub_submitter='Imperato, John',
                 soundhub_submission_datetime='2026-10-22T20:28:23+00:00')
    if change == 'filename':
        after['filename'] = 'different.wav'
    elif change == 'other_field':
        after['notes'] = 'different'
    elif change == 'bad_timestamp':
        after['soundhub_submission_datetime'] = 'bad'
    elif change == 'regression':
        before, after = after, before
    def encoded(rows):
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        return stream.getvalue().encode()
    (event / 'audio_file_metadata.csv').write_bytes(encoded([before]))
    payload = encoded([] if change == 'row_removed' else [after])
    storage.tree['event'].append(_file('audio_file_metadata.csv', __import__('hashlib').sha1(payload).hexdigest()))
    monkeypatch.setattr(BoxMetadataStore, 'snapshot', lambda self, fid: fid)
    monkeypatch.setattr(BoxMetadataStore, 'read', lambda self, ref: payload)
    plan = inspect_deployment_for_cleanup(event, staging, storage, 'field-root')
    assert plan.clearable is accepted
    if not accepted:
        assert any('metadata differ' in reason for reason in plan.reasons)


@pytest.mark.parametrize("force", [False, True])
def test_ingest_hold_blocks_cleanup_even_with_complete_session(tmp_path, force):
    from cassn.core.quality_control import append_qc_report
    staging, event, storage = _build_event(tmp_path)
    append_qc_report(event, "ingest_safety", "p1_ML", "error", "Unrecorded source needs review")
    plan = inspect_deployment_for_cleanup(event, staging, storage, "field-root", force=force)
    assert not plan.clearable
    assert any("Unresolved ingest failure" in reason for reason in plan.reasons)
    assert (event / "raw_data/p1_ML/image.jpg").read_bytes() == b"captured image"


def test_new_evidence_keeps_existing_cleanup_checks_and_rejects_stale_batch(tmp_path):
    from cassn.core.verification_evidence import verification_context, write_result
    staging, event, storage = _build_event(tmp_path)
    session = json.loads((event / 'session.json').read_text())
    config = SimpleNamespace(field_data_folder_id='field-root', profile='legacy')
    context = verification_context(event, session['file_inventory'], session['metadata'], config)
    for kind in ('box', 'hash'):
        write_result(event, kind, context, ok=True, summary='Synthetic check passed')
    plan = inspect_deployment_for_cleanup(event, staging, storage, 'field-root')
    assert plan.clearable, plan.reasons
    # A matching proof never bypasses the independent live Box check.
    storage.tree['device'] = []
    plan = inspect_deployment_for_cleanup(event, staging, storage, 'field-root')
    assert not plan.clearable
    assert any('missing from Box' in reason for reason in plan.reasons)
    # A changed batch fails before any Box access, even if bytes weren't read.
    session['file_inventory'][0]['file_hash_sha256'] = 'c' * 64
    (event / 'session.json').write_text(json.dumps(session))
    plan = inspect_deployment_for_cleanup(event, staging, storage, 'field-root')
    assert not plan.clearable
    assert any('stale or incomplete' in reason for reason in plan.reasons)


@pytest.mark.parametrize('change', ['unknown_version', 'bad_destination'])
def test_malformed_new_proof_does_not_fall_back_to_legacy_cleanup(tmp_path, change):
    staging, event, storage = _build_event(tmp_path)
    proof = {'verified': True, 'evidence_version': 2 if change == 'unknown_version' else 1,
             'destination': []}
    (event / 'qc/box_upload_verification.json').write_text(json.dumps(proof))
    assert not inspect_deployment_for_cleanup(event, staging, storage, 'field-root').clearable


def test_unknown_local_files_and_hidden_notes_prevent_clearance(tmp_path):
    staging, event, storage = _build_event(tmp_path)
    note = event / ".notes.txt"
    note.write_text("Field observation")
    plan = inspect_deployment_for_cleanup(event, staging, storage, "field-root")
    assert not plan.clearable and any("Move .notes.txt outside" in r for r in plan.reasons)
    note.rename(event / "notes.txt")
    storage.tree["event"].append(_file("notes.txt", "0" * 40))
    assert not inspect_deployment_for_cleanup(event, staging, storage, "field-root").clearable
    storage.tree["event"][-1] = _file("notes.txt", sha1(event / "notes.txt"))
    assert inspect_deployment_for_cleanup(event, staging, storage, "field-root").clearable


@pytest.mark.parametrize("change", ["no requests", "legacy receipt", "different bytes"])
def test_camera_clearance_requires_matching_local_proof(tmp_path, change):
    from cassn.core.wi_requests import STATE_FILE
    staging, event, storage = _build_event(tmp_path)
    path = event / STATE_FILE
    if change == "no requests":
        path.unlink()
    else:
        requests = json.loads(path.read_text())
        proof = next(iter(requests["requests"].values()))["rows"][0]["integrity"]
        if change == "legacy receipt":
            del proof["originals_sha256"]
        else:
            proof["originals_sha256"] = "8" * 64
        path.write_text(json.dumps(requests))
    plan = inspect_deployment_for_cleanup(event, staging, storage, "field-root")
    assert not plan.clearable and plan.checks["Wildlife Insights"]["status"] == "blocked"


def test_preview_does_not_authorize_changed_box_or_cli_race(tmp_path):
    from cassn.config import LOCK_DIR
    from cassn.core.event_lock import ResourceLease, deployment_keys
    staging, event, storage = _build_event(tmp_path)
    plan = inspect_deployment_for_cleanup(event, staging, storage, "field-root")
    with ResourceLease(LOCK_DIR, deployment_keys(event, event.name, "field-root")):
        with pytest.raises(StagingCleanupError):
            clear_verified_deployment(plan)
    storage.tree["device"] = []
    with pytest.raises(StagingCleanupError, match="missing from Box"):
        clear_verified_deployment(plan)
    assert (event / "session.json").is_file()


def test_partial_removal_keeps_session_and_report(tmp_path, monkeypatch):
    from cassn.core import staging_cleanup
    staging, event, storage = _build_event(tmp_path)
    config = tmp_path / "profile/config.json"
    plan = inspect_deployment_for_cleanup(event, staging, storage, "field-root", config_path=config)
    unlink = staging_cleanup.os.unlink
    def fail_on_image(name, *args, **kwargs):
        if name == "image.jpg":
            raise OSError("volume unavailable")
        return unlink(name, *args, **kwargs)
    monkeypatch.setattr(staging_cleanup.os, "unlink", fail_on_image)
    with pytest.raises(StagingCleanupError, match="volume unavailable"):
        clear_verified_deployment(plan)
    assert event in discover_staged_deployments(staging)
    report, = (config.parent / "cleanup-reports").glob("*.json")
    assert json.loads(report.read_text())["outcome"] == "removal incomplete"


def test_legacy_receipt_allows_only_the_new_digest(tmp_path):
    from cassn.core.wi_requests import read_requests, verified_row_update
    _, event, _ = _build_event(tmp_path)
    current = next(iter(read_requests(event).values()))["rows"][0]
    old = dict(current, integrity={k:v for k,v in current["integrity"].items() if k != "originals_sha256"})
    assert verified_row_update(old, current)
    assert verified_row_update(current, current)
    assert not verified_row_update(current, old)
    assert not verified_row_update(old, dict(current, confirmed=0))
    assert not verified_row_update(old, dict(current, integrity=dict(current["integrity"], receipt_sha256="e"*64)))
    assert not verified_row_update(current, dict(current, integrity=dict(current["integrity"], originals_sha256="e"*64)))


def test_wi_hash_matches_helper_wire_format():
    from cassn.core.wi_requests import originals_digest
    pairs = [("é.jpg", "2"*64), ("A.jpg", "1"*64)]
    expected = "771e6d45c01e81e3730961ba1b84e44e5a182acf8c4397f910c1797dc7f17f72"
    assert originals_digest(pairs) == originals_digest(reversed(pairs)) == expected
    with pytest.raises(ValueError, match="Duplicate"):
        originals_digest([pairs[0], pairs[0]])


def test_protected_soundhub_folder_survives_a_config_change(tmp_path, monkeypatch):
    from cassn.soundhub import upload
    staging, event, storage = _build_event(tmp_path)
    config = tmp_path / "profile/config.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"soundhub": {"staging_root": str(tmp_path / "alternate")}}))
    monkeypatch.setattr(upload, "SOUNDHUB_STAGING_DEFAULT", event)
    plan = inspect_deployment_for_cleanup(event, staging, storage, "", config_path=config, force=True)
    assert not plan.clearable and any("overlaps" in r for r in plan.reasons)


def test_generated_summary_and_user_edits(tmp_path):
    from cassn.core.inventory import generate_session_summary
    staging, event, storage = _build_event(tmp_path)
    session = json.loads((event / "session.json").read_bytes())
    summary = generate_session_summary(event, session["metadata"], session["file_inventory"], session["devices"])
    storage.tree["qc"].append(_file(summary.name, "0" * 40))
    assert inspect_deployment_for_cleanup(event, staging, storage, "field-root").clearable
    with summary.open("a") as stream:
        stream.write("Field note added after upload\n")
    assert not inspect_deployment_for_cleanup(event, staging, storage, "field-root").clearable


def test_malformed_private_upload_record_blocks_with_a_reason(tmp_path):
    staging, event, storage = _build_event(tmp_path)
    path = event / "qc/box_upload_sessions/metrics.json"
    path.parent.mkdir()
    path.write_text("incomplete")
    plan = inspect_deployment_for_cleanup(event, staging, storage, "field-root")
    assert not plan.clearable and any("qc/box_upload_sessions/metrics.json" in reason for reason in plan.reasons)


def test_legacy_manifest_requires_exact_inventory(tmp_path):
    from cassn.core.inventory import refresh_legacy_device_manifest
    staging, event, storage = _build_event(tmp_path)
    device = event / "raw_data/p1_ML"
    manifest = device / "p1_ML_manifest.json"
    manifest.touch()
    rows = json.loads((event / "session.json").read_bytes())["file_inventory"]
    assert refresh_legacy_device_manifest(device.name, device, rows)
    assert inspect_deployment_for_cleanup(event, staging, storage, "field-root").clearable
    record = json.loads(manifest.read_bytes())
    manifest.write_text(json.dumps(dict(record, notes="Field note")))
    assert not inspect_deployment_for_cleanup(event, staging, storage, "field-root").clearable


@pytest.mark.parametrize("change", ["timestamp", "copy", "extra", "bad time", "duplicate", "field"])
def test_lookup_snapshot_comparison(tmp_path, monkeypatch, change):
    from cassn.core.staging_cleanup import _LOOKUP_MANIFEST, _check_lookup_manifest
    path = tmp_path / _LOOKUP_MANIFEST
    path.parent.mkdir(parents=True)
    copy = path.with_name("sites.csv")
    copy.write_bytes(b"abc")
    row = dict(filename=copy.name, relative_path=copy.relative_to(tmp_path).as_posix(),
               size_bytes=3, sha256=hashlib.sha256(b"abc").hexdigest(), snapshotted_at="2026-11-13T00:00:00+00:00")
    record = dict(files=[row], file_count=1, source_directory="/lookups", generated=row["snapshotted_at"])
    remote = json.dumps(record).encode()
    row["snapshotted_at"] = record["generated"] = "2026-11-13T02:00:00+00:00"
    if change == "copy":
        copy.write_bytes(b"abd")
    if change == "extra":
        path.with_name("notes").touch()
    if change == "bad time":
        record["generated"] = "yesterday"
    if change == "duplicate":
        record["files"].append(dict(row))
        record["file_count"] = 2
    if change == "field":
        record["notes"] = "field note"
    path.write_text(json.dumps(record))
    sha = hashlib.sha1(remote).hexdigest()
    ref = SimpleNamespace(sha1=sha, parent_id="folder", name=path.name)
    store = SimpleNamespace(snapshot=lambda _: ref, read=lambda _: remote, assert_unchanged=lambda _: None)
    monkeypatch.setattr("cassn.core.staging_cleanup.BoxFileStore", lambda _: store)
    storage = SimpleNamespace(client=None, find_child_folder=lambda *_: "folder",
                              folder_file_map=lambda _: {path.name: "file"})
    args = (tmp_path, _LOOKUP_MANIFEST, sha, storage, "root")
    if change == "timestamp":
        _check_lookup_manifest(*args)
    else:
        with pytest.raises(ValueError):
            _check_lookup_manifest(*args)


def test_box_lookup_extra_blocks_even_when_manifest_hash_matches(tmp_path):
    staging, event, storage = _build_event(tmp_path)
    folder = event / "qc/lookup_snapshot"
    folder.mkdir()
    copy = folder / "sites.csv"
    copy.write_text("site\n")
    stamp = "2026-11-13T00:00:00+00:00"
    manifest = folder / "lookup_snapshot_manifest.json"
    manifest.write_text(json.dumps(dict(generated=stamp, source_directory="/lookups", file_count=1,
        files=[dict(filename=copy.name, relative_path=copy.relative_to(event).as_posix(),
                    size_bytes=copy.stat().st_size, sha256=hashlib.sha256(copy.read_bytes()).hexdigest(),
                    snapshotted_at=stamp)])))
    storage.tree["qc"].append(_folder(folder.name, "lookup"))
    storage.tree["lookup"] = [_file(p.name, sha1(p)) for p in folder.iterdir()]
    assert inspect_deployment_for_cleanup(event, staging, storage, "field-root").clearable
    storage.tree["lookup"].append(_file("notes.csv", "a"*40))
    plan = inspect_deployment_for_cleanup(event, staging, storage, "field-root")
    assert not plan.clearable
    assert any("folder contents differ" in r for r in plan.reasons)
