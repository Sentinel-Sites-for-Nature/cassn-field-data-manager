"""Deployment manifest construction for the NDP source namespace.

One ``manifest.json`` describes one deployment: who placed what device where,
over which interval, and exactly which files came off the card. Authoritative
placement and device identity come from the curated lookup snapshot; the filed
metadata rows cross-check it and supply the file inventory.

This module is pure. It touches no filesystem, no clock, and no network, so
every fact it needs — the rendered CSV's SHA-256, the ``generated`` instant, the
lookup row — arrives as an argument. That keeps the manifest reproducible: the
same rows and the same lookup snapshot always yield the same bytes.

Findings are collected rather than raised. A hard error means the manifest is
not written; the caller validates a whole event before writing anything, so one
bad deployment surfaces beside the other fifteen instead of hiding them.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime

from cassn.config import AUDIO_DEVICE_TYPES, CAMERA_DEVICE_TYPES, VERSION
from cassn.lookups import ASSET_TAG_PATTERN, AUDIOMOTH_SERIAL_PATTERN, Site

MANIFEST_VERSION = 1
MANIFEST_TYPE = "cassn.source.deployment"
GENERATOR_NAME = "cassn-field-data-manager"
ACCESS = "private"

# Untagged means v00 everywhere under the namespace, so a manifest generated
# from an uncorrected deployment always states snapshot 0. Corrections take the
# next suffix as a new path and carry the next snapshot number.
SNAPSHOT_VERSION = 0

METADATA_FILENAME = "metadata/file_metadata.csv"
CHECKSUM_ALGORITHM = "sha256"
CHECKSUM_COLUMN = "file_hash_sha256"

# The document family a deployment's rows came from, and the row ``file_type``
# values each family may carry. Config sidecars ride in the audio document
# (D1); they are staged, counted, and hashed, but they carry no recording time.
IMAGE_FILE_TYPES = frozenset({"image"})
AUDIO_FILE_TYPES = frozenset({"audio", "config"})
CONFIG_FILE_TYPE = "config"

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class ManifestBuild:
    """One deployment manifest and the findings raised while building it."""

    deployment_id: str
    manifest: dict | None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


def content_digest(rows: list[dict]) -> str:
    """Roll every row's SHA-256 into one digest for the whole deployment.

    Sorting makes the digest independent of row order; duplicates stay in the
    list so a file counted twice changes the result. No delimiter is needed
    because every element is exactly 64 ASCII bytes. Config sidecar hashes are
    included alongside media, so a changed CONFIG.TXT changes the rollup.

    Recomputable from a staged ``file_metadata.csv`` alone. It detects a changed
    multiset of content hashes and nothing else, which is why the manifest also
    carries ``content.metadata_sha256``.
    """
    hashes = sorted(
        str(row.get(CHECKSUM_COLUMN) or "").strip().lower() for row in rows
    )
    return hashlib.sha256("".join(hashes).encode("ascii")).hexdigest()


def _report(errors: list[str], offenders: list[str], label: str) -> None:
    """Record one row-level fault without listing every affected row.

    A deployment can hold a hundred thousand rows, and a schema-wide fault
    affects all of them. Naming a handful is enough to act on.
    """
    if not offenders:
        return
    errors.append(f"{label}: " + ", ".join(offenders[:5]))
    if len(offenders) > 5:
        errors.append(f"{len(offenders) - 5} further row(s) where {label}")


def _distinct(rows: list[dict], column: str) -> list[str]:
    """Sorted distinct non-blank values of one column."""
    return sorted({str(row.get(column) or "").strip() for row in rows} - {""})


def _site_columns(rows: list[dict]) -> tuple[str, str]:
    """Return the (short name, full name) column pair this document uses.

    Metadata written before the canonical site rename carries ``site`` and
    ``site_full_name``; current metadata carries ``site_short_name`` and
    ``site_name``. Both remain on Box, so both are read.
    """
    fields = rows[0].keys()
    if "site_short_name" in fields or "site_name" in fields:
        return "site_short_name", "site_name"
    return "site", "site_full_name"


def _placement_columns(rows: list[dict]) -> tuple[str, str]:
    """Return the (start, end) placement column pair this document uses.

    The image and audio schemas name the same two dates differently —
    ``start_date``/``end_date`` against
    ``deployment_start_date``/``deployment_end_date``. Harmonizing them belongs
    to the Box reorganization (D18); reading both is what a consumer must do
    until then.
    """
    if "deployment_start_date" in rows[0]:
        return "deployment_start_date", "deployment_end_date"
    return "start_date", "end_date"


def _is_tag_serial_pair(lookup_value: str, metadata_value: str) -> bool:
    """Whether two ARU identifiers differ only by asset-tag/serial vocabulary.

    ``device_id`` in ``deployments.csv`` holds a four-digit asset tag for most
    ARU rows and a sixteen-hex AudioMoth serial for the rows written 2026-09-02,
    while the filed metadata always holds the serial. That is a known split in
    one column's vocabulary, not a device mismatch, so it warns rather than
    failing — see ``local_data/future_work/device_id_asset_tag_vs_serial.md``.
    """
    return bool(
        ASSET_TAG_PATTERN.fullmatch(lookup_value)
        and AUDIOMOTH_SERIAL_PATTERN.fullmatch(metadata_value)
    )


def _single_value(
    rows: list[dict],
    column: str,
    errors: list[str],
    warnings: list[str],
    *,
    required: bool,
) -> str | None:
    """One shared value for a column that must not vary within a deployment."""
    values = _distinct(rows, column)
    if len(values) > 1:
        errors.append(f"{column} is mixed across rows: " + ", ".join(values))
        return None
    if not values:
        if required:
            errors.append(f"{column} is blank on every row")
        else:
            warnings.append(f"{column} is blank on every row; recorded as null")
        return None
    return values[0]


def _coordinate(
    value: str, label: str, limit: float, errors: list[str]
) -> float | None:
    try:
        number = float(value)
    except ValueError:
        errors.append(f"{label} is not a number: {value!r}")
        return None
    if not -limit <= number <= limit:
        errors.append(f"{label} is out of range: {value!r}")
        return None
    return number


def _coordinates(
    rows: list[dict],
    plot_coordinates: dict | None,
    errors: list[str],
    warnings: list[str],
) -> dict | None:
    """Resolve the deployment's position, preferring the filed metadata."""
    latitudes = _distinct(rows, "latitude")
    longitudes = _distinct(rows, "longitude")
    if len(latitudes) > 1 or len(longitudes) > 1:
        errors.append(
            "coordinates are mixed across rows: "
            + ", ".join(f"{lat}/{lon}" for lat in latitudes for lon in longitudes)
        )
        return None

    latitude, longitude = (
        (latitudes[0], longitudes[0]) if latitudes and longitudes else ("", "")
    )
    source = "metadata"
    if not latitude or not longitude:
        fallback = plot_coordinates or {}
        latitude = str(fallback.get("latitude") or "").strip()
        longitude = str(fallback.get("longitude") or "").strip()
        source = "plots.csv"
    if not latitude or not longitude:
        warnings.append(
            "no coordinates in the metadata or plots.csv; recorded as null"
        )
        return None
    if source == "plots.csv":
        warnings.append("coordinates taken from plots.csv; the metadata has none")

    parsed_latitude = _coordinate(latitude, "latitude", 90.0, errors)
    parsed_longitude = _coordinate(longitude, "longitude", 180.0, errors)
    if parsed_latitude is None or parsed_longitude is None:
        return None
    return {"latitude": parsed_latitude, "longitude": parsed_longitude}


