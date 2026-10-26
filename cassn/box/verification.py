"""Qt-free Box/local path and SHA-1 reconciliation."""

from __future__ import annotations

import time
import re
from collections import defaultdict
from pathlib import Path, PurePosixPath

from cassn.box.client import BoxStorage
from cassn.box.metadata_merge import CSV_NAMES, read_csv
from cassn.box.metadata_store import BoxFileStore
from cassn.core.hashing import sha1 as compute_file_sha1
from cassn.core.inventory import index_inventory_by_storage_relpath

# Files that legitimately live on Box but are not in the local file inventory.
_EXPECTED_METADATA = {
    "image_file_metadata.csv",
    "audio_file_metadata.csv",
    "deployment_event_record.json",
    "qc/qc_report.json",
    "qc/box_upload_manifest.json",
    "qc/box_upload_verification.json",
    "qc/deployment_summary.txt",
}


def collect_box_file_hashes(
    storage: BoxStorage,
    folder_id: str,
    *,
    is_cancelled=None,
) -> dict[str, str]:
    """Return every file below ``folder_id`` as ``relative_path -> SHA-1``.

    Box computes the hashes server-side.  Callers therefore get a byte-level
    inventory without downloading file contents.  Blank values are retained so
    a verifier can fail closed while Box is still calculating a fresh hash.
    """
    hashes: dict[str, str] = {}

    def _walk(current_id: str, prefix: tuple[str, ...]) -> None:
        if is_cancelled and is_cancelled():
            return
        for item in storage.iter_folder_items(
            current_id, fields=["id", "type", "name", "sha1"]
        ):
            if is_cancelled and is_cancelled():
                return
            if item.type == "file":
                digest = (
                    getattr(item, "sha_1", None)
                    or getattr(item, "sha1", None)
                    or ""
                )
                relative_path = PurePosixPath(*prefix, item.name).as_posix()
                hashes[relative_path] = str(digest).lower()
            elif item.type == "folder":
                _walk(item.id, (*prefix, item.name))

    _walk(folder_id, ())
    return hashes


def is_orphan_on_box(path: str) -> bool:
    """True for an unexpected Box-only path rather than an allowed sidecar."""
    return (
        path not in _EXPECTED_METADATA
        and not path.startswith("qc/lookup_snapshot/")
        and not path.startswith("qc/box_batches/")
        and not path.startswith("soundhub/")
        and not PurePosixPath(path).name.startswith("wildlife_insights_")
        and not PurePosixPath(path).name.endswith("_manifest.json")
    )


def recorded_archive_paths(storage, folder_id, box_hashes) -> tuple[set[str], list]:
    """A remote row accounts for a raw file only with unique path + matching SHA-1.

    This does not add Box-only records to the local ingest session. Unavailable
    legacy evidence is reported rather than treating filename presence as proof.
    """
    if getattr(storage, "client", None) is None:
        return set(), []  # Pure listing-only callers have no metadata evidence.
    store = BoxFileStore(storage.client)
    items = list(storage.iter_folder_items(folder_id))
    paths_by_name = defaultdict(list)
    for path in box_hashes:
        if path.startswith("raw_data/"):
            paths_by_name[PurePosixPath(path).name].append(path)
    known, issues, names = set(), [], set()
    for name in CSV_NAMES:
        matches = [item.id for item in items if item.type == "file" and item.name == name]
        if len(matches) > 1:
            raise ValueError(f"Multiple metadata files named {name}")
        if not matches:
            continue
        ref = store.snapshot(matches[0])
        _, rows = read_csv(store.read(ref))
        for row in rows:
            filename = row["filename"]
            if filename in names:
                raise ValueError(f"Ambiguous archived filename: {filename}")
            names.add(filename)
            explicit = row.get("storage_relpath") or row.get("relative_path")
            paths = [path for path in paths_by_name[filename] if not explicit or path == explicit]
            digest = row.get("file_hash_sha1", "").lower()
            if len(paths) == 1 and re.fullmatch(r"[0-9a-f]{40}", digest) and box_hashes[paths[0]] == digest:
                known.add(paths[0])
            else:
                issues.append({"type": "archive_metadata_unverified", "filename": filename})
        store.assert_unchanged(ref)
    return known, issues


