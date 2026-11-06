"""Publish final Box check results after both checks have stopped."""
from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path

from cassn.box.metadata_merge import MetadataConflict, merge_qc, read_object
from cassn.box.metadata_store import BoxFileStore
from cassn.box.publication import STATE_PATH, atomic_json

RECEIPT = "qc/box_upload_verification.json"
CHECK_FILES = ("qc/box_file_list_verification.json", "qc/file_hash_verification.json")
FINAL_FILES = (*CHECK_FILES, RECEIPT)


def _delivery_file(folder, state):
    batch = state.get("batch_attempt_id")
    if not isinstance(batch, str) or not re.fullmatch(r"[0-9]{8}T[0-9]{6}_[0-9a-f]{32}", batch):
        raise MetadataConflict("Invalid Box batch identity")
    return folder / "qc" / "box_publications" / batch / "receipt_delivery.json"


def receipt_delivered(folder):
    folder = Path(folder)
    state_file = folder / STATE_PATH
    if not state_file.exists():
        return True
    state = read_object(state_file.read_bytes())
    if not state.get("publish_final_receipt"):
        return True
    delivery = _delivery_file(folder, state)
    if not delivery.exists():
        return False
    saved = read_object(delivery.read_bytes())
    digest = hashlib.sha256((folder / RECEIPT).read_bytes()).hexdigest()
    return saved.get("batch_attempt_id") == state["batch_attempt_id"] and saved.get("sha256") == digest


def publish_final_receipt(storage, folder, state):
    """Write history first. Only the owning batch may replace the current receipt."""
    folder = Path(folder)
    delivery = _delivery_file(folder, state)
    if read_object((folder / STATE_PATH).read_bytes()) != state:
        raise MetadataConflict("The local publication changed before receipt delivery")
    batch = state["batch_attempt_id"]
    store = BoxFileStore(storage.client)

    def target(name):
        parent = state["folder_id"]
        path = Path(name)
        for part in path.parts[:-1]:
            parent = storage.find_or_create_folder(parent, part)
        items = [item.id for item in storage.iter_folder_items(parent)
                 if item.name == path.name and item.type == "file"]
        if len(items) > 1:
            raise MetadataConflict(f"Multiple Box files named {name}")
        ref = store.snapshot(items[0]) if items else None
        return parent, path.name, ref

    def write(destination, payload):
        parent, name, ref = destination
        if ref:
            if ref.sha1 == hashlib.sha1(payload).hexdigest() and len(payload) == ref.size:
                store.assert_unchanged(ref)
            else:
                store.write_metadata(ref, payload)
        else:
            result = storage.client.uploads.upload_file(
                attributes={"name": name, "parent": {"id": parent}}, file=io.BytesIO(payload))
            store._verify_write(result.entries[0].id, payload, parent, name)

    current = target(RECEIPT)
    if not current[2] or read_object(store.read(current[2])).get("batch_attempt_id") != batch:
        raise MetadataConflict("Another batch owns the current Box receipt")
    receipt = (folder / RECEIPT).read_bytes()
    record = read_object(receipt)
    if record.get("batch_attempt_id") != batch:
        raise MetadataConflict("The local receipt belongs to another batch")
    if record.get("verified") is True:
        from cassn.core.verification_evidence import read_result
        if (state.get("status") != "published" or not record.get("context")
                or not all(read_result(folder, kind, record["context"]) for kind in ("box", "hash"))):
            raise MetadataConflict("Both checks must pass for the same batch before publishing a verified receipt")
    for name in (*CHECK_FILES, "qc/qc_report.json", "qc/deployment_summary.txt"):
        path = folder / name
        if not path.is_file():
            continue
        payload = path.read_bytes()
        if name in CHECK_FILES:
            # A file-list failure can stop this run before the hash check starts.
            if read_object(payload).get("context") != record.get("context"):
                continue
            write(target(f"qc/box_batches/{batch}/{name}"), payload)
        destination = target(name)
        if name == "qc/qc_report.json":
            ref = destination[2]
            payload = merge_qc(store.read(ref) if ref else None, payload)
        store.assert_unchanged(current[2])
        write(destination, payload)
    write(target(f"qc/box_batches/{batch}/{RECEIPT}"), receipt)
    write(current, receipt)
    atomic_json(delivery, {"sha256": hashlib.sha256(receipt).hexdigest(), "batch_attempt_id": batch})
