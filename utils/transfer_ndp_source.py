#!/usr/bin/env python3
"""Plan or execute the media-only phase of one NDP source event transfer.

The default is a read-only preflight.  ``--apply`` downloads media through the
Box API, verifies its recorded SHA-256, syncs one deployment at a time to
Pelican, and records remote stat results. It deliberately does not yet publish
the manifest/inventory controls or update Box provenance.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cassn.box.auth import get_box_client, load_box_config  # noqa: E402
from cassn.box.client import BoxStorage  # noqa: E402
from cassn.lookup_sync import validate_lookup_directory  # noqa: E402
from cassn.ndp.box_download import TransferCancelled  # noqa: E402
from cassn.ndp.pelican import PelicanRunner  # noqa: E402
from cassn.ndp.staging import plan_event  # noqa: E402
from cassn.ndp.submission import execute_media_transfer  # noqa: E402
from cassn.ndp.transfer import (  # noqa: E402
    STATE_FILENAME,
    NdpTransferError,
    normalize_destination_root,
    plan_media_transfer,
)
from cassn.ndp.transfer_state import abandonment_targets, load_state  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--event",
        type=Path,
        required=True,
        help="Box Drive event path used as the year/reserve/event locator",
    )
    parser.add_argument(
        "--staging-root",
        type=Path,
        required=True,
        help="Local root containing the event's generated NDP control-file tree",
    )
    parser.add_argument(
        "--lookup-dir",
        type=Path,
        required=True,
        help="Directory holding the curated lookup snapshot",
    )
    parser.add_argument(
        "--scratch-root",
        type=Path,
        required=True,
        help="Local temporary storage; must fit the largest deployment plus margin",
    )
    parser.add_argument(
        "--destination-root",
        required=True,
        help=(
            "User-specified OSDF source collection root; must begin "
            "osdf:///ndp/private/ or osdf:///ndp/public/"
        ),
    )
    parser.add_argument("--token", type=Path, help="Optional Pelican token file")
    parser.add_argument(
        "--keep-scratch",
        action="store_true",
        help="Keep locally verified media after remote stat checks",
    )
    parser.add_argument(
        "--abandonment-plan",
        action="store_true",
        help="List remote data collections requiring deletion; delete nothing",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Transfer media. Default is a read-only preflight.",
    )
    return parser


def _format_bytes(value: int) -> str:
    return f"{value / 1024**3:.2f} GiB"


def _print_plan(plan, *, apply: bool) -> None:
    print(f"Mode: {'media transfer' if apply else 'dry-run'}")
    print(f"Event: {plan.deployment_event_id}")
    print(f"Source: {plan.event_dir}")
    print(f"Destination: {plan.destination_root}")
    print(f"Deployments: {len(plan.deployments)}")
    print(f"Files: {plan.file_count}")
    print(f"Total media: {_format_bytes(plan.total_bytes)}")
    print(
        "Scratch: "
        f"{_format_bytes(plan.scratch_free_bytes)} available; "
        f"{_format_bytes(plan.scratch_required_bytes)} required"
    )
    for deployment in plan.deployments:
        print(
            f"  {deployment.deployment_id}: {len(deployment.files)} files, "
            f"{_format_bytes(deployment.total_bytes)} -> {deployment.data_destination}"
        )
    for warning in plan.warnings:
        print(f"WARNING: {warning}")
    for error in plan.errors:
        print(f"ERROR: {error}")
    print(
        "NOTICE: media only; manifest, file_metadata.csv, and Box provenance "
        "publication are not yet performed."
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.apply and args.abandonment_plan:
        print(
            "ERROR: --apply and --abandonment-plan cannot be combined", file=sys.stderr
        )
        return 2
    if args.abandonment_plan:
        state_path = args.staging_root.expanduser() / args.event.name / STATE_FILENAME
        if not state_path.exists():
            print("No transfer state exists; there is nothing recorded to abandon.")
            return 0
        try:
            state = load_state(state_path)
            if state.destination_root != normalize_destination_root(
                args.destination_root
            ):
                raise NdpTransferError(
                    "the requested destination differs from the recorded transfer state"
                )
            targets = abandonment_targets(state)
        except NdpTransferError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print("Remote data collections requiring explicit deletion:")
        for target in targets:
            print(f"  {target}")
        print("Nothing was deleted.")
        return 0
    try:
        lookups, _ = validate_lookup_directory(args.lookup_dir.expanduser())
        staging = plan_event(
            args.event.expanduser(), args.staging_root.expanduser(), lookups
        )
        if not staging.ok:
            for error in staging.errors:
                print(f"ERROR: {error}", file=sys.stderr)
            return 2
        box_config = load_box_config()
        if not box_config.field_data_folder_id:
            raise NdpTransferError("Box field_data_folder_id is not configured")
        box_client = get_box_client(box_config)
        if box_client is None:
            raise NdpTransferError("Box is not authenticated")
        plan = plan_media_transfer(
            staging,
            lookups,
            BoxStorage(box_client),
            box_config.field_data_folder_id,
            args.scratch_root,
            args.destination_root,
            retain_scratch=args.keep_scratch,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    _print_plan(plan, apply=args.apply)
    if not plan.ok:
        print("Nothing was transferred.", file=sys.stderr)
        return 2

    if not args.apply:
        print(
            "Dry run only; no media was downloaded and nothing was written to Pelican."
        )
        return 0

    try:
        result = execute_media_transfer(
            plan,
            box_client,
            PelicanRunner(token=args.token),
            clear_scratch=not args.keep_scratch,
        )
    except (NdpTransferError, TransferCancelled, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(
        f"Media phase complete: {result.downloaded} downloaded, "
        f"{result.reused} reused from scratch, "
        f"{result.objects_statted} remote objects statted "
        f"({result.checksum_verified} checksum-verified)."
    )
    print("Control-file publication and Box provenance were not performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