def _placement(lookup_row: dict, errors: list[str]) -> dict | None:
    """The curated placement interval; the media never overrides it."""
    placement = {}
    for key, column in (
        ("start_date", "deployment_start_date"),
        ("end_date", "deployment_end_date"),
    ):
        value = str(lookup_row.get(column) or "").strip()
        if not value:
            errors.append(f"deployments.csv has no {column}")
            return None
        try:
            placement[key] = date.fromisoformat(value).isoformat()
        except ValueError:
            errors.append(f"deployments.csv {column} is not a date: {value!r}")
            return None
    return placement


def _as_date(value: str) -> str:
    """The calendar day a filed placement value names, or the value unchanged.

    The image schema's ``start_date``/``end_date`` hold a bare date in some
    metadata generations and a midnight-to-23:59:59 datetime in others. Both
    name the same day, so the comparison is on the day rather than the text.
    """
    try:
        return datetime.fromisoformat(value).date().isoformat()
    except ValueError:
        return value


def _check_placement(
    rows: list[dict],
    placement: dict | None,
    recorded_first: str | None,
    recorded_last: str | None,
    warnings: list[str],
) -> None:
    """Warn where the filed evidence disagrees with the curated placement.

    ``deployments.csv`` stays authoritative — the manifest records it either way
    — but a curated window that the deployment's own metadata or its media
    timestamps contradict is a real defect in one of the two, and silently
    preferring the lookup would publish it without anyone seeing it.
    """
    if placement is None:
        return
    start_column, end_column = _placement_columns(rows)
    for column, key in ((start_column, "start_date"), (end_column, "end_date")):
        filed = _distinct(rows, column)
        drifted = [value for value in filed if _as_date(value) != placement[key]]
        if drifted:
            warnings.append(
                f"metadata {column} {', '.join(repr(v) for v in drifted)} disagrees "
                f"with deployments.csv {placement[key]!r}; the manifest records "
                "deployments.csv"
            )
    if recorded_first and recorded_first[:10] < placement["start_date"]:
        warnings.append(
            f"first recording {recorded_first} predates the curated placement start "
            f"{placement['start_date']}"
        )
    if recorded_last and recorded_last[:10] > placement["end_date"]:
        warnings.append(
            f"last recording {recorded_last} postdates the curated placement end "
            f"{placement['end_date']}"
        )


