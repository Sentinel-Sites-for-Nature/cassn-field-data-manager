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
  5. Rewrite the device manifest(s) and regenerate the metadata CSVs.

After this, the staged data, inventory, manifests, and CSVs all agree, so the
app's Box upload + automatic hash verification work unchanged.

The offset is fully general (years/months/days/hours/minutes/seconds, add or
subtract), not hardcoded to one year. Use --dry-run first.

Example (the Sedgwick +1-year case):
  python utils/fix_camera_clock.py \
    --deployment "/Volumes/G-DRIVE ArmorATD/cassn-field-data-staging/UC_Sedgwick_20260610" \
    --devices p1_ML p2_ML p3_ML p4_ML p4_SA \
    --years 1 --only-year 2025 --dry-run
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Make the cassn package importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cassn.config import LOCAL_DATA_DIR
from cassn.core.hashing import sha256_sha1
from cassn.core.image_metadata import extract_exif_data, parse_camera_recorded_datetime
from cassn.core.inventory import write_device_manifest
from cassn.export.metadata_csv import write_metadata_outputs
from cassn.lookups import LookupTables


def build_shift(args) -> tuple[str, str]:
    """Return (operator, 'Y:M:D h:m:s') for exiftool's date-shift syntax."""
    comps = f"{args.years}:{args.months}:{args.days} {args.hours}:{args.minutes}:{args.seconds}"
    return ("-=" if args.subtract else "+="), comps


def exiftool_shift(dev_dir: Path, shift_arg: str, only_year: int | None) -> str:
    cmd = ["exiftool", shift_arg, "-overwrite_original", "-ext", "jpg"]
    if only_year is not None:
        cmd += ["-if", f"$DateTimeOriginal=~/^{only_year}/"]
    cmd.append(str(dev_dir))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return (proc.stdout + proc.stderr).strip().splitlines()[-1] if (proc.stdout or proc.stderr) else ""


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
    op, comps = build_shift(args)
    shift_arg = f"-AllDates{op}{comps}"
    print(f"Offset: AllDates {op} {comps}   |   dry-run={args.dry_run}   |   only-year={args.only_year}")

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
            print(f"  would re-hash/re-date/refresh-size {len(dev_entries)} entries + rewrite manifest")
            continue

        # 1. EXIF shift
        print(f"  shifting EXIF… ", end="", flush=True)
        print(exiftool_shift(dev_dir, shift_arg, args.only_year))

        # 2-3. re-hash, re-date, refresh size for this device's entries
        updated = 0
        for i, e in enumerate(dev_entries, 1):
            fp = dev_dir / e.get("new_filename", "")
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
                print(f"    …re-inventoried {i}/{len(dev_entries)}")
        total_updated += updated
        print(f"  re-inventoried {updated} files")

        # 4. device manifest
        write_device_manifest(dev, dev_dir, dev_entries)
        print(f"  rewrote {dev}_manifest.json")

    if args.dry_run:
        print("\nDry run complete — no files written.")
        return 0

    # 5. persist inventory + regenerate CSVs / event record / lookup snapshot
    session_path.write_text(json.dumps(session, indent=2))
    print(f"\nSaved session.json ({total_updated} entries updated).")
    lookups = LookupTables.load(LOCAL_DATA_DIR)
    write_metadata_outputs(dep, session["metadata"], inv, session["devices"], lookups, log=print)
    print("\nDone. Staged data, inventory, manifests, and CSVs are back in sync.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
