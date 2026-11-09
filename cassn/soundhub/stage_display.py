"""Read-only evidence for SoundHub staging previews and completion summaries."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

from cassn.soundhub.export import build_recording_rows, event_staging_summary, read_bd_audio_rows
from cassn.soundhub.provenance import _submitted_state
from cassn.soundhub.staging import project_root
from cassn.soundhub.upload import load_soundhub_config, project_prefix


def manifest_rows(staging_root):
    """Index both project CSVs before and after staging. Missing files have no rows."""
    result = {}
    for name, key in (("deployment.csv", "deployment_id"), ("recording.csv", "path")):
        path = project_root(staging_root) / name
        rows = {}
        if path.exists():
            with path.open(encoding="utf-8-sig", newline="") as stream:
                for row in csv.DictReader(stream):
                    identity = row[key]
                    if not identity or identity in rows or None in row:
                        raise ValueError(f"Cannot identify a unique row in {path.name}")
                    rows[identity] = row
        result[name] = rows
    return result


def manifest_changes(before, after):
    changes = {}
    for name, previous in before.items():
        current = after[name]
        shared = previous.keys() & current.keys()
        changes[name] = dict(added=len(current.keys() - previous.keys()),
                             updated=sum(previous[k] != current[k] for k in shared),
                             removed=len(previous.keys() - current.keys()))
    return changes


def _verified_paths(staging_root, event, expected):
    """Match verified landing-zone objects to this event's exact archived recording rows."""
    config = load_soundhub_config()
    prefix = project_prefix(config)
    destination = f"s3://{config['bucket']}/{prefix}/"
    confirmed = set()
    for path in (staging_root.parent / "transfers").glob("*/transfer.json"):
        receipt = json.loads(path.read_bytes())
        planned = receipt.get("planned") or {}
        if (receipt.get("destination") != destination or event not in planned.get("events", [])
                or (receipt.get("verification") or {}).get("ok") is not True):
            continue
        objects = {item["relative"] for item in planned.get("objects", [])
                   if item.get("relative") in expected and item.get("key") == f"{prefix}/{item['relative']}"}
        if not objects:
            continue
        payload = (path.parent / "recording.csv").read_bytes()
        fingerprint = receipt.get("manifests", {}).get("recording.csv", {}).get("sha256")
        if hashlib.sha256(payload).hexdigest() != fingerprint:
            raise ValueError("Archived SoundHub recording manifest differs from its transfer record.")
        archived = csv.DictReader(io.StringIO(payload.decode("utf-8-sig"), newline=""))
        confirmed.update(r["path"] for r in archived if r["path"] in objects and r == expected[r["path"]])
    return confirmed


def event_preview(event_folder, staging_root):
    event_folder, staging_root = Path(event_folder), Path(staging_root)
    audio = read_bd_audio_rows(event_folder)
    expected = {r["path"]: r for r in build_recording_rows(audio)}
    marked = {f"{row['deployment_id']}/{Path(row['filename']).with_suffix('.flac').name}"
              for row in audio if _submitted_state(row)[0] == "submitted"}
    uploaded = marked | _verified_paths(staging_root, event_folder.name, expected)
    matched = event_staging_summary(event_folder, staging_root).matched_paths
    return dict(uploaded=len(uploaded), staged=len(matched - uploaded),
                waiting=len(expected.keys() - matched - uploaded))


def batch_bytes(staging_root):
    """Size of the cumulative batch: manifest files plus their referenced FLACs."""
    root = project_root(staging_root)
    manifests = [root / name for name in ("deployment.csv", "recording.csv")]
    total = sum(path.stat().st_size for path in manifests if path.is_file())
    if not manifests[1].exists():
        return total
    with manifests[1].open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            rel = Path(row["path"])
            if rel.is_absolute() or len(rel.parts) != 2 or ".." in rel.parts or rel.suffix.lower() != ".flac":
                raise ValueError("Invalid SoundHub recording path")
            file = root / rel
            if file.is_symlink() or file.parent.is_symlink() or not file.is_file():
                raise ValueError("Staged SoundHub recording missing or unavailable")
            total += file.stat().st_size
    return total
