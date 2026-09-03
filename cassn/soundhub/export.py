"""``deployment.csv`` and ``recording.csv`` generation and validation.

Recording facts are projected from ``audio_file_metadata.csv``. Before writing
SoundHub deployment rows, the staging boundary refreshes the small set of
fields whose authoritative sources are the current curated deployment lookup,
the SoundHub protocol config, or the shared subproject convention. This makes
older, already-ingested metadata safe to stage without treating its historical
blank defaults as authoritative.

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
from cassn.export.wildlife_insights import (
    SUBPROJECT_DESIGN,
    format_wi_coordinate,
    subproject_for,
)
from cassn.soundhub.staging import SoundHubStagingError, fragments_root, project_root

DEPLOYMENT_CSV = "deployment.csv"
RECORDING_CSV = "recording.csv"


# An AudioMoth WAV that failed before any audio was written (e.g. an SD card
# write error) is header-only: RIFF + fmt + LIST + an empty data chunk, under
# 500 bytes in practice. The shortest real recording is orders of magnitude
# larger, so anything below this holds no audio.
_MIN_AUDIO_WAV_BYTES = 4096

# Fields CA-SSN can populate deterministically and requires before presenting a
# batch as ready to upload. SoundHub has additional optional columns; blanks in
# those are intentional and are not silently invented here.
REQUIRED_DEPLOYMENT_FIELDS = (
    "project_short_name",
    "deployment_id",
    "subproject",
    "subproject_design",
    "placename",
    "longitude",
    "latitude",
    "date_installed",
    "deployment_start_date",
    "deployment_start_time",
    "deployment_end_date",
    "deployment_end_time",
    "frequency",
    "duration",
    "gain",
    "ARU_make",
    "ARU_model",
    "ARU_container",
    "ARU_microphone",
    "mounted_on",
    "sensor_height_meters",
    "recorded_by",
)


def _holds_audio(row: dict) -> bool:
    """False for a recording known from its size to contain no audio.

    Only a parseable ``file_size_bytes`` below the header-only threshold
    excludes a row; a blank or malformed size keeps it, so a genuinely empty
    file that slips through still fails loudly at the encode step rather than
    being silently dropped.
    """
    size = str(row.get("file_size_bytes", "") or "")
    return not (size.isdigit() and int(size) < _MIN_AUDIO_WAV_BYTES)


def read_bd_audio_rows(deployment_folder) -> list[dict]:
    """Return the BD *audio* rows of a deployment's ``audio_file_metadata.csv``.

    Excludes bat (BT) devices, which go to NABat rather than SoundHub, the
    ``CONFIG.TXT`` sidecar rows, which are not recordings, and header-only WAVs
    from failed recordings, which contain no audio to submit — the failure
    itself stays documented in ``audio_file_metadata.csv``.
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
        if r.get("device_type") == SOUNDHUB_DEVICE_TYPE
        and r.get("file_type") == "audio"
        and _holds_audio(r)
    ]


