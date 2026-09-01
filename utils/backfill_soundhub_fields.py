#!/usr/bin/env python3
"""Backfill approved metadata fields in existing SoundHub staging and Box CSVs.

This is a narrow, resumable repair for the pre-upload 2026 SoundHub batch. It
updates the cumulative and per-deployment SoundHub manifests together with the
matching rows in Box ``audio_file_metadata.csv`` and event-local
``soundhub/deployment.csv`` copies. FLAC/WAV media and ``recording.csv`` are
never opened or modified.

The default is a dry run. Pass ``--apply`` only after reviewing the plan.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cassn.config import SOUNDHUB_DEPLOYMENT_FIELDS  # noqa: E402
from cassn.export.wildlife_insights import (  # noqa: E402
    SUBPROJECT_DESIGN,
    subproject_for,
)
from cassn.soundhub.staging import fragments_root, project_root  # noqa: E402
from cassn.soundhub.upload import load_soundhub_config  # noqa: E402


MOUNTED_ON = "metal_pole"
ANZA_SENSOR_HEIGHTS = {
    "UC_AnzaBorrego_plot1_BD_20260516": "2.5",
    "UC_AnzaBorrego_plot2_BD_20260516": "2.5",
    "UC_AnzaBorrego_plot3_BD_20260516": "2",
    "UC_AnzaBorrego_plot4_BD_20260516": "2.5",
}
DEPLOYMENT_ID_RE = re.compile(
    r"^UC_(?P<site>.+)_plot\d+_BD_(?P<date>\d{8})(?:-v\d{2})?$"
)


@dataclass(frozen=True)
class FieldChange:
    path: Path
    deployment_id: str
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
        return self.rows != self.updated_rows


@dataclass
class BackfillResult:
    staging_root: Path
    box_year_root: Path
    deployment_count: int = 0
    changed_files: int = 0
    changed_cells: int = 0
    changes: list[FieldChange] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    applied: bool = False

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


def _targets(deployment_id: str) -> dict[str, str]:
    match = DEPLOYMENT_ID_RE.fullmatch(deployment_id)
    if not match:
        raise ValueError(f"not a supported staged BD deployment ID: {deployment_id!r}")
    date = match.group("date")
    return {
        "subproject": subproject_for(match.group("site"), date),
        "subproject_design": SUBPROJECT_DESIGN,
        "mounted_on": MOUNTED_ON,
        **(
            {"sensor_height_meters": ANZA_SENSOR_HEIGHTS[deployment_id]}
            if deployment_id in ANZA_SENSOR_HEIGHTS
            else {}
        ),
    }


def _updated_document(
    path: Path,
    target_ids: set[str],
    *,
    require_soundhub_schema: bool = False,
) -> tuple[CsvDocument | None, list[FieldChange], list[str], set[str]]:
    try:
        fieldnames, rows, has_bom = _read_csv(path)
    except Exception as exc:
        return None, [], [f"{path}: could not read CSV: {exc}"], set()
    if require_soundhub_schema and fieldnames != SOUNDHUB_DEPLOYMENT_FIELDS:
        return None, [], [f"{path}: header does not match SoundHub deployment schema"], set()
    needed = {
        "deployment_id",
        "subproject",
        "subproject_design",
        "mounted_on",
        "sensor_height_meters",
    }
    missing = sorted(needed - set(fieldnames))
    if missing:
        return None, [], [f"{path}: missing required column(s): {', '.join(missing)}"], set()

    changes: list[FieldChange] = []
    found: set[str] = set()
    updated_rows: list[dict] = []
    for source in rows:
        row = dict(source)
        deployment_id = (row.get("deployment_id") or "").strip()
        if deployment_id in target_ids:
            found.add(deployment_id)
            for column, after in _targets(deployment_id).items():
                before = row.get(column, "")
                if before != after:
                    row[column] = after
                    changes.append(
                        FieldChange(path, deployment_id, column, str(before), after)
                    )
        updated_rows.append(row)
    return CsvDocument(path, fieldnames, rows, updated_rows, has_bom), changes, [], found


def _shallow_event_folders(box_year_root: Path):
    for reserve in sorted(path for path in box_year_root.iterdir() if path.is_dir()):
        for event in sorted(path for path in reserve.iterdir() if path.is_dir()):
            yield event


def _write_atomic(document: CsvDocument) -> None:
    mode = document.path.stat().st_mode
    encoding = "utf-8-sig" if document.has_bom else "utf-8"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{document.path.name}.", suffix=".tmp", dir=document.path.parent
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", newline="", encoding=encoding) as stream:
            writer = csv.DictWriter(stream, fieldnames=document.fieldnames)
            writer.writeheader()
            writer.writerows(document.updated_rows)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, document.path)
    finally:
        if tmp.exists():
            tmp.unlink()


def backfill_soundhub_fields(
    staging_root: Path,
    box_year_root: Path,
    *,
    apply: bool = False,
) -> BackfillResult:
    staging_root = Path(staging_root).expanduser().resolve()
    box_year_root = Path(box_year_root).expanduser().resolve()
    result = BackfillResult(staging_root, box_year_root)
    project_csv = project_root(staging_root) / "deployment.csv"

    try:
        project_fields, project_rows, _ = _read_csv(project_csv)
    except Exception as exc:
        result.errors.append(f"{project_csv}: could not read CSV: {exc}")
        return result
    if project_fields != SOUNDHUB_DEPLOYMENT_FIELDS:
        result.errors.append(f"{project_csv}: header does not match SoundHub deployment schema")
        return result
    target_ids = {(row.get("deployment_id") or "").strip() for row in project_rows}
    target_ids.discard("")
    result.deployment_count = len(target_ids)
    if len(target_ids) != len(project_rows):
        result.errors.append(f"{project_csv}: blank or duplicate deployment_id")
        return result
    try:
        for deployment_id in target_ids:
            _targets(deployment_id)
    except ValueError as exc:
        result.errors.append(str(exc))
        return result

    documents: list[CsvDocument] = []
    project_document, changes, errors, found = _updated_document(
        project_csv, target_ids, require_soundhub_schema=True
    )
    result.errors.extend(errors)
    if project_document is not None:
        documents.append(project_document)
        result.changes.extend(changes)
    if found != target_ids:
        result.errors.append(f"{project_csv}: did not contain every staged deployment ID")

    fragment_found: set[str] = set()
    fragment_documents: list[CsvDocument] = []
    for deployment_id in sorted(target_ids):
        path = fragments_root(staging_root) / deployment_id / "deployment.csv"
        document, changes, errors, found = _updated_document(
            path, {deployment_id}, require_soundhub_schema=True
        )
        result.errors.extend(errors)
        if document is not None:
            if len(document.rows) != 1:
                result.errors.append(f"{path}: expected one deployment row")
            fragment_documents.append(document)
            result.changes.extend(changes)
            fragment_found.update(found)
    documents.extend(fragment_documents)
    if fragment_found != target_ids:
        result.errors.append("SoundHub fragments did not cover every staged deployment ID")

    box_audio_found: set[str] = set()
    box_copy_found: set[str] = set()
    box_documents: list[CsvDocument] = []
    if not box_year_root.is_dir():
        result.errors.append(f"Box year root does not exist: {box_year_root}")
    else:
        for event in _shallow_event_folders(box_year_root):
            audio_path = event / "audio_file_metadata.csv"
            if not audio_path.is_file():
                continue
            try:
                _, audio_rows, _ = _read_csv(audio_path)
            except Exception as exc:
                result.errors.append(f"{audio_path}: could not read CSV: {exc}")
                continue
            overlap = {
                (row.get("deployment_id") or "").strip() for row in audio_rows
            } & target_ids
            if not overlap:
                continue
            document, changes, errors, found = _updated_document(audio_path, overlap)
            result.errors.extend(errors)
            if document is not None:
                box_documents.append(document)
                result.changes.extend(changes)
                box_audio_found.update(found)

            copy_path = event / "soundhub" / "deployment.csv"
            document, changes, errors, found = _updated_document(
                copy_path, overlap, require_soundhub_schema=True
            )
            result.errors.extend(errors)
            if document is not None:
                box_documents.append(document)
                result.changes.extend(changes)
                box_copy_found.update(found)
    documents.extend(box_documents)
    if box_audio_found != target_ids:
        missing = sorted(target_ids - box_audio_found)
        result.errors.append("Box audio metadata missing staged ID(s): " + ", ".join(missing))
    if box_copy_found != target_ids:
        missing = sorted(target_ids - box_copy_found)
        result.errors.append("Box SoundHub copies missing staged ID(s): " + ", ".join(missing))

    if project_document is not None and len(fragment_documents) == len(target_ids):
        project_by_id = {
            row["deployment_id"]: row for row in project_document.updated_rows
        }
        for document in fragment_documents:
            row = document.updated_rows[0]
            if project_by_id.get(row["deployment_id"]) != row:
                result.errors.append(
                    f"{document.path}: normalized fragment differs from project deployment.csv"
                )

    result.changed_files = sum(document.changed for document in documents)
    result.changed_cells = len(result.changes)
    if result.errors or not apply or not result.changed_files:
        return result

    ordered = (
        [document for document in box_documents if document.changed]
        + [document for document in fragment_documents if document.changed]
        + ([project_document] if project_document and project_document.changed else [])
    )
    for document in ordered:
        _write_atomic(document)
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
        "--box-year-root",
        required=True,
        help="Box Drive data year folder containing the source events",
    )
    parser.add_argument("--apply", action="store_true", help="Apply the validated repair")
    return parser


def main() -> int:
    args = _parser().parse_args()
    result = backfill_soundhub_fields(
        Path(args.staging), Path(args.box_year_root), apply=args.apply
    )
    print(f"Mode: {'apply' if args.apply else 'dry-run'}")
    print(f"Deployments: {result.deployment_count}")
    print(f"Changed files: {result.changed_files}")
    print(f"Changed cells: {result.changed_cells}")
    if result.errors:
        print("Errors:")
        for error in result.errors:
            print(f"- {error}")
        return 1
    by_column: dict[str, int] = {}
    for change in result.changes:
        by_column[change.column] = by_column.get(change.column, 0) + 1
    for column, count in sorted(by_column.items()):
        print(f"{column}: {count} cell(s)")
    if args.apply:
        print("Applied." if result.applied else "No writes were needed.")
    else:
        print("No files were changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
