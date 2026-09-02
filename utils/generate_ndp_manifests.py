#!/usr/bin/env python3
"""Stage one Box deployment event for the NDP source namespace.

Writes ``README.md``, ``manifest.json``, and ``metadata/file_metadata.csv`` per
deployment beneath ``--staging-root``. The default is a read-only validation
pass; pass ``--apply`` to write. No media is copied and nothing is written to
Box.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cassn.lookup_sync import validate_lookup_directory  # noqa: E402
from cassn.ndp.staging import NdpStagingError, apply_plan, plan_event  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--event",
        type=Path,
        required=True,
        help="Box deployment-event directory; its folder name is the event id",
    )
    parser.add_argument(
        "--staging-root",
        type=Path,
        required=True,
        help="Local root corresponding to private/ssn/ca/UC-Nature/source",
    )
    parser.add_argument(
        "--lookup-dir",
        type=Path,
        required=True,
        help="Directory holding the curated lookup snapshot (Box app_config)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the staging tree. Default is a read-only validation pass.",
    )
    return parser


def _print_plan(plan, *, apply: bool) -> None:
    print(f"Mode: {'apply' if apply else 'dry-run'}")
    print(f"Event: {plan.deployment_event_id}")
    print(f"Source: {plan.event_dir}")
    print(f"Staging root: {plan.staging_root}")
    print(f"Generated: {plan.generated}")
    print(f"Deployments rendered: {len(plan.deployments)}")
    print(f"Files planned: {plan.file_count}")
    for deployment in plan.deployments:
        print(f"  {deployment.deployment_id}")
    for warning in plan.warnings:
        print(f"WARNING: {warning}")
    for error in plan.errors:
        print(f"ERROR: {error}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        lookups, _ = validate_lookup_directory(args.lookup_dir.expanduser())
        plan = plan_event(args.event, args.staging_root, lookups)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    _print_plan(plan, apply=args.apply)
    if not plan.ok:
        print(
            f"Nothing was written; {len(plan.errors)} validation error(s) found.",
            file=sys.stderr,
        )
        return 2
    if not args.apply:
        print("Dry run only; no files were written.")
        return 0

    try:
        result = apply_plan(plan)
    except (NdpStagingError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote {len(result.written)} file(s); {len(result.unchanged)} already correct.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