def enrich_audio_rows(audio_rows: list[dict], lookups) -> list[dict]:
    """Fill legacy SoundHub blanks without changing source metadata.

    ``audio_file_metadata.csv`` remains the source for measured recording and
    CONFIG.TXT facts and any values already recorded there are preserved. For
    older metadata only, the current deployment lookup fills missing mounting
    and height, ``soundhub_config.json`` fills missing protocol constants, and
    the shared WI/SoundHub convention fills missing subproject fields.
    """
    enriched: list[dict] = []
    errors: list[str] = []
    config = lookups.soundhub_config

    for source in audio_rows:
        row = dict(source)
        deployment_id = str(row.get("deployment_id") or "").strip()
        placement = lookups.deployment_for_id(deployment_id)
        if not placement:
            errors.append(f"{deployment_id or '<blank>'}: no curated deployment row")
            continue
        if placement.get("device_type") != SOUNDHUB_DEVICE_TYPE:
            errors.append(
                f"{deployment_id}: curated device type is "
                f"{placement.get('device_type')!r}, expected {SOUNDHUB_DEVICE_TYPE!r}"
            )
            continue

        site = str(row.get("site_short_name") or placement.get("site_short_name") or "")
        end_date = str(
            row.get("deployment_end_date")
            or placement.get("deployment_end_date")
            or ""
        )
        expected_subproject = subproject_for(site, end_date)
        existing_subproject = str(row.get("subproject") or "").strip()
        if existing_subproject and existing_subproject != expected_subproject:
            errors.append(
                f"{deployment_id}: audio_file_metadata.csv subproject "
                f"{existing_subproject!r} does not match expected "
                f"{expected_subproject!r}"
            )
            continue
        row["subproject"] = existing_subproject or expected_subproject
        row["subproject_design"] = row.get("subproject_design") or SUBPROJECT_DESIGN
        row["mounted_on"] = row.get("mounted_on") or placement.get("mounted_on", "")
        row["sensor_height_meters"] = (
            row.get("sensor_height_meters")
            or placement.get("sensor_height_meters", "")
        )
        row["ARU_container"] = (
            row.get("ARU_container") or config.get("ARU_container_BD", "")
        )
        row["ARU_microphone"] = (
            row.get("ARU_microphone") or config.get("ARU_microphone", "")
        )
        row["feature_type"] = row.get("feature_type") or config.get("feature_type", "")
        # Preserve CONFIG/GUANO-derived make and model when present; the
        # protocol values are fallbacks for older metadata only. Firmware is not
        # sent to SoundHub and has no protocol default.
        row["ARU_make"] = row.get("ARU_make") or config.get("ARU_make", "")
        row["ARU_model"] = row.get("ARU_model") or config.get("ARU_model", "")
        enriched.append(row)

    if errors:
        raise SoundHubStagingError(
            "SoundHub metadata could not be resolved from the curated lookups:\n- "
            + "\n- ".join(sorted(set(errors)))
        )
    return enriched


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
        row["longitude"] = format_wi_coordinate(row.get("longitude", ""))
        row["latitude"] = format_wi_coordinate(row.get("latitude", ""))
        out.append(row)
    return sorted(out, key=lambda r: r["deployment_id"])


