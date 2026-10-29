#!/usr/bin/env python3
"""Recoverable local camera-clock correction, before Box upload.

``prepare --source CARD --output PACKAGE`` creates verified ingest copies
without a session and can run alongside an unrelated FDM upload. The source
card stays unchanged. The deployment mode described below requires closing FDM.

Close FDM first. --dry-run lists every selected image and writes nothing.
Apply uses per-file copies and a durable journal beside the deployment, retaining
originals. Repeat the exact command to resume or report already complete; use
--new-operation NAME only for a deliberate additional correction after completion.
--only-year selects source EXIF years, not a retry mechanism. --scratch selects
fast temporary working space; durable originals/corrected evidence remain next
to the deployment. This does not change timezone offsets or remote Box objects.
"""
from __future__ import annotations

import argparse
import json
import hashlib
import os
import re
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from uuid import uuid4
import shutil
import subprocess
import sys
from pathlib import Path

# Make the cassn package importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cassn.config import LOCAL_DATA_DIR, LOCK_DIR
from cassn.box.publication import atomic_json as _atomic_json
from cassn.box.status import has_box_upload_history
from cassn.core.ingest_safety import require_no_ingest_holds
from cassn.core.hashing import sha256_sha1
from cassn.core.image_metadata import camera_timestamp_fields, extract_exif_data
from cassn.core.inventory import refresh_legacy_device_manifest, inventory_storage_relpath, index_inventory_by_storage_relpath
from cassn.core.quality_control import (
    append_qc_report, camera_date_discrepancies, check_camera_timezones,
    validate_datetimes,
)
from cassn.export.metadata_csv import write_metadata_outputs
from cassn.lookups import LookupTables


def atomic_json(path, value):
    """Flush the containing directory as well as the journal contents."""
    _atomic_json(path, value)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def build_shift(args) -> str:
    """Return exiftool's date-shift argument, e.g. '-AllDates+=1:0:0 0:0:0'."""
    op = "-=" if args.subtract else "+="
    comps = f"{args.years}:{args.months}:{args.days} {args.hours}:{args.minutes}:{args.seconds}"
    return f"-AllDates{op}{comps}"


def exiftool_shift(path: Path, shift_arg: str, only_year: int | None = None) -> int:
    """Shift exactly one prepared copy; any tool error or count mismatch fails."""
    cmd = ["exiftool", shift_arg, "-overwrite_original", str(path.resolve())]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    counts = re.findall(r"^\s*(\d+) image files? updated\s*$", out.stdout, re.MULTILINE)
    if counts != ["1"]:
        raise ValueError(f"ExifTool did not confirm exactly one changed image: {out.stdout} {out.stderr}")
    return 1


def reinventory(entries: list, work_dir: Path) -> int:
    """Recompute hashes, size, and recorded_datetime for each entry from work_dir."""
    updated = 0
    for i, e in enumerate(entries, 1):
        # Deployment-root mode handles split layouts. Keep the historical
        # device-directory call for callers that supply a flat row only.
        relative = inventory_storage_relpath(e)
        filename = str(e.get("new_filename", ""))
        if not relative and (not filename or Path(filename).name != filename or filename in {".", ".."}):
            raise ValueError("Invalid legacy image filename")
        fp = checked_path(work_dir, relative or filename)
        sha256, sha1 = sha256_sha1(fp)
        e["file_hash_sha256"] = sha256
        e["file_hash_sha1"] = sha1
        e["file_size_bytes"] = fp.stat().st_size
        exif, _ = extract_exif_data(fp)
        # Explicit reprocessing replaces the full resolution, including clearing
        # stale UTC/local values if the source clock can no longer be resolved.
        e.update(camera_timestamp_fields(exif))
        updated += 1
        if i % 5000 == 0:
            print(f"    …re-inventoried {i}/{len(entries)}")
    return updated


def checked_path(root: Path, relative: str) -> Path:
    path = root / relative
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"Path escapes deployment: {relative}") from exc
    cursor = path
    while cursor != root.parent:
        if cursor.is_symlink():
            raise ValueError(f"Symlink is not a correction target: {cursor}")
        if cursor == root:
            break
        cursor = cursor.parent
    if not path.is_file():
        raise ValueError(f"Inventoried image is missing: {relative}")
    return path


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_digest(path: Path) -> str:
    return sha256_sha1(path)[0]


def clock_value(path: Path) -> str:
    exif, _ = extract_exif_data(path)
    value = exif.get("DateTimeOriginal") or exif.get("DateTime") or ""
    try:
        datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Unreadable camera date in {path}") from exc
    return value


