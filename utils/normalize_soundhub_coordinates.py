#!/usr/bin/env python3
"""Normalize coordinates in the staged SoundHub deployment manifests.

SoundHub's deployment template accepts four to eight decimal places.  This
utility writes every staged latitude and longitude with exactly eight decimal
places, matching the established Wildlife Insights formatter while leaving the
canonical plot lookup and source deployment metadata untouched.

The default is a read-only dry run.  ``--apply`` updates both the durable
per-deployment fragments and the cumulative project ``deployment.csv``.  The
project file is replaced last, and every replacement is atomic.  Neither
``recording.csv`` nor staged FLAC media is opened or modified.
"""
from __future__ import annotations

import argparse
import csv
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

# Make the cassn package importable when this file is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cassn.config import SOUNDHUB_DEPLOYMENT_FIELDS  # noqa: E402
from cassn.export.wildlife_insights import format_wi_coordinate  # noqa: E402
from cassn.soundhub.export import DEPLOYMENT_CSV  # noqa: E402
from cassn.soundhub.staging import fragments_root, project_root  # noqa: E402
from cassn.soundhub.upload import load_soundhub_config  # noqa: E402


COORDINATE_COLUMNS = ("longitude", "latitude")
COORDINATE_BOUNDS = {
    "longitude": (Decimal("-180"), Decimal("180")),
    "latitude": (Decimal("-90"), Decimal("90")),
}


@dataclass
class CoordinateChange:
    deployment_id: str
    column: str
    before: str
    after: str


@dataclass
class CsvDocument:
    path: Path
    fieldnames: list[str]
    rows: list[dict]
    normalized_rows: list[dict]
    has_bom: bool

    @property
    def changed(self) -> bool:
        return self.rows != self.normalized_rows


