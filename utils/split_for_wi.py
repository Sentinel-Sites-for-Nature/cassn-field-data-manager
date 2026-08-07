#!/usr/bin/env python3
"""Split oversized _ML / _SA device folders into Wildlife-Insights-sized batches.

Wildlife Insights accepts at most 15,000 images per upload. This tool scans a
root (a season folder, a single deployment, or the practice staging drive),
finds every ``pN_ML`` / ``pN_SA`` camera folder over the limit, and divides its
images into ``<device>_1``, ``<device>_2`` ... subfolders — each at or under the
limit — so each subfolder is one WI upload. Whole trigger bursts stay together,
the fixity manifest is left untouched, and the split is fully reversible.

Run it as a terminal WI-prep step, AFTER Box upload and QC for the deployment
are complete (splitting nests the images one level deeper than the manifest /
QC steps expect).

The planning + execution logic lives in ``cassn/core/wi_split.py`` so the app
can reuse it; this file is just the command line.

Default is a DRY RUN — it reports what would change and writes nothing. Pass
``--apply`` to perform the split, or ``--undo`` to reverse a previous one.

Examples:
  # Preview the whole practice drive (writes nothing):
  python utils/split_for_wi.py --root "/Volumes/G-DRIVE ArmorATD/cassn-field-data-staging"

  # Actually split one deployment:
  python utils/split_for_wi.py \
    --root "/Volumes/G-DRIVE ArmorATD/cassn-field-data-staging/UC_JepsonPrairie_20260423" --apply

  # Put everything back:
  python utils/split_for_wi.py \
    --root "/Volumes/G-DRIVE ArmorATD/cassn-field-data-staging/UC_JepsonPrairie_20260423" --undo
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the cassn package importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cassn.core.wi_split import (  # noqa: E402
    DEFAULT_DEVICE_SUFFIXES,
    InvalidLimitError,
    SplitError,
    WI_UPLOAD_LIMIT,
    apply_device_split,
    find_target_devices,
    list_part_dirs,
    normalize_suffixes,
    plan_device,
    undo_device_split,
)


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("Refusing to proceed without a TTY; pass --yes to override.")
        return False
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


def _report_split(plans, limit) -> tuple[int, int]:
    """Print the dry-run/plan report. Returns (devices_with_work, total_moves)."""
    devices_with_work = 0
    total_moves = 0
    for plan in plans:
        rel = plan.device_dir
        if plan.needs_split:
            pending = plan.pending_moves()
            if plan.fully_split:
                print(f"[done]  {rel}  ({plan.image_count:,} images, {len(plan.parts)} parts)")
            else:
                devices_with_work += 1
                total_moves += pending
                resume = " (resume)" if pending < plan.image_count else ""
                print(f"[SPLIT] {rel}  ({plan.image_count:,} images -> "
                      f"{len(plan.parts)} parts, {pending:,} to move){resume}")
                for part in plan.parts:
                    print(f"    {part.name}/  <- {len(part.files):,} images")
        else:
            print(f"[ok]    {rel}  ({plan.image_count:,} images, <= {limit:,})")
        for w in plan.warnings:
            print(f"    !! {w}")
    return devices_with_work, total_moves


def cmd_split(args) -> int:
    if args.limit <= 0:
        raise InvalidLimitError(
            f"Image limit must be a positive integer; got {args.limit!r}"
        )
    suffixes = normalize_suffixes(args.suffixes)
    devices = find_target_devices(args.root, suffixes)
    if not devices:
        print(f"No {', '.join(suffixes)} device folders found under {args.root}")
        return 0

    print(f"Scanning {args.root}")
    print(f"Suffixes: {', '.join(suffixes)}   |   limit: {args.limit:,}   |   "
          f"keep-bursts: {not args.no_keep_bursts}   |   "
          f"mode: {'APPLY' if args.apply else 'dry-run'}\n")

    plans = [
        plan_device(d, limit=args.limit, keep_bursts=not args.no_keep_bursts)
        for d in devices
    ]
    devices_with_work, total_moves = _report_split(plans, args.limit)

    print(f"\n{len(devices)} device folder(s) scanned; "
          f"{devices_with_work} need work ({total_moves:,} images to move).")

    if not args.apply:
        print("\nDry run — nothing changed. Re-run with --apply to perform the split.")
        return 0
    if devices_with_work == 0:
        return 0
    if not _confirm(f"\nMove {total_moves:,} images into part folders across "
                    f"{devices_with_work} device(s)?", args.yes):
        print("Aborted.")
        return 1

    print()
    placed = 0
    for plan in plans:
        if not plan.needs_split or plan.fully_split:
            continue
        print(f"{plan.device_dir}")
        res = apply_device_split(plan, move=True, dry_run=False, log=print)
        placed += res["placed"]
    print(f"\nDone. Moved and verified {placed:,} images into new part folders.")
    return 0


def cmd_undo(args) -> int:
    suffixes = normalize_suffixes(args.suffixes)
    devices = [d for d in find_target_devices(args.root, suffixes) if list_part_dirs(d)]
    if not devices:
        print(f"No split device folders to undo under {args.root}")
        return 0

    print(f"Will restore images from part folders back into these devices:")
    total = 0
    for d in devices:
        parts = list_part_dirs(d)
        n = sum(
            len([p for p in (d / part).iterdir() if p.is_file() and not p.name.startswith(".")])
            for part in parts
        )
        total += n
        print(f"  {d}  ({len(parts)} parts, {n:,} images)")

    if not _confirm(f"\nMove {total:,} images back up and remove the emptied part "
                    f"folders across {len(devices)} device(s)?", args.yes):
        print("Aborted.")
        return 1

    print()
    restored = 0
    for d in devices:
        res = undo_device_split(d, dry_run=False, log=print)
        restored += res["restored"]
        if res["collisions"]:
            print(f"  !! {d}: {len(res['collisions'])} name collision(s) left in place")
    print(f"\nDone. Restored {restored:,} images.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Split oversized _ML/_SA folders into Wildlife-Insights-sized batches."
    )
    ap.add_argument("--root", required=True,
                    help="Folder to scan: a season, a deployment, or the staging drive.")
    ap.add_argument("--limit", type=int, default=WI_UPLOAD_LIMIT,
                    help=f"Max images per part (default {WI_UPLOAD_LIMIT}).")
    ap.add_argument("--suffixes", nargs="+", default=list(DEFAULT_DEVICE_SUFFIXES),
                    help="Device-folder suffixes to target (default: _ML _SA).")
    ap.add_argument("--apply", action="store_true",
                    help="Perform the split. Default is a dry-run report.")
    ap.add_argument("--undo", action="store_true",
                    help="Reverse a previous split under --root.")
    ap.add_argument("--no-keep-bursts", action="store_true",
                    help="Allow a part boundary to fall inside a trigger burst.")
    ap.add_argument("--yes", action="store_true",
                    help="Skip the confirmation prompt on --apply / --undo.")
    args = ap.parse_args()

    if not Path(args.root).is_dir():
        print(f"Not a directory: {args.root}")
        return 2
    if args.undo and args.apply:
        print("Choose one of --apply or --undo, not both.")
        return 2

    try:
        return cmd_undo(args) if args.undo else cmd_split(args)
    except SplitError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
