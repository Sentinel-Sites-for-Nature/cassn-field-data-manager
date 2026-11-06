"""ENG-019: accumulated metadata survives batches, restamping and races."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from cassn.box.client import BoxStorage
from cassn.box.metadata_merge import (
    MetadataConflict, csv_bytes, merge_csv, merge_event, merge_qc, read_csv, stamp_box_provenance,
)
from cassn.box.publication import EventPublication, require_publication_complete
from cassn.box.verification import recorded_archive_paths, verify_box_hashes


def row(name, *, deployment="d1", content=b"recording", **values):
    return {"deployment_id": deployment, "filename": name,
            "file_hash_sha256": hashlib.sha256(content).hexdigest(),
            "file_hash_sha1": hashlib.sha1(content).hexdigest(),
            "file_size_bytes": str(len(content)), "original_filename": name,
            "is_uploaded_to_box": "False", "box_uploader": "", "box_upload_datetime": "",
            "is_submitted_to_soundhub": "False", "soundhub_submitter": "",
            "soundhub_submission_datetime": "", "notes": "", "recorded_datetime": "2026-04-01",
            **values}


def payload(rows):
    return csv_bytes(list(dict.fromkeys(key for item in rows for key in item)), rows)


class FakeBox:
    """Small byte/version store; no filesystem, credentials or network."""
    def __init__(self):
        self.folders = self.files = self.downloads = self.uploads = self
        self.objects = {"event": {"type": "folder", "name": "event", "parent": "root"}}
        self.calls = []
        self.before_write = None
        self.after_write = None
        self.bad_etag = False
        self.bad_download = False

    def add(self, name, data, parent="event"):
        file_id = str(len(self.objects) + 1)
        self.objects[file_id] = {"type": "file", "name": name, "parent": parent,
                                 "payload": data, "version": 1, "history": {"1": data}}
        return file_id

    def change(self, file_id, data):
        obj = self.objects[file_id]
        obj["version"] += 1
        obj["payload"] = data
        obj["history"][str(obj["version"])] = data

    def get_folder_items(self, folder_id, *, fields=None, limit=1000, offset=0):
        items = [NS(id=fid, name=obj["name"], type=obj["type"],
                    sha1=hashlib.sha1(obj.get("payload", b"")).hexdigest())
                 for fid, obj in self.objects.items() if obj["parent"] == folder_id]
        return NS(entries=items[offset:offset + limit])

    def create_folder(self, name, parent):
        folder_id = str(len(self.objects) + 1)
        self.objects[folder_id] = {"name": name, "parent": parent.id, "type": "folder"}
        return NS(id=folder_id)

    def get_file_by_id(self, file_id, *, fields=None):
        obj = self.objects[file_id]
        return NS(id=file_id, name=obj["name"], parent=NS(id=obj["parent"]),
                  etag=None if self.bad_etag else str(obj["version"]),
                  file_version=NS(id=str(obj["version"])),
                  sha_1=hashlib.sha1(obj["payload"]).hexdigest(), size=len(obj["payload"]),
                  permissions=NS(can_upload=True))

    def download_file(self, file_id, *, version):
        data = self.objects[file_id]["history"][version]
        return io.BytesIO(data + b"corrupt" if self.bad_download else data)

    def upload_file_version(self, file_id, attributes, file, *, if_match):
        if self.before_write:
            self.before_write(file_id)
        obj = self.objects[file_id]
        self.calls.append(("version", file_id, if_match))
        if if_match != str(obj["version"]):
            raise RuntimeError("412 stale version")
        self.change(file_id, file.read())
        if self.after_write:
            self.after_write(file_id)

    def upload_file(self, *, attributes, file):
        name, parent = attributes["name"], attributes["parent"]["id"]
        if self.before_write:
            self.before_write(name)
        if any(obj["parent"] == parent and obj["name"] == name for obj in self.objects.values()):
            raise RuntimeError("409 already exists")
        fid = self.add(name, file.read(), parent)
        self.calls.append(("create", fid))
        if self.after_write:
            self.after_write(fid)
        return NS(entries=[NS(id=fid)])

    def by_name(self, name, parent="event"):
        return next(fid for fid, obj in self.objects.items() if obj["parent"] == parent and obj["name"] == name)

    def data(self, name):
        return self.objects[self.by_name(name)]["payload"]


def prepare(api, tmp_path, rows, **kwargs):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "audio_file_metadata.csv").write_bytes(payload(rows))
    publication = EventPublication(BoxStorage(api), "event", tmp_path, **kwargs)
    publication.prepare()
    return publication


def test_blue_oak_56_plus_164_survive_upload_and_provenance(tmp_path):
    api = FakeBox()
    earlier = [row(f"old_{n}.wav", deployment="old", notes="retained", custom="external",
                   is_submitted_to_soundhub="True", soundhub_submitter="Original submitter",
                   soundhub_submission_datetime="2026-01-01") for n in range(56)]
    api.add("audio_file_metadata.csv", payload(earlier))
    newer = [row(f"new_{n}.wav", deployment="new") for n in range(164)]
    p = prepare(api, tmp_path, newer)
    p.publish()
    assert len(read_csv(api.data("audio_file_metadata.csv"))[1]) == 220
    with pytest.raises(MetadataConflict, match="incomplete"):
        require_publication_complete(tmp_path)
    stamped = [{**r, "is_uploaded_to_box": "True", "box_uploader": "John",
                "box_upload_datetime": "2026-10-26"} for r in newer]
    p = prepare(api, tmp_path, stamped, provenance=True)
    p.publish()
    result = read_csv(api.data("audio_file_metadata.csv"))[1]
    assert len(result) == 220
    assert result[:56] == [{**r, "custom": "external"} for r in earlier]
    assert all(r["is_uploaded_to_box"] == "True" for r in result[56:])
    require_publication_complete(tmp_path)
    # Retrying a subset, including a stale local copy, cannot reset history.
    p = prepare(api, tmp_path, newer[:3])
    p.publish()
    result2 = read_csv(api.data("audio_file_metadata.csv"))[1]
    assert result2 == result
    assert all(call[2] for call in api.calls if call[0] == "version")


def test_remote_corrections_history_and_unknown_columns_own_existing_rows():
    old = row("a.wav", notes="corrected", recorded_datetime="2026-04-02", external="value",
              is_submitted_to_soundhub="True", soundhub_submitter="Expert")
    new = row("a.wav", recorded_datetime="2026-04-01", added_column="new schema")
    merged = read_csv(merge_csv(payload([old]), payload([new])))[1][0]
    assert merged == {**old, "added_column": "new schema"}


@pytest.mark.parametrize("changed", [
    {"file_hash_sha256": "a" * 64}, {"file_hash_sha1": "b" * 40},
    {"file_size_bytes": "123"}, {"original_filename": "other.wav"},
    {"storage_relpath": "raw_data/different/a.wav"},
])
def test_conflicting_identity_stops(changed):
    old = row("a.wav", storage_relpath="raw_data/p1_BD/a.wav")
    with pytest.raises(MetadataConflict, match="Conflicting"):
        merge_csv(payload([old]), payload([{**old, **changed}]))


@pytest.mark.parametrize("bad", [b"filename\na.wav\n", b"\xff", b"deployment_id,filename,file_hash_sha256\nd,a.wav,short\n",
                                  b"deployment_id,filename,filename,file_hash_sha256\n", payload([row("a"), row("a")])])
def test_ambiguous_legacy_or_malformed_metadata_stops_before_write(tmp_path, bad):
    api = FakeBox()
    api.add("audio_file_metadata.csv", bad)
    with pytest.raises(MetadataConflict):
        prepare(api, tmp_path, [row("new.wav")])
    assert not api.calls


@pytest.mark.parametrize("fault", ["bad_etag", "bad_download"])
def test_missing_preconditions_and_corrupt_reads_stop(tmp_path, fault):
    api = FakeBox()
    api.add("audio_file_metadata.csv", payload([row("old.wav")]))
    setattr(api, fault, True)
    with pytest.raises(ValueError):
        prepare(api, tmp_path, [row("new.wav")])
    assert not api.calls


def test_concurrent_update_between_read_and_write_is_not_overwritten(tmp_path):
    api = FakeBox()
    fid = api.add("audio_file_metadata.csv", payload([row("old.wav")]))
    p = prepare(api, tmp_path, [row("new.wav")])
    concurrent = payload([row("old.wav", notes="concurrent correction")])
    api.before_write = lambda _: api.change(fid, concurrent)
    with pytest.raises(MetadataConflict, match="conflict") as failure:
        p.publish()
    p.fail(failure.value)
    assert api.data("audio_file_metadata.csv") == concurrent
    assert json.loads((p.attempt_dir / "attempt.json").read_text())["status"] == "needs_recovery"


@pytest.mark.parametrize("existing", [False, True])
def test_lost_success_response_is_reconciled_by_exact_saved_content(tmp_path, existing):
    api = FakeBox()
    if existing:
        api.add("audio_file_metadata.csv", payload([row("old.wav")]))
    p = prepare(api, tmp_path, [row("new.wav")])
    def lose_response(_):
        raise RuntimeError("connection lost after server saved")
    api.after_write = lose_response
    p.publish()
    assert p.state["files"]["audio_file_metadata.csv"]["status"] == "verified"
    assert len(read_csv(api.data("audio_file_metadata.csv"))[1]) == (2 if existing else 1)


def test_create_race_different_payload_is_not_success(tmp_path):
    api = FakeBox()
    p = prepare(api, tmp_path, [row("new.wav")])
    raced = payload([row("someone_else.wav")])
    api.before_write = lambda _: api.add("audio_file_metadata.csv", raced)
    with pytest.raises(MetadataConflict, match="conflict"):
        p.publish()
    assert api.data("audio_file_metadata.csv") == raced


def test_stale_plan_detected_before_any_writes(tmp_path):
    api = FakeBox()
    fid = api.add("audio_file_metadata.csv", payload([row("old.wav")]))
    p = prepare(api, tmp_path, [row("new.wav")])
    api.change(fid, payload([row("old.wav", notes="edited")]))
    with pytest.raises(ValueError, match="changed after preflight"):
        p.publish()
    assert not api.calls


def test_provenance_cannot_introduce_rows_or_reset_history():
    old = row("a.wav", is_uploaded_to_box="True", box_uploader="Original",
              box_upload_datetime="2026-01-01")
    new = row("a.wav", is_uploaded_to_box="True", box_uploader="Retry",
              box_upload_datetime="2026-10-26")
    assert read_csv(merge_csv(payload([old]), payload([new]), provenance=True))[1] == [old]
    with pytest.raises(MetadataConflict, match="unpublished"):
        merge_csv(payload([old]), payload([row("other.wav")]), provenance=True)


def event(label, *, count=1):
    return {"deployment_info": {"deployment_event_id": "event", "site_name": "Test", "deployment_event_end_date": "2026-04-01"},
            "devices": [{"device_label": label, "deployment_id": label}], "file_count": count,
            "generated": "original", "version": "4"}


def test_event_union_and_conflicting_legacy_mapping():
    old, new = event("old"), event("new")
    old["custom"] = "preserved"
    merged = json.loads(merge_event(json.dumps(old).encode(), json.dumps(new).encode(), file_count=2))
    assert merged["devices"] == old["devices"] + new["devices"]
    assert merged["file_count"] == 2 and merged["custom"] == "preserved"
    new["devices"] = [{"device_label": "old", "deployment_id": "another"}]
    with pytest.raises(MetadataConflict, match="Conflicting"):
        merge_event(json.dumps(old).encode(), json.dumps(new).encode(), file_count=2)


@pytest.mark.parametrize("case", ["later plot", "retry", "pending locally", "no lookup",
                                  "wrong assignment", "duplicate placement", "unaccounted files"])
def test_partial_event_can_bind_only_a_proven_pending_device(tmp_path, case):
    from cassn.lookups import DEVICE_DEPLOYMENT_REQUIRED_FIELDS

    api = FakeBox()
    previous = event("p3_BD")
    previous["deployment_info"]["site_short_name"] = "Test"
    pending = {"device_label": "p1_BD", "plot_label": "1", "plot_number": 1, "device_type": "BD"}
    previous["devices"].append(pending)
    assigned = "existing-assigned-id"
    current = json.loads(json.dumps(previous))
    current["devices"][1]["deployment_id"] = assigned
    current["file_count"] = 2
    old_rows = [row("old.wav", deployment="p3_BD")]
    new_rows = old_rows + [row("new.wav", deployment=assigned)]
    if case == "pending locally":
        previous, current = current, previous
        old_rows, new_rows = new_rows, old_rows
    if case == "unaccounted files":
        old_rows = [row("older.wav", deployment="unresolved")]
    api.add("deployment_event_record.json", json.dumps(previous).encode())
    api.add("audio_file_metadata.csv", payload(old_rows))
    (tmp_path / "deployment_event_record.json").write_text(json.dumps(current))
    snapshot = tmp_path / "qc/lookup_snapshot/deployments.csv"
    snapshot.parent.mkdir(parents=True)
    placement = dict.fromkeys(DEVICE_DEPLOYMENT_REQUIRED_FIELDS, "")
    placement.update(deployment_id=assigned, deployment_event_id="event", device_type="BD",
                     site_short_name="Test", plot_number="1", deployment_sequence="0",
                     deployment_start_date="2026-01-01", deployment_end_date="2026-04-01")
    if case == "wrong assignment":
        placement["deployment_id"] = "different"
    if case != "no lookup":
        placements = [placement, placement] if case == "duplicate placement" else [placement]
        snapshot.write_bytes(csv_bytes(list(placement), placements))
    if case not in {"later plot", "retry", "pending locally"}:
        with pytest.raises(MetadataConflict, match="Cannot confirm the deployment ID for p1_BD"):
            prepare(api, tmp_path, new_rows)
        assert api.calls == []
        return
    publication = prepare(api, tmp_path, new_rows)
    publication.publish()
    if case == "retry":
        prepare(api, tmp_path, new_rows).publish()
    merged = json.loads(api.data("deployment_event_record.json"))
    assert merged["devices"][1] == dict(pending, deployment_id=assigned)
    assert merged["file_count"] == 2


def test_legacy_event_site_aliases_preserve_earlier_fields_and_add_current_names():
    old, new = event("old"), event("new")
    old["deployment_info"]["reserve_name"] = old["deployment_info"].pop("site_name")
    old["deployment_info"]["site"] = "TestShort"
    new["deployment_info"]["site_short_name"] = "TestShort"
    new["deployment_info"]["site_code"] = "TST"
    merged = json.loads(merge_event(json.dumps(old).encode(), json.dumps(new).encode(), file_count=2))
    info = merged["deployment_info"]
    assert info["site_name"] == info["reserve_name"] == "Test"
    assert info["site"] == info["site_short_name"] == "TestShort"
    assert info["site_code"] == "TST"
    assert merged["devices"] == old["devices"] + new["devices"]
    old["deployment_info"]["site_name"] = "Different reserve"
    with pytest.raises(MetadataConflict, match="Contradictory event aliases"):
        merge_event(json.dumps(old).encode(), json.dumps(new).encode(), file_count=2)


def test_qc_history_is_append_only_and_retry_is_idempotent():
    first = {"check": "first", "timestamp": "2026-01-01", "severity": "warning"}
    second = {"check": "second", "timestamp": "2026-02-01", "severity": "pass"}
    a = json.dumps({"history": {"session_checks": [first], "devices": {}}, "custom": "kept"}).encode()
    b = json.dumps({"history": {"session_checks": [second], "devices": {}}}).encode()
    merged = merge_qc(a, b)
    assert json.loads(merged)["history"]["session_checks"] == [first, second]
    assert merge_qc(merged, b) == merged


def test_direct_unconditional_shared_upload_is_forbidden(tmp_path):
    path = tmp_path / "audio_file_metadata.csv"
    path.write_bytes(payload([row("a.wav")]))
    api = FakeBox()
    with pytest.raises(MetadataConflict, match="requires"):
        BoxStorage(api).upload_file_with_path(path, "event", Path(path.name))
    assert not api.calls


def test_archive_rows_account_for_existing_files_only_with_hash_evidence():
    api = FakeBox()
    old = row("old.wav")
    api.add("audio_file_metadata.csv", payload([old]))
    hashes = {"raw_data/p1_BD/old.wav": old["file_hash_sha1"], "raw_data/p1_BD/unknown.wav": "0" * 40}
    known, issues = recorded_archive_paths(BoxStorage(api), "event", hashes)
    assert known == {"raw_data/p1_BD/old.wav"} and not issues
    hashes["raw_data/p1_BD/old.wav"] = "0" * 40
    known, issues = recorded_archive_paths(BoxStorage(api), "event", hashes)
    assert not known and issues[0]["type"] == "archive_metadata_unverified"


def test_missing_expected_local_member_is_not_zero_file_success(tmp_path):
    (tmp_path / "raw_data").mkdir()
    result, _, issues = verify_box_hashes(BoxStorage(FakeBox()), "event", "event", [
        {"new_filename": "missing.wav", "device_label": "p1_BD"}], tmp_path)
    assert not result
    assert issues == [{"type": "missing_local", "filename": "raw_data/p1_BD/missing.wav"}]


@pytest.mark.parametrize("stop_at", ["image_file_metadata.csv", "audio_file_metadata.csv", "deployment_event_record.json"])
def test_interrupted_publication_keeps_evidence_and_can_retry_union(tmp_path, stop_at):
    api = FakeBox()
    old_image, old_audio = row("old.jpg", deployment="old"), row("old.wav", deployment="old")
    new_image, new_audio = row("new.jpg", deployment="new"), row("new.wav", deployment="new")
    api.add("image_file_metadata.csv", payload([old_image]))
    api.add("audio_file_metadata.csv", payload([old_audio]))
    api.add("deployment_event_record.json", json.dumps(event("old", count=2)).encode())
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "image_file_metadata.csv").write_bytes(payload([new_image]))
    (tmp_path / "audio_file_metadata.csv").write_bytes(payload([new_audio]))
    (tmp_path / "deployment_event_record.json").write_text(json.dumps(event("new", count=2)))
    publication = EventPublication(BoxStorage(api), "event", tmp_path)
    publication.prepare()
    publication.mark_media_verified()
    def stop(file_id):
        if api.objects[file_id]["name"] == stop_at:
            raise RuntimeError("interrupted before write")
    api.before_write = stop
    with pytest.raises(MetadataConflict) as failure:
        publication.publish()
    publication.fail(failure.value)
    assert (publication.attempt_dir / "audio_file_metadata.csv.source").read_bytes() == payload([old_audio])
    with pytest.raises(MetadataConflict):
        require_publication_complete(tmp_path)
    api.before_write = None
    retry = EventPublication(BoxStorage(api), "event", tmp_path)
    retry.prepare()
    retry.publish()
    assert len(read_csv(api.data("audio_file_metadata.csv"))[1]) == 2
    assert json.loads(api.data("deployment_event_record.json"))["file_count"] == 4
    for name, old in (("image_file_metadata.csv", old_image), ("audio_file_metadata.csv", old_audio)):
        assert old in read_csv(api.data(name))[1]


def test_provenance_stamp_preserves_unknown_columns_and_malformed_source(tmp_path):
    path = tmp_path / "audio_file_metadata.csv"
    path.write_bytes(payload([row("a.wav", external="retained")]))
    stamp_box_provenance(path, "John", "2026-10-26")
    assert read_csv(path.read_bytes())[1][0]["external"] == "retained"
    path.write_bytes(b"filename\na.wav\n")
    with pytest.raises(MetadataConflict):
        stamp_box_provenance(path, "John", "2026-10-26")
    assert path.read_bytes() == b"filename\na.wav\n"


def test_real_threads_keep_prior_batch_and_verify_complete_event(tmp_path, monkeypatch):
    from cassn.box import threads
    api = FakeBox()
    api.objects["year"] = {"type": "folder", "name": "2026", "parent": "root"}
    api.objects["reserve"] = {"type": "folder", "name": "Test", "parent": "year"}
    api.objects["event"]["parent"] = "reserve"
    old = row("old.wav", deployment="old", notes="retained", is_uploaded_to_box="True",
              box_uploader="Earlier", box_upload_datetime="2026-01-01")
    api.add("audio_file_metadata.csv", payload([old]))
    api.add("deployment_event_record.json", json.dumps(event("old")).encode())
    storage = BoxStorage(api)
    raw = storage.find_or_create_folder("event", "raw_data")
    prior_device = storage.find_or_create_folder(raw, "old")
    api.add("old.wav", b"recording", prior_device)
    folder = tmp_path / "event"
    raw_file = folder / "raw_data/new/new.wav"
    raw_file.parent.mkdir(parents=True)
    raw_file.write_bytes(b"recording")
    newer = row("new.wav", deployment="new")
    csv_path = folder / "audio_file_metadata.csv"
    csv_path.write_bytes(payload([newer]))
    (folder / "deployment_event_record.json").write_text(json.dumps(event("new")))
    metadata = event("new")["deployment_info"]
    inventory = [{"new_filename": "new.wav", "device_label": "new", "file_size_bytes": len(b"recording"),
                  "file_hash_sha1": newer["file_hash_sha1"], "file_hash_sha256": newer["file_hash_sha256"]}]
    config = NS(field_data_folder_id="root")
    monkeypatch.setattr(threads, "get_box_client", lambda config: api)
    upload = threads.BoxUploadThread(folder, metadata, inventory,
                                     box_config=config, valid_site_names={"Test"})
    results = []
    upload.finished.connect(lambda *args: results.append(args))
    upload.run()
    assert results[-1][0], results
    assert read_csv(api.data("audio_file_metadata.csv"))[1] == [old, newer]
    assert json.loads(api.data("deployment_event_record.json"))["file_count"] == 2
    stamp_box_provenance(csv_path, "John", "2026-10-26")
    provenance = threads.ProvenanceUploadThread([csv_path], "event", box_config=config)
    provenance.finished.connect(lambda *args: results.append(args))
    provenance.run()
    assert results[-1][0], results
    assert read_csv(api.data("audio_file_metadata.csv"))[1][0] == old
    require_publication_complete(folder)
    verify = threads.BoxVerifyThread(folder, inventory, metadata, box_config=config, valid_site_names={"Test"})
    verification = []
    verify.finished.connect(lambda *args: verification.append(args))
    verify.run()
    assert verification[-1][0] and verification[-1][2] == [], verification
    assert len(inventory) == 1  # Earlier Box-only files are not local acquisitions.
    # Cleanup checks a batch subset against the accumulated event, without
    # demanding that its local CSV equal the larger Box CSV byte for byte.
    from cassn.core.staging_cleanup import inspect_deployment_for_cleanup
    (folder / "session.json").write_text(json.dumps({
        "schema_version": 1, "metadata": metadata, "deployment_folder": str(folder),
        "devices": [[1, "One", "BD", "new"]], "device_statuses": {"new": {"status": "Complete"}},
        "file_inventory": inventory,
    }))
    (folder / "qc/box_upload_verification.json").write_text(json.dumps({"verified": True}))
    plan = inspect_deployment_for_cleanup(folder, tmp_path, BoxStorage(api), "root")
    assert plan.clearable, plan.reasons


def test_provenance_auth_failure_is_reported_without_crashing(tmp_path, monkeypatch):
    from cassn.box import threads
    monkeypatch.setattr(threads, "get_box_client", lambda config: None)
    worker = threads.ProvenanceUploadThread([tmp_path / "missing.csv"], "event", box_config={})
    results = []
    worker.finished.connect(lambda *args: results.append(args))
    worker.run()
    assert results and results[0][0] is False


@pytest.mark.parametrize("force", [False, True])
def test_unresolved_publication_blocks_cleanup_even_with_force(tmp_path, force):
    from cassn.core.staging_cleanup import inspect_deployment_for_cleanup
    folder = tmp_path / "event"
    p = prepare(FakeBox(), folder, [row("a.wav")])
    p.fail("uncertain response")
    plan = inspect_deployment_for_cleanup(folder, tmp_path, None, "", force=force)
    assert not plan.clearable
    assert any("publication" in r for r in plan.reasons)


def test_batch_csv_membership_must_match_inventory(tmp_path):
    p = prepare(FakeBox(), tmp_path, [row("a.wav")])
    with pytest.raises(MetadataConflict, match="membership"):
        p.validate_batch_inventory([])


def test_inconsistent_historical_event_is_not_repaired_without_journal(tmp_path):
    api = FakeBox()
    api.add("audio_file_metadata.csv", payload([row("old.wav")]))
    api.add("deployment_event_record.json", json.dumps(event("old", count=99)).encode())
    (tmp_path / "deployment_event_record.json").write_text(json.dumps(event("new")))
    with pytest.raises(MetadataConflict, match="no verified matching recovery journal"):
        prepare(api, tmp_path, [row("new.wav")])
    assert not api.calls


@pytest.mark.parametrize("stop_at", ["image_file_metadata.csv", "audio_file_metadata.csv"])
def test_provenance_partial_write_can_retry_without_resetting_first_stamp(tmp_path, stop_at):
    api = FakeBox()
    for name, filename in (("image_file_metadata.csv", "a.jpg"), ("audio_file_metadata.csv", "b.wav")):
        api.add(name, payload([row(filename)]))
        (tmp_path / name).write_bytes(payload([row(filename, is_uploaded_to_box="True",
                                                box_uploader="John", box_upload_datetime="2026-10-26")]))
    publication = EventPublication(BoxStorage(api), "event", tmp_path, provenance=True)
    publication.prepare()
    def stop(fid):
        if api.objects.get(fid, {}).get("name", fid) == stop_at:
            raise RuntimeError("interrupted provenance")
    api.before_write = stop
    with pytest.raises(MetadataConflict) as failure:
        publication.publish()
    publication.fail(failure.value)
    api.before_write = None
    retry = EventPublication(BoxStorage(api), "event", tmp_path, provenance=True)
    retry.prepare()
    retry.publish()
    require_publication_complete(tmp_path)
    for name in ("image_file_metadata.csv", "audio_file_metadata.csv"):
        saved = read_csv(api.data(name))[1][0]
        assert saved["is_uploaded_to_box"] == "True" and saved["box_uploader"] == "John"


def test_create_race_equivalent_content_is_verified(tmp_path):
    api = FakeBox()
    publication = prepare(api, tmp_path, [row("a.wav")])
    def race(_):
        api.add("audio_file_metadata.csv", publication.candidates["audio_file_metadata.csv"])
    api.before_write = race
    publication.publish()
    assert publication.state["files"]["audio_file_metadata.csv"]["status"] == "verified"


def test_incomplete_batch_hash_verification_cannot_publish_metadata(tmp_path, monkeypatch):
    from cassn.box import threads
    api = FakeBox()
    api.objects["year"] = {"type": "folder", "name": "2026", "parent": "root"}
    api.objects["reserve"] = {"type": "folder", "name": "Test", "parent": "year"}
    api.objects["event"]["parent"] = "reserve"
    storage = BoxStorage(api)
    raw = storage.find_or_create_folder("event", "raw_data")
    device = storage.find_or_create_folder(raw, "new")
    api.add("new.wav", b"different remote bytes", device)
    folder = tmp_path / "event"
    local = folder / "raw_data/new/new.wav"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"recording")
    metadata = event("new")["deployment_info"]
    r = row("new.wav", deployment="new")
    (folder / "audio_file_metadata.csv").write_bytes(payload([r]))
    inventory = [{"new_filename": "new.wav", "device_label": "new",
                  "file_hash_sha1": r["file_hash_sha1"], "file_hash_sha256": r["file_hash_sha256"]}]
    monkeypatch.setattr(threads, "get_box_client", lambda config: api)
    worker = threads.BoxUploadThread(folder, metadata, inventory,
                                     box_config=NS(field_data_folder_id="root"), valid_site_names={"Test"})
    results = []
    worker.finished.connect(lambda *args: results.append(args))
    worker.run()
    assert not results[-1][0] and "media verification failed" in results[-1][1]
    assert not any(obj["name"] == "audio_file_metadata.csv" for obj in api.objects.values())
    assert json.loads((folder / "qc/box_publication_state.json").read_text())["status"] == "needs_recovery"


def test_pending_chunks_block_provenance_publication_before_box_writes(tmp_path,monkeypatch):
    from cassn.box import threads
    from cassn.box.upload_state import atomic_json
    api=FakeBox()
    csv=tmp_path/'audio_file_metadata.csv'
    csv.write_bytes(payload([row('new.wav')]))
    atomic_json(tmp_path/'qc/box_upload_sessions/pending.json',
                {'version':1,'state':'committing','session_id':'s1'})
    monkeypatch.setattr(threads,'get_box_client',lambda config:api)
    worker=threads.ProvenanceUploadThread([csv],'event',box_config=NS(field_data_folder_id='root'))
    results=[]
    worker.finished.connect(lambda *args:results.append(args))
    worker.run()
    assert not results[-1][0] and 'recovery record' in results[-1][1]
    assert not api.calls


WI_STAMP = {
    "is_submitted_to_wi": "True", "wi_submitter": "Imperato, John",
    "wi_submission_datetime": "2026-11-03T23:00:00+00:00",
}


def wi_row(name="old.jpg", **values):
    return row(name, is_submitted_to_wi="False", wi_submitter="",
               wi_submission_datetime="", **values)


def stamp_images(api, **values):
    fid = api.by_name("image_file_metadata.csv")
    fields, rows = read_csv(api.data("image_file_metadata.csv"))
    api.change(fid, csv_bytes(fields, [{**r, **(values or WI_STAMP)} for r in rows]))
    return fid


def image_publication(api, folder, *, provenance=False):
    folder.mkdir(parents=True, exist_ok=True)
    old = wi_row()
    new = wi_row("new.jpg")
    api.add("image_file_metadata.csv", payload([old] if not provenance else [old, new]))
    if provenance:
        new = {**new, "is_uploaded_to_box": "True", "box_uploader": "John",
               "box_upload_datetime": "2026-11-03"}
    (folder / "image_file_metadata.csv").write_bytes(payload([new]))
    p = EventPublication(BoxStorage(api), "event", folder, provenance=provenance)
    p.prepare()
    return p


@pytest.mark.parametrize("provenance", [False, True])
def test_both_publication_passes_carry_wi_only_edit_between_prepare_and_publish(tmp_path, provenance):
    api = FakeBox()
    p = image_publication(api, tmp_path, provenance=provenance)
    fid = stamp_images(api)
    stamped_version = str(api.objects[fid]["version"])
    p.publish()
    fields, rows = read_csv(api.data("image_file_metadata.csv"))
    by_name = {r["filename"]: r for r in rows}
    assert all(by_name["old.jpg"][k] == v for k, v in WI_STAMP.items())
    assert by_name["new.jpg"]["is_submitted_to_wi"] == ("True" if provenance else "False")
    if provenance:
        assert by_name["new.jpg"]["box_uploader"] == "John"
    assert api.calls == [("version", fid, stamped_version)]
    assert p.state["files"]["image_file_metadata.csv"]["wi_updates"]
    assert read_csv((p.attempt_dir / "image_file_metadata.csv.candidate").read_bytes()) == (fields, rows)
    assert p.state["status"] == ("published" if provenance else "awaiting_provenance")


@pytest.mark.parametrize("provenance", [False, True])
def test_second_non_wi_edit_still_stops_both_passes(tmp_path, provenance):
    api = FakeBox()
    p = image_publication(api, tmp_path, provenance=provenance)
    stamp_images(api)
    p._recheck()
    edited = stamp_images(api, notes="independent correction")
    with pytest.raises(MetadataConflict, match="non-WI"):
        p.publish()
    assert not api.calls
    assert read_csv(api.data("image_file_metadata.csv"))[1][0]["notes"] == "independent correction"


@pytest.mark.parametrize("mutation", ["header", "added_row", "deleted_row", "duplicate", "hash", "clock", "unknown"])
def test_wi_only_comparison_rejects_every_other_change(mutation):
    from cassn.box.metadata_merge import carry_wi_only_update
    fields, rows = read_csv(payload([wi_row(), wi_row("other.jpg")]))
    before = csv_bytes(fields, rows)
    rows[0].update(WI_STAMP)
    if mutation == "header":
        fields = list(reversed(fields))
    elif mutation == "added_row":
        rows.append(wi_row("added.jpg"))
    elif mutation == "deleted_row":
        rows.pop()
    elif mutation == "duplicate":
        rows.append(dict(rows[0]))
    elif mutation == "hash":
        rows[0]["file_hash_sha256"] = "0" * 64
    elif mutation == "clock":
        rows[0]["recorded_datetime"] = "2026-01-01"
    else:
        fields.append("unexpected")
        for r in rows:
            r["unexpected"] = "value"
    with pytest.raises(MetadataConflict):
        carry_wi_only_update(before, csv_bytes(fields, rows), before)


def test_wi_only_comparison_matches_keys_not_row_position():
    from cassn.box.metadata_merge import carry_wi_only_update
    a, b, new = wi_row("a.jpg"), wi_row("b.jpg"), wi_row("new.jpg")
    before = payload([a, b])
    result = carry_wi_only_update(before, payload([{**b, **WI_STAMP}, a]), payload([a, b, new]))
    assert read_csv(result)[1] == [a, {**b, **WI_STAMP}, new]


def test_wi_only_rebase_cannot_replace_file_identity(tmp_path):
    api = FakeBox()
    p = image_publication(api, tmp_path)
    fid = stamp_images(api)
    api.objects[fid]["name"] = "renamed.csv"
    api.add("image_file_metadata.csv", api.objects[fid]["payload"])
    with pytest.raises(MetadataConflict, match="identity"):
        p.publish()
    assert not api.calls


@pytest.mark.parametrize("provenance", [False, True])
def test_wi_rebase_keeps_if_match_for_edit_after_recheck(tmp_path, provenance):
    api = FakeBox()
    p = image_publication(api, tmp_path, provenance=provenance)
    stamp_images(api)
    api.before_write = lambda _: stamp_images(api, notes="raced after final preflight")
    with pytest.raises(MetadataConflict, match="conflict"):
        p.publish()
    assert "raced after final preflight" in api.data("image_file_metadata.csv").decode()
    assert api.calls[0][2] == "2"


@pytest.mark.parametrize("provenance", [False, True])
def test_readback_rejects_non_wi_edit_after_write(tmp_path, provenance):
    api = FakeBox()
    p = image_publication(api, tmp_path, provenance=provenance)
    stamp_images(api)
    api.after_write = lambda _: stamp_images(api, notes="edited before readback")
    with pytest.raises(MetadataConflict, match="conflict"):
        p.publish()
    assert p.state["status"] != "published"


@pytest.mark.parametrize("wi_only", [True, False])
def test_final_check_of_already_written_image_preserves_wi_but_rejects_other_edits(tmp_path, wi_only):
    api = FakeBox()
    p = image_publication(api, tmp_path, provenance=True)
    audio_id = api.add("audio_file_metadata.csv", payload([row("audio.wav")]))
    (tmp_path / "audio_file_metadata.csv").write_bytes(payload([row(
        "audio.wav", is_uploaded_to_box="True", box_uploader="John", box_upload_datetime="today")]))
    p = EventPublication(BoxStorage(api), "event", tmp_path, provenance=True)
    p.prepare()
    api.after_write = lambda fid: stamp_images(api, **({} if wi_only else {"notes": "changed"})) if fid == audio_id else None
    if wi_only:
        p.publish()
        assert p.state["status"] == "published"
        assert all(r["is_submitted_to_wi"] == "True" for r in read_csv(api.data("image_file_metadata.csv"))[1])
    else:
        with pytest.raises(MetadataConflict, match="non-WI"):
            p.publish()


@pytest.mark.parametrize("existing_image", [False, True])
@pytest.mark.parametrize("non_wi_edit", [False, True])
@pytest.mark.parametrize("stop_at", ["audio_file_metadata.csv", "deployment_event_record.json"])
def test_restart_partial_publication_with_intervening_wi_stamp(tmp_path, existing_image, non_wi_edit, stop_at):
    api = FakeBox()
    old_image, old_audio = wi_row(deployment="old"), row("old.wav", deployment="old")
    if existing_image:
        api.add("image_file_metadata.csv", payload([old_image]))
    api.add("audio_file_metadata.csv", payload([old_audio]))
    api.add("deployment_event_record.json", json.dumps(event("old", count=2 if existing_image else 1)).encode())
    (tmp_path / "image_file_metadata.csv").write_bytes(payload([wi_row("new.jpg", deployment="new")]))
    (tmp_path / "audio_file_metadata.csv").write_bytes(payload([row("new.wav", deployment="new")]))
    (tmp_path / "deployment_event_record.json").write_text(json.dumps(event("new", count=2)))
    p = EventPublication(BoxStorage(api), "event", tmp_path)
    p.prepare()
    p.mark_media_verified()
    def stop(fid):
        if api.objects.get(fid, {}).get("name", fid) == stop_at:
            raise RuntimeError("crashed after writing image metadata")
    api.before_write = stop
    with pytest.raises(MetadataConflict) as failure:
        p.publish()
    p.fail(failure.value)
    stamp_images(api, **({"notes": "independent correction"} if non_wi_edit else {}))
    api.before_write = None
    retry = EventPublication(BoxStorage(api), "event", tmp_path)
    if non_wi_edit:
        with pytest.raises(MetadataConflict, match="recovery journal"):
            retry.prepare()
        assert "independent correction" in api.data("image_file_metadata.csv").decode()
    else:
        retry.prepare()
        retry.publish()
        assert retry.state["recovered_from"] == p.attempt_id
        images = read_csv(api.data("image_file_metadata.csv"))[1]
        assert len(images) == (2 if existing_image else 1)
        assert all(all(r[k] == v for k, v in WI_STAMP.items()) for r in images)
        assert json.loads(api.data("deployment_event_record.json"))["file_count"] == (4 if existing_image else 3)


def test_provenance_restart_after_partial_write_keeps_late_wi_stamp(tmp_path):
    api = FakeBox()
    p = image_publication(api, tmp_path, provenance=True)
    aid = api.add("audio_file_metadata.csv", payload([row("a.wav")]))
    (tmp_path / "audio_file_metadata.csv").write_bytes(payload([row(
        "a.wav", is_uploaded_to_box="True", box_uploader="John", box_upload_datetime="today")]))
    p = EventPublication(BoxStorage(api), "event", tmp_path, provenance=True)
    p.prepare()
    def stop(fid):
        if fid == aid:
            raise RuntimeError("crashed during provenance")
    api.before_write = stop
    with pytest.raises(MetadataConflict) as failure:
        p.publish()
    p.fail(failure.value)
    stamp_images(api)
    api.before_write = None
    retry = EventPublication(BoxStorage(api), "event", tmp_path, provenance=True)
    retry.prepare()
    retry.publish()
    require_publication_complete(tmp_path)
    assert all(r["is_submitted_to_wi"] == "True" for r in read_csv(api.data("image_file_metadata.csv"))[1])


@pytest.mark.parametrize("provenance", [False, True])
@pytest.mark.parametrize("hook", ["before_write", "after_write"])
def test_stamp_during_conditional_write_or_readback(tmp_path, provenance, hook):
    api = FakeBox()
    publication = image_publication(api, tmp_path, provenance=provenance)

    def stamp_once(file_id):
        setattr(api, hook, None)
        stamp_images(api)

    setattr(api, hook, stamp_once)
    publication.publish()
    images = read_csv(api.data("image_file_metadata.csv"))[1]
    assert {r["filename"] for r in images} == {"new.jpg", "old.jpg"}
    assert all(images[0][field] == value for field, value in WI_STAMP.items())
    if provenance:
        assert next(r for r in images if r["filename"] == "new.jpg")["box_uploader"] == "John"


def test_wi_folder_created_by_publisher_during_fdm_creation(monkeypatch):
    api = FakeBox()
    def competing_create(name, parent):
        folder = FakeBox.create_folder(api, name, parent)
        api.add("upload_record.md", b"verified by WI", folder.id)
        raise RuntimeError("409 folder already exists")

    monkeypatch.setattr(api, "create_folder", competing_create)
    folder = BoxStorage(api).find_or_create_folder("event", "wildlife_insights")
    assert folder == api.by_name("wildlife_insights")
    assert api.objects[api.by_name("upload_record.md", folder)]["payload"] == b"verified by WI"


def test_wi_manual_inputs_ready_before_media_and_accumulate_across_plots(tmp_path):
    import csv

    api = FakeBox()
    def camera(plot, kind):
        deployment = f"p{plot}_{kind}"
        return row(deployment + ".jpg", deployment=deployment, file_type="image",
                   device_type=kind, camera_id=f"camera-{plot}", project_id=kind)

    def start(batch):
        (tmp_path / "image_file_metadata.csv").write_bytes(payload(batch))
        publication = EventPublication(BoxStorage(api), "event", tmp_path)
        publication.prepare()
        publication.validate_batch_inventory([{"new_filename": r["filename"], **r} for r in batch])
        publication.publish_wi_inputs()
        return publication

    initial = start([camera(1, "ML"), camera(1, "SA")])
    folder = api.by_name("wildlife_insights")
    ml = api.by_name("ML_deployments.csv", folder)
    sa = api.by_name("SA_deployments.csv", folder)
    assert all(o["name"] != "image_file_metadata.csv" for o in api.objects.values())
    record = api.add("upload_record.json", b"WI receipt", folder)
    initial.publish()
    second = start([camera(3, "ML")])
    expected = list(csv.DictReader(io.StringIO(api.objects[ml]["payload"].decode())))
    assert [r["deployment_id"] for r in expected] == ["p1_ML", "p3_ML"]
    assert api.objects[sa]["version"] == 1
    staged_folder = api.by_name("WI_metadata")
    mirrored = api.by_name("wildlife_insights_ML_deployments.csv", staged_folder)
    assert api.objects[mirrored]["payload"] == api.objects[ml]["payload"]
    second.publish()
    start([camera(3, "ML")]).publish()
    assert api.objects[ml]["version"] == 2
    assert api.objects[record]["payload"] == b"WI receipt" and api.objects[record]["version"] == 1
    api.change(ml, api.objects[ml]["payload"].replace(b"camera-1", b"manual-edit"))
    with pytest.raises(MetadataConflict, match="Box WI input differs"):
        start([camera(4, "ML")])
    assert b"manual-edit" in api.objects[ml]["payload"]


def test_display_counts_only_current_files_in_readback_verified_box_records(tmp_path):
    from cassn.box.upload_history import previously_uploaded

    event_folder = tmp_path / "event"
    api = FakeBox()
    old = row("old.wav", deployment_event_id="event")
    batch = prepare(api, event_folder, [old])
    batch.mark_media_verified()
    batch.publish()
    stamped = dict(old, is_uploaded_to_box="True", box_uploader="Operator", box_upload_datetime="2026-10-23")
    publication = prepare(api, event_folder, [stamped], provenance=True)
    publication.publish()
    inventory = [{"deployment_id": old["deployment_id"], "new_filename": n,
                  "file_hash_sha256": old["file_hash_sha256"], "file_size_bytes": int(old["file_size_bytes"])}
                 for n in ("old.wav", "new.wav")]
    assert previously_uploaded(event_folder, inventory) == 1
    prepare(api, event_folder, [row("new.wav", deployment_event_id="event")]).fail("interrupted")
    assert previously_uploaded(event_folder, inventory) == 1
    inventory[0]["file_hash_sha256"] = "f" * 64
    assert previously_uploaded(event_folder, inventory) == 0
    candidate = publication.attempt_dir / "audio_file_metadata.csv.candidate"
    candidate.write_bytes(candidate.read_bytes() + b"\n")
    assert previously_uploaded(event_folder, inventory) is None


def test_final_receipt_history_current_paths_and_retry(tmp_path):
    from cassn.box.receipts import RECEIPT, publish_final_receipt, receipt_delivered
    from cassn.core.verification_evidence import write_result

    api = FakeBox()
    storage = BoxStorage(api)
    def data(path):
        parent = "event"
        for part in Path(path).parts:
            parent = api.by_name(part, parent)
        return api.objects[parent]["payload"]

    initial = EventPublication(storage, "event", tmp_path)
    initial.prepare()
    initial.begin_verification_receipt()
    batch = initial.attempt_id
    assert json.loads(data(RECEIPT)) == {"verified": False, "status": "pending", "batch_attempt_id": batch}
    summary = tmp_path / "qc/deployment_summary.txt"
    summary.write_text("Two devices")
    initial.prepare_current_qc([summary])
    initial.publish()
    assert data("qc/deployment_summary.txt") == summary.read_bytes()
    context = {"profile": "uc", "digest": "example"}
    write_result(tmp_path, "box", context, ok=True, summary="All listed")
    write_result(tmp_path, "hash", context, ok=True, summary="Hashes match")
    path = tmp_path / RECEIPT
    record = json.loads(path.read_bytes())
    path.write_text(json.dumps(dict(record, batch_attempt_id=batch)))
    assert not receipt_delivered(tmp_path)
    summary.write_text("Two devices; final verification complete")
    publish_final_receipt(storage, tmp_path, initial.state)
    assert data("qc/deployment_summary.txt") == summary.read_bytes()
    assert receipt_delivered(tmp_path)
    archived = f"qc/box_batches/{batch}/{RECEIPT}"
    assert data(archived) == data(RECEIPT) == path.read_bytes()
    assert json.loads(data(RECEIPT))["verified"] is True
    calls = len(api.calls)
    publish_final_receipt(storage, tmp_path, initial.state)
    assert len(api.calls) == calls

    second = EventPublication(storage, "event", tmp_path / 'second')
    second.prepare()
    second.begin_verification_receipt()
    assert json.loads(data(RECEIPT))["status"] == "pending"
    assert json.loads(data(archived))["verified"] is True
    with pytest.raises(MetadataConflict, match="Another batch"):
        publish_final_receipt(storage, tmp_path, initial.state)

    second.publish()
    failed = {"verified": False, "batch_attempt_id": second.attempt_id, "context": context}
    (second.local_folder / RECEIPT).write_text(json.dumps(failed))
    publish_final_receipt(storage, second.local_folder, second.state)
    assert json.loads(data(RECEIPT)) == failed
    assert json.loads(data(archived))["verified"] is True
