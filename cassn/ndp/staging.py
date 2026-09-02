"""Split one Box deployment event into per-deployment NDP staging trees.

Reads the two metadata CSVs at a deployment-event root, groups their rows by
``deployment_id``, and renders one staging directory per deployment::

    <staging_root>/<deployment_event_id>/<deployment_id>/
    ├── README.md
    ├── manifest.json
    └── metadata/file_metadata.csv

No ``data/`` is created. Empty directories do not survive object storage, and
media movement belongs to the later transfer phase — this step copies no media,
opens no media file, and writes nothing to Box.

The staged ``file_metadata.csv`` is a faithful row subset of its Box original:
the source header and column order are preserved exactly and no value is
rewritten, so the only difference between the two files is which rows are in
them. Renaming the ``camera_*``/``ARU_*`` and date columns belongs to the Box
reorganization, not to a copy (D18).

The whole event is validated and rendered in memory before anything is written,
and writes are idempotent: an identical file is left alone, a missing one is
published through a temporary sibling and :func:`os.replace`, and a file whose
bytes differ is refused rather than overwritten.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from cassn.config import PACIFIC_TZ
from cassn.lookups import (
    LookupTables,
    deployment_storage_label,
    normalize_deployment_event_metadata,
)
from cassn.ndp.manifest import METADATA_FILENAME, ManifestBuild, build_manifest

MANIFEST_FILENAME = "manifest.json"
README_FILENAME = "README.md"
EVENT_RECORD_FILENAME = "deployment_event_record.json"

# Only these two documents at the event root are read. Everything else beside
# them — WI exports, per-device folders, reports — belongs to another workflow.
SOURCE_DOCUMENTS = {
    "image_file_metadata.csv": "image",
    "audio_file_metadata.csv": "audio",
}

# Columns every document must carry for a deployment to be described at all.
# Identification is by required columns and filename, never by column count:
# nine header generations exist across the filed metadata, several sharing a
# count.
REQUIRED_COLUMNS = frozenset({
    "filename",
    "deployment_id",
    "deployment_event_id",
    "organization",
    "site_code",
    "plot_number",
    "device_type",
    "file_type",
    "file_size_bytes",
    "file_hash_sha256",
    "recorded_datetime",
    "latitude",
    "longitude",
    "recorded_by",
})
REQUIRED_COLUMNS_BY_KIND = {
    "image": REQUIRED_COLUMNS | {"camera_id", "camera_make", "camera_model"},
    "audio": REQUIRED_COLUMNS | {"device_id", "ARU_make", "ARU_model"},
}


class NdpStagingError(Exception):
    """A staging precondition failed. Raised before any file is written."""


@dataclass(frozen=True)
class MetadataDocument:
    """One filed metadata CSV, with its header preserved in source order."""

    path: Path
    kind: str
    fieldnames: list[str]
    rows: list[dict]
    line_terminator: str = "\r\n"


@dataclass(frozen=True)
class PlannedDeployment:
    """One deployment's rendered staging directory, held in memory."""

    deployment_id: str
    files: dict[str, bytes]
    warnings: tuple[str, ...] = ()


@dataclass
class StagingPlan:
    """Everything one event would write, plus the findings raised building it."""

    event_dir: Path
    staging_root: Path
    deployment_event_id: str
    generated: str
    deployments: list[PlannedDeployment] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def event_root(self) -> Path:
        return self.staging_root / self.deployment_event_id

    @property
    def file_count(self) -> int:
        return sum(len(deployment.files) for deployment in self.deployments)


@dataclass(frozen=True)
class ApplyResult:
    """What an apply actually did, per file."""

    written: tuple[Path, ...]
    unchanged: tuple[Path, ...]


def _line_terminator(path: Path) -> str:
    r"""Return the line ending a filed CSV actually uses.

    Most of the filed metadata is CRLF, which is what :mod:`csv` writes by
    default, but three documents on Box are LF. Re-emitting those with CRLF
    would rewrite every line of a file this step claims only to subset, so the
    source terminator is carried through instead.
    """
    with path.open("rb") as stream:
        first_line, _, _ = stream.read(65536).partition(b"\n")
    return "\r\n" if first_line.endswith(b"\r") else "\n"


def read_event_documents(event_dir: Path) -> list[MetadataDocument]:
    """Read the metadata CSVs at an event root, and nothing below it."""
    root = Path(event_dir).expanduser()
    if not root.is_dir():
        raise NdpStagingError(f"Deployment event directory does not exist: {root}")

    documents: list[MetadataDocument] = []
    for filename, kind in sorted(SOURCE_DOCUMENTS.items()):
        path = root / filename
        if not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                if reader.fieldnames is None:
                    raise NdpStagingError(f"{path} has no header")
                rows = list(reader)
        except OSError as exc:
            raise NdpStagingError(f"Could not read {path}: {exc}") from exc

        missing = sorted(REQUIRED_COLUMNS_BY_KIND[kind] - set(reader.fieldnames))
        if missing:
            raise NdpStagingError(
                f"{path.name} is missing required column(s): " + ", ".join(missing)
            )
        # A row with more values than the header would silently lose data on the
        # way back out, so it fails here rather than being staged mangled.
        if any(None in row for row in rows):
            raise NdpStagingError(
                f"{path.name} has row(s) with more values than its header"
            )
        documents.append(
            MetadataDocument(
                path, kind, list(reader.fieldnames), rows, _line_terminator(path)
            )
        )

    if not documents:
        raise NdpStagingError(
            f"No image_file_metadata.csv or audio_file_metadata.csv at {root}"
        )
    return documents


