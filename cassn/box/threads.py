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
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from cassn.box.auth import BoxConfig, get_box_client
from cassn.core.ingest_safety import require_no_ingest_holds
from cassn.box.client import BoxStorage
from cassn.box.scheduling import upload_results
from cassn.box.transfer import (
    UploadCancelled, UploadRuntime, error_response, prepare_upload_client, retry_after,
)
from cassn.box.upload_state import STATE_DIRECTORY, require_uploads_complete
from cassn.config import CHUNKED_UPLOAD_THRESHOLD
from cassn.box.metadata_merge import SHARED_NAMES
from cassn.box.publication import EventPublication, batch_remote_path
from cassn.box.verification import (
    collect_box_file_hashes, is_orphan_on_box, recorded_archive_paths, verify_box_hashes,
)
from cassn.core.classification import sanitize_box_folder_name
from cassn.core.inventory import index_inventory_by_storage_relpath
from cassn.core.quality_control import append_qc_report, qc_path_for

# Box API field projection for the verify/fixity walks: enough to navigate and
# to read each file's server-side SHA-1 without pulling full item payloads.
_SHA1_FIELDS = ["id", "type", "name", "sha1"]


def _publication_error(publication, exc):
    message = str(exc)
    if publication is not None:
        try:
            publication.fail(exc)
        except Exception as journal_error:
            message += f"; recovery record could not be updated: {journal_error}. Retain local staging for review."
    return message