@dataclass
class NormalizationResult:
    staging_root: Path
    project_csv: Path
    deployment_rows: int = 0
    fragment_files: int = 0
    changed_deployments: int = 0
    changed_cells: int = 0
    changed_files: int = 0
    changes: list[CoordinateChange] = field(default_factory=list)
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
    with path.open("r", newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise ValueError("CSV has no header")
        rows = list(reader)
    if any(None in row for row in rows):
        raise ValueError("one or more rows contain more values than the header")
    return list(reader.fieldnames), rows, has_bom


def _normalized_coordinate(value: object, column: str) -> tuple[str, str | None]:
    raw = "" if value is None else str(value).strip()
    if not raw:
        return raw, "is blank"
    try:
        coordinate = Decimal(raw)
    except (InvalidOperation, ValueError):
        return raw, f"is not numeric: {raw!r}"
    if not coordinate.is_finite():
        return raw, f"is not finite: {raw!r}"
    lower, upper = COORDINATE_BOUNDS[column]
    if not lower <= coordinate <= upper:
        return raw, f"is outside {lower} to {upper}: {raw!r}"
    normalized = format_wi_coordinate(raw)
    decimal_places = len(normalized.partition(".")[2])
    if decimal_places != 8:
        return raw, f"could not be normalized to eight decimal places: {raw!r}"
    return normalized, None


def _normalize_document(
    path: Path,
    *,
    expected_deployment_id: str | None = None,
) -> tuple[CsvDocument | None, list[CoordinateChange], list[str]]:
    errors: list[str] = []
    changes: list[CoordinateChange] = []
    try:
        fieldnames, rows, has_bom = _read_csv(path)
    except Exception as exc:
        return None, changes, [f"{path}: could not read CSV: {exc}"]

    if fieldnames != SOUNDHUB_DEPLOYMENT_FIELDS:
        return None, changes, [f"{path}: header does not match the SoundHub deployment schema"]
    if expected_deployment_id is not None and len(rows) != 1:
        return None, changes, [
            f"{path}: fragment must contain exactly one row; found {len(rows)}"
        ]

    normalized_rows: list[dict] = []
    for row_number, source in enumerate(rows, start=2):
        row = dict(source)
        deployment_id = (row.get("deployment_id") or "").strip()
        if not deployment_id:
            errors.append(f"{path}:{row_number}: deployment_id is blank")
        if expected_deployment_id is not None and deployment_id != expected_deployment_id:
            errors.append(
                f"{path}:{row_number}: deployment_id {deployment_id!r} does not "
                f"match fragment folder {expected_deployment_id!r}"
            )
        for column in COORDINATE_COLUMNS:
            before = row.get(column, "")
            after, error = _normalized_coordinate(before, column)
            if error:
                errors.append(
                    f"{path}:{row_number}: {deployment_id or '<blank>'} "
                    f"{column} {error}"
                )
                continue
            if after != before:
                row[column] = after
                changes.append(
                    CoordinateChange(
                        deployment_id=deployment_id,
                        column=column,
                        before=str(before),
                        after=after,
                    )
                )
        normalized_rows.append(row)

    return (
        CsvDocument(
            path=path,
            fieldnames=fieldnames,
            rows=rows,
            normalized_rows=normalized_rows,
            has_bom=has_bom,
        ),
        changes,
        errors,
    )


def _rows_by_id(rows: list[dict], path: Path) -> tuple[dict[str, dict], list[str]]:
    indexed: dict[str, dict] = {}
    errors: list[str] = []
    for row_number, row in enumerate(rows, start=2):
        deployment_id = (row.get("deployment_id") or "").strip()
        if not deployment_id:
            continue
        if deployment_id in indexed:
            errors.append(f"{path}:{row_number}: duplicate deployment_id {deployment_id!r}")
        else:
            indexed[deployment_id] = row
    return indexed, errors


def _plan(staging_root: Path) -> tuple[NormalizationResult, list[CsvDocument]]:
    staging_root = Path(staging_root).expanduser().resolve()
    project_csv = project_root(staging_root) / DEPLOYMENT_CSV
    result = NormalizationResult(staging_root=staging_root, project_csv=project_csv)
    documents: list[CsvDocument] = []

    project_document, project_changes, errors = _normalize_document(project_csv)
    result.errors.extend(errors)
    if project_document is None:
        return result, documents
    result.deployment_rows = len(project_document.rows)

    fragment_base = fragments_root(staging_root)
    if not fragment_base.is_dir():
        result.errors.append(f"No SoundHub fragment directory at {fragment_base}")
        return result, documents
    fragment_dirs = sorted(path for path in fragment_base.iterdir() if path.is_dir())
    if not fragment_dirs:
        result.errors.append(f"No SoundHub deployment fragments under {fragment_base}")
        return result, documents

    fragment_documents: list[CsvDocument] = []
    fragment_changes: list[CoordinateChange] = []
    for directory in fragment_dirs:
        path = directory / DEPLOYMENT_CSV
        if not path.is_file():
            result.errors.append(f"Missing deployment fragment: {path}")
            continue
        document, changes, errors = _normalize_document(
            path,
            expected_deployment_id=directory.name,
        )
        result.errors.extend(errors)
        if document is not None:
            fragment_documents.append(document)
            fragment_changes.extend(changes)

    result.fragment_files = len(fragment_documents)
    if result.errors:
        return result, documents

    project_by_id, errors = _rows_by_id(project_document.normalized_rows, project_csv)
    result.errors.extend(errors)
    fragment_by_id: dict[str, dict] = {}
    for document in fragment_documents:
        row = document.normalized_rows[0]
        deployment_id = (row.get("deployment_id") or "").strip()
        if deployment_id in fragment_by_id:
            result.errors.append(f"Duplicate deployment fragment for {deployment_id!r}")
        else:
            fragment_by_id[deployment_id] = row

    project_ids = set(project_by_id)
    fragment_ids = set(fragment_by_id)
    if project_ids != fragment_ids:
        missing = sorted(fragment_ids - project_ids)
        extra = sorted(project_ids - fragment_ids)
        if missing:
            result.errors.append(
                "Project deployment.csv is missing fragment deployment_id(s): "
                + ", ".join(missing)
            )
        if extra:
            result.errors.append(
                "Project deployment.csv has no fragment for deployment_id(s): "
                + ", ".join(extra)
            )

    for deployment_id in sorted(project_ids & fragment_ids):
        if project_by_id[deployment_id] != fragment_by_id[deployment_id]:
            differing = [
                field
                for field in SOUNDHUB_DEPLOYMENT_FIELDS
                if project_by_id[deployment_id].get(field, "")
                != fragment_by_id[deployment_id].get(field, "")
            ]
            result.errors.append(
                f"Project row and fragment differ for {deployment_id}: "
                + ", ".join(differing)
            )

    if result.errors:
        return result, documents

    # Changes are duplicated physically in the project and fragment layers.
    # Report each logical deployment/column once while retaining every changed
    # document for the atomic apply step.
    unique_changes: dict[tuple[str, str], CoordinateChange] = {}
    for change in [*project_changes, *fragment_changes]:
        unique_changes.setdefault((change.deployment_id, change.column), change)
    result.changes = sorted(
        unique_changes.values(), key=lambda item: (item.deployment_id, item.column)
    )
    result.changed_cells = len(result.changes)
    result.changed_deployments = len({change.deployment_id for change in result.changes})

    documents = [*fragment_documents, project_document]
    result.changed_files = sum(document.changed for document in documents)
    return result, documents


def _prepare_csv(document: CsvDocument) -> Path:
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{document.path.name}.", suffix=".tmp", dir=document.path.parent
    )
    tmp_path = Path(tmp_name)
    try:
        encoding = "utf-8-sig" if document.has_bom else "utf-8"
        with os.fdopen(fd, "w", newline="", encoding=encoding) as stream:
            writer = csv.DictWriter(stream, fieldnames=document.fieldnames)
            writer.writeheader()
            writer.writerows(document.normalized_rows)
            stream.flush()
            os.fsync(stream.fileno())
        shutil.copymode(document.path, tmp_path)
        return tmp_path
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp_path.unlink(missing_ok=True)
        raise


