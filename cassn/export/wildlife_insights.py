"""
Wildlife Insights deployment-CSV export.

WI ingests a 27-column deployment CSV (one row per camera deployment). Two
call sites need that CSV, and the original carried two near-identical copies of
the column list and the writer:

* the **GUI** generates it by reshaping the already-written
  ``image_file_metadata.csv`` (one row per unique camera ``deployment_id``);
* compatibility callers can build it directly from a deployment's devices plus
  an event-scoped camera view, plot coordinates, and ``wi_config.json``. In the
  active app that camera view comes from device-level ``deployments.csv``.

This module is the single source of truth for both. :data:`WI_COLUMNS`, the
``event_name`` convention (:func:`deployment_event_name`), and the
``subproject`` (Site_Year) convention (:func:`subproject_for`) are defined once;
:func:`build_wi_rows` is the CLI builder, :func:`reshape_image_metadata_to_wi`
the GUI builder, and :func:`rows_to_csv_bytes` /
:func:`generate_wi_deployments_from_image_csv` are the two writers.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Callable

from cassn.config import CAMERA_DEVICE_TYPES

# WI deployment CSV schema. Column order is significant and must not change.
WI_COLUMNS = [
    "project_id", "deployment_id", "subproject_name", "subproject_design",
    "placename", "longitude", "latitude", "start_date", "end_date",
    "event_name", "event_description", "event_type", "bait_type", "bait_description",
    "feature_type", "feature_type_methodology", "camera_id", "quiet_period",
    "camera_functioning", "sensor_height", "height_other", "sensor_orientation",
    "orientation_other", "recorded_by", "plot_treatment", "plot_treatment_description",
    "detection_distance",
]

WI_COORDINATE_QUANTUM = Decimal("0.00000001")

# Literal description of the convention implemented by :func:`subproject_for`.
# This is methodology text for downstream templates, not a per-row subproject
# value.
SUBPROJECT_DESIGN = "<Site>_<SamplingYear>"


def format_wi_coordinate(value: object) -> str:
    """Return a coordinate with exactly eight digits after the decimal.

    Wildlife Insights accepts between four and eight decimal places. Using a
    fixed eight preserves all accepted precision and pads shorter coordinates
    deterministically. Blank and non-numeric values are returned unchanged so
    the existing QC checks can report them instead of masking bad source data.
    """
    if value is None:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    try:
        coordinate = Decimal(raw)
        if not coordinate.is_finite():
            return raw
        rounded = coordinate.quantize(WI_COORDINATE_QUANTUM, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return raw

    # Avoid producing a surprising signed zero after rounding a tiny negative.
    if rounded == 0:
        rounded = abs(rounded)
    return format(rounded, ".8f")


def deployment_event_name(start_date: str, end_date: str) -> str:
    """Return the ``YYYYMMM-YYYYMMM`` event name, e.g. ``2025NOV-2026JAN``.

    Empty string if either date doesn't parse. This is the one definition of
    the convention; the metadata CSV writer and :func:`build_wi_rows` both use it.
    """
    try:
        s = datetime.strptime(start_date, "%Y-%m-%d")
        e = datetime.strptime(end_date, "%Y-%m-%d")
        return f"{s.strftime('%Y%b').upper()}-{e.strftime('%Y%b').upper()}"
    except Exception:
        return ""


def subproject_for(site_short_name: str, end_date: str) -> str:
    """Return the ``Site_Year`` subproject grouping, e.g. ``ABCD_2026``.

    Groups every deployment at a site within a calendar year into one
    subproject; the year is taken from the deployment end date. Returns an
    empty string if the site or a 4-digit year is missing. This is the single
    definition of the convention, shared by the metadata CSV writer
    (:mod:`cassn.export.metadata_csv`) and the WI builder below.
    """
    year = (end_date or "")[:4]
    if not site_short_name or len(year) != 4 or not year.isdigit():
        return ""
    return f"{site_short_name}_{year}"


def rows_to_csv_bytes(rows: list[dict]) -> bytes:
    """Encode WI rows as UTF-8 CSV bytes (used for Box uploads in the CLI)."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=WI_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------
