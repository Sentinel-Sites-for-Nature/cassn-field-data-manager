"""Background wrappers for SoundHub staging and upload."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from cassn.soundhub.export import (
    build_recording_rows,
    enrich_audio_rows,
    read_bd_audio_rows,
    refresh_project_csvs,
    validate_staging_manifests,
    write_deployment_copy,
    write_deployment_fragments,
)
from cassn.soundhub.lifecycle import (
    plan_completed_batch_cleanup,
    staging_extension_blockers,
)
from cassn.soundhub.staging import (
    SoundHubStagingError,
    fragments_root,
    project_root,
    stage_deployment,
)
from cassn.soundhub.provenance import DEFAULT_SUBMITTER
from cassn.soundhub.submission import (
    SoundHubSubmissionError,
    SoundHubSubmissionPlan,
    execute_soundhub_submission,
)


class SoundHubStageThread(QThread):
    """Transcode a deployment's BD audio to FLAC and refresh the project CSVs.

    Encoding a full deployment is slow — bird recordings run to a gigabyte each
    — so this reports per-file progress and honours cancellation between files.
    A cancelled run leaves completed FLACs in place; re-running resumes.
    """

    progress = Signal(int, int, str)
    completed = Signal(bool, bool, str, dict)  # success, cancelled, message, result

    def __init__(self, deployment_folder, staging_root, lookups):
        super().__init__()
        self.deployment_folder = Path(deployment_folder)
        self.staging_root = Path(staging_root)
        self.lookups = lookups
        self._cancel_event = threading.Event()

    def cancel(self):
        self._cancel_event.set()

    def _check_existing_batch(self):
        """Keep slow filesystem and Box checks off the GUI thread, before writes."""
        root = project_root(self.staging_root)
        fragments = fragments_root(self.staging_root)
        has_fragments = fragments.is_dir() and any(
            path.is_dir() for path in fragments.iterdir()
        )
        has_manifests = any(
            (root / name).exists() for name in ("deployment.csv", "recording.csv")
        )
        if not (has_fragments or has_manifests):
            return

        self.progress.emit(0, 0, "Checking existing SoundHub batch…")
        validate_staging_manifests(self.staging_root)
        if self._cancel_event.is_set():
            return
        self.progress.emit(0, 0, "Checking existing batch submission records in Box…")
        lifecycle = plan_completed_batch_cleanup(self.staging_root)
        if self._cancel_event.is_set():
            return
        if lifecycle.closed:
            detail = ""
            if lifecycle.errors:
                detail = "\n\nCleanup is currently blocked:\n" + "\n".join(
                    f"• {error}" for error in lifecycle.errors
                )
            raise SoundHubStagingError(
                f"The existing batch contains {lifecycle.recording_count} "
                "recording(s), and all are already recorded as submitted. "
                "A new deployment cannot be added to its completed manifests.\n\n"
                "After SoundHub acceptance is confirmed, use the maintenance "
                "utility in Terminal:\n\n"
                "python utils/prep_soundhub.py clear-completed\n"
                "python utils/prep_soundhub.py clear-completed --apply"
                f"{detail}"
            )
        # Preserve local staging's existing policy: Box provenance errors alone
        # do not block extension, but unsafe local paths/layouts do.
        blockers = staging_extension_blockers(lifecycle)
        if blockers:
            raise SoundHubStagingError(
                "The existing staging batch cannot be safely extended:\n\n"
                + "\n".join(f"• {error}" for error in blockers)
            )

    def run(self):
        try:
            self._check_existing_batch()
            if self._cancel_event.is_set():
                self.completed.emit(
                    False, True, "SoundHub staging cancelled before conversion.", {}
                )
                return
            self.progress.emit(0, 0, "Reading bird audio metadata…")
            audio_rows = enrich_audio_rows(
                read_bd_audio_rows(self.deployment_folder), self.lookups
            )
            if not audio_rows:
                self.completed.emit(
                    False, False,
                    "No bird (BD) audio in this deployment — nothing to send to SoundHub.",
                    {},
                )
                return

            # Fail before a potentially hours-long transcode if the recording
            # manifest cannot be completed from the source metadata.
            build_recording_rows(audio_rows)

            result = stage_deployment(
                self.deployment_folder,
                audio_rows,
                self.staging_root,
                progress=self.progress.emit,
                is_cancelled=self._cancel_event.is_set,
            )

            if result["cancelled"]:
                self.completed.emit(
                    False, True,
                    f"FLAC conversion cancelled after {result['converted']} file(s). "
                    "Completed files are kept; re-run to resume.",
                    result,
                )
                return

            # CSVs are written only once every FLAC exists, so a partial staging
            # tree can never be described by a complete manifest.
            write_deployment_fragments(self.staging_root, audio_rows)
            write_deployment_copy(self.deployment_folder, audio_rows)
            csvs = refresh_project_csvs(self.staging_root)
            validation = validate_staging_manifests(self.staging_root)
            result["csvs"] = csvs
            result["validation"] = validation

            self.completed.emit(
                True, False,
                f"Staged and validated as upload-ready "
                f"{', '.join(result['deployment_ids'])}: "
                f"{result['converted']} converted, "
                f"{result['skipped']} already present. Project manifests now list "
                f"{csvs['deployment_count']} deployment(s) and "
                f"{csvs['recording_count']} recording(s).",
                result,
            )
        except Exception as exc:
            self.completed.emit(False, False, f"SoundHub staging failed: {exc}", {})


class SoundHubUploadThread(QThread):
    """Run the shared upload, verification, and Box-provenance workflow."""

    progress = Signal(int, int, str)
    completed = Signal(bool, bool, str, dict)  # success, cancelled, message, result

    def __init__(
        self,
        submission_plan: SoundHubSubmissionPlan,
        submitter: str = DEFAULT_SUBMITTER,
    ):
        super().__init__()
        self.submission_plan = submission_plan
        self.submitter = submitter
        self._cancel_event = threading.Event()

    def cancel(self):
        self._cancel_event.set()

    def run(self):
        try:
            result = execute_soundhub_submission(
                self.submission_plan,
                submitter=self.submitter,
                progress=self.progress.emit,
                is_cancelled=self._cancel_event.is_set,
            )

            if result["cancelled"]:
                upload = result["upload"]
                self.completed.emit(
                    False, True,
                    f"Upload cancelled after {upload['uploaded']} object(s). "
                    "Objects already sent stay in the bucket; re-run to resume. "
                    f"Transfer record: {result['transfer_directory']}",
                    result,
                )
                return

            if not result["success"]:
                check = result["verification"]
                self.completed.emit(
                    False, False,
                    f"Upload finished but verification failed: "
                    f"{len(check['missing'])} missing, {len(check['mismatched'])} "
                    "size mismatch(es). Re-run the upload to resume. "
                    f"Transfer record: {result['transfer_directory']}",
                    result,
                )
                return

            upload = result["upload"]
            check = result["verification"]
            provenance = result["provenance"]
            record_note = (
                "Metadata and reports were saved directly to Box. "
                if self.submission_plan.provenance.box_store is not None else
                "Keep Box Drive running until the metadata and reports finish syncing. "
            )
            self.completed.emit(
                True, False,
                f"Uploaded {upload['uploaded']} object(s) "
                f"({upload['skipped']} already present), verified all "
                f"{check['checked']} object(s), and recorded "
                f"{provenance.changed_rows} Box metadata row(s) across "
                f"{provenance.changed_files} deployment event(s). "
                f"{record_note}"
                f"Transfer record: {result['transfer_directory']}",
                result,
            )
        except SoundHubSubmissionError as exc:
            if exc.phase == "box_provenance":
                message = (
                    "S3 upload verified, but the Box submission record failed: "
                    f"{exc}. Do not upload again; repair the Box record instead."
                )
            else:
                message = f"SoundHub {exc.phase.replace('_', ' ')} failed: {exc}"
            if exc.result.get("transfer_directory"):
                message += f" Transfer record: {exc.result['transfer_directory']}"
            self.completed.emit(False, False, message, exc.result)
        except Exception as exc:
            self.completed.emit(False, False, f"SoundHub upload failed: {exc}", {})
