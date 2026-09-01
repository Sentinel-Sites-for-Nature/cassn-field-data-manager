"""Verified SoundHub submission provenance for staged backlog batches.

The SoundHub landing zone is drained after ingest, so the durable submission
record belongs with the source metadata on Box.  This module maps the exact
FLAC rows in staged ``recording.csv`` back to their WAV rows in Box
``audio_file_metadata.csv`` files.  Only those rows are stamped; excluded or
unstaged recordings remain untouched.
"""
from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from cassn.config import SOUNDHUB_DEVICE_TYPE, SOUNDHUB_RECORDING_FIELDS
from cassn.soundhub.staging import project_root


DEFAULT_SUBMITTER = "Imperato, John"
PROVENANCE_FIELDS = (
    "is_submitted_to_soundhub",
    "soundhub_submitter",
    "soundhub_submission_datetime",
)
_DEPLOYMENT_YEAR_RE = re.compile(r"_(?P<year>\d{4})\d{4}(?:-v\d{2})?$")
_TRUE = {"true", "1", "yes"}
_FALSE = {"", "false", "0", "no"}


class SoundHubProvenanceError(Exception):
    """The staged-to-Box mapping is unsafe or cannot be applied."""


@dataclass
class MetadataDocument:
    path: Path
    fieldnames: list[str]
    rows: list[dict]
    has_bom: bool
    source_bytes: bytes
    matches: dict[int, tuple[str, str]] = field(default_factory=dict)


@dataclass
class ProvenancePlan:
    staging_root: Path
    box_year_root: Path
    target_keys: set[tuple[str, str]] = field(default_factory=set)
    pending_keys: set[tuple[str, str]] = field(default_factory=set)
    submitted_keys: set[tuple[str, str]] = field(default_factory=set)
    documents: list[MetadataDocument] = field(default_factory=list)
    event_ids: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def deployment_ids(self) -> set[str]:
        return {deployment_id for deployment_id, _ in self.target_keys}

    @property
    def pending_deployment_ids(self) -> set[str]:
        return {deployment_id for deployment_id, _ in self.pending_keys}

    @property
    def matched_file_count(self) -> int:
        return len(self.documents)

    @property
    def pending_documents(self) -> list[MetadataDocument]:
        """Box metadata CSVs containing at least one row in the next upload."""
        return [
            document
            for document in self.documents
            if any(key in self.pending_keys for key in document.matches.values())
        ]

    @property
    def pending_event_ids(self) -> set[str]:
        """Deployment events represented by the pending recording rows only."""
        event_ids: set[str] = set()
        for document in self.pending_documents:
            for index, key in document.matches.items():
                if key in self.pending_keys:
                    event_ids.add(
                        str(document.rows[index].get("deployment_event_id") or "").strip()
                    )
        return event_ids

    @property
    def pending_file_count(self) -> int:
        return len(self.pending_documents)


@dataclass(frozen=True)
class ProvenanceApplyResult:
    changed_files: int
    changed_rows: int
    submitted_at: str
    submitter: str


def _read_csv(path: Path) -> tuple[list[str], list[dict], bool, bytes]:
    source_bytes = path.read_bytes()
    has_bom = source_bytes.startswith(b"\xef\xbb\xbf")
    text = source_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None:
        raise ValueError("CSV has no header")
    rows = list(reader)
    if any(None in row for row in rows):
        raise ValueError("one or more rows contain more values than the header")
    return list(reader.fieldnames), rows, has_bom, source_bytes


def _csv_bytes(fieldnames: list[str], rows: list[dict], *, has_bom: bool) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    payload = stream.getvalue().encode("utf-8")
    return (b"\xef\xbb\xbf" + payload) if has_bom else payload


def _write_atomic(path: Path, payload: bytes) -> None:
    mode = path.stat().st_mode if path.exists() else 0o644
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _manifest_key(row: dict) -> tuple[str, str]:
    return (
        str(row.get("deployment_id") or "").strip(),
        str(row.get("filename") or "").strip(),
    )


def _metadata_key(row: dict) -> tuple[str, str]:
    filename = str(row.get("filename") or "").strip()
    return (
        str(row.get("deployment_id") or "").strip(),
        Path(filename).with_suffix(".flac").name if filename else "",
    )


def _submitted_state(row: dict) -> tuple[str | None, str | None]:
    raw = str(row.get("is_submitted_to_soundhub") or "").strip().lower()
    submitter = str(row.get("soundhub_submitter") or "").strip()
    timestamp = str(row.get("soundhub_submission_datetime") or "").strip()
    if raw in _TRUE:
        if not submitter or not timestamp:
            return None, "submitted row is missing submitter or submission datetime"
        return "submitted", None
    if raw in _FALSE:
        if submitter or timestamp:
            return None, "unsubmitted row already carries submitter or submission datetime"
        return "pending", None
    return None, f"invalid is_submitted_to_soundhub value {raw!r}"


