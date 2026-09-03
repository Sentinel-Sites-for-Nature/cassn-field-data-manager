"""Tests for read-only NDP transfer planning and resumable media execution."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from cassn.lookups import LookupTables
from cassn.ndp.box_download import download_box_file
from cassn.ndp.manifest import METADATA_FILENAME
from cassn.ndp.pelican import PelicanRunner, verify_stat
from cassn.ndp.staging import PlannedDeployment, StagingPlan
from cassn.ndp.submission import execute_media_transfer
from cassn.ndp.transfer import (
    NdpTransferError,
    TransferFile,
    join_destination,
    normalize_destination_root,
    plan_media_transfer,
)
from cassn.ndp.transfer_state import (
    abandonment_targets,
    advance,
    load_or_create_state,
    load_state,
    new_state,
    save_state,
)


EVENT_ID = "UC_QuailRidge_20260108"
DEPLOYMENT_ID = "UC_QuailRidge_plot1_ML_20260108"
DESTINATION = "osdf:///ndp/private/cassn/ca/UC-Nature/source"


def _item(kind, name, item_id, *, size=None, sha1=""):
    return SimpleNamespace(type=kind, name=name, id=item_id, size=size, sha1=sha1)


class FakeStorage:
    def __init__(self, tree):
        self.tree = tree

    def iter_folder_items(self, folder_id, *, fields=None):
        del fields
        yield from self.tree.get(folder_id, [])

    def find_child_folder(self, parent_id, name, *, fields=None):
        del fields
        for item in self.tree.get(parent_id, []):
            if item.type == "folder" and item.name == name:
                return item.id
        return None


class FakeBoxClient:
    def __init__(self, content):
        self.content = content
        self.calls = []
        self.downloads = SimpleNamespace(download_file=self.download_file)

    def download_file(self, file_id):
        self.calls.append(file_id)
        return io.BytesIO(self.content[file_id])


class FakePelican:
    def __init__(self, sizes, checksums=None, *, fail_sync=False):
        self.sizes = sizes
        self.checksums = checksums or {}
        self.fail_sync = fail_sync
        self.syncs = []
        self.stats = []

    def require_available(self):
        return None

    def sync_directory(self, source, destination):
        self.syncs.append((Path(source), destination))
        if self.fail_sync:
            raise NdpTransferError("sync failed")

    def stat_object(self, object_url):
        self.stats.append(object_url)
        filename = object_url.rsplit("/", 1)[-1]
        payload = {"size": self.sizes[filename]}
        if filename in self.checksums:
            payload["checksums"] = {"sha": self.checksums[filename]}
        return payload


def _csv_bytes(rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=[
            "filename",
            "file_size_bytes",
            "file_hash_sha256",
            "file_hash_sha1",
        ],
    )
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


def _fixtures(tmp_path, contents=None):
    contents = contents or {"one.jpg": b"one", "two.jpg": b"two"}
    rows = [
        {
            "filename": name,
            "file_size_bytes": str(len(content)),
            "file_hash_sha256": hashlib.sha256(content).hexdigest(),
            "file_hash_sha1": hashlib.sha1(content).hexdigest(),
        }
        for name, content in contents.items()
    ]
    event = tmp_path / "box" / "2026" / "Quail Ridge Reserve" / EVENT_ID
    staging = StagingPlan(
        event,
        tmp_path / "staging",
        EVENT_ID,
        [
            PlannedDeployment(
                DEPLOYMENT_ID,
                {METADATA_FILENAME: _csv_bytes(rows)},
            )
        ],
    )
    lookups = LookupTables()
    lookups.deployments_by_id = {
        DEPLOYMENT_ID: {
            "deployment_id": DEPLOYMENT_ID,
            "plot_number": "1",
            "device_type": "ML",
            "deployment_sequence": "0",
        }
    }
    tree = {
        "root": [_item("folder", "2026", "year")],
        "year": [_item("folder", "Quail Ridge Reserve", "reserve")],
        "reserve": [_item("folder", EVENT_ID, "event")],
        "event": [_item("folder", "raw_data", "raw")],
        "raw": [_item("folder", "p1_ML", "device")],
        "device": [_item("folder", "card_1", "card")],
        "card": [
            _item(
                "file",
                name,
                f"file-{index}",
                size=len(content),
                sha1=hashlib.sha1(content).hexdigest(),
            )
            for index, (name, content) in enumerate(contents.items(), start=1)
        ],
    }
    return staging, lookups, tree, contents


def _plan(tmp_path, contents=None, **kwargs):
    staging, lookups, tree, contents = _fixtures(tmp_path, contents)
    plan = plan_media_transfer(
        staging,
        lookups,
        FakeStorage(tree),
        "root",
        tmp_path / "scratch",
        DESTINATION,
        scratch_free_bytes=10 * 1024**3,
        **kwargs,
    )
    return plan, contents


def test_destination_root_is_required_to_be_an_explicit_ndp_osdf_url():
    assert normalize_destination_root(DESTINATION + "/") == DESTINATION
    assert join_destination(DESTINATION, "event", "a b#c.jpg").endswith(
        "/event/a%20b%23c.jpg"
    )
    with pytest.raises(NdpTransferError, match="osdf"):
        normalize_destination_root("s3://bucket/source")
    with pytest.raises(NdpTransferError, match="private"):
        normalize_destination_root("osdf:///somewhere/source")
    with pytest.raises(NdpTransferError, match="exact"):
        normalize_destination_root("osdf:/ndp/private/cassn")
    for unsafe in (
        "osdf:///ndp/private/cassn/../other",
        "osdf:///ndp/private/cassn/%2e%2e/other",
        "osdf:///ndp/private/cassn//other",
    ):
        with pytest.raises(NdpTransferError, match="traversal"):
            normalize_destination_root(unsafe)


def test_plan_maps_nested_box_files_to_one_flat_deployment(tmp_path):
    plan, contents = _plan(tmp_path)

    assert plan.ok, plan.errors
    assert plan.file_count == 2
    assert plan.total_bytes == sum(map(len, contents.values()))
    assert plan.scratch_required_bytes >= 1024**3
    deployment = plan.deployments[0]
    assert deployment.box_folder_name == "p1_ML"
    assert deployment.data_destination == (
        DESTINATION + f"/{EVENT_ID}/{DEPLOYMENT_ID}/data"
    )
    assert [item.source_relative_path for item in deployment.files] == [
        "card_1/one.jpg",
        "card_1/two.jpg",
    ]
    assert len(plan.signature) == 64


@pytest.mark.parametrize(
    "failure", ["missing", "extra", "duplicate", "wrong_size", "wrong_sha1"]
)
def test_plan_rejects_a_box_inventory_that_metadata_cannot_describe(tmp_path, failure):
    staging, lookups, tree, _ = _fixtures(tmp_path)
    if failure == "missing":
        tree["card"] = tree["card"][:1]
    elif failure == "extra":
        tree["card"].append(_item("file", "extra.jpg", "extra", size=1))
    elif failure == "duplicate":
        tree["card"].append(_item("file", "one.jpg", "duplicate", size=3))
    elif failure == "wrong_size":
        tree["card"][0].size = 999
    else:
        tree["card"][0].sha1 = "0" * 40

    plan = plan_media_transfer(
        staging,
        lookups,
        FakeStorage(tree),
        "root",
        tmp_path / "scratch",
        DESTINATION,
        scratch_free_bytes=10 * 1024**3,
    )

    assert not plan.ok
    assert plan.errors


def test_plan_fails_fast_when_scratch_cannot_hold_the_largest_deployment(tmp_path):
    staging, lookups, tree, _ = _fixtures(tmp_path)
    plan = plan_media_transfer(
        staging,
        lookups,
        FakeStorage(tree),
        "root",
        tmp_path / "scratch",
        DESTINATION,
        scratch_free_bytes=10,
    )

    assert not plan.ok
    assert any("insufficient scratch" in error for error in plan.errors)


def test_keep_scratch_requires_space_for_the_whole_event(tmp_path):
    staging, lookups, tree, contents = _fixtures(tmp_path)
    plan = plan_media_transfer(
        staging,
        lookups,
        FakeStorage(tree),
        "root",
        tmp_path / "scratch",
        DESTINATION,
        scratch_free_bytes=10 * 1024**3,
        retain_scratch=True,
    )

    assert plan.ok, plan.errors
    assert plan.scratch_required_bytes >= sum(map(len, contents.values())) + 1024**3


def test_box_download_is_atomic_hash_checked_and_reusable(tmp_path):
    content = b"box content"
    source = TransferFile(
        "file-1",
        "one.jpg",
        "card/one.jpg",
        len(content),
        hashlib.sha256(content).hexdigest(),
    )
    client = FakeBoxClient({"file-1": content})
    destination = tmp_path / "data" / "one.jpg"

    first = download_box_file(client, source, destination, chunk_size=2)
    second = download_box_file(client, source, destination, chunk_size=2)

    assert first.bytes_written == len(content)
    assert not first.skipped
    assert second.skipped
    assert client.calls == ["file-1"]
    assert destination.read_bytes() == content
    assert not list(destination.parent.glob("*.partial"))


def test_box_download_never_publishes_bad_content(tmp_path):
    expected = b"correct"
    source = TransferFile(
        "file-1",
        "one.jpg",
        "one.jpg",
        len(expected),
        hashlib.sha256(expected).hexdigest(),
    )
    destination = tmp_path / "one.jpg"

    with pytest.raises(NdpTransferError, match="SHA-256"):
        download_box_file(FakeBoxClient({"file-1": b"corrupt"}), source, destination)

    assert not destination.exists()


def test_state_refuses_changed_inputs_and_lists_abandonment_targets(tmp_path):
    plan, _ = _plan(tmp_path)
    state = new_state(plan)
    advance(state, DEPLOYMENT_ID, "downloaded")
    advance(state, DEPLOYMENT_ID, "synced")
    save_state(plan.state_path, state)

    loaded = load_state(plan.state_path)
    assert abandonment_targets(loaded) == (plan.deployments[0].data_destination,)
    loaded.plan_signature = "changed"
    save_state(plan.state_path, loaded)
    with pytest.raises(NdpTransferError, match="changed"):
        load_or_create_state(plan)


def test_pelican_runner_uses_argument_lists_and_parses_json(tmp_path):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        stdout = json.dumps({"size": 3}) if "stat" in command else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    source = tmp_path / "data"
    source.mkdir()
    token = tmp_path / "token"
    token.write_text("token")
    runner = PelicanRunner(executable="true", token=token, run=run)

    runner.require_available()
    runner.sync_directory(source, DESTINATION)
    assert runner.stat_object(DESTINATION + "/one.jpg") == {"size": 3}
    assert all(isinstance(command, list) for command, _ in calls)
    assert all("--token" in command for command, _ in calls)
    assert calls[0][1] == {"text": True}
    assert calls[1][1] == {"text": True, "capture_output": True}


def test_stat_distinguishes_checksum_verification_from_size_only():
    content = b"one"
    source = TransferFile(
        "file-1",
        "one.jpg",
        "one.jpg",
        len(content),
        hashlib.sha256(content).hexdigest(),
        hashlib.sha1(content).hexdigest(),
    )

    assert verify_stat(source, {"size": 3})["verification"] == "size_only"
    checked = verify_stat(
        source,
        [{"Size": 3, "Checksums": {"sha": hashlib.sha1(content).hexdigest()}}],
    )
    assert checked["verification"] == "checksum"
    with pytest.raises(NdpTransferError, match="checksum"):
        verify_stat(source, {"size": 3, "checksums": {"sha": "0" * 40}})


def test_media_execution_resumes_after_sync_failure_and_never_publishes_controls(
    tmp_path,
):
    plan, contents = _plan(tmp_path)
    box_content = {
        source.file_id: contents[source.filename]
        for source in plan.deployments[0].files
    }
    client = FakeBoxClient(box_content)

    with pytest.raises(NdpTransferError, match="sync failed"):
        execute_media_transfer(
            plan,
            client,
            FakePelican(
                {name: len(value) for name, value in contents.items()}, fail_sync=True
            ),
        )
    assert load_state(plan.state_path).deployments[DEPLOYMENT_ID] == "downloaded"
    assert len(client.calls) == 2

    checksums = {
        name: hashlib.sha256(value).hexdigest() for name, value in contents.items()
    }
    pelican = FakePelican(
        {name: len(value) for name, value in contents.items()}, checksums
    )
    result = execute_media_transfer(plan, client, pelican)

    assert result.media_complete
    assert result.publication_blocked
    assert result.downloaded == 0
    assert result.objects_statted == 2
    assert result.checksum_verified == 2
    assert len(client.calls) == 2  # the completed Box downloads were not repeated
    assert len(pelican.syncs) == 1
    assert not (plan.scratch_root / EVENT_ID / DEPLOYMENT_ID / "data").exists()
    assert not any(
        path.name in {"manifest.json", "file_metadata.csv"}
        for path in plan.scratch_root.rglob("*")
    )


def test_size_only_remote_stat_stops_and_retains_scratch(tmp_path):
    plan, contents = _plan(tmp_path)
    client = FakeBoxClient(
        {
            source.file_id: contents[source.filename]
            for source in plan.deployments[0].files
        }
    )
    pelican = FakePelican({name: len(value) for name, value in contents.items()})

    with pytest.raises(NdpTransferError, match="only size"):
        execute_media_transfer(plan, client, pelican)

    state = load_state(plan.state_path)
    assert state.deployments[DEPLOYMENT_ID] == "stat_recorded"
    assert state.remote_stats[DEPLOYMENT_ID]["size_only"] == 2
    assert (plan.scratch_root / EVENT_ID / DEPLOYMENT_ID / "data").is_dir()


def test_media_execution_refuses_a_concurrent_event_lock(tmp_path):
    plan, _ = _plan(tmp_path)
    plan.state_path.parent.mkdir(parents=True)
    lock = plan.state_path.with_name(".ndp-transfer.lock")
    lock.write_text("12345\n")

    with pytest.raises(NdpTransferError, match="12345"):
        execute_media_transfer(plan, FakeBoxClient({}), FakePelican({}))
