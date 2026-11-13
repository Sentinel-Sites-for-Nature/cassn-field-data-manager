"""Deployment-specific evidence used by the app and command-line cleanup."""
from __future__ import annotations

import csv
from collections import Counter
from datetime import datetime
from pathlib import Path
import re

from cassn.config import CAMERA_DEVICE_TYPES, DEVICE_TYPES
from cassn.core.classification import classify_file
from cassn.core.wi_requests import camera_inventory
from cassn.soundhub.export import (
    _holds_audio, build_recording_rows, event_staging_summary, read_bd_audio_rows,
    validate_staging_manifests,
)
from cassn.soundhub.provenance import _submitted_state
from cassn.soundhub.stage_display import verified_recordings
from cassn.soundhub.staging import fragments_root, project_root, SoundHubStagingError


def platform_inventory(folder, session):
    """Cross-check type, deployment, source bytes, and size before judging applicability."""
    devices = {device[3]: device[2] for device in session["devices"]}
    if len(devices) != len(session["devices"]) or any(code not in DEVICE_TYPES for code in devices.values()):
        raise ValueError("The event contains an unknown or repeated device.")
    inventory = session["file_inventory"]
    exports = {}
    for filename in ("image_file_metadata.csv", "audio_file_metadata.csv"):
        path = folder / filename
        if path.exists():
            with path.open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream, strict=True)
                if len(set(reader.fieldnames or [])) != len(reader.fieldnames or []):
                    raise ValueError(f"Duplicate metadata column in {filename}")
                for row in reader:
                    key = row["filename"]
                    if key in exports or not key or None in row or None in row.values():
                        raise ValueError(f"Ambiguous filename in metadata: {key}")
                    exports[key] = filename, row
    birds = {}
    camera_rows = []
    for entry in inventory:
        name = entry["new_filename"]
        match = exports.pop(name, None)
        if not match:
            raise ValueError(f"Missing exported metadata for {name}")
        filename, row = match
        code = devices[entry["device_label"]]
        kind = classify_file(name)
        camera = code in CAMERA_DEVICE_TYPES and kind == "image"
        bird = code == "BD" and kind == "audio" and _holds_audio(entry)
        if (any(r.get("device_type") not in (None, "", code) or r.get("file_type") not in (None, "", kind)
                for r in (entry, row))
                or (row.get("file_size_bytes") and str(entry["file_size_bytes"]) != row["file_size_bytes"])
                or any(entry.get(k) and row.get(k) and entry[k] != row[k]
                       for k in ("deployment_id", "file_hash_sha256"))):
            raise ValueError(f"Inventory and metadata differ for {name}")
        if camera or bird:
            if (filename != ("image_file_metadata.csv" if camera else "audio_file_metadata.csv")
                    or row.get("file_type") != kind or row.get("device_type") != code
                    or row.get("file_hash_sha256") != entry.get("file_hash_sha256")
                    or not re.fullmatch(r"[a-f0-9]{64}", entry.get("file_hash_sha256") or "")
                    or not row.get("deployment_id")
                    or (not entry.get("deployment_id") and row.get("deployment_event_id") != folder.name)):
                raise ValueError(f"Missing matching deployment and source metadata for {name}")
        if camera:
            camera_rows.append(dict(entry, file_type=kind, deployment_id=row["deployment_id"]))
        if bird:
            birds[(row["deployment_id"], name)] = row["file_hash_sha256"]
    if exports:
        raise ValueError(f"Exported files missing from inventory: {', '.join(sorted(exports)[:5])}")
    cameras = camera_inventory(camera_rows, devices.keys())
    return cameras, birds


def _flac_header(path, rate):
    with path.open("rb") as stream:
        header = stream.read(42)
    if (len(header) != 42 or header[:4] != b"fLaC" or header[4] & 127
            or int.from_bytes(header[5:8], "big") != 34 or path.stat().st_size <= 42):
        raise ValueError(f"Invalid FLAC STREAMINFO header: {path}")
    audio = int.from_bytes(header[18:26], "big")
    if audio >> 44 != int(rate) or audio & ((1 << 36) - 1) == 0:
        raise ValueError(f"Invalid FLAC sample rate or sample count: {path}")


