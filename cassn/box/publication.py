"""Recoverable, conditional publication of accumulated event sidecars.

Each file is conditional and verified. Several files are *not* a transaction;
the local attempt journal records partial publication and blocks cleanup.
"""
from __future__ import annotations

import csv
import io
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from cassn.box.metadata_merge import (
    CSV_NAMES, SHARED_NAMES, WI_INPUT_NAMES, WI_STAGED_NAMES, merge_wi_input, MetadataConflict, merge_csv, merge_event, merge_qc,
    read_csv, read_object, carry_wi_only_update,
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
        if provenance and previous.get("publish_final_receipt"):
            self.state["publish_final_receipt"] = True
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

    def _snapshot(self, name):
        parent = self._parent(name)
        file_id = self._find(parent, Path(name).name) if parent is not None else None
        ref = self.store.snapshot(file_id) if file_id else None
        self.refs[name] = ref
        self.remote[name] = self.store.read(ref) if ref else None
        safe_name = name.replace("/", "__")
        if ref:
            (self.attempt_dir / (safe_name + ".source")).write_bytes(self.remote[name])
        self.state["files"][name] = {"source": asdict(ref) if ref else None, "status": "read"}

    def prepare(self):
        local = {}
        allowed = {Path(p).name for p in self.csv_paths} if self.csv_paths is not None else None
        for name in SHARED_NAMES:
            if self.provenance and name not in CSV_NAMES:
                continue
            path = self.local_folder / name
            if path.exists() and (allowed is None or name in allowed):
                local[name] = path.read_bytes()
            self._snapshot(name)
            if name in local:
                (self.attempt_dir / (name.replace("/", "__") + ".local")).write_bytes(local[name])
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
            file_ids = []
            for side, payload in (("remote", baseline.get(name)), ("local", local[name])):
                ids = set()
                file_ids.append(ids)
                if payload is not None:
                    record = read_object(payload)
                    count = 0
                    for csv_name in CSV_NAMES:
                        data = (baseline if side == "remote" else local).get(csv_name)
                        if data is not None:
                            rows = read_csv(data)[1]
                            count += len(rows)
                            ids.update(r["deployment_id"] for r in rows)
                    if record.get("file_count") != count:
                        raise MetadataConflict(f"{side} event file_count does not match its metadata rows; reconcile before upload")
            count = sum(len(read_csv(data)[1]) for csv_name in CSV_NAMES
                        if (data := self.candidates.get(csv_name, self.remote.get(csv_name))) is not None)
            self.candidates[name] = merge_event(baseline.get(name), local[name], file_count=count,
                                                deployment_lookup=self.local_folder / "qc/lookup_snapshot/deployments.csv",
                                                file_ids=file_ids)
        if "qc/qc_report.json" in local:
            name = "qc/qc_report.json"
            self.candidates[name] = merge_qc(self.remote.get(name), local[name])

        # No filename alone may alias two different deployment identities.
        names = {}
        for name in CSV_NAMES:
            data = self.candidates.get(name, self.remote.get(name))
            if data is not None:
                rows = read_csv(data)[1]
                if name == "image_file_metadata.csv" and name in local and not self.provenance:
                    from cassn.export.wildlife_insights import wi_rows_from_image_metadata
                    for kind, deployments in wi_rows_from_image_metadata(rows).items():
                        self._prepare_wi_inputs(kind, deployments)
                for row in rows:
                    key = (row["deployment_id"], row["filename"])
                    if row["filename"] in names:
                        raise MetadataConflict(f"Ambiguous cross-metadata filename: {row['filename']}")
                    names[row["filename"]] = key
        if not self.provenance:
            for kind, name in WI_STAGED_NAMES.items():
                if name not in self.candidates and (self.local_folder / name).is_file():
                    self._prepare_wi_inputs(kind, [])
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
                    file_state = state.get("files", {}).get(name, {})
                    identity = file_state.get("saved") or file_state.get("source")
                    ref = self.refs.get(name)
                    if name == "image_file_metadata.csv" and identity and (
                            ref is None or any(identity.get(key) != getattr(ref, key)
                                               for key in ("file_id", "parent_id", "name"))):
                        raise ValueError("changed image metadata identity")
                    if self.remote.get(name) in (before, after):
                        baseline[name] = after
                        continue
                    # A stamp may land after our CSV write but before the
                    # event count, or on the old rows before our CSV write.
                    if name != "image_file_metadata.csv" or not identity:
                        raise ValueError("intervening remote edit or changed Box identity")
                    for expected in (after, before):
                        if expected is None:
                            continue
                        try:
                            baseline[name] = carry_wi_only_update(expected, self.remote[name], after)
                            break
                        except MetadataConflict:
                            continue
                    else:
                        raise ValueError("intervening non-WI remote edit")
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
                if name == "image_file_metadata.csv":
                    self._recheck_image(ref)
                else:
                    self.store.assert_unchanged(ref)
            else:
                parent = self._parent(name)
                if parent is not None and self._find(parent, Path(name).name):
                    raise MetadataConflict(f"{name} appeared after preflight; retry the upload to merge it")

    def _recheck_image(self, ref):
        name = "image_file_metadata.csv"
        current = self.store.snapshot(ref.file_id)
        if current == ref:
            return
        self._accept_image_update(ref, current, self.remote[name], self.store.read(current))

    def _accept_image_update(self, ref, current, expected, newer):
        name = "image_file_metadata.csv"
        if (any(getattr(current, key) != getattr(ref, key)
                for key in ("file_id", "parent_id", "name", "can_upload"))
                or self._find(ref.parent_id, ref.name) != ref.file_id):
            raise MetadataConflict("Image metadata Box identity or permission changed after preflight")
        candidate = carry_wi_only_update(
            expected, newer, self.candidates.get(name, expected))
        self.store.assert_unchanged(current)
        # Preserve each accepted version alongside the original source and
        # latest candidate, so a restart can explain a partial publication.
        updates = self.state["files"][name].setdefault("wi_updates", [])
        stem = self.attempt_dir / f"{name}.wi-{len(updates) + 1}"
        Path(str(stem) + ".source").write_bytes(newer)
        if name in self.candidates:
            self.candidates[name] = candidate
            (self.attempt_dir / (name + ".candidate")).write_bytes(candidate)
        self.remote[name] = newer
        self.refs[name] = current
        updates.append({"previous": asdict(ref), "current": asdict(current)})
        if self.state["files"][name]["status"] == "verified":
            self.state["files"][name]["saved"] = asdict(current)
        self._save()

    def _prepare_wi_inputs(self, kind, deployments):
        # Both paths are consumed in the field. Preserve recorded rows from either.
        names = (WI_INPUT_NAMES[kind], WI_STAGED_NAMES[kind])
        staged = self.local_folder / WI_STAGED_NAMES[kind]
        sources = [staged.read_bytes()] if staged.is_file() else []
        for name in names:
            self._snapshot(name)
            if self.remote[name] is not None:
                sources.append(self.remote[name])
        merged = merge_wi_input(None, deployments)
        for source in sources:
            merged = merge_wi_input(source, list(csv.DictReader(io.StringIO(merged.decode("utf-8-sig")))))
        for name in names:
            self.candidates[name] = merged

    def prepare_current_qc(self, paths):
        """Publish current QC documents conditionally beside the historical snapshots."""
        for path in paths:
            name = path.relative_to(self.local_folder).as_posix()
            if name in {"qc/box_upload_manifest.json", "qc/deployment_summary.txt"} or name.startswith("qc/lookup_snapshot/"):
                self._snapshot(name)
                self.candidates[name] = path.read_bytes()
        self._save()

    def begin_verification_receipt(self):
        """Reserve the current receipt for this batch before any media moves."""
        from cassn.box.receipts import RECEIPT
        self.state["publish_final_receipt"] = True
        self._snapshot(RECEIPT)
        marker = {"verified": False, "status": "pending", "batch_attempt_id": self.attempt_id}
        self.candidates[RECEIPT] = json.dumps(marker, indent=2).encode()
        self._publish_files([RECEIPT])

    def publish_wi_inputs(self):
        """Expose manual inputs before media transfer; WI owns the later upload records."""
        names = [name for name in (*WI_INPUT_NAMES.values(), *WI_STAGED_NAMES.values()) if name in self.candidates]
        if names:
            self._publish_files(names)

    def publish(self):
        self.state["status"] = "publishing"
        self._save()
        self._publish_files(self.candidates)
        self.state["status"] = ("awaiting_provenance" if not self.provenance and
                                any(name in self.candidates for name in CSV_NAMES) else "published")
        self._save()

    def _publish_files(self, names):
        for name in names:
            if self.state["files"][name]["status"] == "verified":
                continue
            self._recheck()
            parent = self._parent(name, create=True)
            self.state["files"][name]["status"] = "write_pending"
            self._save()
            for attempt in range(3):
                ref = self.refs[name]
                payload = self.candidates[name]
                expected_id = ref.file_id if ref else None
                error = None
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
                        expected_id = result.entries[0].id
                        self.store._verify_write(expected_id, payload, parent, Path(name).name)
                except Exception as exc:
                    error = exc  # A lost acknowledgement can still be proved by read-back.
                file_id = self._find(parent, Path(name).name)
                if not file_id or (expected_id and file_id != expected_id):
                    raise MetadataConflict(f"Publication unconfirmed for {name}: {error or 'changed identity'}")
                current = self.store.snapshot(file_id)
                actual = self.store.read(current)
                if current.parent_id != parent or current.name != Path(name).name:
                    raise MetadataConflict(f"Saved content changed during verification: {name}")
                if actual == payload:
                    self.remote[name] = actual
                    break
                if name == "image_file_metadata.csv" and expected_id and (not ref or current != ref):
                    # A stamp can win before If-Match, or after our write but before read-back.
                    try:
                        self._accept_image_update(ref or current, current, payload, actual)
                    except MetadataConflict:
                        if ref and error and attempt < 2:
                            try:
                                self._accept_image_update(ref, current, self.remote[name], actual)
                            except MetadataConflict:
                                pass
                            else:
                                continue
                    else:
                        break
                raise MetadataConflict(f"Publication conflict/unconfirmed for {name}: {error or 'changed content'}")
            self.refs[name] = current
            self.state["files"][name].update(status="verified", saved=asdict(current))
            self._save()
        self._recheck()
