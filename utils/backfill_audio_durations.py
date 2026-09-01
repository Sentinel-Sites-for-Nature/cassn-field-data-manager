#!/usr/bin/env python3
"""Backfill missing Box audio durations from small WAV header range reads.

The utility operates directly on Box. It downloads at most the first 1 MiB of
each unique WAV whose confirmed metadata row has a blank
``recording_duration_sec``, derives whole seconds from the RIFF ``fmt`` and
``data`` chunk headers, and changes only that metadata column. Raw media are
never downloaded in full, uploaded, renamed, or modified.

The default is a dry run. Pass ``--apply`` to upload SHA-1-verified new
versions of affected ``audio_file_metadata.csv`` files. Files with malformed
or header-only WAV content remain blank and are listed in the audit.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import struct
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cassn.box.auth import get_box_client, load_box_config  # noqa: E402
from cassn.box.client import BoxStorage  # noqa: E402


TRUE_VALUES = frozenset({"1", "true", "yes", "y"})
HEADER_RANGES = (64 * 1024, 256 * 1024, 1024 * 1024)
AUDIT_DIR = Path("local_data/maintenance_audits")
_PLOT_RE = re.compile(r"_plot(?P<plot>\d+)_", re.IGNORECASE)


@dataclass(frozen=True)
class BoxAudioCsv:
    path: str
    file_id: str
    event_folder_id: str
    payload: bytes


@dataclass(frozen=True)
class DurationTask:
    document_index: int
    row_index: int
    deployment_id: str
    filename: str
    plot_number: str
    device_type: str
    file_size: int | None
    file_hash_sha1: str

    @property
    def cache_key(self) -> str:
        return self.file_hash_sha1 or (
            f"{self.deployment_id}\0{self.filename}\0{self.file_size or ''}"
        )


@dataclass
class AudioDocumentPlan:
    source: BoxAudioCsv
    fieldnames: list[str]
    original_rows: list[dict[str, str]]
    updated_rows: list[dict[str, str]]
    has_bom: bool
    newline: str
    changed_rows: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.changed_rows > 0

    def updated_payload(self) -> bytes:
        return _encode_csv(
            self.fieldnames,
            self.updated_rows,
            has_bom=self.has_bom,
            newline=self.newline,
        )


def wav_duration_seconds_from_prefix(prefix: bytes, *, file_size: int | None = None) -> int | None:
    """Return whole seconds from a RIFF/WAVE prefix, or ``None`` if unresolved."""
    if len(prefix) < 12 or prefix[:4] != b"RIFF" or prefix[8:12] != b"WAVE":
        return None
    avg_bytes_per_sec = 0
    offset = 12
    while offset + 8 <= len(prefix):
        chunk_id = prefix[offset : offset + 4]
        chunk_size = struct.unpack_from("<I", prefix, offset + 4)[0]
        body_start = offset + 8
        if chunk_id == b"fmt ":
            if body_start + min(chunk_size, 12) > len(prefix):
                return None
            if chunk_size >= 12:
                avg_bytes_per_sec = struct.unpack_from("<I", prefix, body_start + 8)[0]
        elif chunk_id == b"data":
            data_size = chunk_size
            if data_size == 0xFFFFFFFF and file_size is not None:
                data_size = max(0, file_size - body_start)
            if avg_bytes_per_sec > 0 and data_size > 0:
                return round(data_size / avg_bytes_per_sec)
            return None
        next_offset = body_start + chunk_size + (chunk_size & 1)
        if next_offset <= offset:
            return None
        offset = next_offset
    return None


def _decode_csv(payload: bytes) -> tuple[list[str], list[dict[str, str]], bool, str]:
    has_bom = payload.startswith(b"\xef\xbb\xbf")
    newline = "\r\n" if b"\r\n" in payload else "\n"
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig"), newline=""))
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


def _int_or_none(value: object) -> int | None:
    try:
        number = int(str(value or "").strip())
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def plan_audio_documents(
    sources: list[BoxAudioCsv],
) -> tuple[list[AudioDocumentPlan], list[DurationTask]]:
    """Parse Box CSVs and identify confirmed audio rows needing durations."""
    plans: list[AudioDocumentPlan] = []
    tasks: list[DurationTask] = []
    required = {
        "deployment_id",
        "filename",
        "plot_number",
        "device_type",
        "file_type",
        "file_size_bytes",
        "is_uploaded_to_box",
    }
    for document_index, source in enumerate(sources):
        try:
            fieldnames, rows, has_bom, newline = _decode_csv(source.payload)
        except Exception as exc:
            plans.append(
                AudioDocumentPlan(source, [], [], [], False, "\n", errors=[str(exc)])
            )
            continue
        missing = sorted(required - set(fieldnames))
        plan = AudioDocumentPlan(
            source,
            fieldnames,
            rows,
            [dict(row) for row in rows],
            has_bom,
            newline,
        )
        if missing:
            plan.errors.append(f"missing column(s): {', '.join(missing)}")
            plans.append(plan)
            continue
        if "recording_duration_sec" not in plan.fieldnames:
            if "duration" not in plan.fieldnames:
                plan.errors.append(
                    "missing recording_duration_sec and no duration column for placement"
                )
                plans.append(plan)
                continue
            insert_at = plan.fieldnames.index("duration") + 1
            plan.fieldnames.insert(insert_at, "recording_duration_sec")
            for row in plan.updated_rows:
                row["recording_duration_sec"] = ""
        for row_index, row in enumerate(rows):
            if str(row.get("file_type") or "").strip().lower() != "audio":
                continue
            if str(row.get("is_uploaded_to_box") or "").strip().lower() not in TRUE_VALUES:
                continue
            if str(row.get("recording_duration_sec") or "").strip():
                continue
            deployment_id = str(row.get("deployment_id") or "").strip()
            filename = str(row.get("filename") or "").strip()
            device_type = str(row.get("device_type") or "").strip().upper()
            plot_number = str(row.get("plot_number") or "").strip()
            if not plot_number:
                match = _PLOT_RE.search(deployment_id)
                plot_number = match.group("plot") if match else ""
            if not deployment_id or not filename or not plot_number or device_type not in {"BD", "BT"}:
                plan.errors.append(
                    f"row {row_index + 2}: cannot resolve deployment, filename, plot, or BD/BT device"
                )
                continue
            tasks.append(
                DurationTask(
                    document_index,
                    row_index,
                    deployment_id,
                    filename,
                    plot_number,
                    device_type,
                    _int_or_none(row.get("file_size_bytes")),
                    str(row.get("file_hash_sha1") or "").strip().lower(),
                )
            )
        plans.append(plan)
    return plans, tasks


def apply_resolved_durations(
    plans: list[AudioDocumentPlan],
    tasks: list[DurationTask],
    durations: dict[str, int],
) -> None:
    """Apply resolved duration values to in-memory CSV plans only."""
    for task in tasks:
        seconds = durations.get(task.cache_key)
        if seconds is None:
            continue
        row = plans[task.document_index].updated_rows[task.row_index]
        if str(row.get("recording_duration_sec") or "").strip():
            continue
        row["recording_duration_sec"] = str(seconds)
        plans[task.document_index].changed_rows += 1


def _download(client, file_id: str, *, byte_range: str | None = None) -> bytes:
    kwargs = {"range": byte_range} if byte_range else {}
    stream = client.downloads.download_file(file_id, **kwargs)
    if stream is None:
        raise RuntimeError(f"Box returned no content for file {file_id}")
    return stream.read()


def read_box_wav_duration(client, file_id: str, *, file_size: int | None) -> int | None:
    for length in HEADER_RANGES:
        prefix = _download(client, file_id, byte_range=f"bytes=0-{length - 1}")
        duration = wav_duration_seconds_from_prefix(prefix, file_size=file_size)
        if duration is not None:
            return duration
        if len(prefix) < length:
            break
    return None


def _box_audio_csvs(storage: BoxStorage, root_id: str, year: int) -> list[BoxAudioCsv]:
    year_id = storage.find_child_folder(root_id, str(year))
    if year_id is None:
        raise RuntimeError(f"Box data folder has no {year} child")
    documents: list[BoxAudioCsv] = []
    for reserve in storage.iter_folder_items(year_id):
        if reserve.type != "folder":
            continue
        for event in storage.iter_folder_items(reserve.id):
            if event.type != "folder":
                continue
            file_id = storage.folder_file_map(event.id).get("audio_file_metadata.csv")
            if not file_id:
                continue
            path = f"{year}/{reserve.name}/{event.name}/audio_file_metadata.csv"
            documents.append(
                BoxAudioCsv(path, file_id, event.id, _download(storage.client, file_id))
            )
    return documents


def _resolve_media_file_id(
    storage: BoxStorage,
    source: BoxAudioCsv,
    task: DurationTask,
    folder_cache: dict[tuple[str, str], str | None],
) -> str | None:
    raw_key = (source.event_folder_id, "raw_data")
    if raw_key not in folder_cache:
        folder_cache[raw_key] = storage.find_child_folder(source.event_folder_id, "raw_data")
    raw_id = folder_cache[raw_key]
    if raw_id is None:
        return None
    folder_name = f"p{task.plot_number}_{task.device_type}"
    device_key = (raw_id, folder_name)
    if device_key not in folder_cache:
        folder_cache[device_key] = storage.find_child_folder(raw_id, folder_name)
    device_id = folder_cache[device_key]
    if device_id is None:
        return None
    return storage.folder_file_map(device_id).get(task.filename)


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
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--audit-path", type=Path)
    parser.add_argument(
        "--workers",
        type=int,
        default=6,
        help="Concurrent Box header reads (default: 6; range 1-12)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.workers <= 12:
        print("ERROR: --workers must be between 1 and 12", file=sys.stderr)
        return 2
    box_config = load_box_config()
    client = get_box_client(box_config)
    if client is None:
        print("ERROR: Box authentication is unavailable", file=sys.stderr)
        return 2
    storage = BoxStorage(client)
    try:
        sources = _box_audio_csvs(storage, str(box_config.field_data_folder_id), args.year)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    plans, tasks = plan_audio_documents(sources)
    errors = [f"{plan.source.path}: {error}" for plan in plans for error in plan.errors]
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if errors:
        return 2

    durations: dict[str, int] = {}
    unresolved: list[dict] = []
    folder_cache: dict[tuple[str, str], str | None] = {}
    task_groups: dict[str, list[DurationTask]] = {}
    for task in tasks:
        task_groups.setdefault(task.cache_key, []).append(task)

    resolved_files: dict[str, tuple[DurationTask, str]] = {}
    for index, (cache_key, group) in enumerate(task_groups.items(), start=1):
        for task in group:
            source = sources[task.document_index]
            file_id = _resolve_media_file_id(storage, source, task, folder_cache)
            if file_id is not None:
                resolved_files[cache_key] = (task, file_id)
                break
        if cache_key not in resolved_files:
            task = group[0]
            source = sources[task.document_index]
            unresolved.append(
                {
                    "path": source.path,
                    "deployment_id": task.deployment_id,
                    "filename": task.filename,
                    "reason": "media file not found in expected Box device folder",
                }
            )
        if index % 250 == 0 or index == len(task_groups):
            print(
                f"Path resolution: {index}/{len(task_groups)} unique recordings",
                flush=True,
            )

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                read_box_wav_duration,
                client,
                file_id,
                file_size=task.file_size,
            ): (cache_key, task)
            for cache_key, (task, file_id) in resolved_files.items()
        }
        for index, future in enumerate(as_completed(futures), start=1):
            cache_key, task = futures[future]
            source = sources[task.document_index]
            try:
                duration = future.result()
            except Exception as exc:
                unresolved.append(
                    {
                        "path": source.path,
                        "deployment_id": task.deployment_id,
                        "filename": task.filename,
                        "reason": f"header read failed: {exc}",
                    }
                )
            else:
                if duration is None:
                    unresolved.append(
                        {
                            "path": source.path,
                            "deployment_id": task.deployment_id,
                            "filename": task.filename,
                            "reason": "RIFF header did not provide a positive duration",
                        }
                    )
                else:
                    durations[cache_key] = duration
            if index % 100 == 0 or index == len(futures):
                print(
                    f"Header progress: {index}/{len(futures)} unique recordings; "
                    f"{len(durations)} durations resolved",
                    flush=True,
                )

    apply_resolved_durations(plans, tasks, durations)
    changed = [plan for plan in plans if plan.changed]
    changed_rows = sum(plan.changed_rows for plan in changed)
    unresolved_keys = {
        (item["deployment_id"], item["filename"], item["reason"]) for item in unresolved
    }
    print(f"Mode: {'apply' if args.apply else 'dry-run'}")
    print(f"Audio metadata files scanned: {len(plans)}")
    print(f"Blank confirmed audio rows examined: {len(tasks):,}")
    print(f"Rows with recovered duration: {changed_rows:,}")
    print(f"Files requiring a new Box version: {len(changed)}")
    print(f"Unique unresolved recordings: {len(unresolved_keys):,}")
    for item in unresolved:
        print(
            "UNRESOLVED: "
            f"{item['deployment_id']}/{item['filename']} — {item['reason']}"
        )
    if not args.apply:
        print("Dry run only; Box was not changed.")
        return 0

    from box_sdk_gen import UploadFileVersionAttributes

    uploaded: list[dict] = []
    for index, plan in enumerate(changed, start=1):
        payload = plan.updated_payload()
        with io.BytesIO(payload) as stream:
            storage.client.uploads.upload_file_version(
                plan.source.file_id,
                attributes=UploadFileVersionAttributes(name="audio_file_metadata.csv"),
                file=stream,
            )
        downloaded = _download(storage.client, plan.source.file_id)
        expected_sha1 = hashlib.sha1(payload).hexdigest()
        actual_sha1 = hashlib.sha1(downloaded).hexdigest()
        if actual_sha1 != expected_sha1:
            print(f"ERROR: Box verification failed for {plan.source.path}", file=sys.stderr)
            return 2
        uploaded.append(
            {
                "path": plan.source.path,
                "file_id": plan.source.file_id,
                "changed_rows": plan.changed_rows,
                "sha1": actual_sha1,
            }
        )
        print(f"Verified {index}/{len(changed)}: {plan.source.path}", flush=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit_path = args.audit_path or AUDIT_DIR / f"audio_durations_{args.year}_{stamp}.json"
    _write_audit(
        audit_path,
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "year": args.year,
            "method": "Box HTTP range reads of RIFF fmt/data headers; whole seconds rounded",
            "header_read_limit_bytes": max(HEADER_RANGES),
            "rows_examined": len(tasks),
            "rows_updated": changed_rows,
            "changed_files": len(uploaded),
            "files": uploaded,
            "unresolved": unresolved,
            "raw_media_changed": False,
        },
    )
    print(f"Audit: {audit_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