def _recording_window(
    rows: list[dict], errors: list[str], warnings: list[str]
) -> tuple[str | None, str | None]:
    """Earliest and latest recording time over media rows only.

    Config sidecars are staged and hashed but were never recorded, so they are
    excluded here. A row whose timestamp is present but unparseable is a hard
    error; a deployment whose timestamps are all blank records nulls.
    """
    stamps: list[datetime] = []
    blank = 0
    for row in rows:
        if str(row.get("file_type") or "").strip() == CONFIG_FILE_TYPE:
            continue
        value = str(row.get("recorded_datetime") or "").strip()
        if not value:
            blank += 1
            continue
        try:
            stamps.append(datetime.fromisoformat(value))
        except ValueError:
            errors.append(f"recorded_datetime is not a timestamp: {value!r}")
            return None, None
    if not stamps:
        warnings.append("no media row carries a recorded_datetime; recorded as null")
        return None, None
    if blank:
        warnings.append(f"{blank} media row(s) carry no recorded_datetime")
    return min(stamps).isoformat(), max(stamps).isoformat()


def _content_counts(
    rows: list[dict], allowed_types: frozenset[str], errors: list[str]
) -> tuple[dict[str, int] | None, int | None]:
    """File-type breakdown and total size over every staged row."""
    counts: dict[str, int] = {}
    total_bytes = 0
    bad_sizes: list[str] = []
    for row in rows:
        file_type = str(row.get("file_type") or "").strip()
        if file_type not in allowed_types:
            errors.append(
                f"unknown file_type {file_type!r} for {row.get('filename', '')!r}; "
                "expected one of " + ", ".join(sorted(allowed_types))
            )
            return None, None
        counts[file_type] = counts.get(file_type, 0) + 1

        size = str(row.get("file_size_bytes") or "").strip()
        try:
            parsed = int(size)
        except ValueError:
            bad_sizes.append(f"{row.get('filename', '')!r}: {size!r}")
            continue
        if parsed < 0:
            bad_sizes.append(f"{row.get('filename', '')!r}: {size!r}")
            continue
        total_bytes += parsed
    _report(errors, bad_sizes, "file_size_bytes is not a non-negative integer")
    return dict(sorted(counts.items())), (None if bad_sizes else total_bytes)


def _check_filenames(rows: list[dict], errors: list[str]) -> None:
    """Refuse a deployment whose flattened filenames would collide.

    Camera card subfolders flatten into one ``data/`` directory (D4), so two
    rows sharing a filename would silently become one object. Failing here is
    correct behavior; three deployments elsewhere on Box trip it today.
    """
    seen: dict[str, int] = {}
    for row in rows:
        filename = str(row.get("filename") or "").strip()
        if not filename:
            errors.append("a row carries no filename")
            return
        seen[filename] = seen.get(filename, 0) + 1
    _report(
        errors,
        [f"{name} (x{count})" for name, count in sorted(seen.items()) if count > 1],
        "duplicate filename within the deployment",
    )


def _check_hashes(rows: list[dict], errors: list[str]) -> None:
    """Every row must carry a well-formed content hash before it is rolled up."""
    malformed = sorted(
        {
            str(row.get(CHECKSUM_COLUMN) or "").strip()
            for row in rows
            if not _SHA256_PATTERN.fullmatch(
                str(row.get(CHECKSUM_COLUMN) or "").strip().lower()
            )
        }
    )
    _report(
        errors,
        [repr(value) for value in malformed],
        f"{CHECKSUM_COLUMN} is not 64 hexadecimal characters",
    )


