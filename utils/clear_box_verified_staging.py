#!/usr/bin/env python3
"""Permanently clear local deployment events only after live Box verification."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cassn.box.auth import get_box_client, load_box_config  # noqa: E402
from cassn.box.client import BoxStorage  # noqa: E402
from cassn.config import CONFIG_JSON  # noqa: E402
from cassn.core.staging_cleanup import (  # noqa: E402
    StagingCleanupError,
    clear_verified_deployment,
    configured_staging_root,
    discover_staged_deployments,
    inspect_deployment_for_cleanup,
)


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify locally staged CA-SSN deployment events against Box using "
            "stored/server-side SHA-1 values, then optionally clear verified events. "
            "The default is a read-only dry run."
        )
    )
    parser.add_argument(
        "--staging-root",
        type=Path,
        help=(
            "staging root to inspect; defaults to the path currently saved by "
            "the Field Data Manager"
        ),
    )
    parser.add_argument(
        "--event",
        action="append",
        default=[],
        metavar="FOLDER_NAME",
        help="inspect only this direct child event folder (repeatable)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="permanently delete each event that passes every safety check",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIG_JSON,
        help=argparse.SUPPRESS,
    )
    return parser


def _selected_deployments(staging_root: Path, names: list[str]) -> list[Path]:
    if not names:
        return discover_staged_deployments(staging_root)

    selected: list[Path] = []
    for name in names:
        candidate_name = Path(name)
        if candidate_name.name != name or name in {"", ".", ".."}:
            raise ValueError(f"--event must be a direct folder name: {name!r}")
        candidate = staging_root / name
        if not candidate.is_dir() or not (candidate / "session.json").is_file():
            raise ValueError(f"staged deployment event not found: {candidate}")
        selected.append(candidate)
    return selected


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    staging_root = (
        args.staging_root.expanduser()
        if args.staging_root
        else configured_staging_root(args.config)
    )
    print(f"Staging root: {staging_root}")
    print(f"Mode: {'APPLY (permanent local deletion)' if args.apply else 'dry run'}")

    if not staging_root.is_dir():
        print(f"ERROR: staging root does not exist: {staging_root}", file=sys.stderr)
        return 2
    try:
        deployments = _selected_deployments(staging_root, args.event)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if not deployments:
        print("No staged deployment events with session.json were found.")
        return 0

    try:
        box_config = load_box_config(args.config)
    except (OSError, KeyError, ValueError) as exc:
        print(f"ERROR: could not load Box configuration: {exc}", file=sys.stderr)
        return 2
    client = get_box_client(box_config)
    if client is None:
        print(
            "ERROR: could not authenticate with Box; no local folders were changed.",
            file=sys.stderr,
        )
        return 2
    storage = BoxStorage(client)

    cleared = 0
    blocked = 0
    cleared_bytes = 0
    for deployment in deployments:
        print(f"\nChecking {deployment.name} ...")
        plan = inspect_deployment_for_cleanup(
            deployment,
            staging_root,
            storage,
            str(box_config.field_data_folder_id or ""),
        )
        if not plan.clearable:
            blocked += 1
            print("  BLOCKED")
            for reason in plan.reasons:
                print(f"    - {reason}")
            continue

        print(
            f"  SAFE TO CLEAR: {plan.file_count:,} raw file(s), "
            f"{_format_bytes(plan.local_bytes)}"
        )
        print(f"  Box: {plan.box_path} (folder {plan.box_folder_id})")
        if not args.apply:
            continue
        try:
            result = clear_verified_deployment(plan)
        except StagingCleanupError as exc:
            blocked += 1
            print(f"  NOT CLEARED: {exc}")
            continue
        cleared += 1
        cleared_bytes += result.bytes_cleared
        print(f"  CLEARED: {result.deployment_folder}")

    safe = len(deployments) - blocked
    if args.apply:
        print(
            f"\nResult: cleared {cleared} event(s), {_format_bytes(cleared_bytes)}; "
            f"blocked {blocked} event(s)."
        )
    else:
        print(
            f"\nDry run: {safe} event(s) safe to clear; {blocked} blocked. "
            "Rerun with --apply to permanently delete only the safe events."
        )
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
