#!/usr/bin/env python3
"""
generate_occurrences.py

Generates an occurrence record CSV by joining pre-processing file metadata
with post-processing Wildlife Insights image results.

Matches on:  file_metadata.new_filename == images.filename
Excludes:    Blank, Human, Vehicle, No CV Result, and empty identifications
Applies to:  Camera trap devices (ML, SA) — not applicable to audio (BD, BT)

Usage:
    python generate_occurrences.py <file_metadata.csv> <images.csv> <output_dir>

Arguments:
    file_metadata_path   Path to file_metadata.csv (pre-processing)
    images_path          Path to images.csv from Wildlife Insights (post-processing)
    output_dir           Directory to write the occurrences CSV

Output filename is derived from the deployment_id in images.csv:
    e.g. UC_Cahill_20260402_occurrences.csv

Output columns:
    filename, recorded_datetime, latitude, longitude,
    class, order, family, genus, species, common_name

Example:
    python utils/generate_occurrences.py \\
        ~/Desktop/CASSN_field_data_staging/UC_Cahill_20260402/file_metadata.csv \\
        ~/Downloads/images.csv \\
        ~/Desktop/CASSN_field_data_staging/UC_Cahill_20260402/
"""

import csv
import re
import sys
from pathlib import Path

# ── Exclusion list ─────────────────────────────────────────────────────────────
EXCLUDE_COMMON_NAMES = {"Blank", "Human", "Vehicle", "No CV Result", ""}


def deployment_event_id_from_deployment_id(deployment_id: str) -> str:
    """Remove plot/device/sequence identity from a deployment ID."""
    parts = deployment_id.split("_")
    try:
        plot_idx = next(
            index
            for index, part in enumerate(parts)
            if part.lower().startswith("plot")
        )
    except StopIteration:
        return deployment_id
    date_token = re.sub(r"-seq\d{2,}$", "", parts[-1])
    return "_".join(parts[:plot_idx]) + "_" + date_token


def generate_occurrences(file_metadata_path, images_path, output_dir):
    file_metadata_path = Path(file_metadata_path)
    images_path        = Path(images_path)
    output_dir         = Path(output_dir)

    # Validate inputs
    if not file_metadata_path.exists():
        print(f"ERROR: file_metadata not found: {file_metadata_path}")
        sys.exit(1)
    if not images_path.exists():
        print(f"ERROR: images.csv not found: {images_path}")
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Load file metadata keyed by filename ──────────────────────────────────
    print("Loading file metadata...")
    file_meta = {}
    with open(file_metadata_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            file_meta[row["new_filename"]] = row
    print(f"  {len(file_meta)} files loaded")

    # ── Derive output filename from deployment_id in images.csv ──────────────
    # deployment_id is plot/device-specific: ORG_SITE_plotN_DEVTYPE_YYYYMMDD
    # Strip plot and device suffix to get deployment event ID: ORG_SITE_YYYYMMDD
    raw_deployment_id = None
    with open(images_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            raw_deployment_id = row.get("deployment_id", "").strip()
            if raw_deployment_id:
                break

    if raw_deployment_id:
        event_id = deployment_event_id_from_deployment_id(raw_deployment_id)
        output_filename = f"{event_id}_occurrences.csv"
    else:
        output_filename = "occurrences.csv"

    output_file = output_dir / output_filename

    # ── Join with WI images.csv and filter ────────────────────────────────────
    print("Processing Wildlife Insights results...")
    occurrences = []
    unmatched   = 0

    with open(images_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            common_name = row.get("common_name", "").strip()

            # Skip non-animal rows
            if common_name in EXCLUDE_COMMON_NAMES:
                continue

            filename = row.get("filename", "").strip()
            meta = file_meta.get(filename)

            if meta is None:
                unmatched += 1
                continue

            occurrences.append({
                "filename":          filename,
                "recorded_datetime": meta.get("recorded_datetime", ""),
                "latitude":          meta.get("latitude", ""),
                "longitude":         meta.get("longitude", ""),
                "class":             row.get("class", ""),
                "order":             row.get("order", ""),
                "family":            row.get("family", ""),
                "genus":             row.get("genus", ""),
                "species":           row.get("species", ""),
                "common_name":       common_name,
            })

    # ── Write output ──────────────────────────────────────────────────────────
    fieldnames = [
        "filename", "recorded_datetime", "latitude", "longitude",
        "class", "order", "family", "genus", "species", "common_name"
    ]

    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(occurrences)

    print(f"\n  Occurrences written: {len(occurrences)}")
    if unmatched:
        print(f"  WARNING: {unmatched} WI rows had no match in file_metadata.csv")
    print(f"\nOutput: {output_file}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python generate_occurrences.py <file_metadata.csv> <images.csv> <output_dir>")
        sys.exit(1)

    generate_occurrences(
        file_metadata_path=sys.argv[1],
        images_path=sys.argv[2],
        output_dir=sys.argv[3],
    )
