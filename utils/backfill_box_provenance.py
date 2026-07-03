#!/usr/bin/env python3
"""Backfill Box upload provenance into deployment metadata CSVs.

Why this exists
---------------
The GUI stamps ``is_uploaded_to_box`` / ``box_uploader`` / ``box_upload_datetime``
into ``image_file_metadata.csv`` and ``audio_file_metadata.csv`` right after a
successful Box upload, then re-uploads the two CSVs as a new Box version. A bug
(``name 'csv' is not defined`` — the ``csv`` module was never imported in
``wizard.py``) made that step throw and get swallowed into a warning, so every
deployment uploaded before the fix has its media safely on Box but its metadata
CSVs never recorded that fact. Separately, deployments uploaded by an older app
version have ``is_uploaded_to_box=True`` but an empty ``box_upload_datetime``
(that column was added later). This script fills both gaps.

It backfills *without* re-running the upload or the long post-upload re-hash
verification: it stamps the three columns and pushes a new version of just the
two CSVs to Box.

Two modes
---------
* **Local** (default): operate on a deployment folder on disk (e.g. the G-DRIVE
  staging copy). Reads the CSVs and ``qc/qc_report.json`` locally, stamps on
  disk, then uploads the new versions to Box. Requires the local staging folder
  to still exist.

* **Box-native** (``--from-box``): operate directly on the Box copies — no local
  staging needed. Reads each CSV straight from Box, derives the upload time from
  the Box files' own ``created_at`` (the server-side record of when the data
  landed), stamps a temp copy, uploads a new version, and SHA-1-verifies it
  against Box. This is the mode for cleaning up the archive after local copies
  are gone.

Timestamp semantics
-------------------
``box_upload_datetime`` records *when the data was actually uploaded to Box*, not
when this backfill runs. Sourced (in order):

1. ``--upload-datetime`` if given;
2. local mode — the ``box_upload`` completion entry in ``qc/qc_report.json``;
   Box mode — the earliest ``created_at`` among the deployment's Box files.

Naive timestamps are interpreted in ``--assume-tz`` (default America/Los_Angeles)
and written as UTC ISO-8601, matching the GUI's
``datetime.now(timezone.utc).isoformat()``.

Usage
-----
    # Local staging folder(s):
    python utils/backfill_box_provenance.py "/path/UC_QuailRidge_20260618"
    python utils/backfill_box_provenance.py --dry-run "/path/dep"      # preview
    python utils/backfill_box_provenance.py --no-upload "/path/dep"    # stamp locally only

    # Box-native — path is relative to the field_data root (year/reserve/deployment):
    python utils/backfill_box_provenance.py --from-box "2026/Quail Ridge Reserve/UC_QuailRidge_20260618"
    python utils/backfill_box_provenance.py --from-box --all           # sweep every deployment on Box

Re-running is safe: rows already carrying a ``box_upload_datetime`` are left
untouched unless ``--force`` is passed, and existing ``box_uploader`` names are
never overwritten (blanks are filled from the deployment's observer).
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Allow running as a plain script from anywhere: make the repo root importable
# so ``cassn`` resolves without needing ``python -m`` or an installed package.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cassn.config import AUDIO_FIELDS, IMAGE_FIELDS  # noqa: E402

# The two provenance-bearing CSVs and their canonical column schemas. Reusing the
# app's field lists keeps the rewritten header/column order identical to what the
# GUI writes, so a backfilled CSV is byte-shaped like a natively-stamped one.
PROVENANCE_CSVS: list[tuple[str, list[str]]] = [
    ("image_file_metadata.csv", IMAGE_FIELDS),
    ("audio_file_metadata.csv", AUDIO_FIELDS),
]
DEFAULT_ASSUME_TZ = "America/Los_Angeles"


# ---------------------------------------------------------------------------
# Shared stamping
# ---------------------------------------------------------------------------

def stamp_rows(rows: list[dict], observer: str, upload_iso: str, *, force: bool) -> int:
    """Stamp the provenance columns in-place; return how many rows changed.

    A row is stamped when its ``box_upload_datetime`` is empty (or always, under
    ``force``): sets ``is_uploaded_to_box=True`` and the datetime, and fills
    ``box_uploader`` from ``observer`` only when it's blank — an existing uploader
    name is never clobbered.
    """
    changed = 0
    for row in rows:
        if str(row.get("box_upload_datetime", "")).strip() and not force:
            continue
        row["is_uploaded_to_box"] = True
        row["box_upload_datetime"] = upload_iso
        if observer and (force or not str(row.get("box_uploader", "")).strip()):
            row["box_uploader"] = observer
        changed += 1
    return changed


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    """Write ``rows`` to ``path`` atomically, in the canonical column order."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Timestamp / metadata sourcing
# ---------------------------------------------------------------------------