def staged_recording_keys(staging_root: Path) -> set[tuple[str, str]]:
    """Return and validate the exact recordings represented by staging."""
    staging_root = Path(staging_root).expanduser().resolve()
    manifest = project_root(staging_root) / "recording.csv"
    try:
        fieldnames, rows, _, _ = _read_csv(manifest)
    except Exception as exc:
        raise SoundHubProvenanceError(f"{manifest}: could not read CSV: {exc}") from exc
    if fieldnames != SOUNDHUB_RECORDING_FIELDS:
        raise SoundHubProvenanceError(
            f"{manifest}: header does not match SoundHub recording schema"
        )

    keys: set[tuple[str, str]] = set()
    for number, row in enumerate(rows, start=2):
        key = _manifest_key(row)
        if not key[0] or not key[1]:
            raise SoundHubProvenanceError(f"{manifest} row {number}: blank key field")
        if key[1].lower().endswith(".flac") is False:
            raise SoundHubProvenanceError(
                f"{manifest} row {number}: expected a .flac filename"
            )
        if key in keys:
            raise SoundHubProvenanceError(f"{manifest}: duplicate recording key {key!r}")
        if not str(row.get("start") or "").strip() or not str(row.get("end") or "").strip():
            raise SoundHubProvenanceError(
                f"{manifest} row {number}: start and end must both be populated"
            )
        media = project_root(staging_root) / key[0] / key[1]
        if not media.is_file():
            raise SoundHubProvenanceError(f"{manifest} row {number}: staged FLAC missing: {media}")
        keys.add(key)
    if not keys:
        raise SoundHubProvenanceError(f"{manifest}: no staged recordings")
    return keys


def infer_submission_year(staging_root: Path) -> int:
    years: set[int] = set()
    for deployment_id, _ in staged_recording_keys(staging_root):
        match = _DEPLOYMENT_YEAR_RE.search(deployment_id)
        if match is None:
            raise SoundHubProvenanceError(
                f"cannot infer year from deployment_id {deployment_id!r}"
            )
        years.add(int(match.group("year")))
    if len(years) != 1:
        raise SoundHubProvenanceError(
            "staging contains more than one deployment year: "
            + ", ".join(str(year) for year in sorted(years))
        )
    return years.pop()


def default_box_year_root(year: int) -> Path:
    return (
        Path.home()
        / "Library"
        / "CloudStorage"
        / "Box-Box"
        / "CASSN"
        / "data"
        / str(year)
    )


def _event_audio_csvs(box_year_root: Path):
    if not box_year_root.is_dir():
        return
    for reserve in sorted(path for path in box_year_root.iterdir() if path.is_dir()):
        for event in sorted(path for path in reserve.iterdir() if path.is_dir()):
            path = event / "audio_file_metadata.csv"
            if path.is_file():
                yield path


def plan_submission_provenance(
    staging_root: Path,
    box_year_root: Path | None = None,
) -> ProvenancePlan:
    """Build a non-mutating, exact staged-FLAC to Box-row mapping."""
    staging_root = Path(staging_root).expanduser().resolve()
    if box_year_root is None:
        box_year_root = default_box_year_root(infer_submission_year(staging_root))
    box_year_root = Path(box_year_root).expanduser().resolve()
    plan = ProvenancePlan(staging_root, box_year_root)

    try:
        plan.target_keys = staged_recording_keys(staging_root)
    except SoundHubProvenanceError as exc:
        plan.errors.append(str(exc))
        return plan
    if not box_year_root.is_dir():
        plan.errors.append(f"Box year root does not exist: {box_year_root}")
        return plan

    found: dict[tuple[str, str], tuple[Path, int, str]] = {}
    states_by_deployment: dict[str, set[str]] = {}
    for path in _event_audio_csvs(box_year_root):
        try:
            fieldnames, rows, has_bom, source_bytes = _read_csv(path)
        except Exception as exc:
            plan.errors.append(f"{path}: could not read CSV: {exc}")
            continue
        missing = sorted(
            {
                "filename",
                "deployment_event_id",
                "deployment_id",
                "device_type",
                "file_type",
                *PROVENANCE_FIELDS,
            }
            - set(fieldnames)
        )
        keys_here = {_metadata_key(row) for row in rows} & plan.target_keys
        if not keys_here:
            continue
        if missing:
            plan.errors.append(f"{path}: missing required column(s): {', '.join(missing)}")
            continue

        document = MetadataDocument(path, fieldnames, rows, has_bom, source_bytes)
        for index, row in enumerate(rows):
            key = _metadata_key(row)
            if key not in plan.target_keys:
                continue
            if row.get("device_type") != SOUNDHUB_DEVICE_TYPE or row.get("file_type") != "audio":
                plan.errors.append(f"{path}: staged key {key!r} is not a BD audio row")
                continue
            if key in found:
                plan.errors.append(
                    f"staged key {key!r} appears in both {found[key][0]} and {path}"
                )
                continue
            state, error = _submitted_state(row)
            if error:
                plan.errors.append(f"{path}: {key!r}: {error}")
                continue
            event_id = str(row.get("deployment_event_id") or "").strip()
            if not event_id:
                plan.errors.append(f"{path}: {key!r}: blank deployment_event_id")
                continue
            document.matches[index] = key
            found[key] = (path, index, state or "")
            plan.event_ids.add(event_id)
            states_by_deployment.setdefault(key[0], set()).add(state or "")
            if state == "submitted":
                plan.submitted_keys.add(key)
            else:
                plan.pending_keys.add(key)
        plan.documents.append(document)

    missing_keys = sorted(plan.target_keys - set(found))
    if missing_keys:
        preview = ", ".join(repr(key) for key in missing_keys[:10])
        suffix = " ..." if len(missing_keys) > 10 else ""
        plan.errors.append(
            f"Box metadata is missing {len(missing_keys)} staged recording(s): {preview}{suffix}"
        )
    for deployment_id, states in sorted(states_by_deployment.items()):
        if len(states) > 1:
            plan.errors.append(
                f"{deployment_id}: mixed submitted and pending recording provenance"
            )
    return plan


