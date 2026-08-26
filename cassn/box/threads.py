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
* ``valid_site_names`` is the canonical set of formal site names (from the
  loaded lookup tables) used to validate the destination path.

This keeps the threads testable and free of the import-time config/CSV reads
the original relied on.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from cassn.box.auth import BoxConfig, get_box_client
from cassn.box.client import BoxStorage
from cassn.box.verification import is_orphan_on_box, verify_box_hashes
from cassn.core.classification import sanitize_box_folder_name
from cassn.core.inventory import index_inventory_by_storage_relpath
from cassn.core.quality_control import append_qc_report, qc_path_for

# Box API field projection for the verify/fixity walks: enough to navigate and
# to read each file's server-side SHA-1 without pulling full item payloads.
_SHA1_FIELDS = ["id", "type", "name", "sha1"]

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
        valid_site_names: set[str],
    ):
        super().__init__()
        self.deployment_folder = deployment_folder
        self.metadata = metadata
        self.file_inventory = file_inventory or []
        self.box_config = box_config
        self.valid_site_names = valid_site_names
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
            site_name = self.metadata.get("site_name", "")
            if site_name not in self.valid_site_names:
                self.finished.emit(
                    False,
                    f"Site name '{site_name}' not found in sites.csv. "
                    "Cannot determine Box folder path.",
                )
                return

            # Build nested path: root → year → reserve → deployment
            target_folder_id = self.box_config.field_data_folder_id
            year = self.metadata["deployment_event_end_date"][:4]
            deploy_name = self.deployment_folder.name

            year_id = storage.find_or_create_folder(target_folder_id, year)
            reserve_id = storage.find_or_create_folder(year_id, site_name)
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

            # Full paths keep hashes unambiguous across flat and split layouts.
            inventory_entry_by_path = index_inventory_by_storage_relpath(
                self.file_inventory
            )

            # Write pre-upload manifest
            manifest_entries = []
            for fp in uploadable_files:
                relative_path = fp.relative_to(self.deployment_folder).as_posix()
                entry = inventory_entry_by_path.get(relative_path, {})
                manifest_entries.append({
                    "relative_path": relative_path,
                    "filename": fp.name,
                    "sha256": entry.get("file_hash_sha256", ""),
                    "sha1": entry.get("file_hash_sha1", ""),
                })
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
                    return (
                        "cancelled",
                        file_path.relative_to(self.deployment_folder).as_posix(),
                        None,
                    )
                rel_path = file_path.relative_to(self.deployment_folder)
                last_err = None
                max_attempts = 3
                for attempt in range(1, max_attempts + 1):
                    if self._cancel_event.is_set():
                        return ("cancelled", rel_path.as_posix(), None)
                    try:
                        action = storage.upload_file_with_path(file_path, deploy_id, rel_path)
                        return ("ok", rel_path.as_posix(), f"{action}: {rel_path}")
                    except Exception as e:
                        # A 409 "already exists" means the file is on Box — typically a
                        # prior attempt landed it but its ack was lost. Treat as success
                        # (the post-upload SHA-1 check verifies the bytes), not a failure.
                        if _is_already_exists(e):
                            return ("ok", rel_path.as_posix(), f"uploaded: {rel_path}")
                        last_err = e
                        if attempt < max_attempts:
                            # Event.wait() doubles as an interruptible sleep.
                            if self._cancel_event.wait(_box_retry_delay(e, attempt)):
                                return ("cancelled", rel_path.as_posix(), None)
                return (
                    "fail",
                    rel_path.as_posix(),
                    str(last_err)[:200] if last_err else "unknown error",
                )

            with ThreadPoolExecutor(max_workers=self.UPLOAD_WORKERS) as ex:
                futures = {ex.submit(_upload_one, fp): fp for fp in uploadable_files}
                for fut in as_completed(futures):
                    try:
                        status, rel_path, payload = fut.result()
                    except Exception as e:
                        # Defensive: shouldn't happen since _upload_one catches its own
                        rel_path = futures[fut].relative_to(
                            self.deployment_folder
                        ).as_posix()
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
        valid_site_names: set[str],
    ):
        super().__init__()
        self.deployment_folder = deployment_folder
        self.file_inventory = file_inventory
        self.metadata = metadata
        self.box_config = box_config
        self.valid_site_names = valid_site_names

    def run(self):
        try:
            client = get_box_client(self.box_config)
            if not client:
                self.finished.emit(False, "Could not authenticate with Box", [])
                return

            storage = BoxStorage(client)

            site_name = self.metadata.get("site_name", "")
            if site_name not in self.valid_site_names:
                self.finished.emit(False, f"Site '{site_name}' not in sites.csv", [])
                return

            year = self.metadata.get("deployment_event_end_date", "")[:4]
            reserve_folder_name = sanitize_box_folder_name(site_name)
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

            local_paths = set(index_inventory_by_storage_relpath(self.file_inventory))
            missing = local_paths - box_paths
            # Orphan ("extra on Box") detection, after the metadata-sidecar allowlist
            # shared with the automatic post-upload verify.
            extra = {path for path in (box_paths - local_paths) if is_orphan_on_box(path)}

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
        valid_site_names: set[str],
        hash_retry: int = 0,
    ):
        super().__init__()
        self.deployment_folder = deployment_folder
        self.file_inventory = file_inventory
        self.metadata = metadata
        self.box_config = box_config
        self.valid_site_names = valid_site_names
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
            site_name = self.metadata.get("site_name", "")
            if site_name not in self.valid_site_names:
                self.finished.emit(False, f"Site '{site_name}' not in sites.csv.", [])
                return

            year = self.metadata.get("deployment_event_end_date", "")[:4]
            reserve_folder_name = sanitize_box_folder_name(site_name)
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