# CLI builder — from devices + lookups
# ---------------------------------------------------------------------------

def build_wi_rows(
    deployment_info: dict,
    devices: list[dict],
    cameras: dict,
    plot_coords: dict,
    wi_config: dict,
    *,
    log: Callable[[str], None] | None = None,
) -> dict[str, list[dict]]:
    """Build WI rows directly from a deployment's devices and the lookup tables.

    Returns ``{dev_type: [row_dict, ...]}`` for ML and SA devices only. ``log``
    receives a warning for any camera missing a ``camera_id``; when ``None``,
    those warnings are dropped.
    """
    _log = log or (lambda _m: None)

    site_short_name = deployment_info.get("site_short_name", "")
    org = deployment_info.get("organization", "")
    start = deployment_info.get("deployment_start", "")
    end = deployment_info.get("deployment_end", "")
    observer = deployment_info.get("observer", "")

    subproject_name = subproject_for(site_short_name, end)
    event_name = deployment_event_name(start, end)

    rows_by_type: dict[str, list[dict]] = {}

    for device in devices:
        dev_type = device.get("device_type", "")
        if dev_type not in CAMERA_DEVICE_TYPES:
            continue

        plot_num = device.get("plot_number")
        try:
            plot_num_int = int(plot_num)
        except (TypeError, ValueError):
            plot_num_int = None

        cam = cameras.get((site_short_name, plot_num_int, dev_type), {}) if plot_num_int else {}
        coords = plot_coords.get((site_short_name, plot_num_int), {}) if plot_num_int else {}

        camera_id = cam.get("camera_id", "")
        if not camera_id:
            _log(
                f"  Warning: camera_id missing for {site_short_name} "
                f"plot {plot_num} {dev_type}"
            )

        row = {
            "project_id": wi_config.get(f"project_id_{dev_type}", ""),
            "deployment_id": (
                f"{org}_{site_short_name}_plot{plot_num}_{dev_type}_"
                f"{end.replace('-', '')}"
            ),
            "subproject_name": subproject_name,
            "subproject_design": "",
            "placename": f"{site_short_name}_plot{plot_num}",
            "longitude": format_wi_coordinate(coords.get("longitude", "")),
            "latitude": format_wi_coordinate(coords.get("latitude", "")),
            "start_date": f"{start} 00:00:00" if start else "",
            "end_date": f"{end} 23:59:59" if end else "",
            "event_name": event_name,
            "event_description": "",
            "event_type": wi_config.get("event_type", "Temporal"),
            "bait_type": wi_config.get(f"bait_type_{dev_type}", ""),
            "bait_description": wi_config.get(f"bait_description_{dev_type}", ""),
            "feature_type": cam.get("feature_type", ""),
            "feature_type_methodology": "",
            "camera_id": camera_id,
            "quiet_period": wi_config.get("quiet_period", 0),
            "camera_functioning": wi_config.get("camera_functioning_default", "Camera Functioning"),
            "sensor_height": cam.get("sensor_height", "Knee height"),
            "height_other": "",
            "sensor_orientation": cam.get("sensor_orientation", "Parallel" if dev_type == "ML" else "Pointed Downward"),
            "orientation_other": "",
            "recorded_by": observer,
            "plot_treatment": "",
            "plot_treatment_description": "",
            "detection_distance": "",
        }
        rows_by_type.setdefault(dev_type, []).append(row)

    return rows_by_type


# ---------------------------------------------------------------------------
# GUI builder — reshape from image_file_metadata.csv
# ---------------------------------------------------------------------------