def _box_retry_delay(exc, attempt: int, *, base: float = 1.0, cap: float = 30.0) -> float:
    """Seconds to wait before the next upload retry.

    Honors a Box **429** ``Retry-After`` header when the rate-limit response carries
    one; otherwise falls back to exponential backoff (``base``, 2×, 4×…) capped at
    ``cap``. Best-effort — the Box SDK's exception shape varies, so status/header
    extraction is defensive and never raises.
    """
    status, headers = error_response(exc)
    if status == 429:
        server_wait = retry_after(headers)
        if server_wait is not None:
            return server_wait
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
    metrics_updated = Signal(dict)  # aggregate payload/rate/wait/stage counters

    UPLOAD_WORKERS = 8  # parallel upload workers

    def __init__(
        self,
        deployment_folder: Path,
        metadata: dict,
        file_inventory: list | None = None,
        *,
        box_config: BoxConfig,
        valid_site_names: set[str],
        upload_workers: int = UPLOAD_WORKERS,
        part_workers: int = 2,
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
        self.upload_workers = upload_workers
        self.upload_runtime = UploadRuntime(cancel=self._cancel_event, transfers=upload_workers,
                                            part_workers=part_workers)
        self.upload_runtime.metrics.callback = self.metrics_updated.emit

    def cancel(self):
        """Request cooperative cancellation. In-flight uploads finish; no new ones start."""
        self._cancel_event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def run(self):
        """Upload deployment folder to Box."""
        publication = None
        network = None
        metrics = self.upload_runtime.metrics
        outcome = 'failed'
        try:
            require_no_ingest_holds(self.deployment_folder)
            metrics.output_path = self.deployment_folder / 'qc' / STATE_DIRECTORY / 'metrics.json'
            metrics.set_stage('preparing')
            self.client = get_box_client(self.box_config)
            if not self.client:
                self.finished.emit(False, "Could not authenticate with Box")
                return

            self.client, network = prepare_upload_client(self.client, self.upload_runtime)
            storage = BoxStorage(self.client, upload_runtime=self.upload_runtime,
                                 journal_dir=self.deployment_folder / 'qc' / STATE_DIRECTORY)

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

            append_qc_report(self.deployment_folder, "box_publication", "", "info",
                             "Preparing a batch upload; completion requires metadata publication and verification.")
            publication = EventPublication(storage, deploy_id, self.deployment_folder)
            publication.prepare()  # Reject unsafe merges before sending media.
            publication.validate_batch_inventory(self.file_inventory)

            def remote_path(path):
                relative = path.relative_to(self.deployment_folder)
                return batch_remote_path(relative, publication.attempt_id)

            uploadable_files = [
                path
                for path in self.deployment_folder.rglob("*")
                if path.is_file() and storage.should_upload_file(path)
            ]

            # Full paths keep hashes unambiguous across flat and split layouts.
            inventory_entry_by_path = index_inventory_by_storage_relpath(
                self.file_inventory
            )

            # Write pre-upload manifest
            manifest_entries = []
            for fp in uploadable_files:
                local_relative = fp.relative_to(self.deployment_folder).as_posix()
                relative_path = remote_path(fp).as_posix()
                entry = inventory_entry_by_path.get(local_relative, {})
                manifest_entries.append({
                    "relative_path": relative_path,
                    "filename": fp.name,
                    "sha256": entry.get("file_hash_sha256", ""),
                    "sha1": entry.get("file_hash_sha1", ""),
                })
            manifest_data = {
                "scope": "local_batch",
                "attempt_id": publication.attempt_id,
                "generated": datetime.now().isoformat(),
                "deployment": self.deployment_folder.name,
                "file_count": len(manifest_entries),
                "files": manifest_entries,
            }
            manifest_path = qc_path_for(self.deployment_folder, "box_upload_manifest.json")
            with open(manifest_path, "w") as f:
                json.dump(manifest_data, f, indent=2)

            # The manifest describes the pre-upload snapshot and therefore does
            # not list itself, but it is still an uploadable QC artifact. The
            # historical ordering wrote it after ``uploadable_files`` was
            # collected and unintentionally left it local-only.
            if manifest_path not in uploadable_files:
                uploadable_files.append(manifest_path)
            uploadable_files = [fp for fp in uploadable_files
                                if fp.relative_to(self.deployment_folder).as_posix() not in SHARED_NAMES]
            total_files = len(uploadable_files)
            completed = 0
            uploaded = 0
            skipped = 0
            versioned = 0

            # PHASE 1 — Pre-resolve all unique destination folders and prime caches.
            # Doing this once up-front (instead of per file) eliminates ~2 API
            # roundtrips per file, which dominates wall-clock time when files are
            # small and the network is the bottleneck.
            unique_parent_paths = sorted(
                {
                    tuple(remote_path(fp).parts[:-1])
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
                """One file job; request/chunk layers own retry and recovery."""
                rel_path = remote_path(file_path)
                if self._cancel_event.is_set():
                    return ("cancelled", rel_path.as_posix(), None)
                try:
                    action = storage.upload_file_with_path(file_path, deploy_id, rel_path)
                    metrics.add(**{f'{action}_files': 1})
                    return ("ok", rel_path.as_posix(), f"{action}: {rel_path}")
                except UploadCancelled:
                    return ("cancelled", rel_path.as_posix(), None)
                except Exception as exc:
                    # Preserve the existing final hash check for raw-file races;
                    # no whole-file retry can restart a chunk session here.
                    if _is_already_exists(exc) and "raw_data" in rel_path.parts:
                        metrics.add(skipped_files=1)
                        return ("ok", rel_path.as_posix(), f"skipped: {rel_path}")
                    metrics.add(failed_files=1)
                    return ("fail", rel_path.as_posix(), str(exc)[:200])

            metrics.set_stage('transferring')
            for file_path, fut in upload_results(
                uploadable_files, _upload_one, cancel=self._cancel_event,
                is_large=lambda fp: fp.stat().st_size >= CHUNKED_UPLOAD_THRESHOLD,
                workers=self.upload_workers, pending_limit=max(32, self.upload_workers), metrics=metrics,
            ):
                try:
                    status, rel_path, payload = fut.result()
                except Exception as exc:
                    rel_path = file_path.relative_to(self.deployment_folder).as_posix()
                    status, payload = "fail", str(exc)[:200]

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
                        completed += 1
                        self.progress.emit(completed, total_files, f"failed: {rel_path}")

            if self._cancel_event.is_set():
                outcome = 'cancelled'
                cancelled_count = total_files - completed
                publication.fail("Upload cancelled before metadata publication")
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

            if failed_uploads:
                raise RuntimeError(f"{len(failed_uploads)} file upload(s) failed: {failed_uploads[:10]}")

            # Only publish accumulated metadata after the batch's media is
            # verified. Earlier archive rows never become local inventory rows.
            require_uploads_complete(self.deployment_folder)
            metrics.set_stage('verifying_media')
            ok, summary, issues = verify_box_hashes(
                storage, deploy_id, deploy_name, self.file_inventory,
                self.deployment_folder, hash_retry=2,
            )
            if not ok:
                raise RuntimeError(f"Batch media verification failed before publication: {summary}; {issues[:10]}")
            if self._cancel_event.is_set():
                raise RuntimeError("Upload cancelled before metadata publication")
            publication.mark_media_verified()
            metrics.set_stage('publishing_metadata')
            publication.publish()

            # Post-upload presence check: recursively list the Box folder and confirm
            # every expected file arrived. Presence-only and non-fatal — the GUI then
            # runs the full automatic hash + orphan verification
            # (_start_post_upload_verification → BoxVerifyThread + FixityCheckThread),
            # so we deliberately don't re-do hashing here. Paginates each folder —
            # without this, large device folders (>1000 files) were undercounted,
            # causing huge false-positive "missing from Box" reports.
            try:
                metrics.set_stage('checking_presence')
                box_paths = storage.collect_file_paths(deploy_id)

                expected_paths = {e["relative_path"] for e in manifest_entries}
                missing = expected_paths - box_paths
                if missing:
                    missing_str = ", ".join(sorted(missing)[:10])
                    publication.fail(f"Required upload paths missing: {missing_str}")
                    append_qc_report(
                        self.deployment_folder,
                        "box_upload",
                        "",
                        "warning",
                        f"{len(missing)} file(s) uploaded but missing from Box: {missing_str}",
                    )
                    self.finished.emit(
                        False,
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
            except Exception as exc:
                raise RuntimeError(f"Post-publication presence check failed: {exc}") from exc

            outcome = 'complete'
            self.finished.emit(
                True,
                f"Batch media verified and event metadata merged: {uploaded} uploaded, "
                f"{len(publication.candidates)} shared sidecar(s) verified, "
                f"{skipped} existing raw file(s) skipped.",
            )
        except Exception as e:
            outcome = 'cancelled' if self._cancel_event.is_set() else 'failed'
            self.finished.emit(False, f"Upload error: {_publication_error(publication, e)}")
        finally:
            metrics.set_stage(outcome)
            if network is not None:
                network.close()


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

            box_hashes = collect_box_file_hashes(storage, deploy_id)
            box_paths = set(box_hashes)
            archive_paths, archive_issues = recorded_archive_paths(storage, deploy_id, box_hashes)

            local_paths = set(index_inventory_by_storage_relpath(self.file_inventory))
            missing = local_paths - box_paths
            # Orphan ("extra on Box") detection, after the metadata-sidecar allowlist
            # shared with the automatic post-upload verify.
            extra = {path for path in (box_paths - local_paths - archive_paths) if is_orphan_on_box(path)}

            issues = [
                {"type": "missing_from_box", "filename": n} for n in sorted(missing)
            ] + [{"type": "extra_on_box", "filename": n} for n in sorted(extra)] + archive_issues
            ok = bool(local_paths) and not missing and not archive_issues
            if not local_paths:
                summary = "No valid expected inventory to verify."
            elif not missing and not extra:
                summary = "All batch files are present on Box."
            else:
                summary = f"Box verify: {len(missing)} missing from Box, {len(extra)} unexpected."
            summary += (f" Archive metadata: {len(archive_paths)} recorded raw path(s) matched; "
                        f"{len(archive_issues)} unverified row(s). Batch presence and archive coverage are separate checks.")
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
        publication = None
        network = None
        runtime = UploadRuntime()
        try:
            client = get_box_client(self.box_config)
            if not client:
                self.finished.emit(False, "Could not authenticate with Box for provenance upload")
                return

            if not self.csv_paths or any(not path.is_file() for path in self.csv_paths):
                raise ValueError("Expected provenance CSV is missing")
            folder = self.csv_paths[0].parent
            if any(path.parent != folder for path in self.csv_paths):
                raise ValueError("Provenance files belong to different local events")
            require_uploads_complete(folder)
            runtime.metrics.output_path = folder / 'qc' / STATE_DIRECTORY / 'diagnostics' / 'provenance.json'
            runtime.metrics.set_stage('publishing_provenance')
            client, network = prepare_upload_client(client, runtime)
            publication = EventPublication(BoxStorage(client), self.deploy_folder_id,
                                           folder, provenance=True, csv_paths=self.csv_paths)
            publication.prepare()
            publication.publish()
            runtime.metrics.set_stage('complete')

            self.finished.emit(True, "Provenance CSVs uploaded to Box")
        except Exception as e:
            runtime.metrics.set_stage('failed')
            self.finished.emit(False, f"Provenance upload error: {_publication_error(publication, e)}")

        finally:
            if network is not None:
                network.close()

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
