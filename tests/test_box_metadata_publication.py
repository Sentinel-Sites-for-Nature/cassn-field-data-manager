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
        if api.objects[fid]["name"] == stop_at:
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