def _device(
    rows: list[dict],
    lookup_row: dict,
    device_type: str,
    errors: list[str],
    warnings: list[str],
) -> dict:
    """Device identity: the lookup states it, the metadata cross-checks it."""
    if device_type in CAMERA_DEVICE_TYPES:
        id_column, make_column, model_column = "camera_id", "camera_make", "camera_model"
    else:
        id_column, make_column, model_column = "device_id", "ARU_make", "ARU_model"

    lookup_id = str(lookup_row.get("device_id") or "").strip()
    metadata_ids = _distinct(rows, id_column)
    if len(metadata_ids) > 1:
        errors.append(f"{id_column} is mixed across rows: " + ", ".join(metadata_ids))
    metadata_id = metadata_ids[0] if len(metadata_ids) == 1 else ""

    device_id: str | None = lookup_id or metadata_id or None
    if lookup_id and metadata_id and lookup_id != metadata_id:
        if device_type in AUDIO_DEVICE_TYPES and _is_tag_serial_pair(
            lookup_id, metadata_id
        ):
            warnings.append(
                f"deployments.csv device_id {lookup_id!r} is an asset tag while the "
                f"metadata holds serial {metadata_id!r}; recording what the lookup says"
            )
        else:
            errors.append(
                f"device_id disagrees: deployments.csv says {lookup_id!r}, "
                f"the metadata {id_column} says {metadata_id!r}"
            )
            device_id = None
    elif not device_id:
        warnings.append(
            f"no device identifier in deployments.csv or the metadata {id_column}; "
            "recorded as null"
        )

    make = _single_value(rows, make_column, errors, warnings, required=False)
    model = _single_value(rows, model_column, errors, warnings, required=False)
    return {"id": device_id, "make": make, "model": model}


def _site_block(
    rows: list[dict],
    site: Site | None,
    site_short_name: str,
    errors: list[str],
    warnings: list[str],
) -> dict | None:
    """Canonical site identity, with a warning for drifted filed display names."""
    if site is None:
        errors.append(f"sites.csv has no row for site_short_name {site_short_name!r}")
        return None

    short_column, full_column = _site_columns(rows)
    for column, canonical in (
        (short_column, site.site_short_name),
        (full_column, site.site_name),
        ("site_code", site.site_code),
    ):
        filed = _distinct(rows, column)
        drifted = [value for value in filed if value != canonical]
        if drifted:
            warnings.append(
                f"metadata {column} {', '.join(repr(v) for v in drifted)} disagrees "
                f"with sites.csv {canonical!r}; the manifest records sites.csv"
            )
    return {
        "short_name": site.site_short_name,
        "full_name": site.site_name,
        "code": site.site_code,
    }


