"""
``deployment.csv`` and ``recording.csv`` generation.

Both are projections of ``audio_file_metadata.csv``. Every SoundHub deployment
column except ``project_short_name`` is already an ``AUDIO_FIELDS`` column under
the same name, and ``recorded_datetime`` already holds the per-file timestamp
the app reads out of each WAV's GUANO chunk. So there is nothing to re-derive
here: no second pass over the audio, no separate GUANO parse, no filename
parsing.

Both CSVs live at the *project* root in S3, not per deployment, so they are
cumulative across every deployment ever submitted. Each staging run writes its
own rows to a fragment under ``.cassn_fragments/<deployment_id>/`` and then
rebuilds the two project-level CSVs from every fragment present. That makes
re-staging a deployment idempotent — the fragment is replaced, not appended —
and lets a re-upload overwrite the project CSVs in place, which the IAM role
permits even though it cannot delete.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

from cassn.config import (
    SOUNDHUB_DATETIME_SEPARATOR,
    SOUNDHUB_DEPLOYMENT_FIELDS,
    SOUNDHUB_DEVICE_TYPE,
    SOUNDHUB_PROJECT_SHORT_NAME,
    SOUNDHUB_RECORDING_FIELDS,
)
from cassn.soundhub.staging import fragments_root, project_root

DEPLOYMENT_CSV = "deployment.csv"
RECORDING_CSV = "recording.csv"


def read_bd_audio_rows(deployment_folder) -> list[dict]:
    """Return the BD *audio* rows of a deployment's ``audio_file_metadata.csv``.

    Excludes bat (BT) devices, which go to NABat rather than SoundHub, and the
    ``CONFIG.TXT`` sidecar rows, which are not recordings.
    """
    csv_path = Path(deployment_folder) / "audio_file_metadata.csv"
    if not csv_path.exists():
        raise FileNotFoundError(
            f"No audio_file_metadata.csv in {deployment_folder}. Generate the "
            "deployment's metadata before staging for SoundHub."
        )
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return [
        r for r in rows
        if r.get("device_type") == SOUNDHUB_DEVICE_TYPE and r.get("file_type") == "audio"
    ]


def group_by_deployment(rows: list[dict]) -> dict[str, list[dict]]:
    """Group audio rows by ``deployment_id``, preserving file order."""
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row.get("deployment_id", ""), []).append(row)
    return grouped


def _format_datetime(value: str, offset_seconds: float = 0.0) -> str:
    """Render an ISO 8601 timestamp in SoundHub's recording format.

    ``recorded_datetime`` is already ISO 8601 with the device's UTC offset, so
    this only changes the separator and optionally advances the clock by a
    recording's duration. Returns '' for a blank or unparseable input rather
    than inventing a timestamp.
    """
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return ""
    if offset_seconds:
        moment = moment + timedelta(seconds=offset_seconds)
    return moment.isoformat(sep=SOUNDHUB_DATETIME_SEPARATOR)


def build_deployment_rows(audio_rows: list[dict]) -> list[dict]:
    """One ``deployment.csv`` row per deployment, in the template's column order.

    Deployment dates and times are passed through unchanged: unlike the
    recording timestamps, SoundHub's deployment template carries no UTC offset.
    """
    out: list[dict] = []
    for deployment_id, rows in group_by_deployment(audio_rows).items():
        source = rows[0]
        row = {field: source.get(field, "") for field in SOUNDHUB_DEPLOYMENT_FIELDS}
        row["project_short_name"] = SOUNDHUB_PROJECT_SHORT_NAME
        row["deployment_id"] = deployment_id
        out.append(row)
    return sorted(out, key=lambda r: r["deployment_id"])


def build_recording_rows(audio_rows: list[dict]) -> list[dict]:
    """One ``recording.csv`` row per FLAC.

    ``start`` is the GUANO-derived recording timestamp; ``end`` is that plus the
    recording's measured duration. The filename is the *staged* name — SoundHub
    receives ``.flac``, while the app's inventory names the source ``.wav``.
    """
    out: list[dict] = []
    for row in audio_rows:
        start = row.get("recorded_datetime", "")
        try:
            duration = float(row.get("recording_duration_sec") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        out.append(
            {
                "filename": Path(row.get("filename", "")).with_suffix(".flac").name,
                "deployment_id": row.get("deployment_id", ""),
                "start": _format_datetime(start),
                "end": _format_datetime(start, duration) if duration else "",
            }
        )
    return out


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    """Write a CSV atomically, so a crash cannot leave a half-written manifest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def write_deployment_fragments(staging_root, audio_rows: list[dict]) -> list[Path]:
    """Record each deployment's CSV rows outside the S3 mirror, one dir per id.

    A CA-SSN deployment event spans several plots, and each plot is a separate
    SoundHub deployment, so the rows are split by ``deployment_id`` rather than
    written as one blob. Fragments are the durable per-deployment record that
    :func:`refresh_project_csvs` rebuilds the project-level CSVs from; keying
    them by id means re-staging replaces a deployment's rows instead of
    duplicating them. Use :func:`write_deployment_copy` for the copy that
    travels to Box.
    """
    written: list[Path] = []
    for deployment_id, rows in group_by_deployment(audio_rows).items():
        fragment_dir = fragments_root(staging_root) / deployment_id
        _write_csv(
            fragment_dir / DEPLOYMENT_CSV,
            SOUNDHUB_DEPLOYMENT_FIELDS,
            build_deployment_rows(rows),
        )
        _write_csv(
            fragment_dir / RECORDING_CSV,
            SOUNDHUB_RECORDING_FIELDS,
            build_recording_rows(rows),
        )
        written.append(fragment_dir)
    return written


def write_deployment_copy(deployment_folder, audio_rows: list[dict]) -> Path:
    """Write both CSVs into the deployment folder so they reach Box too."""
    out_dir = Path(deployment_folder) / "soundhub"
    _write_csv(
        out_dir / DEPLOYMENT_CSV,
        SOUNDHUB_DEPLOYMENT_FIELDS,
        build_deployment_rows(audio_rows),
    )
    _write_csv(
        out_dir / RECORDING_CSV,
        SOUNDHUB_RECORDING_FIELDS,
        build_recording_rows(audio_rows),
    )
    return out_dir


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def refresh_project_csvs(staging_root) -> dict:
    """Rebuild the two project-level CSVs from every fragment in staging.

    This is what makes re-staging safe: a deployment's rows are replaced by its
    fragment rather than appended to a growing file, so running the pipeline
    twice produces the same two CSVs both times.
    """
    fragments = fragments_root(staging_root)
    deployment_rows: list[dict] = []
    recording_rows: list[dict] = []

    for fragment_dir in sorted(p for p in fragments.glob("*") if p.is_dir()):
        deployment_rows.extend(_read_csv(fragment_dir / DEPLOYMENT_CSV))
        recording_rows.extend(_read_csv(fragment_dir / RECORDING_CSV))

    deployment_rows.sort(key=lambda r: r.get("deployment_id", ""))
    recording_rows.sort(key=lambda r: (r.get("deployment_id", ""), r.get("filename", "")))

    root = project_root(staging_root)
    _write_csv(root / DEPLOYMENT_CSV, SOUNDHUB_DEPLOYMENT_FIELDS, deployment_rows)
    _write_csv(root / RECORDING_CSV, SOUNDHUB_RECORDING_FIELDS, recording_rows)

    return {
        "deployment_csv": root / DEPLOYMENT_CSV,
        "recording_csv": root / RECORDING_CSV,
        "deployment_count": len(deployment_rows),
        "recording_count": len(recording_rows),
    }
