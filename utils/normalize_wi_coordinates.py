#!/usr/bin/env python3
"""Normalize coordinates in existing Wildlife Insights deployment CSVs.

Wildlife Insights requires latitude and longitude to contain between four and
eight digits after the decimal point. This utility writes every valid coordinate
with exactly eight digits, padding shorter values and rounding longer values.

The default is a dry run. Pass ``--apply`` to replace changed CSVs atomically.
The target may be one CSV or a directory containing deployment folders.
Directory scans only select canonical
``WI_metadata/wildlife_insights_*_deployments.csv`` files.
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

from cassn.export.wildlife_insights import format_wi_coordinate  # noqa: E402


WI_CSV_GLOB = "wildlife_insights_*_deployments.csv"
COORDINATE_COLUMNS = ("longitude", "latitude")


@dataclass
class FileResult:
    path: Path
    rows: int = 0
    changed_rows: int = 0
    changed_cells: int = 0
    blank_cells: int = 0
    invalid_cells: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.changed_cells > 0

    @property
    def ok(self) -> bool:
        return not self.errors


def discover_wi_csvs(target: Path) -> list[Path]:
    """Return canonical WI deployment CSVs below ``target``.

    A directly supplied file is accepted regardless of its filename, which is
    useful for testing or repairing an individually renamed export.
    """
    target = target.expanduser().resolve()
    if target.is_file():
        return [target] if target.suffix.lower() == ".csv" else []
    if not target.is_dir():
        return []
    return sorted(
        path
        for path in target.rglob(WI_CSV_GLOB)
        if path.is_file() and path.parent.name == "WI_metadata"
    )


def _is_finite_coordinate(value: object) -> bool:
    raw = "" if value is None else str(value).strip()
    if not raw:
        return False
    try:
        return Decimal(raw).is_finite()
    except (InvalidOperation, ValueError):
        return False


def _read_csv(path: Path) -> tuple[list[str], list[dict], bool]:
    """Read a UTF-8 CSV and return fields, rows, and whether it had a BOM."""
    with path.open("rb") as raw:
        has_bom = raw.read(3) == b"\xef\xbb\xbf"
    with path.open("r", newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise ValueError("CSV has no header")
        return list(reader.fieldnames), list(reader), has_bom


def _write_csv_atomically(
    path: Path,
    fieldnames: list[str],
    rows: list[dict],
    *,
    has_bom: bool,
) -> None:
    """Replace ``path`` only after a complete CSV has been written."""
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    tmp_path = Path(tmp_name)
    try:
        encoding = "utf-8-sig" if has_bom else "utf-8"
        with os.fdopen(fd, "w", newline="", encoding=encoding) as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
            stream.flush()
            os.fsync(stream.fileno())
        shutil.copymode(path, tmp_path)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp_path.unlink(missing_ok=True)
        raise


def normalize_wi_csv(path: Path, *, apply: bool = False) -> FileResult:
    """Plan or apply coordinate normalization to one WI deployment CSV."""
    path = Path(path)
    result = FileResult(path=path)
    try:
        fieldnames, rows, has_bom = _read_csv(path)
    except Exception as exc:
        result.errors.append(f"could not read CSV: {exc}")
        return result

    missing = [column for column in COORDINATE_COLUMNS if column not in fieldnames]
    if missing:
        result.errors.append("missing required column(s): " + ", ".join(missing))
        return result

    result.rows = len(rows)
    for row in rows:
        row_changed = False
        for column in COORDINATE_COLUMNS:
            original = row.get(column, "")
            raw = "" if original is None else str(original).strip()
            if not raw:
                result.blank_cells += 1
                continue
            if not _is_finite_coordinate(raw):
                result.invalid_cells += 1
                continue
            normalized = format_wi_coordinate(raw)
            if normalized != original:
                row[column] = normalized
                result.changed_cells += 1
                row_changed = True
        if row_changed:
            result.changed_rows += 1

    if apply and result.changed:
        try:
            _write_csv_atomically(path, fieldnames, rows, has_bom=has_bom)
        except Exception as exc:
            result.errors.append(f"could not replace CSV: {exc}")
    return result


def _display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Pad or round latitude/longitude to eight decimal places in existing "
            "Wildlife Insights deployment CSVs."
        )
    )
    parser.add_argument(
        "target",
        type=Path,
        help="One WI CSV, a deployment folder, or a broader folder such as a year.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Atomically replace changed CSVs. Default is a read-only dry run.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    target = args.target.expanduser().resolve()
    files = discover_wi_csvs(target)
    if not files:
        print(f"No Wildlife Insights deployment CSVs found under {target}")
        return 1

    mode = "APPLY" if args.apply else "dry-run"
    print(f"Scanning {target}")
    print(f"Mode: {mode} | coordinate precision: 8 decimal places\n")

    total_rows = total_changed_rows = total_changed_cells = 0
    total_blank = total_invalid = errors = 0
    root = target if target.is_dir() else target.parent
    for path in files:
        result = normalize_wi_csv(path, apply=args.apply)
        total_rows += result.rows
        total_changed_rows += result.changed_rows
        total_changed_cells += result.changed_cells
        total_blank += result.blank_cells
        total_invalid += result.invalid_cells
        rel = _display_path(path, root)
        if result.errors:
            errors += 1
            print(f"[ERROR] {rel}: {'; '.join(result.errors)}")
        elif result.changed:
            action = "UPDATED" if args.apply else "CHANGE"
            print(
                f"[{action}] {rel}: {result.changed_cells} coordinate(s) in "
                f"{result.changed_rows}/{result.rows} row(s)"
            )
        else:
            print(f"[ok] {rel}: {result.rows} row(s), already compliant")
        if result.blank_cells or result.invalid_cells:
            print(
                f"    warning: {result.blank_cells} blank and "
                f"{result.invalid_cells} invalid coordinate cell(s) left unchanged"
            )

    print(
        f"\n{len(files)} file(s), {total_rows} row(s); "
        f"{total_changed_cells} coordinate(s) in {total_changed_rows} row(s) "
        f"{'updated' if args.apply else 'would change'}."
    )
    if total_blank or total_invalid:
        print(
            f"Warnings: {total_blank} blank and {total_invalid} invalid "
            "coordinate cell(s) left unchanged."
        )
    if not args.apply:
        print("Dry run — nothing changed. Re-run with --apply to update the CSVs.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
