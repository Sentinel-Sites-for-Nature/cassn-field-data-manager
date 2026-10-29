"""Durable local records of SoundHub transfer attempts, outside FLAC staging."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from cassn.soundhub.staging import project_root


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_value(value):
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"Unsupported transfer record value: {type(value).__name__}")


class TransferArchive:
    """One attempt, with manifest snapshots and an atomically updated journal.

    A hard process termination may leave status=running and finished_at=null.
    That is an incomplete attempt, never evidence of successful submission.
    """

    def __init__(self, directory: Path, record: dict):
        self.directory = directory
        self.record = record

    @classmethod
    def begin(cls, plan, submitter: str) -> "TransferArchive":
        root = plan.staging_root.parent / "transfers"
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%SZ")
        directory = root / f"{stamp}_{uuid4().hex[:8]}"
        directory.mkdir(parents=True, exist_ok=False)
        manifests = {}
        for name in ("deployment.csv", "recording.csv"):
            payload = (project_root(plan.staging_root) / name).read_bytes()
            with (directory / name).open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            manifests[name] = {
                "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)
            }
        (directory / "upload.log").touch(exist_ok=False)
        settings = plan.settings
        archive = cls(directory, {
            "schema_version": 1,
            "transfer_id": directory.name,
            "started_at": _now(),
            "finished_at": None,
            "status": "running",
            "phase": "upload",
            "submitter": submitter,
            "destination": f"s3://{settings['bucket']}/{settings['upload_prefix']}/{settings['project_short_name']}/",
            "staging_root": str(plan.staging_root),
            "box_metadata_access": "api" if plan.provenance.box_store else "drive",
            "soundhub_ingestion": "unconfirmed",
            "planned": {
                "recordings": len(plan.provenance.pending_keys),
                "deployments": sorted(plan.provenance.pending_deployment_ids),
                "events": sorted(plan.provenance.pending_event_ids),
                "bytes": plan.total_bytes,
                "objects": [{k: item[k] for k in ("key", "relative", "size")}
                            for item in plan.objects],
            },
            "manifests": manifests,
            "source_metadata": [{
                "path": str(document.path),
                "sha256_before": hashlib.sha256(document.source_bytes).hexdigest(),
                "box_file": document.box_file,
            } for document in plan.provenance.pending_documents],
            "upload": None,
            "verification": None,
            "provenance": None,
            "reports": [],
            "error": None,
        })
        archive.update()
        return archive

    def update(self, **fields) -> None:
        self.record.update(fields)
        self.record["updated_at"] = _now()
        payload = (json.dumps(self.record, indent=2, default=_json_value) + "\n").encode()
        fd, name = tempfile.mkstemp(prefix=".transfer-", suffix=".tmp", dir=self.directory)
        temp = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.directory / "transfer.json")
        finally:
            temp.unlink(missing_ok=True)
        self.write_summary()

    def write_summary(self) -> None:
        """Human-readable outcomes, including failures before Box reports exist."""
        record = self.record
        planned = record["planned"]
        verification = record.get("verification") or {}
        provenance = record.get("provenance")
        if is_dataclass(provenance):
            provenance = asdict(provenance)
        content = [
            "# SoundHub upload summary", "",
            f"- Transfer: `{record['transfer_id']}`",
            f"- Started (UTC): {record['started_at']}",
            f"- Finished (UTC): {record.get('finished_at') or 'Still running'}",
            f"- Workflow: {record['status']} ({record['phase']})",
            f"- Destination: `{record['destination']}`",
            f"- Batch: {planned['recordings']} recordings, "
            f"{len(planned['deployments'])} deployments, {len(planned['events'])} events",
            f"- Data: {planned['bytes'] / 1e9:.2f} GB",
            f"- S3 verification: {'PASSED' if verification.get('ok') else 'Not passed'} "
            f"({verification.get('present', 0)} / {verification.get('checked', 0)} objects)",
            f"- Box submission flags: {'Saved' if provenance else 'Not confirmed saved'}",
            f"- Box reports: {len(record.get('reports', []))} confirmed",
            "- SoundHub ingestion/processing: unconfirmed", "",
        ]
        if record.get("error"):
            content.extend(["## Error", "", record["error"], ""])
        if verification.get("ok") and record["status"] != "complete":
            content.extend(["S3 transfer is verified. Do not upload this batch again. "
                            "Restore the UC Box connection and repair submission records.", ""])
        content.extend(["## Deployment events", ""] +
                       [f"- `{event}`" for event in planned["events"]] + [""])
        fd, name = tempfile.mkstemp(prefix=".summary-", dir=self.directory)
        temp = Path(name)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write("\n".join(content))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.directory / "upload_summary.md")
        finally:
            temp.unlink(missing_ok=True)

    def finish(self, status: str, **fields) -> None:
        self.update(status=status, finished_at=_now(), **fields)