def build_manifest(
    deployment_id: str,
    rows: list[dict],
    *,
    document_kind: str,
    deployment_event_id: str,
    lookup_row: dict,
    site: Site | None,
    plot_coordinates: dict | None,
    event_recorded_by: str,
    generated: str,
    metadata_sha256: str,
) -> ManifestBuild:
    """Build one ``cassn.source.deployment`` manifest from staged rows.

    ``rows`` are exactly the rows staged into ``metadata/file_metadata.csv``,
    with values as filed. ``document_kind`` is ``image`` or ``audio``, taken
    from the source document's filename, and fixes both the primary media type
    and the row ``file_type`` values allowed. ``metadata_sha256`` is the digest
    of the rendered CSV bytes, so it is computed before this is called.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not deployment_id:
        errors.append("deployment_id is blank")
    if not rows:
        errors.append("no rows staged for this deployment")
        return ManifestBuild(deployment_id, None, tuple(errors), tuple(warnings))

    filed_ids = _distinct(rows, "deployment_id")
    if filed_ids != [deployment_id]:
        errors.append(
            "rows do not share one deployment_id: " + ", ".join(filed_ids or ["<blank>"])
        )
    if not lookup_row:
        errors.append(
            f"deployments.csv has no row for {deployment_id!r}; refusing to guess a "
            "placement window"
        )

    filed_event_id = _single_value(
        rows, "deployment_event_id", errors, warnings, required=True
    )
    if filed_event_id and filed_event_id != deployment_event_id:
        errors.append(
            f"metadata deployment_event_id {filed_event_id!r} does not match the "
            f"event folder {deployment_event_id!r}"
        )
    organization = _single_value(rows, "organization", errors, warnings, required=True)

    device_type = str(lookup_row.get("device_type") or "").strip()
    plot_value = str(lookup_row.get("plot_number") or "").strip()
    plot_number: int | None = None
    try:
        plot_number = int(plot_value)
    except ValueError:
        if lookup_row:
            errors.append(f"deployments.csv plot_number is not an integer: {plot_value!r}")
    if plot_number is not None and plot_number < 1:
        errors.append(f"deployments.csv plot_number is not a plot: {plot_value!r}")
        plot_number = None

    filed_plots = _distinct(rows, "plot_number")
    if plot_number is not None and filed_plots != [str(plot_number)]:
        errors.append(
            f"metadata plot_number {', '.join(filed_plots) or '<blank>'} disagrees "
            f"with deployments.csv {plot_number}"
        )
    filed_device_types = _distinct(rows, "device_type")
    if device_type and filed_device_types != [device_type]:
        errors.append(
            f"metadata device_type {', '.join(filed_device_types) or '<blank>'} "
            f"disagrees with deployments.csv {device_type!r}"
        )

    # The source document decides the primary media type and which row
    # ``file_type`` values are legal; the curated device type has to agree with
    # it. Reading the allowed types off the lookup instead would turn a missing
    # lookup row into an "unknown file_type" error on every row.
    media_type = document_kind
    allowed_types = IMAGE_FILE_TYPES if document_kind == "image" else AUDIO_FILE_TYPES
    if device_type in CAMERA_DEVICE_TYPES:
        expected_kind = "image"
    elif device_type in AUDIO_DEVICE_TYPES:
        expected_kind = "audio"
    else:
        expected_kind = None
        if lookup_row:
            errors.append(
                f"deployments.csv device_type {device_type!r} is neither a camera nor "
                "an audio recorder"
            )
    if expected_kind is not None and expected_kind != document_kind:
        errors.append(
            f"{device_type} rows are filed in the {document_kind} document; a "
            f"{expected_kind} document was expected"
        )

    # Site identity hangs off the lookup row, so a deployment with no lookup row
    # has already reported the only error worth reporting.
    site_block = (
        _site_block(
            rows,
            site,
            str(lookup_row.get("site_short_name") or "").strip(),
            errors,
            warnings,
        )
        if lookup_row
        else None
    )
    recorded_by = _single_value(rows, "recorded_by", errors, warnings, required=False)
    if recorded_by is None and event_recorded_by:
        recorded_by = event_recorded_by
        warnings.append(
            "recorded_by taken from the deployment event record; the metadata has none"
        )

    _check_filenames(rows, errors)
    _check_hashes(rows, errors)
    counts, total_bytes = _content_counts(rows, allowed_types, errors)
    recorded_first, recorded_last = _recording_window(rows, errors, warnings)
    coordinates = _coordinates(rows, plot_coordinates, errors, warnings)
    placement = _placement(lookup_row, errors) if lookup_row else None
    _check_placement(rows, placement, recorded_first, recorded_last, warnings)
    device = _device(rows, lookup_row, device_type, errors, warnings)

    if errors:
        return ManifestBuild(deployment_id, None, tuple(errors), tuple(warnings))

    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "manifest_type": MANIFEST_TYPE,
        "snapshot_version": SNAPSHOT_VERSION,
        "generated": generated,
        "generator": {"name": GENERATOR_NAME, "version": VERSION},
        "access": ACCESS,
        "deployment": {
            "deployment_id": deployment_id,
            "deployment_event_id": deployment_event_id,
            "organization": organization,
            "site": site_block,
            "plot_number": plot_number,
            "device_type": device_type,
            "device": device,
            "coordinates": coordinates,
            "placement": placement,
            "recorded_by": recorded_by,
        },
        "content": {
            "media_type": media_type,
            "file_count": len(rows),
            "file_counts_by_type": counts,
            "total_bytes": total_bytes,
            "recorded_first": recorded_first,
            "recorded_last": recorded_last,
            "metadata_file": METADATA_FILENAME,
            "metadata_sha256": metadata_sha256,
            "checksum_algorithm": CHECKSUM_ALGORITHM,
            "checksum_column": CHECKSUM_COLUMN,
            "content_digest": content_digest(rows),
        },
        # A freshly generated manifest has compared nothing against the media it
        # describes. Only the later transfer phase, after checking each staged
        # object against its recorded hash, may write "verified".
        "verification": {"status": "metadata_only"},
    }
    return ManifestBuild(deployment_id, manifest, (), tuple(warnings))