def plan_images(dep, session, devices, only_year):
    indexed = index_inventory_by_storage_relpath(session["file_inventory"])
    if len(indexed) != len(session["file_inventory"]):
        raise ValueError("Every inventory row needs a complete unique stored path")
    selected = []
    for dev in devices:
        if not dev or Path(dev).name != dev or dev in {".", ".."}:
            raise ValueError(f"Invalid device: {dev}")
        entries = [(path, e) for path, e in indexed.items()
                   if e.get("device_label") == dev and e.get("file_type") == "image"]
        if not entries:
            raise ValueError(f"No inventoried images for {dev}")
        expected = {path for path, _ in entries}
        dev_dir = dep / "raw_data" / dev
        actual = set()
        for path in dev_dir.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"Symlink in selected device: {path}")
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg"} and not path.name.startswith("."):
                actual.add(path.relative_to(dep).as_posix())
        if actual != expected:
            raise ValueError(f"Device {dev} inventory/files differ: missing={sorted(expected-actual)}, untracked={sorted(actual-expected)}")
        for relative, entry in sorted(entries):
            path = checked_path(dep, relative)
            before = clock_value(path)
            if only_year is not None and int(before[:4]) != only_year:
                continue
            sha256, sha1 = sha256_sha1(path)
            stored = entry.get("file_hash_sha256")
            if stored and stored != sha256:
                raise ValueError(f"Stored checksum differs from current file: {relative}")
            selected.append({"path": relative, "before_clock": before, "before_sha256": sha256,
                             "before_sha1": sha1, "before_size": path.stat().st_size, "status": "pending"})
    if not selected:
        raise ValueError("No images match the requested correction; nothing was changed")
    for index, item in enumerate(selected):
        item["index"] = index
    return selected


def validate_membership(dep, session, devices):
    indexed = index_inventory_by_storage_relpath(session["file_inventory"])
    for dev in devices:
        expected = {relative for relative, row in indexed.items()
                    if row.get("device_label") == dev and row.get("file_type") == "image"}
        actual = set()
        for path in (dep / "raw_data" / dev).rglob("*"):
            if path.is_symlink():
                raise ValueError(f"Symlink in selected device: {path}")
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg"} and not path.name.startswith("."):
                actual.add(path.relative_to(dep).as_posix())
        if actual != expected:
            raise ValueError(f"Selected device file set changed: {dev}")


