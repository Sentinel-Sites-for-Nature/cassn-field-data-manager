#!/usr/bin/env python3
"""Camera clock-offset fix via a fast SSD scratch dir (for slow/HDD staging).

Same end result as fix_camera_clock.py, but it avoids the spinning-disk random-I/O
wall: per device it COPIES the folder to a fast local scratch dir (one big
sequential read the HDD is good at), does the EXIF shift + re-hash + re-date there
(instant on an SSD), and copies the corrected files back only if they changed.
Then it patches session.json, rewrites manifests, and regenerates the CSVs.

Use when the deployment lives on a slow external HDD and you have enough free
space on the internal SSD for one device folder at a time.

Example:
  python utils/fix_camera_clock_scratch.py \
    --deployment "/Volumes/G-DRIVE ArmorATD/.../UC_Sedgwick_20260610" \
    --devices p1_ML p2_ML p3_ML p4_ML p4_SA \
    --years 1 --only-year 2025 \
    --scratch /tmp/cassn_scratch
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cassn.config import LOCAL_DATA_DIR
from cassn.core.hashing import sha256_sha1
from cassn.core.image_metadata import extract_exif_data, parse_camera_recorded_datetime
from cassn.core.inventory import write_device_manifest
from cassn.export.metadata_csv import write_metadata_outputs
from cassn.lookups import LookupTables


def build_shift(a) -> str:
    op = "-=" if a.subtract else "+="
    return f"-AllDates{op}{a.years}:{a.months}:{a.days} {a.hours}:{a.minutes}:{a.seconds}"


def ditto(src: Path, dst: Path) -> None:
    subprocess.run(["ditto", str(src), str(dst)], check=True)


def exiftool_shift(folder: Path, shift_arg: str, only_year: int | None) -> int:
    """Shift EXIF in folder; return number of files updated."""
    cmd = ["exiftool", shift_arg, "-overwrite_original", "-ext", "jpg"]
    if only_year is not None:
        cmd += ["-if", f"$DateTimeOriginal=~/^{only_year}/"]
    cmd.append(str(folder))
    out = subprocess.run(cmd, capture_output=True, text=True)
    text = out.stdout + out.stderr
    n = 0
    for line in text.splitlines():
        line = line.strip()
        if line.endswith("image files updated") or line.endswith("image file updated"):
            try:
                n = int(line.split()[0])
            except ValueError:
                pass
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="Camera clock fix via SSD scratch dir.")
    ap.add_argument("--deployment", required=True)
    ap.add_argument("--devices", nargs="+", required=True)
    ap.add_argument("--years", type=int, default=0)
    ap.add_argument("--months", type=int, default=0)
    ap.add_argument("--days", type=int, default=0)
    ap.add_argument("--hours", type=int, default=0)
    ap.add_argument("--minutes", type=int, default=0)
    ap.add_argument("--seconds", type=int, default=0)
    ap.add_argument("--subtract", action="store_true")
    ap.add_argument("--only-year", type=int, default=None)
    ap.add_argument("--scratch", default="/tmp/cassn_scratch", help="fast local scratch dir (internal SSD)")
    args = ap.parse_args()

    dep = Path(args.deployment)
    session_path = dep / "session.json"
    session = json.loads(session_path.read_text())
    inv = session["file_inventory"]
    shift_arg = build_shift(args)
    scratch_root = Path(args.scratch)
    scratch_root.mkdir(parents=True, exist_ok=True)
    print(f"Offset: {shift_arg} | only-year={args.only_year} | scratch={scratch_root}")

    total_updated = 0
    for dev in args.devices:
        t0 = time.time()
        gdrive_dev = dep / "raw_data" / dev
        if not gdrive_dev.is_dir():
            print(f"  ! {dev}: not found, skipping"); continue
        scratch_dev = scratch_root / dev
        if scratch_dev.exists():
            shutil.rmtree(scratch_dev)
        dev_entries = [e for e in inv if e.get("device_label") == dev]
        print(f"\n=== {dev} ({len(dev_entries)} entries) ===")

        print(f"  [1/4] copy → scratch …", flush=True)
        ditto(gdrive_dev, scratch_dev)

        print(f"  [2/4] EXIF shift on SSD …", flush=True)
        n_shifted = exiftool_shift(scratch_dev, shift_arg, args.only_year)
        print(f"        {n_shifted} files shifted")

        print(f"  [3/4] re-hash + re-date on SSD …", flush=True)
        updated = 0
        for i, e in enumerate(dev_entries, 1):
            fp = scratch_dev / e.get("new_filename", "")
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
        total_updated += updated
        print(f"        re-inventoried {updated} files")

        if n_shifted > 0:
            print(f"  [4/4] copy corrected files back → G-DRIVE …", flush=True)
            ditto(scratch_dev, gdrive_dev)
        else:
            print(f"  [4/4] no EXIF change (guard skipped all) — no copy-back needed")

        write_device_manifest(dev, gdrive_dev, dev_entries)
        shutil.rmtree(scratch_dev, ignore_errors=True)
        print(f"  done in {time.time()-t0:.0f}s")

    session_path.write_text(json.dumps(session, indent=2))
    print(f"\nSaved session.json ({total_updated} entries updated).")
    lookups = LookupTables.load(LOCAL_DATA_DIR)
    write_metadata_outputs(dep, session["metadata"], inv, session["devices"], lookups, log=print)
    print("\nDone — staged data, inventory, manifests, and CSVs back in sync.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
