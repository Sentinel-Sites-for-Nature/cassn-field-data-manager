#!/usr/bin/env python3
"""Correct a camera clock offset on staged images, end to end.

A camera deployed with a wrong internal clock writes wrong timestamps into every
photo's EXIF. This tool fixes that on the *staged* copies and keeps the
deployment's QC/fixity records consistent, in one pass per device:

  1. Shift EXIF by a user-specified offset with exiftool. ``-AllDates`` moves
     DateTimeOriginal/CreateDate/ModifyDate; because the Reconyx MakerNote tag is
     also named "DateTimeOriginal", it shifts too.
  2. Recompute SHA-256/SHA-1 and file size for every affected file (the bytes
     changed, so the old hashes are stale).
  3. Re-read recorded_datetime from the corrected EXIF.
  4. Patch the deployment's session.json file_inventory.
  5. Refresh any existing legacy device manifest and regenerate metadata CSVs.

After this, the staged data, inventory, metadata CSVs, and any legacy manifest
still present all agree, so Box upload and automatic hash verification work
unchanged. The utility never creates a new per-device manifest.

The offset is fully general (years/months/days/hours/minutes/seconds, add or
subtract), not hardcoded to one year. Use --dry-run first.

IMPORTANT: close the CASSN app before running. Its autosave would otherwise
overwrite the corrected session.json.

--scratch (optional speed-up for slow/HDD staging)
--------------------------------------------------
The per-file work (EXIF rewrite + re-hash) is brutal on a spinning HDD's random
I/O. Pass ``--scratch <fast_dir>`` (e.g. an internal SSD) and the tool, per
device, COPIES the device folder to that scratch dir (one sequential read the
HDD is good at), does the heavy work there (fast on an SSD), and copies the
corrected files back only if they changed. Then it patches session.json,
refreshes an existing legacy manifest, and regenerates the CSVs exactly as the
in-place run does.
The scratch dir is machine-agnostic: any fast local path with room for one
device folder at a time. Omit the flag to work in place.

Examples (the Sedgwick +1-year case):
  # Preview first (writes nothing):
  python utils/fix_camera_clock.py \
    --deployment "/Volumes/G-DRIVE ArmorATD/.../UC_Sedgwick_20260610" \
    --devices p1_ML p2_ML p3_ML p4_ML p4_SA \
    --years 1 --only-year 2025 --dry-run

  # In place:
  python utils/fix_camera_clock.py \
    --deployment "/Volumes/G-DRIVE ArmorATD/.../UC_Sedgwick_20260610" \
    --devices p1_ML p2_ML p3_ML p4_ML p4_SA \
    --years 1 --only-year 2025

  # Routed through an SSD scratch dir (recommended when staging is on an HDD):
  python utils/fix_camera_clock.py \
    --deployment "/Volumes/G-DRIVE ArmorATD/.../UC_Sedgwick_20260610" \
    --devices p1_ML p2_ML p3_ML p4_ML p4_SA \
    --years 1 --only-year 2025 --scratch /tmp/cassn_scratch
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Make the cassn package importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cassn.config import LOCAL_DATA_DIR
from cassn.core.hashing import sha256_sha1
from cassn.core.image_metadata import extract_exif_data, parse_camera_recorded_datetime
from cassn.core.inventory import refresh_legacy_device_manifest
from cassn.export.metadata_csv import write_metadata_outputs
from cassn.lookups import LookupTables


def build_shift(args) -> str:
    """Return exiftool's date-shift argument, e.g. '-AllDates+=1:0:0 0:0:0'."""
    op = "-=" if args.subtract else "+="
    comps = f"{args.years}:{args.months}:{args.days} {args.hours}:{args.minutes}:{args.seconds}"
    return f"-AllDates{op}{comps}"


def ditto(src: Path, dst: Path) -> None:
    """Copy a folder, preserving metadata/bundles (macOS sequential copy)."""
    subprocess.run(["ditto", str(src), str(dst)], check=True)


def exiftool_shift(folder: Path, shift_arg: str, only_year: int | None) -> int:
    """Shift EXIF dates in folder; return the number of files exiftool updated."""
    cmd = ["exiftool", shift_arg, "-overwrite_original", "-ext", "jpg"]
    if only_year is not None:
        cmd += ["-if", f"$DateTimeOriginal=~/^{only_year}/"]
    cmd.append(str(folder))
    out = subprocess.run(cmd, capture_output=True, text=True)
    n = 0
    for line in (out.stdout + out.stderr).splitlines():
        line = line.strip()
        if line.endswith("image files updated") or line.endswith("image file updated"):
            try:
                n = int(line.split()[0])
            except ValueError:
                pass
    return n


