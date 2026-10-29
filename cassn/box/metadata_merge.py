"""Conservative event metadata unions; remote facts/history own existing rows.

Ordinary ingestion is not a correction editor. Only the Box upload provenance
group may advance in the provenance pass. All other existing values survive.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
from pathlib import Path, PurePosixPath
from uuid import uuid4


CSV_NAMES = ("image_file_metadata.csv", "audio_file_metadata.csv")
SHARED_NAMES = (*CSV_NAMES, "deployment_event_record.json", "qc/qc_report.json")
BOX_HISTORY = ("is_uploaded_to_box", "box_uploader", "box_upload_datetime")


class MetadataConflict(ValueError):
    """No safe automatic merge exists; retain all local and remote evidence."""


def read_csv(payload: bytes) -> tuple[list[str], list[dict]]:
    try:
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig"), newline=""), strict=True)
        fields = reader.fieldnames or []
        if (not {"deployment_id", "filename", "file_hash_sha256"}.issubset(fields)
                or len(fields) != len(set(fields)) or any(not f for f in fields)):
            raise ValueError("missing or duplicate identity/hash/header columns")
        rows = list(reader)
        seen = set()
        for row in rows:
            if None in row or None in row.values():
                raise ValueError("row length differs from header")
            key = (row["deployment_id"], row["filename"])
            if (not all(key) or key in seen or PurePosixPath(key[1]).name != key[1]
                    or "\\" in key[1] or key[1] in {".", ".."}):
                raise ValueError(f"ambiguous or duplicate file identity {key}")
            if not re.fullmatch(r"[0-9a-fA-F]{64}", row["file_hash_sha256"]):
                raise ValueError(f"missing/invalid SHA-256 for {key}")
            seen.add(key)
        return fields, rows
    except (ValueError, UnicodeError, csv.Error) as exc:
        raise MetadataConflict(f"Cannot safely read metadata CSV: {exc}") from exc


def csv_bytes(fields, rows) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def stamp_box_provenance(path: Path, uploader: str, uploaded_at: str):
    """Stamp only this local batch, retaining all columns and the original on error."""
    fields, rows = read_csv(path.read_bytes())
    if not uploader or not uploaded_at:
        raise MetadataConflict("Uploader and upload time are required for Box provenance")
    fields += [field for field in BOX_HISTORY if field not in fields]
    for row in rows:
        row.update(dict(zip(BOX_HISTORY, ("True", uploader, uploaded_at))))
    temporary = path.with_name("." + path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(csv_bytes(fields, rows))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def merge_csv(remote: bytes | None, local: bytes, *, provenance=False) -> bytes:
    local_fields, incoming = read_csv(local)
    if remote is None:
        return csv_bytes(local_fields, incoming)
    fields, rows = read_csv(remote)
    fields = fields + [field for field in local_fields if field not in fields]
    indexed = {(row["deployment_id"], row["filename"]): row for row in rows}
    for new in incoming:
        key = (new["deployment_id"], new["filename"])
        old = indexed.get(key)
        if old is None:
            if provenance:
                raise MetadataConflict(f"Provenance cannot introduce an unpublished file: {key}")
            rows.append(dict(new))
            continue
        for field in ("file_hash_sha256", "file_hash_sha1", "file_size_bytes",
                      "original_filename", "storage_relpath", "relative_path"):
            left, right = old.get(field, ""), new.get(field, "")
            if left and right and (left.lower() != right.lower() if "hash" in field else left != right):
                raise MetadataConflict(f"Conflicting {field} for {key}; review file identity before upload")
        # New schema columns may be added; a blank existing value remains an
        # explicit remote value, not permission to replace a correction.
        for field in local_fields:
            if field not in old:
                old[field] = new[field]
        if provenance and str(old.get(BOX_HISTORY[0], "")).lower() not in {"true", "1", "yes"}:
            if str(new.get(BOX_HISTORY[0], "")).lower() != "true" or not all(new.get(f) for f in BOX_HISTORY[1:]):
                raise MetadataConflict(f"Incomplete Box provenance for {key}")
            old.update({field: new[field] for field in BOX_HISTORY})
    return csv_bytes(fields, [{field: row.get(field, "") for field in fields} for row in rows])


def read_object(payload: bytes) -> dict:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key}")
            result[key] = value
        return result
    try:
        value = json.loads(payload, object_pairs_hook=unique_pairs)
        if not isinstance(value, dict):
            raise ValueError("expected a JSON object")
        return value
    except (ValueError, UnicodeError) as exc:
        raise MetadataConflict(f"Cannot safely read shared JSON: {exc}") from exc


def merge_event(remote: bytes | None, local: bytes, *, file_count: int) -> bytes:
    new = read_object(local)
    old = read_object(remote) if remote is not None else {}
    for obj in (new, old):
        if obj and (not isinstance(obj.get("deployment_info"), dict)
                    or not isinstance(obj.get("devices"), list)):
            raise MetadataConflict("Unsupported event-record schema; explicit reconciliation required")
    def event_info(info):
        # Recognized historical names are aliases, not missing evidence or
        # permission to ignore a different site/date. Retain the original keys.
        normalized = dict(info)
        for canonical, aliases in (
            ("site_name", ("site_full_name", "reserve_name")),
            ("site_short_name", ("site",)),
            ("deployment_event_start_date", ("deployment_start",)),
            ("deployment_event_end_date", ("deployment_end",)),
        ):
            values = {info[key] for key in (canonical, *aliases) if info.get(key)}
            if len(values) > 1:
                raise MetadataConflict(f"Contradictory event aliases for {canonical}")
            if values:
                normalized[canonical] = values.pop()
        return normalized
    new_info = event_info(new["deployment_info"])
    old_info = event_info(old["deployment_info"]) if old else {}
    if old:
        for field in ("deployment_event_id", "site_name", "deployment_event_end_date"):
            if not old_info.get(field) or old_info.get(field) != new_info.get(field):
                raise MetadataConflict(f"Event record disagrees on {field}")
        for field in ("organization", "site_short_name", "deployment_event_start_date"):
            if old_info.get(field) and new_info.get(field) and old_info[field] != new_info[field]:
                raise MetadataConflict(f"Event record disagrees on {field}")
    devices = {}
    for row in old.get("devices", []) + new["devices"]:
        if not isinstance(row, dict) or not row.get("device_label"):
            raise MetadataConflict("Device record has no unambiguous label")
        label = row["device_label"]
        if label in devices:
            previous = devices[label]
            if any(previous[field] != row[field] for field in previous.keys() & row.keys()):
                raise MetadataConflict(f"Conflicting deployment mapping for {label}")
            if previous.get("deployment_id") != row.get("deployment_id"):
                raise MetadataConflict(f"Legacy deployment mapping needs review for {label}")
            devices[label] = {**row, **previous}
        else:
            devices[label] = dict(row)
    result = {**new, **old, "deployment_info": {**new_info, **old_info},
              "devices": list(devices.values()), "file_count": file_count}
    return (json.dumps(result, indent=2) + "\n").encode()


def merge_qc(remote: bytes | None, local: bytes) -> bytes:
    from cassn.core.quality_control import _compute_qc_current_state

    new = read_object(local)
    old = read_object(remote) if remote is not None else {}
    def history(obj):
        h = obj.get("history", {"session_checks": obj.get("session_checks", []), "devices": obj.get("devices", {})})
        if (not isinstance(h, dict) or not isinstance(h.get("session_checks"), list)
                or not isinstance(h.get("devices"), dict)):
            raise MetadataConflict("Unsupported QC history; explicit reconciliation required")
        for entries in [h["session_checks"], *h["devices"].values()]:
            if not isinstance(entries, list) or any(not isinstance(e, dict) or not {"check", "timestamp"}.issubset(e) for e in entries):
                raise MetadataConflict("Malformed QC history entries")
        return h
    a, b = history(old), history(new)
    def union(left, right):
        seen, result = set(), []
        for item in left + right:
            key = json.dumps(item, sort_keys=True)
            if key not in seen:
                seen.add(key)
                result.append(item)
        return sorted(result, key=lambda e: e["timestamp"])
    sessions = union(a["session_checks"], b["session_checks"])
    devices = {label: union(a["devices"].get(label, []), b["devices"].get(label, []))
               for label in a["devices"].keys() | b["devices"].keys()}
    result = {**new, **old, "history": {**b, **a, "session_checks": sessions, "devices": devices},
              "current_state": _compute_qc_current_state(sessions, devices)}
    return (json.dumps(result, indent=2) + "\n").encode()
