#!/usr/bin/env python3
"""Recover Anza-Borrego SoundHub durations from staged FLAC headers.

This is a narrow, synchronized repair for the legacy 2026 Anza-Borrego event.
It reads the exact sample rate and total-sample count from each staged FLAC's
STREAMINFO block, then fills:

* ``recording_duration_sec`` in Box ``audio_file_metadata.csv``;
* ``end`` in the four staging fragment ``recording.csv`` files;
* ``end`` in the cumulative staging ``recording.csv``; and
* ``end`` in the Box event-local ``soundhub/recording.csv`` copy.

The default is a dry run. Pass ``--apply`` only after reviewing the plan. Audio
files, filenames, identifiers, start timestamps, and deployment dates are never
modified.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cassn.config import SOUNDHUB_RECORDING_FIELDS  # noqa: E402
from cassn.soundhub.staging import fragments_root, project_root  # noqa: E402
from cassn.soundhub.upload import load_soundhub_config  # noqa: E402


ANZA_DEPLOYMENT_IDS = {
    f"UC_AnzaBorrego_plot{plot}_BD_20260516" for plot in range(1, 5)
}
EXPECTED_RECORDINGS = 48
DURATION_COLUMN = "recording_duration_sec"
PROVENANCE_FILENAME = "metadata_correction_20260824_soundhub_durations.json"
_TOTAL_SAMPLES_MASK = (1 << 36) - 1


@dataclass(frozen=True)
class FlacDuration:
    path: Path
    sample_rate_hz: int
    total_samples: int
    seconds: str
    microseconds: int


@dataclass(frozen=True)
class CellChange:
    path: Path
    filename: str
    column: str
    before: str
    after: str


@dataclass
class CsvDocument:
    path: Path
    fieldnames: list[str]
    rows: list[dict]
    updated_rows: list[dict]
    has_bom: bool

    @property
    def changed(self) -> bool:
        return self.fieldnames != self.updated_fieldnames or self.rows != self.updated_rows

    @property
    def updated_fieldnames(self) -> list[str]:
        return getattr(self, "_updated_fieldnames", self.fieldnames)

    @updated_fieldnames.setter
    def updated_fieldnames(self, value: list[str]) -> None:
        self._updated_fieldnames = value


@dataclass
class BackfillResult:
    staging_root: Path
    box_event_root: Path
    recording_count: int = 0
    deployment_count: int = 0
    changed_files: int = 0
    changed_cells: int = 0
    changes: list[CellChange] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    applied: bool = False
    provenance_path: Path | None = None

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def changed(self) -> bool:
        return self.changed_files > 0


def _read_csv(path: Path) -> tuple[list[str], list[dict], bool]:
    with path.open("rb") as raw:
        has_bom = raw.read(3) == b"\xef\xbb\xbf"
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise ValueError("CSV has no header")
        rows = list(reader)
    if any(None in row for row in rows):
        raise ValueError("one or more rows contain more values than the header")
    return list(reader.fieldnames), rows, has_bom


def _csv_bytes(document: CsvDocument) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=document.updated_fieldnames)
    writer.writeheader()
    writer.writerows(document.updated_rows)
    payload = stream.getvalue().encode("utf-8")
    return (b"\xef\xbb\xbf" + payload) if document.has_bom else payload


def _write_atomic(path: Path, payload: bytes) -> None:
    mode = path.stat().st_mode if path.exists() else 0o644
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
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


def _duration_text(total_samples: int, sample_rate_hz: int) -> str:
    with localcontext() as context:
        context.prec = 30
        value = Decimal(total_samples) / Decimal(sample_rate_hz)
        rounded = value.quantize(Decimal("0.000000001"), rounding=ROUND_HALF_UP)
    return format(rounded, "f").rstrip("0").rstrip(".")


def read_flac_duration(path: Path) -> FlacDuration:
    """Read duration directly from the mandatory FLAC STREAMINFO block."""
    try:
        with path.open("rb") as stream:
            if stream.read(4) != b"fLaC":
                raise ValueError("missing FLAC marker")
            block_header = stream.read(4)
            if len(block_header) != 4:
                raise ValueError("truncated metadata block header")
            block_type = block_header[0] & 0x7F
            block_length = int.from_bytes(block_header[1:4], "big")
            if block_type != 0 or block_length != 34:
                raise ValueError("first metadata block is not 34-byte STREAMINFO")
            streaminfo = stream.read(block_length)
            if len(streaminfo) != block_length:
                raise ValueError("truncated STREAMINFO block")
    except OSError as exc:
        raise ValueError(str(exc)) from exc

    packed = int.from_bytes(streaminfo[10:18], "big")
    sample_rate_hz = packed >> 44
    total_samples = packed & _TOTAL_SAMPLES_MASK
    if sample_rate_hz <= 0 or total_samples <= 0:
        raise ValueError(
            f"invalid STREAMINFO sample rate/total samples: {sample_rate_hz}/{total_samples}"
        )
    microseconds = (total_samples * 1_000_000 + sample_rate_hz // 2) // sample_rate_hz
    return FlacDuration(
        path=path,
        sample_rate_hz=sample_rate_hz,
        total_samples=total_samples,
        seconds=_duration_text(total_samples, sample_rate_hz),
        microseconds=microseconds,
    )


def _recording_key(row: dict) -> tuple[str, str]:
    return (str(row.get("deployment_id") or "").strip(), str(row.get("filename") or "").strip())


def _expected_end(start: str, duration: FlacDuration) -> str:
    try:
        moment = datetime.fromisoformat(start)
    except ValueError as exc:
        raise ValueError(f"invalid start timestamp {start!r}") from exc
    return (moment + timedelta(microseconds=duration.microseconds)).isoformat(sep=" ")


def _load_recording_document(
    path: Path,
    expected: dict[tuple[str, str], FlacDuration],
    *,
    target_ids: set[str],
) -> tuple[CsvDocument | None, list[CellChange], list[str], set[tuple[str, str]]]:
    try:
        fieldnames, rows, has_bom = _read_csv(path)
    except Exception as exc:
        return None, [], [f"{path}: could not read CSV: {exc}"], set()
    if fieldnames != SOUNDHUB_RECORDING_FIELDS:
        return None, [], [f"{path}: header does not match SoundHub recording schema"], set()

    changes: list[CellChange] = []
    errors: list[str] = []
    found: set[tuple[str, str]] = set()
    seen: set[tuple[str, str]] = set()
    updated_rows: list[dict] = []
    for source in rows:
        row = dict(source)
        key = _recording_key(row)
        if key in seen:
            errors.append(f"{path}: duplicate recording key {key!r}")
        seen.add(key)
        if key[0] in target_ids:
            if key not in expected:
                errors.append(f"{path}: unexpected Anza recording row {key!r}")
            else:
                found.add(key)
                try:
                    after = _expected_end(row.get("start", ""), expected[key])
                except ValueError as exc:
                    errors.append(f"{path}: {key!r}: {exc}")
                else:
                    before = row.get("end", "")
                    if before not in ("", after):
                        errors.append(
                            f"{path}: {key!r}: existing end {before!r} conflicts with derived {after!r}"
                        )
                    elif before != after:
                        row["end"] = after
                        changes.append(CellChange(path, key[1], "end", before, after))
        updated_rows.append(row)
    document = CsvDocument(path, fieldnames, rows, updated_rows, has_bom)
    return document, changes, errors, found


def _load_audio_document(
    path: Path,
    expected: dict[tuple[str, str], FlacDuration],
) -> tuple[CsvDocument | None, list[CellChange], list[str], set[tuple[str, str]]]:
    try:
        fieldnames, rows, has_bom = _read_csv(path)
    except Exception as exc:
        return None, [], [f"{path}: could not read CSV: {exc}"], set()
    needed = {"filename", "deployment_id", "duration"}
    missing = sorted(needed - set(fieldnames))
    if missing:
        return None, [], [f"{path}: missing required column(s): {', '.join(missing)}"], set()

    updated_fieldnames = list(fieldnames)
    if DURATION_COLUMN not in updated_fieldnames:
        updated_fieldnames.insert(updated_fieldnames.index("duration") + 1, DURATION_COLUMN)

    changes: list[CellChange] = []
    errors: list[str] = []
    found: set[tuple[str, str]] = set()
    seen: set[tuple[str, str]] = set()
    updated_rows: list[dict] = []
    for source in rows:
        row = dict(source)
        deployment_id = str(row.get("deployment_id") or "").strip()
        wav_name = str(row.get("filename") or "").strip()
        flac_name = Path(wav_name).with_suffix(".flac").name if wav_name else ""
        key = (deployment_id, flac_name)
        if key in expected:
            if key in seen:
                errors.append(f"{path}: duplicate source metadata row {key!r}")
            seen.add(key)
            found.add(key)
            after = expected[key].seconds
            before = row.get(DURATION_COLUMN, "")
            if before not in ("", after):
                errors.append(
                    f"{path}: {key!r}: existing duration {before!r} conflicts with derived {after!r}"
                )
            elif before != after:
                row[DURATION_COLUMN] = after
                changes.append(CellChange(path, wav_name, DURATION_COLUMN, before, after))
        elif DURATION_COLUMN not in row:
            row[DURATION_COLUMN] = ""
        updated_rows.append(row)

    document = CsvDocument(path, fieldnames, rows, updated_rows, has_bom)
    document.updated_fieldnames = updated_fieldnames
    return document, changes, errors, found


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _provenance_payload(
    documents: list[CsvDocument],
    durations: dict[tuple[str, str], FlacDuration],
) -> dict:
    files = []
    for document in documents:
        if not document.changed:
            continue
        before = document.path.read_bytes()
        after = _csv_bytes(document)
        files.append(
            {
                "path": str(document.path),
                "before_sha256": _sha256(before),
                "after_sha256": _sha256(after),
            }
        )
    by_deployment = Counter(deployment_id for deployment_id, _ in durations)
    sample_rates = Counter(item.sample_rate_hz for item in durations.values())
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "deployment_event_id": "UC_AnzaBorrego_20260516",
        "purpose": (
            "Recover missing legacy recording durations and SoundHub recording end "
            "timestamps from staged FLAC STREAMINFO headers."
        ),
        "method": (
            "Duration seconds = STREAMINFO total_samples / sample_rate_hz; end = "
            "existing recording start plus that duration, rounded to the nearest "
            "microsecond for ISO 8601 timestamp representation."
        ),
        "recordings_updated": len(durations),
        "recordings_by_deployment": dict(sorted(by_deployment.items())),
        "sample_rates_hz": {str(key): value for key, value in sorted(sample_rates.items())},
        "files": files,
        "unchanged": [
            "FLAC and WAV audio bytes",
            "filenames",
            "deployment and recording identifiers",
            "recording start timestamps",
            "deployment dates",
        ],
        "excluded": (
            "Three 488-byte header-only Anza BD WAV failure rows are not staged and "
            "remain blank in recording_duration_sec."
        ),
    }


def backfill_anza_soundhub_durations(
    staging_root: Path,
    box_event_root: Path,
    *,
    apply: bool = False,
    expected_recordings: int = EXPECTED_RECORDINGS,
) -> BackfillResult:
    staging_root = Path(staging_root).expanduser().resolve()
    box_event_root = Path(box_event_root).expanduser().resolve()
    result = BackfillResult(staging_root, box_event_root)

    project_csv = project_root(staging_root) / "recording.csv"
    try:
        fields, project_rows, _ = _read_csv(project_csv)
    except Exception as exc:
        result.errors.append(f"{project_csv}: could not read CSV: {exc}")
        return result
    if fields != SOUNDHUB_RECORDING_FIELDS:
        result.errors.append(f"{project_csv}: header does not match SoundHub recording schema")
        return result

    target_rows = [row for row in project_rows if _recording_key(row)[0] in ANZA_DEPLOYMENT_IDS]
    target_keys = {_recording_key(row) for row in target_rows}
    if len(target_rows) != len(target_keys):
        result.errors.append(f"{project_csv}: duplicate Anza recording key")
        return result
    result.recording_count = len(target_keys)
    result.deployment_count = len({key[0] for key in target_keys})
    if result.recording_count != expected_recordings:
        result.errors.append(
            f"{project_csv}: expected {expected_recordings} Anza recordings, found {result.recording_count}"
        )
        return result
    found_ids = {key[0] for key in target_keys}
    if found_ids != ANZA_DEPLOYMENT_IDS:
        missing = sorted(ANZA_DEPLOYMENT_IDS - found_ids)
        result.errors.append("staging is missing Anza deployment ID(s): " + ", ".join(missing))
        return result

    durations: dict[tuple[str, str], FlacDuration] = {}
    for key in sorted(target_keys):
        deployment_id, filename = key
        flac_path = project_root(staging_root) / deployment_id / filename
        try:
            durations[key] = read_flac_duration(flac_path)
        except ValueError as exc:
            result.errors.append(f"{flac_path}: could not read FLAC duration: {exc}")
    if result.errors:
        return result

    documents: list[CsvDocument] = []
    project_document, changes, errors, found = _load_recording_document(
        project_csv, durations, target_ids=ANZA_DEPLOYMENT_IDS
    )
    result.errors.extend(errors)
    result.changes.extend(changes)
    if project_document is not None:
        documents.append(project_document)
    if found != target_keys:
        result.errors.append(f"{project_csv}: did not contain every staged Anza recording")

    fragment_documents: list[CsvDocument] = []
    fragment_found: set[tuple[str, str]] = set()
    for deployment_id in sorted(ANZA_DEPLOYMENT_IDS):
        path = fragments_root(staging_root) / deployment_id / "recording.csv"
        expected = {key: value for key, value in durations.items() if key[0] == deployment_id}
        document, changes, errors, found = _load_recording_document(
            path, expected, target_ids={deployment_id}
        )
        result.errors.extend(errors)
        result.changes.extend(changes)
        fragment_found.update(found)
        if document is not None:
            fragment_documents.append(document)
            documents.append(document)
    if fragment_found != target_keys:
        result.errors.append("staging fragments did not contain every staged Anza recording")

    box_recording_path = box_event_root / "soundhub" / "recording.csv"
    box_recording_document, changes, errors, found = _load_recording_document(
        box_recording_path, durations, target_ids=ANZA_DEPLOYMENT_IDS
    )
    result.errors.extend(errors)
    result.changes.extend(changes)
    if box_recording_document is not None:
        documents.append(box_recording_document)
    if found != target_keys:
        result.errors.append(f"{box_recording_path}: did not contain every staged Anza recording")

    audio_path = box_event_root / "audio_file_metadata.csv"
    audio_document, changes, errors, found = _load_audio_document(audio_path, durations)
    result.errors.extend(errors)
    result.changes.extend(changes)
    if audio_document is not None:
        documents.append(audio_document)
    if found != target_keys:
        result.errors.append(f"{audio_path}: did not contain every staged Anza recording")

    if project_document is not None and len(fragment_documents) == 4:
        project_target_rows = {
            _recording_key(row): row
            for row in project_document.updated_rows
            if _recording_key(row)[0] in ANZA_DEPLOYMENT_IDS
        }
        fragment_target_rows = {
            _recording_key(row): row
            for document in fragment_documents
            for row in document.updated_rows
        }
        if project_target_rows != fragment_target_rows:
            result.errors.append("updated staging fragments differ from cumulative recording.csv")
        if box_recording_document is not None:
            box_rows = {_recording_key(row): row for row in box_recording_document.updated_rows}
            if project_target_rows != box_rows:
                result.errors.append("updated Box SoundHub copy differs from staging recording rows")

    result.changed_files = sum(document.changed for document in documents)
    result.changed_cells = len(result.changes)
    if result.errors or not apply or not result.changed_files:
        return result

    provenance = _provenance_payload(documents, durations)
    result.provenance_path = box_event_root / "qc" / PROVENANCE_FILENAME
    ordered = [
        document
        for document in documents
        if document.path.is_relative_to(box_event_root) and document.changed
    ] + [
        document
        for document in documents
        if not document.path.is_relative_to(box_event_root) and document.changed
    ]
    for document in ordered:
        _write_atomic(document.path, _csv_bytes(document))
    provenance_bytes = (json.dumps(provenance, indent=2) + "\n").encode("utf-8")
    _write_atomic(result.provenance_path, provenance_bytes)
    result.applied = True
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--staging",
        default=load_soundhub_config()["staging_root"],
        help="SoundHub staging root",
    )
    parser.add_argument(
        "--box-event-root",
        required=True,
        help="Box Drive Anza-Borrego deployment-event folder",
    )
    parser.add_argument("--apply", action="store_true", help="Apply the validated repair")
    return parser


def main() -> int:
    args = _parser().parse_args()
    result = backfill_anza_soundhub_durations(
        Path(args.staging), Path(args.box_event_root), apply=args.apply
    )
    print(f"Mode: {'apply' if args.apply else 'dry-run'}")
    print(f"Deployments: {result.deployment_count}")
    print(f"Recordings: {result.recording_count}")
    print(f"Changed files: {result.changed_files}")
    print(f"Changed cells: {result.changed_cells}")
    if result.errors:
        print("Errors:")
        for error in result.errors:
            print(f"- {error}")
        return 1
    by_column = Counter(change.column for change in result.changes)
    for column, count in sorted(by_column.items()):
        print(f"{column}: {count} cell(s)")
    if args.apply:
        print("Applied." if result.applied else "No writes were needed.")
        if result.provenance_path:
            print(f"Provenance: {result.provenance_path}")
    else:
        print("No files were changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
