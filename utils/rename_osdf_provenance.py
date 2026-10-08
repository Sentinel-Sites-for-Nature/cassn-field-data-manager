#!/usr/bin/env python3
"""Rename unused Pelican provenance headers to OSDF terminology.

The app originally named archival-publication fields after Pelican, the
transfer client. OSDF is the durable destination the provenance actually
describes. This migration changes exactly three CSV header names and preserves
every byte after the header::

    is_uploaded_to_pelican  -> is_uploaded_to_osdf
    pelican_uploader        -> osdf_uploader
    pelican_upload_datetime -> osdf_upload_datetime

Safety is intentionally strict. Every legacy status must still be false/blank,
and every uploader and timestamp must be blank. Mixed, partial, populated, or
malformed schemas are refused. The default is a read-only recursive preview.
Pass ``--apply --in-place`` for Box Drive so the existing file inode, Box ID,
and Box version history are retained.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

METADATA_NAMES = frozenset({"image_file_metadata.csv", "audio_file_metadata.csv"})
RENAMES = {
    "is_uploaded_to_pelican": "is_uploaded_to_osdf",
    "pelican_uploader": "osdf_uploader",
    "pelican_upload_datetime": "osdf_upload_datetime",
}
LEGACY_FIELDS = frozenset(RENAMES)
OSDF_FIELDS = frozenset(RENAMES.values())
FALSE_VALUES = frozenset({"", "0", "false", "no"})
AUDIT_DIR = Path("local_data/maintenance_audits")
COPY_CHUNK_BYTES = 8 * 1024 * 1024


class ProvenanceRenameError(Exception):
    """A file cannot be migrated without making an unsafe assumption."""


@dataclass(frozen=True)
class FilePlan:
    path: Path
    rows: int
    size: int
    source_sha1: str
    needs_change: bool


@dataclass(frozen=True)
class AppliedFile:
    path: str
    rows: int
    old_size: int
    new_size: int
    old_sha1: str
    new_sha1: str


def _sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(COPY_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _header(path: Path) -> tuple[bytes, list[str]]:
    with Path(path).open("rb") as stream:
        header = stream.readline()
    if not header:
        raise ProvenanceRenameError("file is empty")
    try:
        text = header.decode("utf-8-sig")
        fields = next(csv.reader([text.rstrip("\r\n")]))
    except (UnicodeError, csv.Error, StopIteration) as exc:
        raise ProvenanceRenameError(f"header is not valid UTF-8 CSV: {exc}") from exc
    if not fields or len(fields) != len(set(fields)):
        raise ProvenanceRenameError("header is empty or contains duplicate fields")
    return header, fields


def inspect_file(path: Path) -> FilePlan:
    """Validate one CSV and return its idempotent migration plan."""
    path = Path(path)
    _, fields = _header(path)
    present_legacy = LEGACY_FIELDS.intersection(fields)
    present_osdf = OSDF_FIELDS.intersection(fields)
    legacy = present_legacy == LEGACY_FIELDS and not present_osdf
    current = present_osdf == OSDF_FIELDS and not present_legacy
    if not (legacy or current):
        raise ProvenanceRenameError(
            "expected either all three Pelican fields or all three OSDF fields; "
            f"found legacy={sorted(present_legacy)}, osdf={sorted(present_osdf)}"
        )

    rows = 0
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            for number, row in enumerate(reader, start=2):
                rows += 1
                if None in row:
                    raise ProvenanceRenameError(
                        f"row {number} has more values than the header"
                    )
                if legacy:
                    status = str(row.get("is_uploaded_to_pelican") or "").strip()
                    uploader = str(row.get("pelican_uploader") or "").strip()
                    uploaded_at = str(
                        row.get("pelican_upload_datetime") or ""
                    ).strip()
                    if status.lower() not in FALSE_VALUES:
                        raise ProvenanceRenameError(
                            f"row {number} has used Pelican status {status!r}"
                        )
                    if uploader or uploaded_at:
                        raise ProvenanceRenameError(
                            f"row {number} has used Pelican uploader/timestamp"
                        )
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ProvenanceRenameError(f"could not validate CSV: {exc}") from exc

    return FilePlan(path, rows, path.stat().st_size, _sha1(path), legacy)


def discover(root: Path) -> list[Path]:
    return sorted(
        path
        for name in METADATA_NAMES
        for path in Path(root).rglob(name)
        if path.is_file()
    )


def _transformed_copy(source: Path, destination: Path) -> None:
    """Copy one file while replacing only exact ASCII names in its header."""
    with source.open("rb") as incoming, destination.open("wb") as outgoing:
        header = incoming.readline()
        transformed = header
        for old, new in RENAMES.items():
            old_bytes = old.encode("ascii")
            if transformed.count(old_bytes) != 1:
                raise ProvenanceRenameError(
                    f"header does not contain exactly one {old!r} field"
                )
            transformed = transformed.replace(old_bytes, new.encode("ascii"), 1)
        outgoing.write(transformed)
        shutil.copyfileobj(incoming, outgoing, COPY_CHUNK_BYTES)
        outgoing.flush()
        os.fsync(outgoing.fileno())

    _, fields = _header(destination)
    if OSDF_FIELDS.intersection(fields) != OSDF_FIELDS or LEGACY_FIELDS.intersection(
        fields
    ):
        raise ProvenanceRenameError("transformed header failed validation")


def _copy_in_place(source: Path, destination: Path) -> None:
    with source.open("rb") as incoming, destination.open("wb") as outgoing:
        shutil.copyfileobj(incoming, outgoing, COPY_CHUNK_BYTES)
        outgoing.flush()
        os.fsync(outgoing.fileno())


def apply_file(plan: FilePlan, *, in_place: bool) -> AppliedFile | None:
    """Apply one unchanged preflight plan and verify its resulting SHA-1."""
    if not plan.needs_change:
        return None
    if _sha1(plan.path) != plan.source_sha1:
        raise ProvenanceRenameError("file changed after preflight")

    with tempfile.TemporaryDirectory(prefix="cassn-osdf-provenance-") as directory:
        temporary = Path(directory)
        backup = temporary / "original.csv"
        transformed = temporary / "transformed.csv"
        shutil.copy2(plan.path, backup)
        _transformed_copy(backup, transformed)
        expected_sha1 = _sha1(transformed)

        if in_place:
            try:
                _copy_in_place(transformed, plan.path)
            except BaseException:
                _copy_in_place(backup, plan.path)
                raise
        else:
            replacement = plan.path.with_name(f".{plan.path.name}.osdf.tmp")
            try:
                shutil.copy2(transformed, replacement)
                os.replace(replacement, plan.path)
            finally:
                replacement.unlink(missing_ok=True)

    actual_sha1 = _sha1(plan.path)
    if actual_sha1 != expected_sha1:
        raise ProvenanceRenameError("post-write SHA-1 verification failed")
    verified = inspect_file(plan.path)
    if verified.needs_change or verified.rows != plan.rows:
        raise ProvenanceRenameError("post-write schema/row verification failed")
    return AppliedFile(
        str(plan.path),
        plan.rows,
        plan.size,
        plan.path.stat().st_size,
        plan.source_sha1,
        actual_sha1,
    )


def _write_audit(path: Path, root: Path, applied: list[AppliedFile]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "renames": RENAMES,
        "files": [asdict(item) for item in applied],
    }
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help="Root searched recursively for image/audio metadata CSVs",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the migration; default is a read-only preview",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Preserve the existing inode; required when applying on Box Drive",
    )
    parser.add_argument(
        "--expect-files",
        type=int,
        help="Refuse the run unless exactly this many metadata CSVs are found",
    )
    parser.add_argument("--audit-path", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.expanduser()
    if not root.is_dir():
        print(f"ERROR: not a directory: {root}", file=sys.stderr)
        return 2
    if args.apply and "Box-Box" in root.parts and not args.in_place:
        print(
            "ERROR: --in-place is required on Box Drive to retain file IDs/history",
            file=sys.stderr,
        )
        return 2

    paths = discover(root)
    if args.expect_files is not None and len(paths) != args.expect_files:
        print(
            f"ERROR: found {len(paths)} metadata files; expected {args.expect_files}",
            file=sys.stderr,
        )
        return 2
    if not paths:
        print(f"ERROR: no metadata CSVs found under {root}", file=sys.stderr)
        return 2

    plans: list[FilePlan] = []
    errors: list[str] = []
    for index, path in enumerate(paths, start=1):
        try:
            plan = inspect_file(path)
        except ProvenanceRenameError as exc:
            errors.append(f"{path}: {exc}")
        else:
            plans.append(plan)
            state = "rename" if plan.needs_change else "already OSDF"
            print(
                f"[{index}/{len(paths)}] {state}: {path} "
                f"({plan.rows:,} rows, {plan.size:,} bytes)",
                flush=True,
            )
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        print("Nothing was changed.", file=sys.stderr)
        return 2

    changing = [plan for plan in plans if plan.needs_change]
    print(
        f"Preflight passed: {len(plans)} files, {sum(p.rows for p in plans):,} rows; "
        f"{len(changing)} require the header rename."
    )
    if not args.apply:
        print("Dry run only; nothing was changed.")
        return 0

    applied: list[AppliedFile] = []
    for index, plan in enumerate(changing, start=1):
        try:
            result = apply_file(plan, in_place=args.in_place)
        except (OSError, ProvenanceRenameError) as exc:
            print(f"ERROR: {plan.path}: {exc}", file=sys.stderr)
            print("Rerun the same command after resolving the error.", file=sys.stderr)
            return 2
        if result is not None:
            applied.append(result)
        print(f"Verified {index}/{len(changing)}: {plan.path}", flush=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit_path = args.audit_path or (
        AUDIT_DIR / f"osdf_provenance_columns_{stamp}.json"
    )
    _write_audit(audit_path, root, applied)
    print(f"Applied and locally verified: {len(applied)} file(s)")
    print(f"Audit: {audit_path}")
    if "Box-Box" in root.parts:
        print("Box Drive synchronization is separate; verify server SHA-1 values later.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
