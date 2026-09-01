#!/usr/bin/env python3
"""Backfill Wildlife Insights submission provenance from the WI tracker.

The utility operates directly on Box metadata and never reads image bytes. It
matches tracker rows by ``deployment_id`` and changes only
``is_submitted_to_wi`` from a false/blank value to ``True`` when the tracker
status begins ``WI -``. ``Box`` tracker statuses never clear an existing true
value. Unknown submission dates and submitters remain blank rather than being
invented.

The default is a dry run. Pass ``--apply`` to upload verified new versions of
the affected ``image_file_metadata.csv`` files.
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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cassn.box.auth import get_box_client, load_box_config  # noqa: E402
from cassn.box.client import BoxStorage  # noqa: E402
from cassn.reporting.data_collection_summary import (  # noqa: E402
    WISubmissionTracker,
    load_wi_submission_tracker,
)


TRUE_VALUES = frozenset({"1", "true", "yes", "y"})
AUDIT_DIR = Path("local_data/maintenance_audits")


@dataclass(frozen=True)
class BoxCsv:
    path: str
    file_id: str
    payload: bytes


@dataclass
class CsvPlan:
    source: BoxCsv
    updated_payload: bytes
    changed_rows: int = 0
    changed_deployments: set[str] = field(default_factory=set)
    tracker_box_metadata_true: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.updated_payload != self.source.payload


def _decode_csv(payload: bytes) -> tuple[list[str], list[dict[str, str]], bool, str]:
    has_bom = payload.startswith(b"\xef\xbb\xbf")
    newline = "\r\n" if b"\r\n" in payload else "\n"
    text = payload.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None:
        raise ValueError("CSV has no header")
    rows = list(reader)
    if any(None in row for row in rows):
        raise ValueError("one or more rows contain more values than the header")
    return list(reader.fieldnames), rows, has_bom, newline


def _encode_csv(
    fieldnames: list[str], rows: list[dict[str, str]], *, has_bom: bool, newline: str
) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator=newline)
    writer.writeheader()
    writer.writerows(rows)
    payload = stream.getvalue().encode("utf-8")
    return (b"\xef\xbb\xbf" + payload) if has_bom else payload


def plan_wi_csv(source: BoxCsv, tracker: WISubmissionTracker) -> CsvPlan:
    """Return an in-memory, field-limited update plan for one image CSV."""
    try:
        fieldnames, rows, has_bom, newline = _decode_csv(source.payload)
    except Exception as exc:
        return CsvPlan(source, source.payload, errors=[f"{source.path}: {exc}"])
    required = {"deployment_id", "file_type", "is_submitted_to_wi"}
    missing = sorted(required - set(fieldnames))
    if missing:
        return CsvPlan(
            source,
            source.payload,
            errors=[f"{source.path}: missing column(s): {', '.join(missing)}"],
        )

    changed_rows = 0
    changed_deployments: set[str] = set()
    tracker_box_metadata_true: set[str] = set()
    updated_rows: list[dict[str, str]] = []
    for original in rows:
        row = dict(original)
        if str(row.get("file_type") or "").strip().lower() != "image":
            updated_rows.append(row)
            continue
        deployment_id = str(row.get("deployment_id") or "").strip()
        tracker_state = tracker.submitted(deployment_id)
        metadata_true = (
            str(row.get("is_submitted_to_wi") or "").strip().lower() in TRUE_VALUES
        )
        if tracker_state is True and not metadata_true:
            row["is_submitted_to_wi"] = "True"
            changed_rows += 1
            changed_deployments.add(deployment_id)
        elif tracker_state is False and metadata_true:
            tracker_box_metadata_true.add(deployment_id)
        updated_rows.append(row)

    updated = _encode_csv(fieldnames, updated_rows, has_bom=has_bom, newline=newline)
    return CsvPlan(
        source,
        updated,
        changed_rows=changed_rows,
        changed_deployments=changed_deployments,
        tracker_box_metadata_true=tracker_box_metadata_true,
    )


def _download(client, file_id: str) -> bytes:
    stream = client.downloads.download_file(file_id)
    if stream is None:
        raise RuntimeError(f"Box returned no content for file {file_id}")
    return stream.read()


def _box_image_csvs(storage: BoxStorage, root_id: str, year: int) -> list[BoxCsv]:
    year_id = storage.find_child_folder(root_id, str(year))
    if year_id is None:
        raise RuntimeError(f"Box data folder has no {year} child")
    documents: list[BoxCsv] = []
    for reserve in storage.iter_folder_items(year_id):
        if reserve.type != "folder":
            continue
        for event in storage.iter_folder_items(reserve.id):
            if event.type != "folder":
                continue
            file_id = storage.folder_file_map(event.id).get("image_file_metadata.csv")
            if not file_id:
                continue
            label = f"{year}/{reserve.name}/{event.name}/image_file_metadata.csv"
            documents.append(BoxCsv(label, file_id, _download(storage.client, file_id)))
    return documents


def _write_audit(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--wi-tracker", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--audit-path", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    tracker = load_wi_submission_tracker(args.wi_tracker)
    box_config = load_box_config()
    client = get_box_client(box_config)
    if client is None:
        print("ERROR: Box authentication is unavailable", file=sys.stderr)
        return 2
    storage = BoxStorage(client)
    try:
        documents = _box_image_csvs(storage, str(box_config.field_data_folder_id), args.year)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    plans = [plan_wi_csv(document, tracker) for document in documents]
    errors = [error for plan in plans for error in plan.errors]
    changed = [plan for plan in plans if plan.changed]
    changed_rows = sum(plan.changed_rows for plan in changed)
    deployments = set().union(*(plan.changed_deployments for plan in plans))
    conflicts = set().union(*(plan.tracker_box_metadata_true for plan in plans))

    print(f"Mode: {'apply' if args.apply else 'dry-run'}")
    print(f"Image metadata files scanned: {len(plans)}")
    print(f"Files requiring a new Box version: {len(changed)}")
    print(f"Rows to stamp is_submitted_to_wi=True: {changed_rows:,}")
    print(f"Deployments to stamp: {len(deployments):,}")
    print(f"Tracker Box / metadata true conflicts preserved: {len(conflicts):,}")
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if errors:
        return 2
    if not args.apply:
        print("Dry run only; Box was not changed.")
        return 0

    from box_sdk_gen import UploadFileVersionAttributes

    uploaded: list[dict] = []
    for index, plan in enumerate(changed, start=1):
        with io.BytesIO(plan.updated_payload) as stream:
            storage.client.uploads.upload_file_version(
                plan.source.file_id,
                attributes=UploadFileVersionAttributes(name="image_file_metadata.csv"),
                file=stream,
            )
        downloaded = _download(storage.client, plan.source.file_id)
        expected_sha1 = hashlib.sha1(plan.updated_payload).hexdigest()
        actual_sha1 = hashlib.sha1(downloaded).hexdigest()
        if actual_sha1 != expected_sha1:
            print(f"ERROR: Box verification failed for {plan.source.path}", file=sys.stderr)
            return 2
        uploaded.append(
            {
                "path": plan.source.path,
                "file_id": plan.source.file_id,
                "changed_rows": plan.changed_rows,
                "deployments": sorted(plan.changed_deployments),
                "sha1": actual_sha1,
            }
        )
        print(f"Verified {index}/{len(changed)}: {plan.source.path}", flush=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit_path = args.audit_path or AUDIT_DIR / f"wi_provenance_{args.year}_{stamp}.json"
    _write_audit(
        audit_path,
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "year": args.year,
            "tracker": str(args.wi_tracker.expanduser().resolve()),
            "tracker_sha256": hashlib.sha256(args.wi_tracker.read_bytes()).hexdigest(),
            "changed_files": len(uploaded),
            "changed_rows": changed_rows,
            "changed_deployments": sorted(deployments),
            "files": uploaded,
            "unchanged_fields": ["wi_submitter", "wi_submission_datetime"],
        },
    )
    print(f"Audit: {audit_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
