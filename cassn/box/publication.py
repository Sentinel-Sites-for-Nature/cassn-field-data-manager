"""Recoverable, conditional publication of accumulated event sidecars.

Each file is conditional and verified. Several files are *not* a transaction;
the local attempt journal records partial publication and blocks cleanup.
"""
from __future__ import annotations

import io
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from cassn.box.metadata_merge import (
    CSV_NAMES, SHARED_NAMES, MetadataConflict, merge_csv, merge_event, merge_qc,
    read_csv, read_object,
)
from cassn.box.metadata_store import BoxFileStore


STATE_PATH = "qc/box_publication_state.json"


def batch_remote_path(relative: Path, attempt_id: str) -> Path:
    if relative.parts[0] in {"raw_data", "soundhub"} or relative.as_posix() in SHARED_NAMES:
        return relative
    return Path("qc/box_batches") / attempt_id / relative


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def require_publication_complete(folder):
    path = Path(folder) / STATE_PATH
    if not path.exists():
        return  # Existing sessions retain their prior cleanup rules.
    try:
        state = read_object(path.read_bytes())
        if state.get("status") != "published":
            raise ValueError("metadata publication is incomplete or needs recovery")
    except (OSError, ValueError) as exc:
        raise MetadataConflict(f"Review {path} before cleanup: {exc}") from exc


