"""
Qt background threads that drive Box uploads and reconciliation.

These are the only Box-aware classes that touch Qt. Each one is a thin
orchestrator: it authenticates, walks/creates the deployment's folder path,
then delegates the repetitive folder/upload primitives to
:class:`cassn.box.client.BoxStorage`. The thread's job is the *workflow* —
manifest writing, parallel scheduling, cancellation, progress signals, and QC
reporting — not the Box API mechanics.

Dependencies are injected rather than read from module globals:

* ``box_config`` (:class:`cassn.box.auth.BoxConfig`) carries the credentials
  and the field-data root folder id;
* ``valid_reserve_names`` is the canonical set of reserve names (from the
  loaded lookup tables) used to validate the destination path.

This keeps the threads testable and free of the import-time config/CSV reads
the original relied on.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from cassn.box.auth import BoxConfig, get_box_client
from cassn.box.client import BoxStorage
from cassn.core.classification import sanitize_box_folder_name
from cassn.core.hashing import sha1 as compute_file_sha1
from cassn.core.quality_control import append_qc_report, qc_path_for

# Box API field projection for the verify/fixity walks: enough to navigate and
# to read each file's server-side SHA-1 without pulling full item payloads.
_SHA1_FIELDS = ["id", "type", "name", "sha1"]

# Files that legitimately live on Box but are not in the local file_inventory
# (metadata/QC sidecars). Used to exclude them from orphan ("extra on Box")
# detection so they aren't flagged as unexpected.
_EXPECTED_METADATA = {
    "image_file_metadata.csv",
    "audio_file_metadata.csv",
    "deployment_event_record.json",
    "qc/qc_report.json",
    "qc/box_upload_manifest.json",
    "qc/box_upload_verification.json",
    "qc/deployment_summary.txt",
}


def _is_orphan_on_box(path: str) -> bool:
    """True if a Box-only path is an unexpected file (not an allowed sidecar)."""
    return (
        path not in _EXPECTED_METADATA
        and not path.startswith("qc/lookup_snapshot/")
        and not Path(path).name.startswith("wildlife_insights_")
        and not Path(path).name.endswith("_manifest.json")
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
    """Compare local raw_data bytes against Box via SHA-1, the shared core of the
    manual fixity check and the automatic post-upload verification.

    Recursively reads every Box file's server-side SHA-1, walks local
    ``raw_data/``, and reports hash mismatches, files missing from Box, and Box
    files whose SHA-1 the API has not yet computed. When ``detect_orphans`` is
    set it also flags Box-only files that are not in the local inventory (after
    the metadata-sidecar allowlist).

    Box can lag computing a file's SHA-1 right after upload; ``hash_retry`` (with
    ``hash_retry_delay`` seconds between tries) re-fetches the Box listing for the
    still-unavailable files before reporting them as unavailable.

    ``progress`` (``checked, total, filename``) and ``is_cancelled`` (``-> bool``)
    are optional callbacks so a QThread can drive a progress bar and cancel.

    Returns ``(ok, summary, issues)`` matching the FixityCheckThread contract.
    """

    def _cancelled() -> bool:
        return bool(is_cancelled and is_cancelled())

    def _emit(checked, total, filename):
        if progress:
            progress(checked, total, filename)

    # 1) Recursively collect every Box file's SHA-1, keyed by (parent_folder, filename).
    # This mirrors the local layout raw_data/<device_label>/<filename>.
    box_hashes: dict[tuple[str, str], str] = {}
    box_paths: set[str] = set()  # relative paths, for orphan detection
    _emit(0, 0, "Listing Box folder contents…")

    def _collect():
        box_hashes.clear()
        box_paths.clear()

        def _walk(folder_id, parent_name, prefix: tuple):
            if _cancelled():
                return
            for it in storage.iter_folder_items(folder_id, fields=_SHA1_FIELDS):
                if it.type == "file":
                    sha1 = getattr(it, "sha_1", None) or getattr(it, "sha1", None) or ""
                    box_hashes[(parent_name, it.name)] = sha1.lower()
                    box_paths.add(str(Path(*prefix, it.name)) if prefix else it.name)
                elif it.type == "folder":
                    _walk(it.id, it.name, (*prefix, it.name))

        _walk(deploy_id, deploy_name, ())

    _collect()
    if _cancelled():
        return False, "Verification cancelled.", []

    # 2) Walk local raw_data/ and verify each file's bytes against Box.
    raw_data_dir = deployment_folder / "raw_data"
    if not raw_data_dir.is_dir():
        return False, "raw_data/ directory not found.", []

    files_to_check = [
        f for f in raw_data_dir.rglob("*") if f.is_file() and BoxStorage.should_upload_file(f)
    ]
    inventory_sha1_by_key = {
        (e.get("device_label", ""), e.get("new_filename", "")): e.get("file_hash_sha1", "")
        for e in file_inventory
        if e.get("device_label") and e.get("new_filename")
    }
    inventory_entry_by_key = {
        (e.get("device_label", ""), e.get("new_filename", "")): e
        for e in file_inventory
        if e.get("device_label") and e.get("new_filename")
    }
    total = len(files_to_check)
    mismatches = []  # local SHA-1 != Box SHA-1
    missing_from_box = []  # local file has no counterpart on Box
    box_hash_unavailable = []  # Box file found, but API has not returned a SHA-1
    checked = 0

    for fp in files_to_check:
        if _cancelled():
            return False, f"Verification cancelled after {checked}/{total} files.", []
        _emit(checked, total, fp.name)
        key = (fp.parent.name, fp.name)
        box_sha1 = box_hashes.get(key)
        if box_sha1 is None:
            missing_from_box.append(f"{fp.parent.name}/{fp.name}")
        elif not box_sha1:
            box_hash_unavailable.append(f"{fp.parent.name}/{fp.name}")
        else:
            local_sha1 = inventory_sha1_by_key.get(key)
            if not local_sha1:
                local_sha1 = compute_file_sha1(fp)
                entry = inventory_entry_by_key.get(key)
                if entry is not None:
                    entry["file_hash_sha1"] = local_sha1
            if local_sha1.lower() != box_sha1:
                mismatches.append(
                    {
                        "filename": f"{fp.parent.name}/{fp.name}",
                        "local_sha1": local_sha1,
                        "box_sha1": box_sha1,
                    }
                )
        checked += 1

    # 3) Retry on box_hash_unavailable: Box may still be computing a freshly
    # uploaded file's SHA-1. Re-fetch the listing after a short delay and
    # re-resolve only the still-unavailable files before reporting them.
    attempt = 0
    while box_hash_unavailable and attempt < hash_retry and not _cancelled():
        attempt += 1
        time.sleep(hash_retry_delay)
        _emit(checked, total, f"Re-checking {len(box_hash_unavailable)} Box hash(es) (retry {attempt})…")
        _collect()
        still_unavailable = []
        for rel in box_hash_unavailable:
            parent_name, name = rel.split("/", 1)
            key = (parent_name, name)
            box_sha1 = box_hashes.get(key)
            if not box_sha1:
                still_unavailable.append(rel)
                continue
            local_sha1 = inventory_sha1_by_key.get(key)
            if not local_sha1:
                fp = raw_data_dir / parent_name / name
                local_sha1 = compute_file_sha1(fp) if fp.is_file() else ""
                entry = inventory_entry_by_key.get(key)
                if entry is not None and local_sha1:
                    entry["file_hash_sha1"] = local_sha1
            if local_sha1 and local_sha1.lower() != box_sha1:
                mismatches.append(
                    {"filename": rel, "local_sha1": local_sha1, "box_sha1": box_sha1}
                )
        box_hash_unavailable = still_unavailable

    _emit(checked, total, "")

    # 4) Optional orphan detection (extra-on-Box files not in local inventory).
    orphans: list[str] = []
    if detect_orphans:
        local_paths = {
            str(Path("raw_data", e["device_label"], e["new_filename"]))
            for e in file_inventory
            if e.get("device_label") and e.get("new_filename")
        }
        orphans = sorted(p for p in (box_paths - local_paths) if _is_orphan_on_box(p))

    # 5) Compose result.
    all_ok = not mismatches and not missing_from_box and not box_hash_unavailable and not orphans
    summary = (
        f"End-to-end hash verification: {checked} local file(s) checked against Box. "
        f"{len(mismatches)} hash mismatch(es), {len(missing_from_box)} file(s) missing from Box, "
        f"{len(box_hash_unavailable)} Box hash(es) unavailable."
    )
    if detect_orphans:
        summary += f" {len(orphans)} unexpected file(s) on Box."
    issues = (
        [{"type": "mismatch", "filename": m["filename"]} for m in mismatches]
        + [{"type": "missing", "filename": n} for n in missing_from_box]
        + [{"type": "box_hash_unavailable", "filename": n} for n in box_hash_unavailable]
        + [{"type": "extra_on_box", "filename": n} for n in orphans]
    )
    return all_ok, summary, issues


def _box_retry_delay(exc, attempt: int, *, base: float = 1.0, cap: float = 30.0) -> float:
    """Seconds to wait before the next upload retry.

    Honors a Box **429** ``Retry-After`` header when the rate-limit response carries
    one; otherwise falls back to exponential backoff (``base``, 2×, 4×…) capped at
    ``cap``. Best-effort — the Box SDK's exception shape varies, so status/header
    extraction is defensive and never raises.
    """
    try:
        info = getattr(exc, "response_info", None) or getattr(exc, "response", None)
        status = (
            getattr(info, "status_code", None)
            or getattr(exc, "status_code", None)
            or getattr(exc, "status", None)
        )
        if status == 429:
            headers = getattr(info, "headers", None) or getattr(exc, "headers", None) or {}
            for k, v in dict(headers).items():
                if str(k).lower() == "retry-after":
                    return min(float(v), cap)
    except Exception:
        pass
    return min(base * (2 ** (attempt - 1)), cap)


def _is_already_exists(exc) -> bool:
    """True if a Box upload error is a 409 'item already exists'.

    This means the file is already on Box — almost always because a prior attempt
    in the same run actually landed it but its success response was lost, so the
    retry hit a duplicate. Treating it as success (instead of a failure) avoids
    cosmetic false-failures; the post-upload SHA-1 verification still confirms the
    bytes match. Defensive across Box SDK exception shapes.
    """
    try:
        info = getattr(exc, "response_info", None) or getattr(exc, "response", None)
        status = (
            getattr(info, "status_code", None)
            or getattr(exc, "status_code", None)
            or getattr(exc, "status", None)
        )
        if status == 409:
            return True
    except Exception:
        pass
    msg = str(exc).lower()
    return "already exists" in msg or "item with the same name" in msg


class BoxUploadThread(QThread):
    """Background thread for uploading a deployment folder to Box."""

    progress = Signal(int, int, str)  # current, total, message
    finished = Signal(bool, str)  # success, message

    UPLOAD_WORKERS = 8  # parallel upload workers

    def __init__(
        self,
        deployment_folder: Path,
        metadata: dict,
        file_inventory: list | None = None,
        *,
        box_config: BoxConfig,
        valid_reserve_names: set[str],
    ):
        super().__init__()
        self.deployment_folder = deployment_folder
        self.metadata = metadata
        self.file_inventory = file_inventory or []
        self.box_config = box_config
        self.valid_reserve_names = valid_reserve_names
        self.client = None
        self.deploy_folder_id = None  # set during run(); used for provenance re-upload
        # Cooperative cancellation flag — set from the GUI thread via cancel()
        self._cancel_event = threading.Event()
        self._state_lock = threading.Lock()  # protects shared upload counters

    def cancel(self):
        """Request cooperative cancellation. In-flight uploads finish; no new ones start."""
        self._cancel_event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def run(self):
        """Upload deployment folder to Box."""
        try:
            self.client = get_box_client(self.box_config)
            if not self.client:
                self.finished.emit(False, "Could not authenticate with Box")
                return

            storage = BoxStorage(self.client)

            # Validate reserve name against canonical list before building path
            reserve_name = self.metadata.get("reserve_name", "")
            if reserve_name not in self.valid_reserve_names:
                self.finished.emit(
                    False,
                    f"Reserve name '{reserve_name}' not found in sites.csv. "
                    "Cannot determine Box folder path.",
                )
                return

            # Build nested path: root → year → reserve → deployment
            target_folder_id = self.box_config.field_data_folder_id
            year = self.metadata["deployment_end"][:4]
            deploy_name = self.deployment_folder.name

            year_id = storage.find_or_create_folder(target_folder_id, year)
            reserve_id = storage.find_or_create_folder(year_id, reserve_name)
            deploy_id = storage.find_or_create_folder(reserve_id, deploy_name)
            self.deploy_folder_id = deploy_id  # saved for provenance re-upload

            uploadable_files = [
                path
                for path in self.deployment_folder.rglob("*")
                if path.is_file() and storage.should_upload_file(path)
            ]
            total_files = len(uploadable_files)
            completed = 0
            uploaded = 0
            skipped = 0
            versioned = 0

            # Build hash lookups from file_inventory (name → sha256 / sha1)
            hash_lookup = {
                e.get("new_filename", ""): e.get("file_hash_sha256", "")
                for e in self.file_inventory
            }
            sha1_lookup = {
                e.get("new_filename", ""): e.get("file_hash_sha1", "")
                for e in self.file_inventory
            }

            # Write pre-upload manifest
            manifest_entries = [
                {
                    "relative_path": str(fp.relative_to(self.deployment_folder)),
                    "filename": fp.name,
                    "sha256": hash_lookup.get(fp.name, ""),
                    "sha1": sha1_lookup.get(fp.name, ""),
                }
                for fp in uploadable_files
            ]
            manifest_data = {
                "generated": datetime.now().isoformat(),
                "deployment": self.deployment_folder.name,
                "file_count": len(manifest_entries),
                "files": manifest_entries,
            }
            manifest_path = qc_path_for(self.deployment_folder, "box_upload_manifest.json")
            with open(manifest_path, "w") as f:
                json.dump(manifest_data, f, indent=2)

            # PHASE 1 — Pre-resolve all unique destination folders and prime caches.
            # Doing this once up-front (instead of per file) eliminates ~2 API
            # roundtrips per file, which dominates wall-clock time when files are
            # small and the network is the bottleneck.
            unique_parent_paths = sorted(
                {
                    tuple(fp.relative_to(self.deployment_folder).parts[:-1])
                    for fp in uploadable_files
                }
            )
            parent_id_for_path: dict[tuple, str] = {(): deploy_id}
            for path_parts in unique_parent_paths:
                if not path_parts:
                    continue
                # Resolve each segment, building up the parent_id chain.
                current_id = deploy_id
                for i, segment in enumerate(path_parts):
                    sub_key = path_parts[: i + 1]
                    if sub_key in parent_id_for_path:
                        current_id = parent_id_for_path[sub_key]
                        continue
                    current_id = storage.find_or_create_folder(current_id, segment)
                    parent_id_for_path[sub_key] = current_id

            # Pre-populate the per-folder file-existence cache for every unique
            # parent. folder_file_map() is idempotent, so duplicate ids are free.
            for folder_id in parent_id_for_path.values():
                storage.folder_file_map(folder_id)

            # PHASE 2 — Parallel upload via ThreadPoolExecutor. Each upload is
            # network I/O so the GIL releases during socket ops; threading is
            # effective. State mutation is protected by self._state_lock.
            failed_uploads: list[tuple[str, str]] = []  # (relative_path, error_message)
            cancelled_count = 0

            def _upload_one(file_path):
                """Worker: upload one file with up to 3 attempts and backoff.

                Retries are spaced by exponential backoff (honoring a Box 429
                Retry-After when present) instead of hammering the API instantly —
                important on long multi-GB uploads that can trip rate limiting. The
                backoff wait wakes immediately if the user cancels.
                """
                if self._cancel_event.is_set():
                    return ("cancelled", str(file_path.relative_to(self.deployment_folder)), None)
                rel_path = file_path.relative_to(self.deployment_folder)
                last_err = None
                max_attempts = 3
                for attempt in range(1, max_attempts + 1):
                    if self._cancel_event.is_set():
                        return ("cancelled", str(rel_path), None)
                    try:
                        action = storage.upload_file_with_path(file_path, deploy_id, rel_path)
                        return ("ok", str(rel_path), f"{action}: {rel_path}")
                    except Exception as e:
                        # A 409 "already exists" means the file is on Box — typically a
                        # prior attempt landed it but its ack was lost. Treat as success
                        # (the post-upload SHA-1 check verifies the bytes), not a failure.
                        if _is_already_exists(e):
                            return ("ok", str(rel_path), f"uploaded: {rel_path}")
                        last_err = e
                        if attempt < max_attempts:
                            # Event.wait() doubles as an interruptible sleep.
                            if self._cancel_event.wait(_box_retry_delay(e, attempt)):
                                return ("cancelled", str(rel_path), None)
                return ("fail", str(rel_path), str(last_err)[:200] if last_err else "unknown error")

            with ThreadPoolExecutor(max_workers=self.UPLOAD_WORKERS) as ex:
                futures = {ex.submit(_upload_one, fp): fp for fp in uploadable_files}
                for fut in as_completed(futures):
                    try:
                        status, rel_path, payload = fut.result()
                    except Exception as e:
                        # Defensive: shouldn't happen since _upload_one catches its own
                        rel_path = str(futures[fut].relative_to(self.deployment_folder))
                        status, payload = "fail", str(e)[:200]

                    with self._state_lock:
                        if status == "ok":
                            completed += 1
                            if payload and payload.startswith("skipped:"):
                                skipped += 1
                            elif payload and payload.startswith("versioned:"):
                                versioned += 1
                            else:
                                uploaded += 1
                            self.progress.emit(completed, total_files, payload or "")
                        elif status == "cancelled":
                            cancelled_count += 1
                        else:
                            failed_uploads.append((rel_path, payload or ""))
                            # Still bump the progress so the UI doesn't appear stuck
                            completed += 1
                            self.progress.emit(completed, total_files, f"failed: {rel_path}")

            if self._cancel_event.is_set():
                # User-requested cancellation — exit before verification.
                append_qc_report(
                    self.deployment_folder,
                    "box_upload",
                    "",
                    "warning",
                    f"Upload cancelled by user. {completed}/{total_files} completed, "
                    f"{cancelled_count} not attempted, {len(failed_uploads)} failed.",
                )
                self.finished.emit(
                    False,
                    f"Upload cancelled. {completed} of {total_files} files completed; "
                    f"{cancelled_count} not yet attempted; {len(failed_uploads)} failed. "
                    "Re-run the upload to resume — already-uploaded files will be skipped.",
                )
                return

            # Post-upload presence check: recursively list the Box folder and confirm
            # every expected file arrived. Presence-only and non-fatal — the GUI then
            # runs the full automatic hash + orphan verification
            # (_start_post_upload_verification → BoxVerifyThread + FixityCheckThread),
            # so we deliberately don't re-do hashing here. Paginates each folder —
            # without this, large device folders (>1000 files) were undercounted,
            # causing huge false-positive "missing from Box" reports.
            try:
                box_paths = storage.collect_file_paths(deploy_id)

                expected_paths = {e["relative_path"] for e in manifest_entries}
                missing = expected_paths - box_paths
                if missing:
                    missing_str = ", ".join(sorted(missing)[:10])
                    append_qc_report(
                        self.deployment_folder,
                        "box_upload",
                        "",
                        "warning",
                        f"{len(missing)} file(s) uploaded but missing from Box: {missing_str}",
                    )
                    self.finished.emit(
                        failed_uploads == [],
                        f"Completed {completed} of {total_files} files "
                        f"({uploaded} uploaded, {versioned} metadata versioned, {skipped} skipped). "
                        f"{len(missing)} appear missing from Box after verification: {missing_str}. "
                        f"{len(failed_uploads)} per-file upload error(s). "
                        "Run 'Verify Box Upload' to investigate. Re-running the upload will retry failed/missing files.",
                    )
                    return
                append_qc_report(
                    self.deployment_folder,
                    "box_upload",
                    "",
                    "pass",
                    f"All {uploaded} file(s) uploaded and verified on Box",
                )
            except Exception:
                pass  # verification failure is non-fatal; upload succeeded

            if failed_uploads:
                # Surface per-file failures even if Box-side reconciliation passed
                fail_lines = "\n".join(f"  • {p}: {err}" for p, err in failed_uploads[:10])
                more = f"\n... +{len(failed_uploads) - 10} more" if len(failed_uploads) > 10 else ""
                append_qc_report(
                    self.deployment_folder,
                    "box_upload",
                    "",
                    "warning",
                    f"{len(failed_uploads)} file(s) failed to upload after retries",
                )
                self.finished.emit(
                    False,
                    f"Completed {completed} of {total_files} files "
                    f"({uploaded} uploaded, {versioned} metadata versioned, {skipped} skipped). "
                    f"{len(failed_uploads)} file(s) failed after 3 retries each:\n{fail_lines}{more}\n\n"
                    "Re-run the upload — successfully uploaded files will be skipped, only failed ones retry.",
                )
                return

            self.finished.emit(
                True,
                f"Box upload verified: {uploaded} uploaded, {versioned} metadata file(s) versioned, "
                f"{skipped} existing raw file(s) skipped.",
            )
        except Exception as e:
            self.finished.emit(False, f"Upload error: {str(e)}")


class BoxVerifyThread(QThread):
    """List the Box deployment folder and compare against local file_inventory.

    Reports files missing from Box and unexpected files on Box.
    """

    finished = Signal(bool, str, list)  # ok, summary, issues

    def __init__(
        self,
        deployment_folder: Path,
        file_inventory: list,
        metadata: dict,
        *,
        box_config: BoxConfig,
        valid_reserve_names: set[str],
    ):
        super().__init__()
        self.deployment_folder = deployment_folder
        self.file_inventory = file_inventory
        self.metadata = metadata
        self.box_config = box_config
        self.valid_reserve_names = valid_reserve_names

    def run(self):
        try:
            client = get_box_client(self.box_config)
            if not client:
                self.finished.emit(False, "Could not authenticate with Box", [])
                return

            storage = BoxStorage(client)

            reserve_name = self.metadata.get("reserve_name", "")
            if reserve_name not in self.valid_reserve_names:
                self.finished.emit(False, f"Reserve '{reserve_name}' not in sites.csv", [])
                return

            year = self.metadata.get("deployment_end", "")[:4]
            reserve_folder_name = sanitize_box_folder_name(reserve_name)
            deploy_name = self.deployment_folder.name

            year_id = storage.find_child_folder(
                self.box_config.field_data_folder_id, year, fields=_SHA1_FIELDS
            )
            if not year_id:
                self.finished.emit(False, f"Year folder '{year}' not found on Box", [])
                return
            reserve_id = storage.find_child_folder(year_id, reserve_folder_name, fields=_SHA1_FIELDS)
            if not reserve_id:
                self.finished.emit(False, f"Reserve folder '{reserve_folder_name}' not found on Box", [])
                return
            deploy_id = storage.find_child_folder(reserve_id, deploy_name, fields=_SHA1_FIELDS)
            if not deploy_id:
                self.finished.emit(False, f"Deployment folder '{deploy_name}' not found on Box", [])
                return

            box_paths = storage.collect_file_paths(deploy_id, fields=_SHA1_FIELDS)

            local_paths = {
                str(Path("raw_data", e["device_label"], e["new_filename"]))
                for e in self.file_inventory
                if e.get("device_label") and e.get("new_filename")
            }
            missing = local_paths - box_paths
            # Orphan ("extra on Box") detection, after the metadata-sidecar allowlist
            # shared with the automatic post-upload verify.
            extra = {path for path in (box_paths - local_paths) if _is_orphan_on_box(path)}

            issues = [
                {"type": "missing_from_box", "filename": n} for n in sorted(missing)
            ] + [{"type": "extra_on_box", "filename": n} for n in sorted(extra)]
            ok = not missing
            if not missing and not extra:
                summary = "All files are present on Box."
            else:
                summary = f"Box verify: {len(missing)} missing from Box, {len(extra)} unexpected."
            self.finished.emit(ok, summary, issues)
        except Exception as e:
            self.finished.emit(False, f"Box verify error: {e}", [])


class ProvenanceUploadThread(QThread):
    """Re-uploads the two provenance-updated metadata CSVs to Box after upload completion."""

    finished = Signal(bool, str)

    def __init__(self, csv_paths, deploy_folder_id, *, box_config: BoxConfig):
        super().__init__()
        self.csv_paths = csv_paths  # list of Path objects
        self.deploy_folder_id = deploy_folder_id
        self.box_config = box_config

    def run(self):
        try:
            client = get_box_client(self.box_config)
            if not client:
                self.finished.emit(False, "Could not authenticate with Box for provenance upload")
                return

            for csv_path in self.csv_paths:
                if not csv_path.exists():
                    continue
                # Check if file already exists in the folder; if so, upload new version
                items = client.folders.get_folder_items(self.deploy_folder_id).entries
                existing = next(
                    (item for item in items if item.type == "file" and item.name == csv_path.name),
                    None,
                )
                with open(csv_path, "rb") as f:
                    if existing:
                        from box_sdk_gen import UploadFileVersionAttributes

                        client.uploads.upload_file_version(
                            existing.id,
                            attributes=UploadFileVersionAttributes(name=csv_path.name),
                            file=f,
                        )
                    else:
                        client.uploads.upload_file(
                            attributes={"name": csv_path.name, "parent": {"id": self.deploy_folder_id}},
                            file=f,
                        )

            self.finished.emit(True, "Provenance CSVs uploaded to Box")
        except Exception as e:
            self.finished.emit(False, f"Provenance upload error: {e}")


class FixityCheckThread(QThread):
    """End-to-end file-hash verification against Box.

    Uses the SHA-1 captured at local ingest when available; falls back to
    computing local SHA-1 for older sessions. Box's SHA-1 is computed
    server-side from the bytes that actually arrived and are stored.
    """

    progress = Signal(int, int, str)  # checked, total, current_filename
    finished = Signal(bool, str, list)  # ok, summary_message, mismatch_list

    def __init__(
        self,
        deployment_folder: Path,
        file_inventory: list,
        metadata: dict,
        *,
        box_config: BoxConfig,
        valid_reserve_names: set[str],
        hash_retry: int = 0,
    ):
        super().__init__()
        self.deployment_folder = deployment_folder
        self.file_inventory = file_inventory
        self.metadata = metadata
        self.box_config = box_config
        self.valid_reserve_names = valid_reserve_names
        # >0 when run as the automatic post-upload verify: Box can lag computing a
        # fresh file's SHA-1, so re-fetch unavailable hashes before reporting them.
        # The manual button leaves this 0 (on-demand re-check, no lag expected).
        self.hash_retry = hash_retry
        self._cancel_event = threading.Event()

    def cancel(self):
        self._cancel_event.set()

    def run(self):
        try:
            # 1) Authenticate with Box
            client = get_box_client(self.box_config)
            if not client:
                self.finished.emit(False, "Could not authenticate with Box.", [])
                return

            storage = BoxStorage(client)

            # 2) Walk to the deployment folder on Box
            reserve_name = self.metadata.get("reserve_name", "")
            if reserve_name not in self.valid_reserve_names:
                self.finished.emit(False, f"Reserve '{reserve_name}' not in sites.csv.", [])
                return

            year = self.metadata.get("deployment_end", "")[:4]
            reserve_folder_name = sanitize_box_folder_name(reserve_name)
            deploy_name = self.deployment_folder.name

            year_id = storage.find_child_folder(
                self.box_config.field_data_folder_id, year, fields=_SHA1_FIELDS
            )
            if not year_id:
                self.finished.emit(False, f"Year folder '{year}' not found on Box.", [])
                return
            reserve_id = storage.find_child_folder(year_id, reserve_folder_name, fields=_SHA1_FIELDS)
            if not reserve_id:
                self.finished.emit(False, f"Reserve folder '{reserve_folder_name}' not found on Box.", [])
                return
            deploy_id = storage.find_child_folder(reserve_id, deploy_name, fields=_SHA1_FIELDS)
            if not deploy_id:
                self.finished.emit(False, f"Deployment folder '{deploy_name}' not found on Box.", [])
                return

            # 3) Delegate the SHA-1 collection + comparison to the shared helper.
            # The manual button passes hash_retry=0 (on-demand re-check); the
            # automatic post-upload run passes hash_retry>0 to absorb Box's SHA-1 lag.
            all_ok, summary, issues = verify_box_hashes(
                storage,
                deploy_id,
                deploy_name,
                self.file_inventory,
                self.deployment_folder,
                hash_retry=self.hash_retry,
                progress=lambda c, t, n: self.progress.emit(c, t, n),
                is_cancelled=self._cancel_event.is_set,
            )
            if self._cancel_event.is_set():
                self.finished.emit(False, summary, [])
                return
            self.finished.emit(all_ok, summary, issues)

        except Exception as e:
            self.finished.emit(False, f"Verification error: {e}", [])
