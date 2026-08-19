"""Qt-free Box/local path and SHA-1 reconciliation."""

from __future__ import annotations

import time
from pathlib import Path, PurePosixPath

from cassn.box.client import BoxStorage
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


def is_orphan_on_box(path: str) -> bool:
    """True for an unexpected Box-only path rather than an allowed sidecar."""
    return (
        path not in _EXPECTED_METADATA
        and not path.startswith("qc/lookup_snapshot/")
        and not path.startswith("soundhub/")
        and not PurePosixPath(path).name.startswith("wildlife_insights_")
        and not PurePosixPath(path).name.endswith("_manifest.json")
    )


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
        box_hashes.clear()
        box_paths.clear()

        def _walk(folder_id, prefix: tuple):
            if _cancelled():
                return
            for item in storage.iter_folder_items(
                folder_id, fields=["id", "type", "name", "sha1"]
            ):
                if item.type == "file":
                    box_sha1 = (
                        getattr(item, "sha_1", None)
                        or getattr(item, "sha1", None)
                        or ""
                    )
                    relative_path = PurePosixPath(*prefix, item.name).as_posix()
                    box_hashes[relative_path] = box_sha1.lower()
                    box_paths.add(relative_path)
                elif item.type == "folder":
                    _walk(item.id, (*prefix, item.name))

        _walk(deploy_id, ())

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
    if detect_orphans:
        local_paths = set(inventory_entry_by_path)
        orphans = sorted(
            path for path in (box_paths - local_paths) if is_orphan_on_box(path)
        )

    all_ok = not mismatches and not missing_from_box and not box_hash_unavailable and not orphans
    summary = (
        f"End-to-end hash verification: {checked} local file(s) checked against Box. "
        f"{len(mismatches)} hash mismatch(es), {len(missing_from_box)} file(s) "
        f"missing from Box, {len(box_hash_unavailable)} Box hash(es) unavailable."
    )
    if detect_orphans:
        summary += f" {len(orphans)} unexpected file(s) on Box."
    issues = (
        [{"type": "mismatch", "filename": item["filename"]} for item in mismatches]
        + [{"type": "missing", "filename": path} for path in missing_from_box]
        + [
            {"type": "box_hash_unavailable", "filename": path}
            for path in box_hash_unavailable
        ]
        + [{"type": "extra_on_box", "filename": path} for path in orphans]
    )
    return all_ok, summary, issues