class EventPublication:
    def __init__(self, storage, folder_id, local_folder, *, provenance=False, csv_paths=None):
        self.storage = storage
        self.folder_id = folder_id
        self.local_folder = Path(local_folder)
        self.provenance = provenance
        self.csv_paths = csv_paths
        state_path = self.local_folder / STATE_PATH
        previous = read_object(state_path.read_bytes()) if state_path.exists() else {}
        self.store = BoxFileStore(storage.client)
        self.refs = {}
        self.remote = {}
        self.candidates = {}
        self.attempt_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid4().hex
        self.attempt_dir = self.local_folder / "qc" / "box_publications" / self.attempt_id
        self.attempt_dir.mkdir(parents=True)
        self.state = {"attempt_id": self.attempt_id, "folder_id": folder_id,
                      "batch_attempt_id": previous.get("batch_attempt_id", self.attempt_id) if provenance else self.attempt_id,
                      "phase": "provenance" if provenance else "initial",
                      "status": "preparing", "files": {}}
        self._save()
        # An earlier passing artifact cannot certify this attempt.
        atomic_json(self.local_folder / "qc/box_upload_verification.json",
                    {"verified": False, "publication_attempt": self.attempt_id})

    def _save(self):
        atomic_json(self.attempt_dir / "attempt.json", self.state)
        atomic_json(self.local_folder / STATE_PATH, self.state)

    def fail(self, exc):
        self.state.update(status="needs_recovery", error=str(exc))
        self._save()

    def validate_batch_inventory(self, inventory):
        """Bind the local CSV rows to this batch, never to older Box-only rows."""
        incoming = []
        for name in CSV_NAMES:
            path = self.local_folder / name
            if path.exists():
                incoming.extend(read_csv(path.read_bytes())[1])
        by_name = {row["filename"]: row for row in incoming}
        if len(by_name) != len(incoming) or len(inventory) != len(incoming):
            raise MetadataConflict("Local metadata membership does not match the batch inventory")
        seen = set()
        for entry in inventory:
            filename = entry.get("new_filename")
            row = by_name.get(filename)
            if row is None or filename in seen:
                raise MetadataConflict(f"Missing or ambiguous batch metadata for {filename}")
            seen.add(filename)
            if (entry.get("deployment_id") and entry["deployment_id"] != row["deployment_id"]
                    or str(entry.get("file_hash_sha256", "")).lower() != row["file_hash_sha256"].lower()):
                raise MetadataConflict(f"Batch identity/hash disagrees with its metadata: {filename}")

    def _find(self, parent, name):
        matches = [item.id for item in self.storage.iter_folder_items(parent)
                   if item.type == "file" and item.name == name]
        if len(matches) > 1:
            raise MetadataConflict(f"Multiple Box files named {name}; review before upload")
        return matches[0] if matches else None

    def _parent(self, name, *, create=False):
        parent = self.folder_id
        for part in Path(name).parts[:-1]:
            parent = (self.storage.find_or_create_folder(parent, part) if create
                      else self.storage.find_child_folder(parent, part))
            if parent is None:
                return None
        return parent

    def prepare(self):
        local = {}
        allowed = {Path(p).name for p in self.csv_paths} if self.csv_paths is not None else None
        for name in SHARED_NAMES:
            if self.provenance and name not in CSV_NAMES:
                continue
            path = self.local_folder / name
            if path.exists() and (allowed is None or name in allowed):
                local[name] = path.read_bytes()
            parent = self._parent(name)
            file_id = self._find(parent, Path(name).name) if parent is not None else None
            ref = self.store.snapshot(file_id) if file_id else None
            self.refs[name] = ref
            self.remote[name] = self.store.read(ref) if ref else None
            safe_name = name.replace("/", "__")
            if ref:
                (self.attempt_dir / (safe_name + ".source")).write_bytes(self.remote[name])
            if name in local:
                (self.attempt_dir / (safe_name + ".local")).write_bytes(local[name])
            self.state["files"][name] = {"source": asdict(ref) if ref else None, "status": "read"}
            self._save()

        baseline = dict(self.remote)
        if not self.provenance and "deployment_event_record.json" in local:
            remote_event = self.remote.get("deployment_event_record.json")
            if remote_event is not None:
                count = sum(len(read_csv(data)[1]) for name in CSV_NAMES
                            if (data := self.remote.get(name)) is not None)
                if read_object(remote_event).get("file_count") != count:
                    baseline = self._recover_baseline(local)

        for name in CSV_NAMES:
            if name in local:
                if self.provenance and self.remote[name] is None:
                    raise MetadataConflict(f"No previously published {name}; run the full upload first")
                self.candidates[name] = merge_csv(baseline[name], local[name], provenance=self.provenance)
            elif self.remote.get(name) is not None:
                read_csv(self.remote[name])  # Fail closed on an unreadable other half.

        if "deployment_event_record.json" in local:
            name = "deployment_event_record.json"
            for side, payload in (("remote", baseline.get(name)), ("local", local[name])):
                if payload is not None:
                    record = read_object(payload)
                    count = sum(len(read_csv(data)[1]) for csv_name in CSV_NAMES
                                if (data := (baseline.get(csv_name) if side == "remote" else local.get(csv_name))) is not None)
                    if record.get("file_count") != count:
                        raise MetadataConflict(f"{side} event file_count does not match its metadata rows; reconcile before upload")
            count = sum(len(read_csv(data)[1]) for csv_name in CSV_NAMES
                        if (data := self.candidates.get(csv_name, self.remote.get(csv_name))) is not None)
            self.candidates[name] = merge_event(baseline.get(name), local[name], file_count=count)
        if "qc/qc_report.json" in local:
            name = "qc/qc_report.json"
            self.candidates[name] = merge_qc(self.remote.get(name), local[name])

        # No filename alone may alias two different deployment identities.
        names = {}
        for name in CSV_NAMES:
            data = self.candidates.get(name, self.remote.get(name))
            if data is not None:
                for row in read_csv(data)[1]:
                    key = (row["deployment_id"], row["filename"])
                    if row["filename"] in names:
                        raise MetadataConflict(f"Ambiguous cross-metadata filename: {row['filename']}")
                    names[row["filename"]] = key
        for name, payload in self.candidates.items():
            ref = self.refs[name]
            if ref and not ref.can_upload:
                raise MetadataConflict(f"No permission to update {name}")
            (self.attempt_dir / (name.replace("/", "__") + ".candidate")).write_bytes(payload)
        self.state["status"] = "prepared"
        self._save()

    def _recover_baseline(self, local):
        """Resume only our own proven partial publication of this exact batch.

        Historical count mismatches and intervening remote edits are never
        repaired by guessing. Sources/candidates must explain every CSV and
        event record, and the prior attempt must have verified its media.
        """
        root = self.attempt_dir.parent
        names = (*CSV_NAMES, "deployment_event_record.json")
        for previous in sorted(root.iterdir(), reverse=True):
            if previous == self.attempt_dir or not previous.is_dir():
                continue
            try:
                state = read_object((previous / "attempt.json").read_bytes())
                if (state.get("folder_id") != self.folder_id or not state.get("media_verified")
                        or state.get("phase") != "initial"):
                    continue
                baseline = dict(self.remote)
                for name in names:
                    stem = previous / name.replace("/", "__")
                    saved_local = Path(str(stem) + ".local")
                    if (saved_local.read_bytes() if saved_local.exists() else None) != local.get(name):
                        raise ValueError("different local batch")
                    source, candidate = Path(str(stem) + ".source"), Path(str(stem) + ".candidate")
                    before = source.read_bytes() if source.exists() else None
                    after = candidate.read_bytes() if candidate.exists() else before
                    if self.remote.get(name) not in (before, after):
                        raise ValueError("intervening remote edit")
                    baseline[name] = after
                record = read_object(baseline["deployment_event_record.json"])
                count = sum(len(read_csv(data)[1]) for name in CSV_NAMES
                            if (data := baseline.get(name)) is not None)
                if record.get("file_count") != count:
                    continue
                self.state["recovered_from"] = previous.name
                self._save()
                return baseline
            except (OSError, ValueError, TypeError):
                continue
        raise MetadataConflict("remote event file_count does not match its metadata rows; no verified matching recovery journal explains it")

    def mark_media_verified(self):
        self.state["media_verified"] = True
        self._save()

    def _recheck(self):
        for name, ref in self.refs.items():
            if ref:
                self.store.assert_unchanged(ref)
            else:
                parent = self._parent(name)
                if parent is not None and self._find(parent, Path(name).name):
                    raise MetadataConflict(f"{name} appeared after preflight; retry the upload to merge it")

    def publish(self):
        self.state["status"] = "publishing"
        self._save()
        for name, payload in self.candidates.items():
            self._recheck()
            ref = self.refs[name]
            parent = self._parent(name, create=True)
            self.state["files"][name]["status"] = "write_pending"
            self._save()  # Durable intent before a possibly ambiguous response.
            try:
                if ref:
                    if self.remote[name] != payload:
                        self.store.write_metadata(ref, payload)
                else:
                    result = self.storage.client.uploads.upload_file(
                        attributes={"name": Path(name).name, "parent": {"id": parent}},
                        file=io.BytesIO(payload),
                    )
                    if not result.entries or len(result.entries) != 1:
                        raise MetadataConflict(f"No confirmed file identity returned for {name}")
                    self.store._verify_write(result.entries[0].id, payload, parent, Path(name).name)
            except Exception as exc:
                # A lost ACK or a create conflict is success ONLY if an actual
                # fresh read proves the exact intended payload is now present.
                file_id = self._find(parent, Path(name).name)
                if not file_id:
                    raise MetadataConflict(f"Publication unconfirmed for {name}: {exc}") from exc
                current = self.store.snapshot(file_id)
                if self.store.read(current) != payload:
                    raise MetadataConflict(f"Publication conflict/unconfirmed for {name}: {exc}") from exc
            file_id = self._find(parent, Path(name).name)
            current = self.store.snapshot(file_id)
            if current.parent_id != parent or current.name != Path(name).name or self.store.read(current) != payload:
                raise MetadataConflict(f"Saved content changed during verification: {name}")
            self.refs[name] = current
            self.state["files"][name].update(status="verified", saved=asdict(current))
            self._save()
        self._recheck()
        self.state["status"] = ("awaiting_provenance" if not self.provenance and
                                any(name in self.candidates for name in CSV_NAMES) else "published")
        self._save()