def apply_submission_provenance(
    plan: ProvenancePlan,
    *,
    submitter: str = DEFAULT_SUBMITTER,
    submitted_at: datetime | None = None,
) -> ProvenanceApplyResult:
    """Stamp every pending planned row after successful S3 verification."""
    if not plan.ok:
        raise SoundHubProvenanceError("cannot apply an invalid provenance plan")
    submitter = submitter.strip()
    if not submitter:
        raise SoundHubProvenanceError("submitter cannot be blank")
    submitted_at = submitted_at or datetime.now(timezone.utc)
    if submitted_at.tzinfo is None:
        raise SoundHubProvenanceError("submitted_at must include a timezone")
    submitted_iso = submitted_at.astimezone(timezone.utc).isoformat()

    for document in plan.documents:
        if document.path.read_bytes() != document.source_bytes:
            raise SoundHubProvenanceError(
                f"{document.path}: changed after preflight; rebuild the provenance plan"
            )

    writes: list[tuple[Path, bytes]] = []
    changed_rows = 0
    for document in plan.documents:
        updated = [dict(row) for row in document.rows]
        document_changes = 0
        for index, key in document.matches.items():
            if key not in plan.pending_keys:
                continue
            updated[index]["is_submitted_to_soundhub"] = "True"
            updated[index]["soundhub_submitter"] = submitter
            updated[index]["soundhub_submission_datetime"] = submitted_iso
            document_changes += 1
        if document_changes:
            writes.append(
                (
                    document.path,
                    _csv_bytes(document.fieldnames, updated, has_bom=document.has_bom),
                )
            )
            changed_rows += document_changes

    for path, payload in writes:
        _write_atomic(path, payload)
    return ProvenanceApplyResult(len(writes), changed_rows, submitted_iso, submitter)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_submission_report(
    plan: ProvenancePlan,
    *,
    settings: dict,
    upload_result: dict,
    verification: dict,
    provenance_result: ProvenanceApplyResult,
    planned_objects: list[dict],
) -> list[Path]:
    """Write the verified batch report into every affected Box event."""
    if not verification.get("ok"):
        raise SoundHubProvenanceError("cannot write a success report for failed verification")
    moment = datetime.fromisoformat(provenance_result.submitted_at)
    stamp = moment.astimezone(timezone.utc).strftime("%Y-%m-%d_%H%M%SZ")
    filename = f"{stamp}_soundhub_submission.md"

    root = project_root(plan.staging_root)
    deployment_manifest = root / "deployment.csv"
    recording_manifest = root / "recording.csv"
    total_bytes = sum(item["size"] for item in planned_objects)
    destination = (
        f"s3://{settings['bucket']}/{settings['upload_prefix']}/"
        f"{settings['project_short_name']}/"
    )
    event_lines = "\n".join(
        f"- `{event_id}`" for event_id in sorted(plan.pending_event_ids)
    )
    content = f"""# SoundHub submission report — {settings['project_short_name']}

**Status:** Verified successfully

**Submitted by:** {provenance_result.submitter}

**Completed:** {provenance_result.submitted_at}

**Destination:** `{destination}`

## Summary

| Metric | Result |
|---|---:|
| Deployment events | {len(plan.pending_event_ids)} |
| SoundHub deployments | {len(plan.pending_deployment_ids)} |
| FLAC recordings | {len(plan.pending_keys)} |
| Project manifests | 2 |
| Objects verified | {verification['present']} / {verification['checked']} |
| Planned data | {total_bytes / 1e9:.2f} GB |
| Objects uploaded in this run | {upload_result.get('uploaded', 0)} |
| Existing objects skipped | {upload_result.get('skipped', 0)} |
| Box metadata files updated | {provenance_result.changed_files} |
| Box metadata rows updated | {provenance_result.changed_rows} |

## Verification

- Missing objects: {len(verification.get('missing', []))}
- Size mismatches: {len(verification.get('mismatched', []))}
- `deployment.csv` SHA-256: `{_sha256(deployment_manifest)}`
- `recording.csv` SHA-256: `{_sha256(recording_manifest)}`

## Deployment events

{event_lines}
"""
    event_roots = sorted({document.path.parent for document in plan.pending_documents})
    paths = [event_root / "soundhub" / filename for event_root in event_roots]
    payload = content.encode("utf-8")
    for path in paths:
        _write_atomic(path, payload)
    return paths