def read_event_recorded_by(event_dir: Path) -> str:
    """Return the observer named by the optional legacy event record.

    ``deployment_event_record.json`` is the only fallback when a metadata
    document carries no ``recorded_by``. It is a historical artifact written in
    several shapes, so its keys are normalized on the way in and its absence is
    not an error.
    """
    path = Path(event_dir) / EVENT_RECORD_FILENAME
    if not path.is_file():
        return ""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    info = normalize_deployment_event_metadata(record.get("deployment_info") or record)
    for key in ("recorded_by", "observer"):
        value = str(info.get(key) or "").strip()
        if value:
            return value
    return ""


def _render_metadata_csv(document: MetadataDocument, rows: list[dict]) -> bytes:
    """Re-emit ``rows`` under the source header, byte-for-byte as filed."""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer, fieldnames=document.fieldnames, lineterminator=document.line_terminator
    )
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _render_manifest(manifest: dict) -> bytes:
    return (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _render_readme(manifest: dict) -> bytes:
    """A short human-readable card, derived only from manifest facts.

    Everything here restates the manifest, so the README can never disagree with
    it and re-rendering the same manifest always produces the same bytes.
    """
    deployment = manifest["deployment"]
    content = manifest["content"]
    site = deployment["site"]
    placement = deployment["placement"]
    device = deployment["device"]
    counts = ", ".join(
        f"{count:,} {file_type}" for file_type, count in content["file_counts_by_type"].items()
    )
    lines = [
        f"# {deployment['deployment_id']}",
        "",
        f"{site['full_name']} ({site['short_name']}), plot {deployment['plot_number']}, "
        f"device type {deployment['device_type']}.",
        f"Placed {placement['start_date']} to {placement['end_date']} by "
        f"{deployment['recorded_by'] or 'an unrecorded observer'}.",
        "",
        "## Contents",
        "",
        f"- Primary media: {content['media_type']}",
        f"- Files described: {content['file_count']:,} ({counts})",
        f"- Total size: {content['total_bytes']:,} bytes",
        f"- Recorded: {content['recorded_first'] or 'unknown'} to "
        f"{content['recorded_last'] or 'unknown'}",
        f"- Device: {device['make'] or 'unknown make'} {device['model'] or ''}".rstrip()
        + f", id {device['id'] or 'unknown'}",
        "",
        "## Reading this directory",
        "",
        f"`{MANIFEST_FILENAME}` is a `{manifest['manifest_type']}` document and is the "
        "authoritative description of this deployment.",
        f"`{METADATA_FILENAME}` lists every file with its size, SHA-256, and "
        "acquisition metadata; it is a faithful row subset of the deployment event's "
        "filed metadata on Box, with the source columns unchanged.",
        "",
        f"Verification status is `{manifest['verification']['status']}`: this snapshot "
        "describes the deployment from its filed metadata and has not been checked "
        "against the transferred media.",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def _plan_deployment(
    deployment_id: str,
    document: MetadataDocument,
    rows: list[dict],
    lookups: LookupTables,
    *,
    deployment_event_id: str,
    event_recorded_by: str,
    generated: str,
) -> tuple[PlannedDeployment | None, ManifestBuild]:
    """Render one deployment's three files, or report why it cannot be."""
    csv_bytes = _render_metadata_csv(document, rows)
    lookup_row = lookups.deployment_for_id(deployment_id)
    site_short_name = str(lookup_row.get("site_short_name") or "")
    plot_number = str(lookup_row.get("plot_number") or "")
    site = lookups.site_by_short_name.get(site_short_name)
    plot_coordinates = (
        lookups.plot_coords.get((site_short_name, int(plot_number)))
        if plot_number.isdigit()
        else None
    )

    build = build_manifest(
        deployment_id,
        rows,
        document_kind=document.kind,
        deployment_event_id=deployment_event_id,
        lookup_row=lookup_row,
        site=site,
        plot_coordinates=plot_coordinates,
        event_recorded_by=event_recorded_by,
        generated=generated,
        metadata_sha256=hashlib.sha256(csv_bytes).hexdigest(),
    )
    if build.manifest is None:
        return None, build
    return (
        PlannedDeployment(
            deployment_id,
            {
                README_FILENAME: _render_readme(build.manifest),
                MANIFEST_FILENAME: _render_manifest(build.manifest),
                METADATA_FILENAME: csv_bytes,
            },
            build.warnings,
        ),
        build,
    )


def _existing_generated(event_root: Path) -> str:
    """Reuse the ``generated`` instant already published under this event.

    All manifests in one plan share one instant, so a rerun that finishes an
    interrupted apply must reuse it rather than stamp a second one — otherwise
    every already-written manifest would differ from its rebuild and the apply
    would refuse to proceed.
    """
    if not event_root.is_dir():
        return ""
    instants: dict[str, str] = {}
    for path in sorted(event_root.glob(f"*/{MANIFEST_FILENAME}")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        instant = str(manifest.get("generated") or "")
        if instant:
            instants.setdefault(instant, path.parent.name)
    if len(instants) > 1:
        raise NdpStagingError(
            f"Manifests already under {event_root} carry different generated "
            "instants, so they were not written by one plan: "
            + ", ".join(f"{instant} ({name})" for instant, name in sorted(instants.items()))
        )
    return next(iter(instants), "")


def plan_event(
    event_dir: Path,
    staging_root: Path,
    lookups: LookupTables,
    *,
    generated: str = "",
) -> StagingPlan:
    """Validate and render an entire deployment event without writing anything.

    ``event_dir`` is a Box deployment-event directory; its folder name is the
    authoritative ``deployment_event_id`` that every row must agree with.
    """
    event_dir = Path(event_dir).expanduser()
    staging_root = Path(staging_root).expanduser()
    deployment_event_id = event_dir.name
    documents = read_event_documents(event_dir)
    event_recorded_by = read_event_recorded_by(event_dir)

    if not generated:
        generated = _existing_generated(
            staging_root / deployment_event_id
        ) or datetime.now(PACIFIC_TZ).replace(microsecond=0).isoformat()

    plan = StagingPlan(event_dir, staging_root, deployment_event_id, generated)

    groups: dict[str, tuple[MetadataDocument, list[dict]]] = {}
    for document in documents:
        by_id: dict[str, list[dict]] = {}
        for row in document.rows:
            by_id.setdefault(str(row.get("deployment_id") or "").strip(), []).append(row)
        for deployment_id, rows in by_id.items():
            if deployment_id in groups:
                plan.errors.append(
                    f"{deployment_id or '<blank>'}: rows appear in both the image and "
                    "audio documents"
                )
                continue
            groups[deployment_id] = (document, rows)
        if not document.rows:
            plan.warnings.append(f"{document.path.name} has no rows")

    for deployment_id, (document, rows) in sorted(groups.items()):
        planned, build = _plan_deployment(
            deployment_id,
            document,
            rows,
            lookups,
            deployment_event_id=deployment_event_id,
            event_recorded_by=event_recorded_by,
            generated=generated,
        )
        label = deployment_id or f"<blank deployment_id in {document.path.name}>"
        plan.errors.extend(f"{label}: {message}" for message in build.errors)
        plan.warnings.extend(f"{label}: {message}" for message in build.warnings)
        if planned is not None:
            plan.deployments.append(planned)

    plan.warnings.extend(
        _undescribed_device_folders(event_dir, sorted(groups), lookups)
    )
    return plan


def _undescribed_device_folders(
    event_dir: Path, deployment_ids: list[str], lookups: LookupTables
) -> list[str]:
    """Name ``raw_data/`` device folders that no described deployment covers.

    A card copied to Box whose metadata was never generated would be left out of
    the upload in silence, and nothing else in this step would notice. Only the
    folder names one level under ``raw_data/`` are listed; nothing descends into
    a media directory or opens a media file.
    """
    raw_data = Path(event_dir) / "raw_data"
    if not raw_data.is_dir():
        return []
    described = {
        deployment_storage_label(lookups.deployment_for_id(deployment_id))
        for deployment_id in deployment_ids
        if lookups.deployment_for_id(deployment_id)
    }
    return [
        f"raw_data/{path.name} holds media that no described deployment covers"
        for path in sorted(raw_data.iterdir())
        if path.is_dir() and not path.name.startswith(".") and path.name not in described
    ]


def _write_atomic(path: Path, payload: bytes) -> None:
    """Publish a file through a unique temporary sibling.

    A crash may leave some complete files and some missing, never a partial one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def apply_plan(plan: StagingPlan) -> ApplyResult:
    """Write a validated plan, leaving anything already correct untouched.

    An existing file with the same bytes is left alone; one whose bytes differ
    is refused, because a differing manifest or metadata table describes a
    different snapshot and belongs at the next version suffix, not in place.
    """
    if not plan.ok:
        raise NdpStagingError("Refusing to apply a plan that has validation errors")

    # Compare everything before writing anything, so a conflict in the last
    # deployment cannot leave the first eight written.
    conflicts: list[str] = []
    pending: list[tuple[Path, bytes]] = []
    unchanged: list[Path] = []
    for deployment in plan.deployments:
        for relative, payload in deployment.files.items():
            path = plan.event_root / deployment.deployment_id / relative
            if not path.exists():
                pending.append((path, payload))
            elif path.is_file() and path.read_bytes() == payload:
                unchanged.append(path)
            else:
                conflicts.append(str(path))
    if conflicts:
        raise NdpStagingError(
            "Refusing to replace "
            f"{len(conflicts)} existing file(s) whose contents differ: "
            + ", ".join(conflicts[:5])
        )

    for path, payload in pending:
        _write_atomic(path, payload)
    return ApplyResult(tuple(path for path, _ in pending), tuple(unchanged))