def normalize_soundhub_staging(staging_root: Path, *, apply: bool = False) -> NormalizationResult:
    """Plan or apply coordinate normalization to one SoundHub staging tree."""
    result, documents = _plan(staging_root)
    if not result.ok or not apply or not result.changed:
        return result

    changed_documents = [document for document in documents if document.changed]
    # _plan orders fragments first and the cumulative project manifest last.
    prepared: list[tuple[CsvDocument, Path]] = []
    try:
        for document in changed_documents:
            prepared.append((document, _prepare_csv(document)))
    except Exception as exc:
        for _document, tmp_path in prepared:
            tmp_path.unlink(missing_ok=True)
        result.errors.append(f"Could not prepare atomic replacements: {exc}")
        return result

    try:
        for document, tmp_path in prepared:
            os.replace(tmp_path, document.path)
    except Exception as exc:
        for _document, tmp_path in prepared:
            tmp_path.unlink(missing_ok=True)
        result.errors.append(
            "Could not finish replacing manifests; rerun the utility safely to "
            f"resume: {exc}"
        )
        return result

    verification, _documents = _plan(result.staging_root)
    if not verification.ok:
        result.errors.append(
            "Post-apply verification failed: " + "; ".join(verification.errors)
        )
        return result
    if verification.changed:
        result.errors.append(
            f"Post-apply verification found {verification.changed_cells} pending "
            "coordinate change(s)"
        )
        return result
    result.applied = True
    return result


def parse_args() -> argparse.Namespace:
    settings = load_soundhub_config()
    parser = argparse.ArgumentParser(
        description=(
            "Pad or round coordinates to eight decimal places in staged "
            "SoundHub deployment manifests."
        )
    )
    parser.add_argument(
        "--staging",
        type=Path,
        default=Path(settings["staging_root"]),
        help=(
            "SoundHub staging root (default: config.json soundhub.staging_root "
            "or the application default)."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Atomically replace changed manifests. Default is a read-only dry run.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    mode = "APPLY" if args.apply else "dry-run"
    print(f"Staging root: {args.staging.expanduser().resolve()}")
    print(f"Mode: {mode} | coordinate precision: 8 decimal places\n")

    result = normalize_soundhub_staging(args.staging, apply=args.apply)
    if result.errors:
        for error in result.errors:
            print(f"ERROR: {error}")
        print("\nNo upload was attempted.")
        return 1

    for change in result.changes:
        print(
            f"{change.deployment_id} {change.column}: "
            f"{change.before} -> {change.after}"
        )

    action = "updated" if result.applied else "would change"
    if result.changed:
        print(
            f"\n{result.changed_cells} coordinate value(s) in "
            f"{result.changed_deployments}/{result.deployment_rows} deployment(s) "
            f"{action}; {result.changed_files} physical manifest file(s)."
        )
    else:
        print(
            f"{result.deployment_rows} deployment row(s) across "
            f"{result.fragment_files} fragment(s); coordinates already compliant."
        )

    if not args.apply and result.changed:
        print("Dry run — nothing changed. Re-run with --apply to update the manifests.")
    print("recording.csv and staged FLAC files were not touched. No upload was attempted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