def atomic_copy(source: Path, target: Path):
    """Stage on the target filesystem, flush, then replace one file atomically."""
    for part in (target, *target.parents):
        if part.is_symlink():
            raise ValueError(f"Unsafe copy destination: {part}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name("." + target.name + ".clock-" + uuid4().hex)
    try:
        shutil.copy2(source, temporary)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        descriptor = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def correction_lock(dep):
    # Same deployment-path key and global lock directory as hosted FDM windows.
    from PySide6.QtCore import QLockFile
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    key = "deployment-path:" + os.path.normcase(str(dep.resolve()))
    lock = QLockFile(str(LOCK_DIR / (digest_bytes(key.encode()) + ".lock")))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        raise ValueError("Deployment is open or another correction is running; close FDM first")
    try:
        yield
    finally:
        lock.unlock()


def operation_home(dep):
    home = dep.parent / ".cassn-clock-corrections" / digest_bytes(str(dep).encode())
    for part in (home.parent, home):
        if part.is_symlink():
            raise ValueError(f"Unsafe journal location: {part}")
    return home


def validate_session(dep, journal):
    current = digest_bytes((dep / "session.json").read_bytes())
    allowed = {journal["original_session_sha256"], journal.get("final_session_sha256")}
    if current not in allowed:
        raise ValueError("session.json changed outside this operation; retain journal and review before resuming")


def apply_image(dep, op, journal, item, entry, shift, scratch):
    path = checked_path(dep, item["path"])
    original = op / "originals" / (str(item["index"]) + ".jpg")
    candidate = op / "corrected" / original.name
    current = file_digest(path)
    if item["status"] not in {"pending", "prepared", "applied"}:
        raise ValueError("Invalid file correction state")
    if item["status"] == "applied" and current != item["after_sha256"]:
        raise ValueError(f"Previously corrected file changed: {item['path']}")
    if item["status"] == "pending":
        if current != item["before_sha256"]:
            raise ValueError(f"Unplanned file change: {item['path']}")
        if original.exists():
            if file_digest(original) != item["before_sha256"]:
                raise ValueError("Retained original changed; stop for review")
        else:
            atomic_copy(path, original)
        if file_digest(original) != item["before_sha256"]:
            raise ValueError("Original backup did not verify")
        with tempfile.TemporaryDirectory(prefix="cassn-clock-", dir=scratch) as temporary:
            working = Path(temporary) / "image.jpg"
            shutil.copy2(original, working)
            if exiftool_shift(working, shift, None) != 1:
                raise ValueError("ExifTool did not update exactly one image")
            if file_digest(working) == item["before_sha256"] or clock_value(working) == item["before_clock"]:
                raise ValueError("ExifTool reported success but the image/clock did not change")
            corrected_entry = deepcopy(entry)
            sha256, sha1 = sha256_sha1(working)
            exif, _ = extract_exif_data(working)
            corrected_entry.update(file_hash_sha256=sha256, file_hash_sha1=sha1,
                                   file_size_bytes=working.stat().st_size, **camera_timestamp_fields(exif))
            atomic_copy(working, candidate)
        item.update(status="prepared", after_sha256=sha256, entry=corrected_entry)
        atomic_json(op / "items" / f"{item['index']}.json", item)
    if file_digest(original) != item["before_sha256"] or file_digest(candidate) != item["after_sha256"]:
        raise ValueError("Retained original or corrected copy changed; stop for review")
    current = file_digest(path)
    if current == item["before_sha256"]:
        atomic_copy(candidate, path)
    elif current != item["after_sha256"]:
        raise ValueError(f"Unrecognized current bytes: {item['path']}")
    if file_digest(path) != item["after_sha256"]:
        raise ValueError("Replacement did not verify")
    item["status"] = "applied"
    atomic_json(op / "items" / f"{item['index']}.json", item)


def refresh_date_qc(dep, session, devices):
    """Recheck corrected inventory; preserve prior findings in QC history."""
    metadata = session["metadata"]
    start = metadata.get("deployment_event_start_date", "")
    end = metadata.get("deployment_event_end_date", "")
    try:
        if datetime.strptime(start, "%Y-%m-%d") > datetime.strptime(end, "%Y-%m-%d"):
            raise ValueError("Reversed deployment dates")
    except (TypeError, ValueError):
        for device in devices:
            append_qc_report(dep, "camera_date_review", device, "error",
                             "Cannot refresh camera date QC: valid deployment start and collection dates are required.")
            append_qc_report(dep, "temporal_plausibility", device, "warning",
                             "Cannot refresh temporal QC: valid deployment dates are required.")
        return
    for device in devices:
        entries = [e for e in session["file_inventory"]
                   if e.get("device_label") == device and e.get("file_type") == "image"]
        if not entries:
            raise ValueError(f"No camera images to recheck: {device}")
        # The check functions ignore unreadable timestamps; do not turn that
        # absence of evidence into a passing result.
        for entry in entries:
            datetime.fromisoformat(entry.get("recorded_datetime") or "")
        findings = camera_date_discrepancies(entries, start, end)
        if findings:
            finding = findings[0]
            append_qc_report(dep, "camera_date_review", device, "error",
                             f"{finding['before_count']} before start; {finding['after_count']} after collection date. "
                             "Operator review required after clock correction.")
        else:
            append_qc_report(dep, "camera_date_review", device, "pass",
                             "Rechecked after clock correction: no camera dates outside the deployment window.")
        warnings = validate_datetimes(entries, start, end)
        append_qc_report(dep, "temporal_plausibility", device,
                         "warning" if warnings else "pass",
                         "\n".join(warnings) if warnings else "Rechecked after clock correction: OK.")


def execute(args):
    dep = Path(args.deployment).absolute()
    if dep.is_symlink() or dep.resolve() != dep:
        raise ValueError("Deployment must be a real, canonical local directory")
    session_path = dep / "session.json"
    if session_path.is_symlink():
        raise ValueError("session.json must not be a symlink")
    source = session_path.read_bytes()
    session = json.loads(source)
    devices = sorted(set(args.devices))
    if len(devices) != len(args.devices):
        raise ValueError("Duplicate device selection")
    shift = build_shift(args)
    request = {"deployment": str(dep), "devices": devices, "shift": shift, "only_year": args.only_year,
               "operation_label": args.new_operation}
    home = operation_home(dep)
    active_path = home / "active.json"
    active_bytes = active_path.read_bytes() if active_path.exists() else None
    journal = None
    if active_path.exists():
        active = json.loads(active_path.read_text())
        operation_id = active["operation_id"]
        if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
            raise ValueError("Invalid correction journal identity")
        op = home / operation_id
        for path in (op, op / "items", op / "originals", op / "corrected", op / "journal.json"):
            if path.is_symlink():
                raise ValueError(f"Unsafe journal path: {path}")
        journal = json.loads((op / "journal.json").read_text())
        if journal.get("schema_version") != 1 or journal.get("status") not in {"applying", "finalizing", "complete"}:
            raise ValueError("Unsupported correction journal")
        for index, planned in enumerate(journal["files"]):
            if planned["index"] != index:
                raise ValueError("Invalid journal file index")
            saved_path = op / "items" / f"{index}.json"
            if saved_path.exists():
                saved = json.loads(saved_path.read_text())
                if any(saved.get(key) != planned.get(key) for key in
                       ("index", "path", "before_sha256", "before_clock", "before_size", "before_sha1")):
                    raise ValueError("Saved file evidence does not match the operation plan")
                journal["files"][index] = saved
        if journal["request"] != request:
            if journal["status"] != "complete":
                raise ValueError("An unfinished correction exists; resume its exact command first")
            if not args.new_operation:
                raise ValueError("A completed correction exists; use --new-operation for a deliberate additional correction")
            for past in home.glob("*/journal.json"):
                previous = json.loads(past.read_text())
                if previous.get("request", {}).get("operation_label") == args.new_operation:
                    raise ValueError("That operation label was already used; retain the earlier journal for review")
            journal = None
        else:
            validate_session(dep, journal)
            validate_membership(dep, session, devices)
            for item in journal["files"]:
                current = file_digest(checked_path(dep, item["path"]))
                if current not in {item["before_sha256"], item.get("after_sha256")}:
                    raise ValueError(f"File changed outside correction: {item['path']}")
            if journal["status"] == "complete":
                for relative, digest in journal["final_artifacts"].items():
                    if file_digest(checked_path(dep, relative)) != digest:
                        raise ValueError(f"Completed metadata changed: {relative}")
                if any(file_digest(checked_path(dep, i["path"])) != i["after_sha256"] for i in journal["files"]):
                    raise ValueError("A completed correction's media changed; retain journal for review")
                if not args.dry_run:
                    with correction_lock(dep):
                        validate_session(dep, journal)
                        refresh_date_qc(dep, session, devices)
                        append_qc_report(dep, "ingest_safety", "clock_correction", "pass", f"Clock correction complete; originals retained in {op}")
                print(f"Already complete: {op}. No images shifted again.")
                return 0
    if journal is None:
        files = plan_images(dep, session, devices, args.only_year)
    else:
        files = journal["files"]
    print(f"Offset: {shift}; {len(files)} exact image target(s). Clock shift, not timezone correction.")
    for item in files:
        print(f"  {item['path']} | {item['before_clock']} | {item['status']}")
    if args.dry_run:
        print("Dry run complete — no files written. Apply revalidates the full selection.")
        return 0
    with correction_lock(dep):
        # Re-read under the shared FDM lease before any mutation.
        if (active_path.read_bytes() if active_path.exists() else None) != active_bytes:
            raise ValueError("Correction journal changed while planning; rerun the command")
        if session_path.read_bytes() != source:
            raise ValueError("Session changed while planning")
        validate_membership(dep, session, devices)
        for output in ("image_file_metadata.csv", "audio_file_metadata.csv", "deployment_event_record.json", "qc"):
            if (dep / output).is_symlink():
                raise ValueError(f"Unsafe output path: {output}")
        try:
            require_no_ingest_holds(dep)
        except ValueError:
            report = json.loads((dep / "qc/qc_report.json").read_text())
            errors = [entry for entry in report["current_state"]
                      if entry.get("severity") == "error" and entry.get("check") in {"ingest_safety", "card_ingest"}]
            if journal is None or any(entry.get("check") != "ingest_safety" or entry.get("_device") != "clock_correction" for entry in errors):
                raise
        lookups = LookupTables.load(LOCAL_DATA_DIR)
        selected = {item["path"] for item in files}
        prospective = [dict(e, file_hash_sha256="") if inventory_storage_relpath(e) in selected else dict(e)
                       for e in session["file_inventory"]]
        write_metadata_outputs(dep, session["metadata"], prospective, session["devices"], lookups, dry_run=True)
        if has_box_upload_history(dep):
            raise ValueError("Box upload history exists; this local pre-upload utility cannot change published media")
        if journal is None:
            operation_id = uuid4().hex
            op = home / operation_id
            op.mkdir(parents=True)
            atomic_copy(session_path, op / "original_session.json")
            journal = {"schema_version": 1, "request": request, "status": "applying", "files": files,
                       "original_session_sha256": digest_bytes(source)}
            atomic_json(op / "journal.json", journal)
            atomic_json(active_path, {"operation_id": operation_id})
        validate_session(dep, journal)
        append_qc_report(dep, "ingest_safety", "clock_correction", "error", f"Clock correction unfinished; resume exact command. Journal: {op}")
        baseline = json.loads((op / "original_session.json").read_text())
        if digest_bytes((op / "original_session.json").read_bytes()) != journal["original_session_sha256"]:
            raise ValueError("Original session backup changed")
        indexed = index_inventory_by_storage_relpath(baseline["file_inventory"])
        scratch = Path(args.scratch).absolute() if args.scratch else None
        if scratch:
            if scratch.is_symlink() or scratch.resolve() != scratch or scratch == dep or dep in scratch.parents:
                raise ValueError("Scratch must be a real directory outside the deployment")
            scratch.mkdir(parents=True, exist_ok=True)
        for item in journal["files"]:
            validate_session(dep, journal)
            apply_image(dep, op, journal, item, indexed[item["path"]], shift, scratch)
        updated = deepcopy(baseline)
        updated_by_path = index_inventory_by_storage_relpath(updated["file_inventory"])
        for item in journal["files"]:
            updated_by_path[item["path"]].update(item["entry"])
        final_bytes = json.dumps(updated, indent=2).encode()
        journal["final_session_sha256"] = digest_bytes(final_bytes)
        journal["status"] = "finalizing"
        atomic_json(op / "journal.json", journal)
        validate_session(dep, journal)
        # Use atomic_json's exact serialization for the recorded final digest.
        atomic_json(session_path, updated)
        for dev in devices:
            entries = [e for e in updated["file_inventory"] if e.get("device_label") == dev]
            dev_dir = dep / "raw_data" / dev
            manifest = dev_dir / f"{dev}_manifest.json"
            if manifest.is_symlink():
                raise ValueError(f"Unsafe legacy manifest: {manifest}")
            if manifest.exists() and not refresh_legacy_device_manifest(dev, dev_dir, entries):
                raise ValueError(f"Legacy manifest refresh failed: {manifest}")
            severity, message = check_camera_timezones(entries)
            append_qc_report(dep, "camera_timezone_consistency", dev, severity, message)
        write_metadata_outputs(dep, updated["metadata"], updated["file_inventory"], updated["devices"], lookups, log=print)
        artifacts = ["image_file_metadata.csv", "audio_file_metadata.csv", "deployment_event_record.json"]
        artifacts.extend(f"raw_data/{dev}/{dev}_manifest.json" for dev in devices
                         if (dep / "raw_data" / dev / f"{dev}_manifest.json").exists())
        journal["final_artifacts"] = {relative: file_digest(checked_path(dep, relative)) for relative in artifacts}
        refresh_date_qc(dep, updated, devices)
        journal["status"] = "complete"
        atomic_json(op / "journal.json", journal)
        append_qc_report(dep, "ingest_safety", "clock_correction", "pass", f"Clock correction complete; originals retained in {op}")
        print(f"Done: {len(files)} images corrected and verified. Originals and journal retained: {op}")
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "prepare":
        from utils.prepare_camera_clock import main as prepare_main
        return prepare_main(sys.argv[2:])
    ap = argparse.ArgumentParser(description="Recoverable local camera-clock correction; close FDM first.")
    ap.add_argument("--deployment", required=True)
    ap.add_argument("--devices", nargs="+", required=True)
    for component in ("years", "months", "days", "hours", "minutes", "seconds"):
        ap.add_argument("--" + component, type=int, default=0)
    ap.add_argument("--subtract", action="store_true", help="Subtract offset; default adds")
    ap.add_argument("--only-year", type=int, help="Select original EXIF year (not retry protection)")
    ap.add_argument("--scratch", help="Fast temporary working directory outside deployment")
    ap.add_argument("--dry-run", action="store_true", help="List exact targets; write nothing")
    ap.add_argument("--new-operation", metavar="NAME", help="Unique name for a deliberate additional correction after completion; reuse the same name on retry")
    args = ap.parse_args()
    if args.new_operation is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", args.new_operation):
        ap.error("--new-operation needs a short unique name using letters, numbers, underscores or hyphens")
    values = [getattr(args, part) for part in ("years", "months", "days", "hours", "minutes", "seconds")]
    if not any(values) or any(value < 0 for value in values):
        ap.error("Use nonnegative offset components, at least one nonzero; --subtract controls direction")
    return execute(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.SubprocessError, KeyError) as exc:
        print(f"Clock correction stopped: {exc}. Retain staged files and journal; resume the exact command after resolving the problem.", file=sys.stderr)
        raise SystemExit(1)
