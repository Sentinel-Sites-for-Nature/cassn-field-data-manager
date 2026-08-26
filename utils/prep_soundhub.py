#!/usr/bin/env python3
"""
Stage and upload previously-downloaded deployments to Wildlife SoundHub.

The application does this automatically for deployments it ingests. This CLI
covers the backlog: deployments already downloaded and pushed to Box before the
SoundHub step existed. Both paths share the same code in ``cassn.soundhub``, so
a deployment prepared here is byte-identical to one prepared by the app.

Bird (BD) audio only. Source WAVs are never modified.

    # Stage one deployment (WAV -> FLAC + refresh the project CSVs):
    python utils/prep_soundhub.py stage --deployment "/path/to/UC_QuailRidge_20260108"

    # Stage every deployment under a season folder:
    python utils/prep_soundhub.py stage --root "/path/to/2026"

    # Review what is staged and where it would land in S3:
    python utils/prep_soundhub.py status

    # Push to the bucket, then verify:
    python utils/prep_soundhub.py upload

    # After SoundHub acceptance, preview and clear the completed local batch:
    python utils/prep_soundhub.py clear-completed
    python utils/prep_soundhub.py clear-completed --apply

Replaces the earlier ``convert_to_flac.py`` and ``verify_flac_conversion.py``,
which rebuilt each deployment_id by parsing folder names — a derivation that
dropped the organization prefix and would have written unfixable S3 keys, since
the IAM role cannot delete.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cassn.soundhub.export import (  # noqa: E402
    enrich_audio_rows,
    read_bd_audio_rows,
    refresh_project_csvs,
    validate_staging_manifests,
    write_deployment_copy,
    write_deployment_fragments,
)
from cassn.config import LOCAL_DATA_DIR  # noqa: E402
from cassn.lookups import LookupTables  # noqa: E402
from cassn.soundhub.lifecycle import (  # noqa: E402
    SoundHubLifecycleError,
    clear_completed_batch,
    plan_completed_batch_cleanup,
)
from cassn.soundhub.provenance import (  # noqa: E402
    DEFAULT_SUBMITTER,
)
from cassn.soundhub.submission import (  # noqa: E402
    SoundHubSubmissionError,
    execute_soundhub_submission,
    plan_soundhub_submission,
)
from cassn.soundhub.staging import (  # noqa: E402
    SoundHubStagingError,
    flac_available,
    fragments_root,
    project_root,
    stage_deployment,
)
from cassn.soundhub.upload import (  # noqa: E402
    SoundHubUploadError,
    load_soundhub_config,
    project_prefix,
    staged_objects,
    verify_project,
)


def find_deployments(root: Path) -> list[Path]:
    """Deployment folders below ``root`` — those with an audio metadata CSV."""
    if (root / "audio_file_metadata.csv").exists():
        return [root]
    return sorted(p.parent for p in root.rglob("audio_file_metadata.csv"))


def cmd_stage(args) -> int:
    if not flac_available():
        print("ERROR: 'flac' is not installed. Install it with: brew install flac")
        return 1

    settings = load_soundhub_config()
    lookups = LookupTables.load(LOCAL_DATA_DIR)
    staging_root = Path(args.staging or settings["staging_root"])
    root = project_root(staging_root)
    if root.exists() or fragments_root(staging_root).exists():
        lifecycle = plan_completed_batch_cleanup(
            staging_root,
            getattr(args, "box_year_root", None),
        )
        if lifecycle.closed:
            print("STAGING BLOCKED — the existing SoundHub batch is completed.")
            print(f"  Completed recordings: {lifecycle.recording_count}")
            print(f"  Local batch folder:   {lifecycle.project_root}")
            print(
                "Review and clear it before starting a new batch:\n"
                "  python utils/prep_soundhub.py clear-completed"
            )
            return 1
        if lifecycle.errors:
            print("STAGING BLOCKED — the existing SoundHub batch is invalid:")
            for error in lifecycle.errors:
                print(f"  - {error}")
            return 1

    if args.deployment:
        deployments = [Path(args.deployment)]
    else:
        deployments = find_deployments(Path(args.root))
    if not deployments:
        print(f"No deployment folders with audio_file_metadata.csv found under {args.root}")
        return 1

    print("SoundHub staging")
    print(f"  Local batch folder: {staging_root}")
    print(f"  Deployment event folders found: {len(deployments)}\n")

    failures = 0
    for folder in deployments:
        try:
            audio_rows = enrich_audio_rows(read_bd_audio_rows(folder), lookups)
        except FileNotFoundError as e:
            print(f"[{folder.name}] SKIP — {e}")
            failures += 1
            continue

        if not audio_rows:
            print(f"[{folder.name}] SKIP — no bird (BD) audio")
            continue

        def show(current, total, name, _folder=folder):
            print(f"  [{_folder.name}] {current}/{total}  {name}", flush=True)

        try:
            result = stage_deployment(folder, audio_rows, staging_root, progress=show)
            write_deployment_fragments(staging_root, audio_rows)
            write_deployment_copy(folder, audio_rows)
        except SoundHubStagingError as e:
            print(f"[{folder.name}] ERROR — {e}")
            failures += 1
            continue

        print(
            f"[{folder.name}] {', '.join(result['deployment_ids'])}: "
            f"{result['converted']} converted, {result['skipped']} already present\n"
        )

    csvs = refresh_project_csvs(staging_root)
    try:
        validate_staging_manifests(staging_root)
    except SoundHubStagingError as e:
        print(f"STAGING INVALID — {e}")
        return 1
    print(
        f"Cumulative project manifests rebuilt: "
        f"{csvs['deployment_count']} SoundHub deployment(s), "
        f"{csvs['recording_count']} recording(s)"
    )
    print(f"  {csvs['deployment_csv']}")
    print(f"  {csvs['recording_csv']}")
    print("STAGING VALIDATED — manifests and FLACs are upload-ready.")
    return 1 if failures else 0


def cmd_status(args) -> int:
    settings = load_soundhub_config()
    staging_root = Path(args.staging or settings["staging_root"])
    root = project_root(staging_root)
    if not root.exists():
        print(f"Nothing staged at {root}")
        return 1

    try:
        objects = staged_objects(staging_root, settings)
    except SoundHubUploadError as e:
        print(f"ERROR: {e}")
        return 1

    by_deployment: dict[str, list[dict]] = {}
    for obj in objects:
        head = obj["relative"].split("/")[0]
        key = head if "/" in obj["relative"] else "(project metadata)"
        by_deployment.setdefault(key, []).append(obj)

    total_bytes = sum(o["size"] for o in objects)
    print("Current SoundHub staging batch")
    print(f"  Local batch folder: {root}")
    print(f"  S3 destination:     s3://{settings['bucket']}/{project_prefix(settings)}/\n")
    print(
        "  This table shows the full cumulative staging set. Run "
        "`upload --preflight-only`\n  to see only the unsubmitted recordings in "
        "the next transfer.\n"
    )
    print(f"{'SoundHub deployment':<44}{'Objects':>9}{'Size':>14}")
    print("-" * 65)
    for name, group in sorted(by_deployment.items()):
        size = sum(o["size"] for o in group)
        print(f"{name:<44}{len(group):>7}{size / 1e9:>12.2f} GB")
    print("-" * 65)
    print(f"{'TOTAL S3 OBJECTS':<44}{len(objects):>9}{total_bytes / 1e9:>12.2f} GB")
    return 0


def cmd_upload(args) -> int:
    settings = load_soundhub_config()
    staging_root = Path(args.staging or settings["staging_root"])

    def show(current, total, name):
        print(f"  {current}/{total}  {name}", flush=True)

    if args.verify_only:
        try:
            check = verify_project(staging_root, settings=settings)
        except SoundHubUploadError as e:
            print(f"ERROR: {e}")
            return 1
        return _print_verification(check, heading="Landing-zone comparison")

    try:
        plan = plan_soundhub_submission(
            staging_root,
            settings=settings,
            box_year_root=args.box_year_root,
        )
    except Exception as e:
        print(f"ERROR: {e}")
        return 1
    _print_preflight(plan)
    if plan.errors:
        print("\nUPLOAD BLOCKED")
        for error in plan.errors:
            print(f"  - {error}")
        return 1
    if args.preflight_only:
        print("\nPREFLIGHT PASSED — no S3 or Box writes were attempted.")
        if not plan.provenance.pending_keys:
            print("No unsubmitted recordings remain; a live upload would not start.")
        return 0
    if not plan.provenance.pending_keys:
        print("\nBATCH COMPLETE — all staged recordings are recorded as submitted.")
        print(
            "After SoundHub confirms acceptance, preview local cleanup with:\n"
            "  python utils/prep_soundhub.py clear-completed"
        )
        return 1

    print(f"\nUploading to s3://{settings['bucket']}/{project_prefix(settings)}/\n")
    try:
        result = execute_soundhub_submission(
            plan,
            submitter=args.submitter,
            progress=show,
        )
    except SoundHubSubmissionError as e:
        if e.phase == "box_provenance":
            print(
                "ERROR: S3 verification succeeded, but the Box submission record "
                f"failed: {e}\nDo not upload again; repair the Box record instead."
            )
        else:
            print(f"ERROR during {e.phase.replace('_', ' ')}: {e}")
        return 1

    upload = result["upload"]
    print(
        f"\nS3 transfer finished: {upload['uploaded']} object(s) uploaded, "
        f"{upload['skipped']} already present, "
        f"{upload['uploaded_bytes'] / 1e9:.2f} GB sent.\n"
    )
    check = result["verification"]
    verification_code = _print_verification(check, heading="Immediate S3 verification")
    if verification_code:
        return verification_code
    provenance = result["provenance"]
    reports = result["reports"]
    print(
        f"\nBox submission record updated: {provenance.changed_rows} metadata "
        f"row(s) across {provenance.changed_files} deployment event(s)."
    )
    print(f"Submission report saved with {len(reports)} Box deployment event(s):")
    for report in reports:
        print(f"  {report}")
    print("\nSOUNDHUB SUBMISSION COMPLETE")
    return 0


def cmd_clear_completed(args) -> int:
    """Preview or apply deletion of one fully submitted local staging batch."""
    settings = load_soundhub_config()
    staging_root = Path(args.staging or settings["staging_root"])
    try:
        plan = plan_completed_batch_cleanup(
            staging_root,
            args.box_year_root,
        )
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1

    print("Completed SoundHub batch cleanup preflight")
    print(f"  Local project folder:     {plan.project_root}")
    print(f"  Local fragment folder:    {plan.fragments_root}")
    print(f"  Deployment events:        {plan.event_count}")
    print(f"  SoundHub deployments:     {plan.deployment_count}")
    print(f"  Recordings:               {plan.recording_count}")
    print(f"  Pending recordings:       {plan.pending_count}")
    print(f"  Local files to remove:    {plan.local_file_count}")
    print(f"  Local data to remove:     {plan.local_bytes / 1e9:.2f} GB")
    print(f"  Shared verified report:   {plan.report_name or '(not confirmed)'}")

    if not plan.closed:
        print("\nCLEANUP BLOCKED — this staging batch is not fully submitted.")
        for error in plan.errors:
            print(f"  - {error}")
        return 1
    if plan.errors:
        print("\nCLEANUP BLOCKED")
        for error in plan.errors:
            print(f"  - {error}")
        return 1

    if not args.apply:
        print("\nDRY RUN PASSED — nothing was deleted.")
        print(
            "This verifies local staging and Box Drive records; it does not "
            "confirm SoundHub ingestion."
        )
        print(
            "After SoundHub acceptance is confirmed, delete this derived local "
            "batch with:\n"
            "  python utils/prep_soundhub.py clear-completed --apply"
        )
        return 0

    try:
        result = clear_completed_batch(plan)
    except SoundHubLifecycleError as exc:
        print(f"\nCLEANUP FAILED — {exc}")
        return 1

    print("\nCOMPLETED SOUNDHUB BATCH CLEARED")
    print(f"  Removed project folder:  {result.removed_project_root}")
    if result.removed_fragments_root is not None:
        print(f"  Removed fragment folder: {result.removed_fragments_root}")
    print(f"  Removed files:           {result.removed_files}")
    print(f"  Freed data:              {result.removed_bytes / 1e9:.2f} GB")
    print("  Box data, source WAVs, submission reports, and S3 were not changed.")
    return 0


def _print_preflight(plan) -> None:
    provenance = plan.provenance
    print("SoundHub upload preflight")
    print(f"  Staged recordings:                  {len(provenance.target_keys)}")
    print(f"  Recordings in next submission:      {len(provenance.pending_keys)}")
    print(f"  Already recorded as submitted:      {len(provenance.submitted_keys)}")
    print(f"  Deployment events in next batch:    {len(provenance.pending_event_ids)}")
    print(f"  SoundHub deployments in next batch: {len(provenance.pending_deployment_ids)}")
    print(f"  Box metadata CSVs to update:         {provenance.pending_file_count}")
    print(f"  Box metadata root:                   {provenance.box_year_root}")
    print(f"  S3 objects to upload and verify:     {len(plan.objects)}")
    print(f"  Data in next submission:             {plan.total_bytes / 1e9:.2f} GB")


def _print_verification(check: dict, *, heading: str = "S3 verification") -> int:
    """Print one verification result and return its command exit code."""

    print(f"{heading}: {check['present']}/{check['checked']} objects present with matching sizes")
    for key in check["missing"][:20]:
        print(f"  MISSING     {key}")
    for item in check["mismatched"][:20]:
        print(f"  SIZE DIFFER {item['key']} local={item['local']} remote={item['remote']}")

    if check["ok"]:
        print("All planned objects verified successfully.")
        return 0

    print(
        "\nSome objects are missing or differ. If SoundHub has already ingested "
        "this submission the landing zone is drained and an empty result is "
        "expected — check the platform before re-uploading."
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build a cumulative bird-audio staging batch, then upload only its "
            "unsubmitted recordings to Wildlife SoundHub."
        ),
    )
    parser.add_argument("--staging", help="Staging root (default: config.json soundhub.staging_root)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_stage = sub.add_parser(
        "stage",
        help="Add or refresh deployment events in the cumulative staging batch",
    )
    target = p_stage.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--deployment", help="One field-data deployment-event folder to add or refresh"
    )
    target.add_argument(
        "--root", help="Find and add deployment-event folders below this directory"
    )
    p_stage.add_argument(
        "--box-year-root",
        help="Override Box field_data/<year> only when checking an existing batch",
    )
    p_stage.set_defaults(func=cmd_stage)

    p_status = sub.add_parser(
        "status", help="Summarize the full cumulative staging set and destination"
    )
    p_status.set_defaults(func=cmd_status)

    p_upload = sub.add_parser(
        "upload",
        help="Preflight, upload the pending batch, verify it, and update Box",
    )
    upload_mode = p_upload.add_mutually_exclusive_group()
    upload_mode.add_argument(
        "--verify-only",
        action="store_true",
        help="Compare the full staged tree with the current landing-zone contents",
    )
    upload_mode.add_argument(
        "--preflight-only",
        action="store_true",
        help="Show the exact pending transfer and Box updates without writing",
    )
    p_upload.add_argument(
        "--box-year-root",
        help="Override the automatically inferred Box field_data/<year> folder",
    )
    p_upload.add_argument(
        "--submitter",
        default=DEFAULT_SUBMITTER,
        help=f"SoundHub submitter written to Box (default: {DEFAULT_SUBMITTER})",
    )
    p_upload.set_defaults(func=cmd_upload)

    p_clear = sub.add_parser(
        "clear-completed",
        help="Preview or clear a completed local staging batch after acceptance",
    )
    p_clear.add_argument(
        "--apply",
        action="store_true",
        help="Delete the preflighted derived local batch (default is dry run)",
    )
    p_clear.add_argument(
        "--box-year-root",
        help="Override the automatically inferred Box field_data/<year> folder",
    )
    p_clear.set_defaults(func=cmd_clear_completed)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
