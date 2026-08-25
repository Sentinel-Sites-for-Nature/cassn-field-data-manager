"""Background wrappers for SoundHub staging and upload."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from cassn.soundhub.export import (
    read_bd_audio_rows,
    refresh_project_csvs,
    write_deployment_copy,
    write_deployment_fragments,
)
from cassn.soundhub.staging import stage_deployment
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

    def __init__(self, deployment_folder, staging_root):
        super().__init__()
        self.deployment_folder = Path(deployment_folder)
        self.staging_root = Path(staging_root)
        self._cancel_event = threading.Event()

    def cancel(self):
        self._cancel_event.set()

    def run(self):
        try:
            audio_rows = read_bd_audio_rows(self.deployment_folder)
            if not audio_rows:
                self.completed.emit(
                    False, False,
                    "No bird (BD) audio in this deployment — nothing to send to SoundHub.",
                    {},
                )
                return

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
            result["csvs"] = csvs

            self.completed.emit(
                True, False,
                f"Staged {', '.join(result['deployment_ids'])}: "
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
                    "Objects already sent stay in the bucket; re-run to resume.",
                    result,
                )
                return

            if not result["success"]:
                check = result["verification"]
                self.completed.emit(
                    False, False,
                    f"Upload finished but verification failed: "
                    f"{len(check['missing'])} missing, {len(check['mismatched'])} "
                    "size mismatch(es). Re-run the upload to resume.",
                    result,
                )
                return

            upload = result["upload"]
            check = result["verification"]
            provenance = result["provenance"]
            self.completed.emit(
                True, False,
                f"Uploaded {upload['uploaded']} object(s) "
                f"({upload['skipped']} already present), verified all "
                f"{check['checked']} object(s), and recorded "
                f"{provenance.changed_rows} Box metadata row(s) across "
                f"{provenance.changed_files} deployment event(s). Keep Box Drive "
                "running until the updated metadata and receipts finish syncing.",
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
            self.completed.emit(False, False, message, exc.result)
        except Exception as exc:
            self.completed.emit(False, False, f"SoundHub upload failed: {exc}", {})