def parse_override(value: str, assume_tz: ZoneInfo) -> datetime:
    """Parse a ``--upload-datetime`` override into an aware UTC datetime."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=assume_tz)
    return dt.astimezone(timezone.utc)


def load_deployment_metadata(dep: Path) -> dict:
    """Return ``{reserve_name, deployment_end, observer, ...}`` for a local deployment."""
    rec = dep / "deployment_event_record.json"
    if rec.exists():
        try:
            info = json.loads(rec.read_text()).get("deployment_info", {})
            if info.get("reserve_name"):
                return info
        except Exception:
            pass
    sess = dep / "session.json"
    if sess.exists():
        try:
            return json.loads(sess.read_text()).get("metadata", {})
        except Exception:
            pass
    return {}


def qc_upload_datetime(dep: Path, assume_tz: ZoneInfo) -> datetime | None:
    """Return the Box-upload completion time from a local ``qc/qc_report.json``."""
    qc = dep / "qc" / "qc_report.json"
    if not qc.exists():
        return None
    try:
        report = json.loads(qc.read_text())
    except Exception:
        return None
    checks = report.get("history", {}).get("session_checks", [])
    box = [e for e in checks if e.get("check") == "box_upload" and e.get("timestamp")]
    if not box:
        return None
    passes = [e for e in box if e.get("severity") == "pass"] or box
    latest = max(passes, key=lambda e: e["timestamp"])
    dt = datetime.fromisoformat(latest["timestamp"])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=assume_tz)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Local mode
# ---------------------------------------------------------------------------

def process_local(dep: Path, args, assume_tz: ZoneInfo, box_config) -> bool:
    """Backfill one local deployment folder. Returns True on success."""
    print(f"\n=== {dep.name} (local) ===")
    if not dep.is_dir():
        print("  ERROR: not a directory — skipped.")
        return False

    meta = load_deployment_metadata(dep)
    reserve, deployment_end = meta.get("reserve_name"), meta.get("deployment_end")
    observer = meta.get("observer", "")
    if not reserve or not deployment_end:
        print("  ERROR: no reserve_name/deployment_end in deployment_event_record.json / session.json — skipped.")
        return False

    if args.upload_datetime:
        upload_dt, source = parse_override(args.upload_datetime, assume_tz), "override"
    else:
        upload_dt, source = qc_upload_datetime(dep, assume_tz), "qc_report.json"
    if upload_dt is None:
        print("  ERROR: no box_upload time in qc_report.json. Pass --upload-datetime — skipped.")
        return False
    upload_iso = upload_dt.isoformat()
    print(f"  observer: {observer or '(blank)'} | upload time: {upload_iso} [{source}]")

    present, to_upload = 0, []
    for name, fields in PROVENANCE_CSVS:
        path = dep / name
        if not path.exists():
            continue
        present += 1
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        changed = stamp_rows(rows, observer, upload_iso, force=args.force)
        if changed and not args.dry_run:
            write_csv(path, fields, rows)
            to_upload.append(path)
        verb = "would stamp" if args.dry_run else "stamped"
        print(f"  {name}: {len(rows)} row(s) — {changed} {verb}")

    if not present:
        print("  No metadata CSVs found — nothing to do.")
        return False
    if args.dry_run:
        print("  [dry-run] no writes/upload.")
        return True
    if args.no_upload:
        print("  [--no-upload] stamped locally; not uploading.")
        return True
    if not to_upload:
        print("  Already complete — nothing to upload.")
        return True
    try:
        from cassn.box.client import BoxStorage
        storage = _box_storage(box_config)
        year_id = storage.find_or_create_folder(box_config.field_data_folder_id, str(deployment_end)[:4])
        reserve_id = storage.find_or_create_folder(year_id, reserve)
        deploy_id = storage.find_or_create_folder(reserve_id, dep.name)
        for p in to_upload:
            action = storage.upload_file_with_path(p, deploy_id, Path(p.name))
            print(f"  Box: {p.name} -> {action}")
    except Exception as e:
        print(f"  WARNING: stamped locally but Box upload failed: {e}")
        return False
    return True


# ---------------------------------------------------------------------------
# Box-native mode
# ---------------------------------------------------------------------------

def _box_storage(box_config):
    from cassn.box.auth import get_box_client
    from cassn.box.client import BoxStorage
    client = get_box_client(box_config)
    if not client:
        raise RuntimeError("Could not authenticate with Box")
    return BoxStorage(client)


def _resolve_box_path(storage, root_id: str, box_path: str):
    """Resolve a ``year/reserve/deployment`` path to its Box folder id (find-only)."""
    parent = root_id
    for part in [p for p in box_path.strip("/").split("/") if p]:
        child = storage.find_child_folder(parent, part)
        if child is None:
            return None
        parent = child
    return parent


def _enumerate_box_deployments(storage, root_id: str):
    """Yield ``(display_path, deploy_id)`` for every year/reserve/deployment folder."""
    for yr in storage.iter_folder_items(root_id, fields=["id", "type", "name"]):
        if yr.type != "folder":
            continue
        for res in storage.iter_folder_items(yr.id, fields=["id", "type", "name"]):
            if res.type != "folder":
                continue
            for dep in storage.iter_folder_items(res.id, fields=["id", "type", "name"]):
                if dep.type == "folder":
                    yield f"{yr.name}/{res.name}/{dep.name}", dep.id


def _download_bytes(storage, file_id: str) -> bytes:
    return storage.client.downloads.download_file(file_id).read()


def process_box(display_path: str, deploy_id: str, args, assume_tz: ZoneInfo, storage) -> bool:
    """Backfill one deployment directly on Box. Returns True on success."""
    import hashlib
    name = display_path.rstrip("/").split("/")[-1]
    print(f"\n=== {name} (Box) ===")

    items = list(storage.iter_folder_items(deploy_id, fields=["id", "type", "name", "created_at"]))
    fmap = {i.name: i.id for i in items if i.type == "file"}

    # Upload time: override, else earliest created_at among the deployment's Box files.
    if args.upload_datetime:
        upload_dt, source = parse_override(args.upload_datetime, assume_tz), "override"
    else:
        created = [datetime.fromisoformat(str(i.created_at))
                   for i in items if i.type == "file" and i.created_at]
        if not created:
            print("  ERROR: no files with created_at on Box — skipped.")
            return False
        upload_dt, source = min(created).astimezone(timezone.utc), "Box created_at"
    upload_iso = upload_dt.isoformat()

    observer = ""
    if "deployment_event_record.json" in fmap:
        try:
            rec = json.loads(_download_bytes(storage, fmap["deployment_event_record.json"]).decode("utf-8"))
            observer = rec.get("deployment_info", {}).get("observer", "")
        except Exception:
            pass
    print(f"  observer: {observer or '(blank)'} | upload time: {upload_iso} [{source}]")

    present = 0
    for cname, fields in PROVENANCE_CSVS:
        if cname not in fmap:
            continue
        present += 1
        text = _download_bytes(storage, fmap[cname]).decode("utf-8", errors="replace")
        rows = list(csv.DictReader(io.StringIO(text)))
        if not rows:
            print(f"  {cname}: 0 rows — skipped")
            continue
        changed = stamp_rows(rows, observer, upload_iso, force=args.force)
        if not changed:
            print(f"  {cname}: {len(rows)} row(s) — already complete")
            continue
        if args.dry_run:
            print(f"  {cname}: {len(rows)} row(s) — would stamp {changed} [dry-run]")
            continue
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        data = buf.getvalue().encode("utf-8")
        tmp = Path(args.tmp_dir) / f"{name}__{cname}"
        tmp.write_bytes(data)
        storage.upload_file_with_path(tmp, deploy_id, Path(cname))
        box_sha1 = hashlib.sha1(_download_bytes(storage, fmap[cname])).hexdigest()
        ok = box_sha1 == hashlib.sha1(data).hexdigest()
        print(f"  {cname}: {len(rows)} row(s) — stamped {changed}, uploaded, sha1 {'OK' if ok else 'MISMATCH'}")
        if not ok:
            return False

    if not present:
        print("  No metadata CSVs on Box — nothing to do.")
        return False
    return True


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("targets", nargs="*",
                        help="Local deployment folder(s); or, with --from-box, "
                             "Box paths like '2026/Reserve/Deployment'.")
    parser.add_argument("--from-box", action="store_true",
                        help="Operate directly on Box copies (no local folder needed).")
    parser.add_argument("--all", action="store_true",
                        help="With --from-box: process every deployment under field_data.")
    parser.add_argument("--upload-datetime", metavar="ISO",
                        help="Override the upload time (ISO-8601). Naive values use --assume-tz.")
    parser.add_argument("--assume-tz", default=DEFAULT_ASSUME_TZ,
                        help=f"Timezone for naive timestamps (default: {DEFAULT_ASSUME_TZ}).")
    parser.add_argument("--force", action="store_true",
                        help="Re-stamp rows that already have a box_upload_datetime.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change; write/upload nothing.")
    parser.add_argument("--no-upload", action="store_true",
                        help="Local mode only: stamp on disk but don't upload to Box.")
    args = parser.parse_args()

    try:
        assume_tz = ZoneInfo(args.assume_tz)
    except Exception:
        print(f"ERROR: unknown timezone {args.assume_tz!r}.")
        return 2

    # ----- Box-native mode -----
    if args.from_box:
        if not args.targets and not args.all:
            print("ERROR: --from-box needs Box path(s) or --all.")
            return 2
        from cassn.box.auth import load_box_config
        box_config = load_box_config()
        if not box_config or not box_config.is_complete:
            print("ERROR: Box is not configured (config.json).")
            return 2
        try:
            storage = _box_storage(box_config)
        except Exception as e:
            print(f"ERROR: {e}")
            return 2

        import tempfile
        args.tmp_dir = tempfile.mkdtemp(prefix="box_provenance_")
        root = box_config.field_data_folder_id

        if args.all:
            work = list(_enumerate_box_deployments(storage, root))
        else:
            work = []
            for p in args.targets:
                did = _resolve_box_path(storage, root, p)
                if did is None:
                    print(f"\n=== {p} (Box) ===\n  ERROR: path not found on Box — skipped.")
                else:
                    work.append((p, did))

        ok = sum(1 for path, did in work if process_box(path, did, args, assume_tz, storage))
        print(f"\nDone: {ok}/{len(work)} deployment(s) processed successfully.")
        return 0 if work and ok == len(work) else 1

    # ----- Local mode -----
    if not args.targets:
        print("ERROR: provide one or more local deployment folders (or use --from-box).")
        return 2
    box_config = None
    if not args.dry_run and not args.no_upload:
        from cassn.box.auth import load_box_config
        box_config = load_box_config()
        if not box_config or not box_config.is_complete:
            print("ERROR: Box is not configured. Use --no-upload to stamp locally only.")
            return 2

    ok = 0
    for dep in args.targets:
        if process_local(Path(dep).expanduser(), args, assume_tz, box_config):
            ok += 1
    print(f"\nDone: {ok}/{len(args.targets)} deployment(s) processed successfully.")
    return 0 if ok == len(args.targets) else 1


if __name__ == "__main__":
    raise SystemExit(main())
