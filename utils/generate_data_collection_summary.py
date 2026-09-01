#!/usr/bin/env python3
"""Generate a CA-SSN data-collection summary from filed Box metadata.

The default is a read-only preview.  Pass ``--apply`` to write the approved
three-sheet XLSX report beneath a date-of-generation folder.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cassn.config import PACIFIC_TZ  # noqa: E402
from cassn.reporting.data_collection_summary import (  # noqa: E402
    BOX_REPORTS_FOLDER_ID,
    build_data_collection_summary,
    dated_output_path,
    default_box_reports_root,
    default_box_year_root,
    discover_box_year_metadata,
    load_wi_submission_tracker,
    render_data_collection_summary_workbook,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, required=True, help="Box data year folder")
    parser.add_argument(
        "--box-year-root",
        type=Path,
        help="Override the Box Drive data/<year> source folder",
    )
    parser.add_argument(
        "--wi-tracker",
        type=Path,
        help="CSV or XLSX export of CASSN_WI_Validation_Tracker_v2",
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
        help="Write the XLSX report. Default is a read-only preview.",
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
    print(f"{'Wrote' if apply and summary.ok else 'Would write'}: {output_path}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    year_root = args.box_year_root or default_box_year_root(args.year)
    try:
        documents = discover_box_year_metadata(year_root)
        tracker = load_wi_submission_tracker(args.wi_tracker) if args.wi_tracker else None
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

    try:
        render_data_collection_summary_workbook(summary, output_path)
    except Exception as exc:
        print(f"ERROR: could not write report: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