def sample_years(dev_dir: Path, n: int = 12) -> dict:
    """Year histogram of a small evenly-spaced sample (for --dry-run reporting)."""
    files = sorted(f for f in dev_dir.rglob("*") if f.suffix.lower() == ".jpg" and not f.name.startswith("._"))
    if not files:
        return {}
    stride = max(1, len(files) // n)
    hist: dict[str, int] = {}
    for f in files[::stride]:
        exif, _ = extract_exif_data(f)
        y = (exif.get("DateTimeOriginal") or "")[:4] or "?"
        hist[y] = hist.get(y, 0) + 1
    return hist


def reinventory(entries: list, work_dir: Path) -> int:
    """Recompute hashes, size, and recorded_datetime for each entry from work_dir."""
    updated = 0
    for i, e in enumerate(entries, 1):
        fp = work_dir / e.get("new_filename", "")
        if not fp.exists():
            continue
        sha256, sha1 = sha256_sha1(fp)
        e["file_hash_sha256"] = sha256
        e["file_hash_sha1"] = sha1
        e["file_size_bytes"] = fp.stat().st_size
        exif, _ = extract_exif_data(fp)
        dt = parse_camera_recorded_datetime(exif)
        if dt:
            e["recorded_datetime"] = dt
        updated += 1
        if i % 5000 == 0:
            print(f"    …re-inventoried {i}/{len(entries)}")
    return updated


def main() -> int:
    ap = argparse.ArgumentParser(description="Shift a camera clock offset on staged images and re-sync QC records.")
    ap.add_argument("--deployment", required=True, help="Deployment folder (contains session.json and raw_data/)")
    ap.add_argument("--devices", nargs="+", required=True, help="Device folder names under raw_data/, e.g. p2_ML p4_SA")
    ap.add_argument("--years", type=int, default=0)
    ap.add_argument("--months", type=int, default=0)
    ap.add_argument("--days", type=int, default=0)
    ap.add_argument("--hours", type=int, default=0)
    ap.add_argument("--minutes", type=int, default=0)
    ap.add_argument("--seconds", type=int, default=0)
    ap.add_argument("--subtract", action="store_true", help="Subtract the offset (clock was AHEAD). Default: add (behind).")
    ap.add_argument("--only-year", type=int, default=None, help="Only shift files whose current year matches (safe re-runs).")
    ap.add_argument("--scratch", default=None,
                    help="Fast local dir (e.g. an SSD) to route the heavy per-file work through. "
                         "Omit to work in place. Needs room for one device folder at a time.")
    ap.add_argument("--dry-run", action="store_true", help="Report what would change; write nothing.")
    args = ap.parse_args()

    if not any((args.years, args.months, args.days, args.hours, args.minutes, args.seconds)):
        ap.error("offset is zero — specify at least one of --years/--months/--days/--hours/--minutes/--seconds")

    dep = Path(args.deployment)
    session_path = dep / "session.json"
    if not session_path.exists():
        ap.error(f"session.json not found at {session_path}")

    session = json.loads(session_path.read_text())
    inv = session["file_inventory"]
    shift_arg = build_shift(args)
    scratch_root = Path(args.scratch) if args.scratch else None
    if scratch_root and not args.dry_run:
        scratch_root.mkdir(parents=True, exist_ok=True)
    mode = f"scratch={scratch_root}" if scratch_root else "in place"
    print(f"Offset: {shift_arg}   |   {mode}   |   dry-run={args.dry_run}   |   only-year={args.only_year}")

    total_updated = 0
    for dev in args.devices:
        dev_dir = dep / "raw_data" / dev
        if not dev_dir.is_dir():
            print(f"  ! {dev}: folder not found ({dev_dir}) — skipping")
            continue
        dev_entries = [e for e in inv if e.get("device_label") == dev]
        print(f"\n=== {dev} ({len(dev_entries)} inventory entries) ===")

        if args.dry_run:
            print(f"  before (sampled years): {sample_years(dev_dir)}")
            print(f"  would run: exiftool {shift_arg} -overwrite_original -ext jpg"
                  f"{' -if year==' + str(args.only_year) if args.only_year else ''} <dir>")
            if scratch_root:
                print(f"  would route the work through scratch dir: {scratch_root / dev}")
            legacy_note = (
                " + refresh existing legacy manifest"
                if (dev_dir / f"{dev}_manifest.json").exists()
                else ""
            )
            print(
                f"  would re-hash/re-date/refresh-size {len(dev_entries)} entries"
                f"{legacy_note}"
            )
            continue

        t0 = time.time()

        # Choose the working directory: a scratch copy (fast) or the device folder in place.
        if scratch_root:
            work_dir = scratch_root / dev
            if work_dir.exists():
                shutil.rmtree(work_dir)
            print("  copy → scratch …", flush=True)
            ditto(dev_dir, work_dir)
        else:
            work_dir = dev_dir

        # 1. EXIF shift
        print("  shifting EXIF …", flush=True)
        n_shifted = exiftool_shift(work_dir, shift_arg, args.only_year)
        print(f"    {n_shifted} files shifted")

        # 2-3. re-hash, re-date, refresh size for this device's entries
        updated = reinventory(dev_entries, work_dir)
        total_updated += updated
        print(f"  re-inventoried {updated} files")

        # Copy corrected files back to the HDD device folder (scratch mode only).
        if scratch_root:
            if n_shifted > 0:
                print("  copy corrected files back → staging …", flush=True)
                ditto(work_dir, dev_dir)
            else:
                print("  no EXIF change (guard skipped all) — no copy-back needed")
            shutil.rmtree(work_dir, ignore_errors=True)

        # 4. Keep a historical manifest consistent, but never create a new one.
        legacy_manifest = dev_dir / f"{dev}_manifest.json"
        had_legacy_manifest = legacy_manifest.exists()
        refreshed = refresh_legacy_device_manifest(dev, dev_dir, dev_entries)
        if refreshed:
            print(f"  refreshed legacy {dev}_manifest.json")
        elif had_legacy_manifest:
            print(f"  ! could not refresh legacy {dev}_manifest.json")
        print(f"  device complete   |   done in {time.time() - t0:.0f}s")

    if args.dry_run:
        print("\nDry run complete — no files written.")
        return 0

    # 5. persist inventory + regenerate CSVs / event record / lookup snapshot
    session_path.write_text(json.dumps(session, indent=2))
    print(f"\nSaved session.json ({total_updated} entries updated).")
    lookups = LookupTables.load(LOCAL_DATA_DIR)
    write_metadata_outputs(dep, session["metadata"], inv, session["devices"], lookups, log=print)
    print("\nDone. Staged data, inventory, and CSVs are back in sync.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