def build_recording_rows(audio_rows: list[dict]) -> list[dict]:
    """One ``recording.csv`` row per FLAC.

    ``start`` is the GUANO-derived recording timestamp; ``end`` is that plus the
    recording's measured duration. The filename is the *staged* name — SoundHub
    receives ``.flac``, while the app's inventory names the source ``.wav``.

    A row that carries a ``start`` but no usable duration is refused rather than
    written with a blank ``end``. ``recording.csv`` exists precisely to supply
    the per-file timestamps SoundHub cannot recover from our renamed files, so a
    blank ``end`` is a silently incomplete submission — and the landing zone
    cannot be deleted from once written. Header-only WAVs are already dropped by
    ``read_bd_audio_rows``, so every row reaching here holds audio and has a
    duration to find.
    """
    out: list[dict] = []
    missing: list[str] = []
    for row in audio_rows:
        start = row.get("recorded_datetime", "")
        name = Path(row.get("filename", "")).with_suffix(".flac").name
        try:
            duration = float(row.get("recording_duration_sec") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        if start and not duration:
            missing.append(name)
        out.append(
            {
                "filename": name,
                "deployment_id": row.get("deployment_id", ""),
                "start": _format_datetime(start),
                "end": _format_datetime(start, duration) if duration else "",
            }
        )
    if missing:
        shown = ", ".join(missing[:5])
        more = f" (and {len(missing) - 5} more)" if len(missing) > 5 else ""
        raise SoundHubStagingError(
            f"{len(missing)} recording(s) have a start timestamp but no usable "
            f"recording_duration_sec, which would stage an incomplete "
            f"recording.csv: {shown}{more}"
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


def _write_manifest_pair(out_dir: Path, audio_rows: list[dict]) -> None:
    """Validate both SoundHub manifests before replacing either file.

    Building ``recording.csv`` can fail when a recording timestamp has no
    recoverable duration.  Constructing both row sets first prevents that
    validation failure from leaving a new ``deployment.csv`` beside a missing
    or stale ``recording.csv``.
    """
    deployment_rows = build_deployment_rows(audio_rows)
    recording_rows = build_recording_rows(audio_rows)
    _write_csv(
        out_dir / DEPLOYMENT_CSV,
        SOUNDHUB_DEPLOYMENT_FIELDS,
        deployment_rows,
    )
    _write_csv(
        out_dir / RECORDING_CSV,
        SOUNDHUB_RECORDING_FIELDS,
        recording_rows,
    )


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
        _write_manifest_pair(fragment_dir, rows)
        written.append(fragment_dir)
    return written


def write_deployment_copy(deployment_folder, audio_rows: list[dict]) -> Path:
    """Write both CSVs into the deployment folder so they reach Box too."""
    out_dir = Path(deployment_folder) / "soundhub"
    _write_manifest_pair(out_dir, audio_rows)
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


def _require_header(path: Path, expected: list[str]) -> list[dict]:
    if not path.is_file():
        raise SoundHubStagingError(f"Missing SoundHub manifest: {path}")
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if list(reader.fieldnames or []) != expected:
            raise SoundHubStagingError(
                f"{path}: header does not match the SoundHub schema"
            )
        rows = list(reader)
    if any(None in row for row in rows):
        raise SoundHubStagingError(f"{path}: row has more values than the header")
    return rows


def _parse_iso_date(value: str, *, path: Path, row_number: int, field: str) -> None:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise SoundHubStagingError(
            f"{path} row {row_number}: {field} must use YYYY-MM-DD"
        ) from exc


def _parse_time(value: str, *, path: Path, row_number: int, field: str) -> None:
    for form in ("%H:%M", "%H:%M:%S"):
        try:
            datetime.strptime(value, form)
            return
        except ValueError:
            pass
    raise SoundHubStagingError(
        f"{path} row {row_number}: {field} must use 24-hour HH:MM or HH:MM:SS"
    )


def validate_staging_manifests(staging_root) -> dict:
    """Prove cumulative manifests are complete and exactly match fragments."""
    staging_root = Path(staging_root).expanduser().resolve()
    fragments = fragments_root(staging_root)
    fragment_dirs = sorted(path for path in fragments.glob("*") if path.is_dir())
    if not fragment_dirs:
        raise SoundHubStagingError(f"No SoundHub deployment fragments found in {fragments}")

    expected_deployments: list[dict] = []
    expected_recordings: list[dict] = []
    for fragment_dir in fragment_dirs:
        deployments = _require_header(
            fragment_dir / DEPLOYMENT_CSV, SOUNDHUB_DEPLOYMENT_FIELDS
        )
        recordings = _require_header(
            fragment_dir / RECORDING_CSV, SOUNDHUB_RECORDING_FIELDS
        )
        if len(deployments) != 1:
            raise SoundHubStagingError(
                f"{fragment_dir}: expected exactly one deployment row, found "
                f"{len(deployments)}"
            )
        if deployments[0].get("deployment_id") != fragment_dir.name:
            raise SoundHubStagingError(
                f"{fragment_dir}: deployment row ID does not match fragment folder"
            )
        if not recordings:
            raise SoundHubStagingError(f"{fragment_dir}: no recording rows")
        expected_deployments.extend(deployments)
        expected_recordings.extend(recordings)

    expected_deployments.sort(key=lambda row: row.get("deployment_id", ""))
    expected_recordings.sort(
        key=lambda row: (row.get("deployment_id", ""), row.get("filename", ""))
    )
    root = project_root(staging_root)
    deployments = _require_header(root / DEPLOYMENT_CSV, SOUNDHUB_DEPLOYMENT_FIELDS)
    recordings = _require_header(root / RECORDING_CSV, SOUNDHUB_RECORDING_FIELDS)
    if deployments != expected_deployments:
        raise SoundHubStagingError(
            f"{root / DEPLOYMENT_CSV}: cumulative rows differ from deployment fragments; "
            "restage or rebuild the batch"
        )
    if recordings != expected_recordings:
        raise SoundHubStagingError(
            f"{root / RECORDING_CSV}: cumulative rows differ from deployment fragments; "
            "restage or rebuild the batch"
        )

    deployment_ids: set[str] = set()
    for number, row in enumerate(deployments, start=2):
        deployment_id = str(row.get("deployment_id") or "").strip()
        if deployment_id in deployment_ids:
            raise SoundHubStagingError(
                f"{root / DEPLOYMENT_CSV}: duplicate deployment_id {deployment_id!r}"
            )
        deployment_ids.add(deployment_id)
        missing = [
            field for field in REQUIRED_DEPLOYMENT_FIELDS
            if not str(row.get(field) or "").strip()
        ]
        if missing:
            raise SoundHubStagingError(
                f"{root / DEPLOYMENT_CSV} row {number} ({deployment_id}): "
                "required values are blank: " + ", ".join(missing)
            )
        if row["project_short_name"] != SOUNDHUB_PROJECT_SHORT_NAME:
            raise SoundHubStagingError(
                f"{root / DEPLOYMENT_CSV} row {number}: unexpected project_short_name"
            )
        if row["subproject_design"] != SUBPROJECT_DESIGN:
            raise SoundHubStagingError(
                f"{root / DEPLOYMENT_CSV} row {number}: subproject_design must be "
                f"{SUBPROJECT_DESIGN!r}"
            )
        for field in ("date_installed", "deployment_start_date", "deployment_end_date"):
            _parse_iso_date(row[field], path=root / DEPLOYMENT_CSV,
                            row_number=number, field=field)
        for field in ("deployment_start_time", "deployment_end_time"):
            _parse_time(row[field], path=root / DEPLOYMENT_CSV,
                        row_number=number, field=field)

    recording_keys: set[tuple[str, str]] = set()
    for number, row in enumerate(recordings, start=2):
        key = (
            str(row.get("deployment_id") or "").strip(),
            str(row.get("filename") or "").strip(),
        )
        if not all(key) or key in recording_keys:
            raise SoundHubStagingError(
                f"{root / RECORDING_CSV} row {number}: blank or duplicate recording key"
            )
        recording_keys.add(key)
        if key[0] not in deployment_ids:
            raise SoundHubStagingError(
                f"{root / RECORDING_CSV} row {number}: deployment_id is absent from "
                "deployment.csv"
            )
        if not key[1].lower().endswith(".flac"):
            raise SoundHubStagingError(
                f"{root / RECORDING_CSV} row {number}: filename must end in .flac"
            )
        try:
            start = datetime.fromisoformat(str(row.get("start") or ""))
            end = datetime.fromisoformat(str(row.get("end") or ""))
        except ValueError as exc:
            raise SoundHubStagingError(
                f"{root / RECORDING_CSV} row {number}: invalid start or end timestamp"
            ) from exc
        if end <= start:
            raise SoundHubStagingError(
                f"{root / RECORDING_CSV} row {number}: end must be after start"
            )
        if start.utcoffset() is None or end.utcoffset() is None:
            raise SoundHubStagingError(
                f"{root / RECORDING_CSV} row {number}: start and end must include "
                "a UTC offset"
            )
        media = root / key[0] / key[1]
        if not media.is_file():
            raise SoundHubStagingError(
                f"{root / RECORDING_CSV} row {number}: staged FLAC is missing: {media}"
            )

    actual_media = {
        (path.parent.name, path.name)
        for path in root.glob("*/*.flac")
        if path.is_file()
    }
    if actual_media != recording_keys:
        extra = sorted(actual_media - recording_keys)
        missing = sorted(recording_keys - actual_media)
        details: list[str] = []
        if extra:
            details.append(f"{len(extra)} unlisted FLAC(s)")
        if missing:
            details.append(f"{len(missing)} missing FLAC(s)")
        raise SoundHubStagingError(
            f"{root}: staged media does not exactly match recording.csv ("
            + ", ".join(details)
            + ")"
        )

    return {
        "deployment_count": len(deployments),
        "recording_count": len(recordings),
        "fragment_count": len(fragment_dirs),
    }