def soundhub_clearance(event, settings, profile, birds, read_box_audio, details):
    audio = read_bd_audio_rows(event)
    if {(row["deployment_id"], row["filename"]) for row in audio} != birds.keys():
        raise ValueError("Eligible SoundHub recordings differ from the event inventory.")
    expected = {row["path"]: row for row in build_recording_rows(audio)}
    if len(expected) != len(birds):
        raise ValueError("SoundHub recording paths are ambiguous.")
    for key, row in expected.items():
        ident = row["deployment_id"]
        if (not ident or Path(ident).name != ident or ident in {".", ".."}
                or Path(key).parts != (ident, Path(key).name)):
            raise ValueError(f"Unsafe SoundHub recording path: {key}")
    counts = {d: dict(expected=n, submitted=0, staged=0, missing=0, unchecked=n)
              for d, n in Counter(r["deployment_id"] for r in expected.values()).items()}
    details.update(deployments=counts, receipts=[])
    receipts = verified_recordings(settings["staging_root"], event.name, expected, settings, profile)
    submitted = set()
    if receipts:
        box_rows = {}
        for row in read_box_audio():
            key = (row["deployment_id"], row["filename"])
            if key in birds:
                if key in box_rows:
                    raise ValueError(f"Duplicate Box audio metadata row: {key}")
                box_rows[key] = row
        valid_sources = set()
        stamped = set()
        for key, digest in birds.items():
            relative = f"{key[0]}/{Path(key[1]).with_suffix('.flac').name}"
            row = box_rows.get(key, {})
            if row.get("file_hash_sha256") != digest:
                continue
            valid_sources.add(relative)
            if _submitted_state(row)[0] != "submitted":
                continue
            try:
                stamp = datetime.fromisoformat(row["soundhub_submission_datetime"].replace("Z", "+00:00"))
            except ValueError:
                continue
            if stamp.utcoffset() is not None:
                stamped.add(relative)
        for receipt, (complete, paths) in receipts.items():
            if paths <= valid_sources and (complete or paths <= stamped):
                submitted.update(paths)
                details["receipts"].append(str(receipt))
        for relative in submitted:
            count = counts[expected[relative]["deployment_id"]]
            count["submitted"] += 1
            count["unchecked"] -= 1
    needed = expected.keys() - submitted
    files = []
    warnings = []
    if needed:
        staging = Path(settings["staging_root"]).expanduser()
        if not staging.is_dir():
            raise OSError(f"Connect the staging drive to check recordings: {staging}")
        matched = event_staging_summary(event, staging).matched_paths
        missing = needed - matched
        for relative in missing:
            count = counts[expected[relative]["deployment_id"]]
            count["missing"] += 1
            count["unchecked"] -= 1
        candidates = needed & matched
        ids = {expected[p]["deployment_id"] for p in candidates}
        if ids:
            validate_staging_manifests(staging, deployment_ids=ids)
        project = project_root(staging)
        fragments = fragments_root(staging)
        for ident in ids:
            for path in (staging, project, fragments, fragments / ident, project / ident,
                         *(base / csv_name for base in (fragments / ident, project)
                           for csv_name in ("recording.csv", "deployment.csv"))):
                if path.is_symlink():
                    raise ValueError(f"SoundHub evidence is a symlink: {path}")
        for relative in sorted(candidates):
            path = project / relative
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Missing regular SoundHub file: {path}")
            _flac_header(path, expected[relative]["sample_rate"])
            counts[expected[relative]["deployment_id"]]["staged"] += 1
            counts[expected[relative]["deployment_id"]]["unchecked"] -= 1
            files.append(path)
        if missing:
            raise ValueError(f"Missing independent SoundHub evidence for {len(missing)} recording(s): {min(missing)}")
        try:
            validate_staging_manifests(staging)
        except SoundHubStagingError as exc:
            warnings.append(f"Selected recordings passed. Other SoundHub batch evidence needs review: {exc}")
    return files, warnings
