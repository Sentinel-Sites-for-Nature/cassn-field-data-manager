#!/usr/bin/env python3
"""Generate a CA-SSN data-collection summary from filed Box metadata.

The default is a read-only preview.  Pass ``--apply`` to write the approved
three-sheet XLSX report, plus a same-named JSON sidecar for dashboards,
beneath a date-of-generation folder.  The WI tracker is the newest
``WI Processing Tracker*`` export in ~/Downloads unless ``--wi-tracker`` names
one; it is moved into that folder so each report keeps the snapshot it used.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cassn.config import PACIFIC_TZ  # noqa: E402
from cassn.reporting.data_collection_summary import (  # noqa: E402
    BOX_REPORTS_FOLDER_ID,
    build_data_collection_summary,
    dated_output_path,
    default_box_reports_root,
    default_box_year_root,
    default_downloads_dir,
    discover_box_year_metadata,
    find_latest_wi_tracker_download,
    json_sidecar_path,
    load_wi_submission_tracker,
    publish_data_collection_summary,
    wi_tracker_snapshot_path,
)

STALE_DOWNLOAD_AGE = timedelta(hours=6)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, required=True, help="Box data year folder")
    parser.add_argument(
        "--box-year-root",
        type=Path,
        help="Override the Box Drive data/<year> source folder",
    )
    tracker = parser.add_mutually_exclusive_group()
    tracker.add_argument(
        "--wi-tracker",
        type=Path,
        help=(
            "CSV or XLSX export of the WI Processing Tracker; default is the newest "
            "'WI Processing Tracker*' file in --downloads-dir, moved into the report folder"
        ),
    )
    tracker.add_argument(
        "--no-wi-tracker",
        action="store_true",
        help="Summarize from Box metadata provenance only",
    )
    parser.add_argument(
        "--downloads-dir",
        type=Path,
        default=default_downloads_dir(),
        help="Where to look for the WI tracker export (default: ~/Downloads)",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=default_box_reports_root(),
        help=(
            "Report root; default is the Box Drive folder corresponding to "
            f"Box folder id {BOX_REPORTS_FOLDER_ID}"
        ),
    )
    parser.add_argument(
        "--generation-date",
        type=date.fromisoformat,
        help="Override date folder (YYYY-MM-DD); primarily for reproducible runs",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the XLSX report and JSON sidecar. Default is a read-only preview.",
    )
    return parser


def _print_summary(summary, output_path: Path, *, apply: bool) -> None:
    totals = summary.totals
    print(f"Mode: {'apply' if apply else 'dry-run'}")
    print(f"Box reporting year: {summary.year}")
    print(f"Metadata files scanned: {summary.metadata_files_scanned}")
    print(f"Sites with confirmed Box-uploaded data: {summary.site_count:,}")
    print(f"Images captured: {totals.images_captured:,}")
    print(f"Images classified with AI: {totals.images_classified_ai:,}")
    print(f"Images validated by humans: {totals.images_validated_human:,}")
    print(f"Bird audio recorded: {float(totals.bird_minutes_recorded):,.1f} minutes")
    print(
        "Bird audio classified with AI: "
        f"{float(totals.bird_minutes_classified_ai):,.1f} minutes"
    )
    print(f"Bat audio recorded: {float(totals.bat_minutes_recorded):,.1f} minutes")
    print(f"Total data volume: {float(totals.data_gb):,.2f} GB")
    print(f"Unconfirmed Box-upload rows excluded: {summary.quality_counts['unconfirmed_box_upload_rows']:,}")
    print(f"Audio rows missing exact duration: {summary.quality_counts['missing_audio_duration_rows']:,}")
    if summary.wi_tracker_source:
        print(f"WI tracker: {summary.wi_tracker_source}")
    for warning in summary.warnings:
        print(f"WARNING: {warning}")
    for error in summary.errors[:20]:
        print(f"ERROR: {error}")
    if len(summary.errors) > 20:
        print(f"ERROR: {len(summary.errors) - 20:,} additional validation errors omitted")
    print(f"Would write: {output_path}")
    print(f"Would write: {json_sidecar_path(output_path)}")
    if summary.wi_tracker_source:
        print(f"Would archive WI tracker as: {wi_tracker_snapshot_path(output_path, summary.wi_tracker_source)}")


def _resolve_tracker(args) -> tuple[Path | None, bool]:
    """Return the tracker export and whether it was found in Downloads."""
    if args.no_wi_tracker:
        return None, False
    if args.wi_tracker:
        return args.wi_tracker, False
    found = find_latest_wi_tracker_download(args.downloads_dir)
    if found is None:
        raise FileNotFoundError(
            f"No 'WI Processing Tracker' export in {args.downloads_dir}. Download it from "
            "Google Sheets (File > Download > .xlsx), or pass --wi-tracker or --no-wi-tracker."
        )
    modified = datetime.fromtimestamp(found.stat().st_mtime, PACIFIC_TZ)
    print(f"WI tracker download: {found} (downloaded {modified:%Y-%m-%d %H:%M})")
    if datetime.now(PACIFIC_TZ) - modified > STALE_DOWNLOAD_AGE:
        print(
            "WARNING: this tracker download is more than "
            f"{STALE_DOWNLOAD_AGE.seconds // 3600} hours old; re-download it if the sheet has changed."
        )
    return found, True


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    year_root = args.box_year_root or default_box_year_root(args.year)
    try:
        tracker_path, from_downloads = _resolve_tracker(args)
        documents = discover_box_year_metadata(year_root)
        tracker = load_wi_submission_tracker(tracker_path) if tracker_path else None
        summary = build_data_collection_summary(args.year, documents, wi_tracker=tracker)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    generation_date = args.generation_date or datetime.now(PACIFIC_TZ).date()
    output_path = dated_output_path(args.output_root, args.year, generation_date)
    _print_summary(summary, output_path, apply=args.apply)
    if not summary.ok:
        print("Report was not written because validation errors were found.", file=sys.stderr)
        return 2
    if not args.apply:
        print("Dry run only; no files were written.")
        return 0

    generated_at = datetime.now(PACIFIC_TZ)
    try:
        workbook, sidecar = publish_data_collection_summary(summary, output_path, generated_at=generated_at)
    except Exception as exc:
        print(f"ERROR: could not write report: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote: {workbook}")
    print(f"Wrote: {sidecar}")
    if summary.wi_tracker_source:
        snapshot = wi_tracker_snapshot_path(workbook, summary.wi_tracker_source)
        print(f"Wrote: {snapshot}")
        source = Path(summary.wi_tracker_source)
        if from_downloads and source != snapshot:
            try:
                source.unlink()
                print(f"Removed download: {source}")
            except OSError as exc:
                print(f"WARNING: report written, but could not remove {source}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
