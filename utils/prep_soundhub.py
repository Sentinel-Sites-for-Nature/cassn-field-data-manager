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
    read_bd_audio_rows,
    refresh_project_csvs,
    write_deployment_copy,
    write_deployment_fragments,
)
from cassn.soundhub.staging import (  # noqa: E402
    SoundHubStagingError,
    flac_available,
    project_root,
    stage_deployment,
)
from cassn.soundhub.upload import (  # noqa: E402
    SoundHubUploadError,
    load_soundhub_config,
    project_prefix,
    staged_objects,
    upload_project,
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
    staging_root = Path(args.staging or settings["staging_root"])

    if args.deployment:
        deployments = [Path(args.deployment)]
    else:
        deployments = find_deployments(Path(args.root))
    if not deployments:
        print(f"No deployment folders with audio_file_metadata.csv found under {args.root}")
        return 1

    print(f"Staging root: {staging_root}")
    print(f"Deployments:  {len(deployments)}\n")

    failures = 0
    for folder in deployments:
        try:
            audio_rows = read_bd_audio_rows(folder)
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
    print(
        f"Project manifests rebuilt: {csvs['deployment_count']} deployment(s), "
        f"{csvs['recording_count']} recording(s)"
    )
    print(f"  {csvs['deployment_csv']}")
    print(f"  {csvs['recording_csv']}")
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
        key = head if "/" in obj["relative"] else "(project manifests)"
        by_deployment.setdefault(key, []).append(obj)

    total_bytes = sum(o["size"] for o in objects)
    print(f"Staged at:   {root}")
    print(f"Destination: s3://{settings['bucket']}/{project_prefix(settings)}/\n")
    print(f"{'Deployment':<44}{'Files':>7}{'Size':>14}")
    print("-" * 65)
    for name, group in sorted(by_deployment.items()):
        size = sum(o["size"] for o in group)
        print(f"{name:<44}{len(group):>7}{size / 1e9:>12.2f} GB")
    print("-" * 65)
    print(f"{'TOTAL':<44}{len(objects):>7}{total_bytes / 1e9:>12.2f} GB")
    return 0


def cmd_upload(args) -> int:
    settings = load_soundhub_config()
    staging_root = Path(args.staging or settings["staging_root"])

    def show(current, total, name):
        print(f"  {current}/{total}  {name}", flush=True)

    try:
        if args.verify_only:
            check = verify_project(staging_root, settings=settings)
        else:
            print(f"Uploading to s3://{settings['bucket']}/{project_prefix(settings)}/\n")
            result = upload_project(staging_root, settings=settings, progress=show)
            print(
                f"\nUploaded {result['uploaded']} object(s), "
                f"skipped {result['skipped']} already present "
                f"({result['uploaded_bytes'] / 1e9:.2f} GB sent)\n"
            )
            check = verify_project(staging_root, settings=settings)
    except SoundHubUploadError as e:
        print(f"ERROR: {e}")
        return 1

    print(f"Verification: {check['present']}/{check['checked']} objects present in the bucket")
    for key in check["missing"][:20]:
        print(f"  MISSING     {key}")
    for item in check["mismatched"][:20]:
        print(f"  SIZE DIFFER {item['key']} local={item['local']} remote={item['remote']}")

    if check["ok"]:
        print("\nAll staged objects verified against the bucket.")
        return 0

    print(
        "\nSome objects are missing or differ. If SoundHub has already ingested "
        "this submission the landing zone is drained and an empty result is "
        "expected — check the platform before re-uploading."
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stage and upload bird audio to Wildlife SoundHub.",
    )
    parser.add_argument("--staging", help="Staging root (default: config.json soundhub.staging_root)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_stage = sub.add_parser("stage", help="Convert BD WAVs to FLAC and refresh the project CSVs")
    target = p_stage.add_mutually_exclusive_group(required=True)
    target.add_argument("--deployment", help="A single deployment folder")
    target.add_argument("--root", help="Scan below this folder for deployments")
    p_stage.set_defaults(func=cmd_stage)

    p_status = sub.add_parser("status", help="Show what is staged and where it will land")
    p_status.set_defaults(func=cmd_status)

    p_upload = sub.add_parser("upload", help="Push the staged tree to S3 and verify")
    p_upload.add_argument(
        "--verify-only",
        action="store_true",
        help="Skip the transfer; only reconcile staging against the bucket",
    )
    p_upload.set_defaults(func=cmd_upload)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