def reshape_image_metadata_to_wi(image_csv_path: Path) -> dict[str, list[dict]]:
    """Reshape ``image_file_metadata.csv`` into WI deployment rows.

    Keeps one row per unique ML/SA camera ``deployment_id`` (first occurrence
    wins). Returns ``{dev_type: [row_dict, ...]}``. The WI columns are pulled
    straight from the image metadata, which already carries them.
    """
    seen: set[str] = set()
    rows_by_type: dict[str, list[dict]] = {}
    with open(image_csv_path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            if row.get('file_type') != 'image':
                continue
            dev_type = row.get('device_type', '')
            if dev_type not in CAMERA_DEVICE_TYPES:
                continue
            dep_id = row.get('deployment_id', '')
            if dep_id in seen:
                continue
            seen.add(dep_id)
            wi_row = {
                "project_id":              row.get('project_id', ''),
                "deployment_id":           dep_id,
                "subproject_name":         row.get('subproject', ''),
                "subproject_design":       row.get('subproject_design', ''),
                "placename":               row.get('placename', ''),
                "longitude":               format_wi_coordinate(row.get('longitude', '')),
                "latitude":                format_wi_coordinate(row.get('latitude', '')),
                "start_date":              row.get('start_date', ''),
                "end_date":                row.get('end_date', ''),
                "event_name":              row.get('event_name', ''),
                "event_description":       row.get('event_description', ''),
                "event_type":              row.get('event_type', ''),
                "bait_type":               row.get('bait_type', ''),
                "bait_description":        row.get('bait_description', ''),
                "feature_type":            row.get('feature_type', ''),
                "feature_type_methodology": row.get('feature_type_methodology', ''),
                "camera_id":               row.get('camera_id', ''),
                "quiet_period":            row.get('quiet_period', ''),
                "camera_functioning":      row.get('camera_functioning', ''),
                "sensor_height":           row.get('sensor_height', ''),
                "height_other":            row.get('height_other', ''),
                "sensor_orientation":      row.get('sensor_orientation', ''),
                "orientation_other":       row.get('orientation_other', ''),
                "recorded_by":             row.get('recorded_by', ''),
                "plot_treatment":          row.get('plot_treatment', ''),
                "plot_treatment_description": row.get('plot_treatment_description', ''),
                "detection_distance":      row.get('detection_distance', ''),
            }
            rows_by_type.setdefault(dev_type, []).append(wi_row)
    return rows_by_type


def generate_wi_deployments_from_image_csv(
    deployment_folder: Path,
    *,
    log: Callable[[str], None] = lambda _m: None,
) -> list[str]:
    """Generate ``WI_metadata/wildlife_insights_<type>_deployments.csv`` from the image CSV.

    Mirrors the GUI's behavior: skips cleanly when the image CSV is absent,
    writes one CSV per camera device type, and logs an incremental row-count
    diff for each. Returns short status lines for the review pane.
    """
    status_lines: list[str] = []

    img_path = deployment_folder / "image_file_metadata.csv"
    if not img_path.exists():
        msg = "Skipping WI export: image_file_metadata.csv not found"
        log(msg)
        status_lines.append(msg)
        return status_lines

    try:
        rows_by_type = reshape_image_metadata_to_wi(img_path)
    except Exception as e:
        msg = f"Error reading image_file_metadata.csv for WI export: {e}"
        log(msg)
        status_lines.append(msg)
        return status_lines

    wi_dir = deployment_folder / "WI_metadata"
    wi_dir.mkdir(exist_ok=True)

    for dev_type in sorted(rows_by_type):
        rows = rows_by_type[dev_type]
        filename = f"wildlife_insights_{dev_type}_deployments.csv"
        out_path = wi_dir / filename
        prev_count = -1
        if out_path.exists():
            try:
                with open(out_path, newline="", encoding="utf-8") as f:
                    prev_count = sum(1 for _ in csv.DictReader(f))
            except Exception:
                prev_count = -1
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=WI_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        new_count = len(rows)
        if prev_count < 0:
            msg = f"Generated: WI_metadata/{filename} ({new_count} rows)"
        elif new_count == prev_count:
            msg = f"Updated: WI_metadata/{filename} (no change, {new_count} rows)"
        else:
            delta = new_count - prev_count
            sign = "+" if delta > 0 else ""
            msg = f"Updated: WI_metadata/{filename} ({sign}{delta} rows, {new_count} total)"
        log(msg)
        status_lines.append(f"  - {filename} ({new_count} rows)")

    return status_lines