def verify_box_hashes(
    storage: BoxStorage,
    deploy_id: str,
    deploy_name: str,
    file_inventory: list,
    deployment_folder: Path,
    *,
    detect_orphans: bool = False,
    hash_retry: int = 0,
    hash_retry_delay: float = 2.5,
    progress=None,
    is_cancelled=None,
) -> tuple[bool, str, list]:
    """Compare local raw-data bytes against Box using complete relative paths.

    ``deploy_name`` remains in the public signature for compatibility with the
    thread callers; paths are relative to its Box folder and therefore do not
    include the deployment name itself.
    """
    del deploy_name

    def _cancelled() -> bool:
        return bool(is_cancelled and is_cancelled())

    def _emit(checked, total, filename):
        if progress:
            progress(checked, total, filename)

    box_hashes: dict[str, str] = {}
    box_paths: set[str] = set()
    _emit(0, 0, "Listing Box folder contents…")

    def _collect():
        if _cancelled():
            return
        refreshed = collect_box_file_hashes(
            storage, deploy_id, is_cancelled=_cancelled
        )
        box_hashes.clear()
        box_hashes.update(refreshed)
        box_paths.clear()
        box_paths.update(refreshed)

    _collect()
    if _cancelled():
        return False, "Verification cancelled.", []

    raw_data_dir = deployment_folder / "raw_data"
    if not raw_data_dir.is_dir():
        return False, "raw_data/ directory not found.", []

    files_to_check = [
        path
        for path in raw_data_dir.rglob("*")
        if path.is_file() and BoxStorage.should_upload_file(path)
    ]
    inventory_entry_by_path = index_inventory_by_storage_relpath(file_inventory)
    # Inventory members missing locally must never disappear from the check.
    present_paths = {p.relative_to(deployment_folder).as_posix() for p in files_to_check}
    missing_local = sorted(set(inventory_entry_by_path) - present_paths)
    total = len(files_to_check)
    mismatches = []
    missing_from_box = []
    box_hash_unavailable = []
    checked = 0

    for local_path in files_to_check:
        if _cancelled():
            return False, f"Verification cancelled after {checked}/{total} files.", []
        relative_path = local_path.relative_to(deployment_folder).as_posix()
        _emit(checked, total, relative_path)
        box_sha1 = box_hashes.get(relative_path)
        if box_sha1 is None:
            missing_from_box.append(relative_path)
        elif not box_sha1:
            box_hash_unavailable.append(relative_path)
        else:
            entry = inventory_entry_by_path.get(relative_path)
            local_sha1 = entry.get("file_hash_sha1", "") if entry else ""
            if not local_sha1:
                local_sha1 = compute_file_sha1(local_path)
                if entry is not None:
                    entry["file_hash_sha1"] = local_sha1
            if local_sha1.lower() != box_sha1:
                mismatches.append({
                    "filename": relative_path,
                    "local_sha1": local_sha1,
                    "box_sha1": box_sha1,
                })
        checked += 1

    attempt = 0
    while box_hash_unavailable and attempt < hash_retry and not _cancelled():
        attempt += 1
        time.sleep(hash_retry_delay)
        _emit(
            checked,
            total,
            f"Re-checking {len(box_hash_unavailable)} Box hash(es) (retry {attempt})…",
        )
        _collect()
        still_unavailable = []
        for relative_path in box_hash_unavailable:
            box_sha1 = box_hashes.get(relative_path)
            if not box_sha1:
                still_unavailable.append(relative_path)
                continue
            entry = inventory_entry_by_path.get(relative_path)
            local_sha1 = entry.get("file_hash_sha1", "") if entry else ""
            if not local_sha1:
                local_path = deployment_folder.joinpath(
                    *PurePosixPath(relative_path).parts
                )
                local_sha1 = compute_file_sha1(local_path) if local_path.is_file() else ""
                if entry is not None and local_sha1:
                    entry["file_hash_sha1"] = local_sha1
            if local_sha1 and local_sha1.lower() != box_sha1:
                mismatches.append({
                    "filename": relative_path,
                    "local_sha1": local_sha1,
                    "box_sha1": box_sha1,
                })
        box_hash_unavailable = still_unavailable

    _emit(checked, total, "")

    orphans: list[str] = []
    archive_issues = []
    known_archive = set()
    if detect_orphans:
        known_archive, archive_issues = recorded_archive_paths(storage, deploy_id, box_hashes)
        local_paths = set(inventory_entry_by_path)
        orphans = sorted(
            path for path in (box_paths - local_paths - known_archive) if is_orphan_on_box(path)
        )

    all_ok = not (mismatches or missing_from_box or box_hash_unavailable or orphans or missing_local or archive_issues)
    summary = (
        f"End-to-end hash verification: {checked} local file(s) checked against Box. "
        f"{len(mismatches)} hash mismatch(es), {len(missing_from_box)} file(s) "
        f"missing from Box, {len(box_hash_unavailable)} Box hash(es) unavailable."
    )
    if detect_orphans:
        summary += (f" {len(orphans)} unexpected file(s) on Box. "
                    f"Archive metadata: {len(known_archive)} recorded raw path(s) matched; "
                    f"{len(archive_issues)} unverified row(s).")
    if missing_local:
        summary += f" {len(missing_local)} inventoried file(s) missing locally."
    issues = (
        [{"type": "mismatch", "filename": item["filename"]} for item in mismatches]
        + [{"type": "missing", "filename": path} for path in missing_from_box]
        + [
            {"type": "box_hash_unavailable", "filename": path}
            for path in box_hash_unavailable
        ]
        + [{"type": "extra_on_box", "filename": path} for path in orphans]
        + [{"type": "missing_local", "filename": path} for path in missing_local]
        + archive_issues
    )
    return all_ok, summary, issues
