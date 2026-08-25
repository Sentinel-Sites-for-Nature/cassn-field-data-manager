"""
Wildlife SoundHub submission pipeline — bird (BD) audio only.

Box keeps the AudioMoth WAVs as the archival original; SoundHub gets a lossless
FLAC transcode plus two project-level CSVs. The split happens here, downstream
of the app's normal ingest, so nothing in this package ever modifies a source
WAV or the deployment's own metadata beyond the submission provenance columns.

Layers, bottom up:

* :mod:`cassn.soundhub.staging` — WAV to FLAC, into a local tree that mirrors
  the S3 key layout exactly;
* :mod:`cassn.soundhub.export` — ``deployment.csv`` and ``recording.csv``,
  projected from ``audio_file_metadata.csv``;
* :mod:`cassn.soundhub.upload` — the boto3 push and its verification.
* :mod:`cassn.soundhub.lifecycle` — guarded rollover of completed local batches.
* :mod:`cassn.soundhub.submission` — the shared safe preflight-to-report
  workflow used by both the CLI and GUI.

None of these import Qt. The GUI wrappers live in
:mod:`cassn.gui.soundhub_thread`.
"""

from cassn.soundhub.export import (
    build_deployment_rows,
    build_recording_rows,
    read_bd_audio_rows,
    refresh_project_csvs,
    write_deployment_copy,
    write_deployment_fragments,
)
from cassn.soundhub.staging import (
    SoundHubStagingError,
    flac_available,
    project_root,
    stage_deployment,
    validate_deployment_id,
)
from cassn.soundhub.submission import (
    SoundHubSubmissionError,
    execute_soundhub_submission,
    plan_soundhub_submission,
)
from cassn.soundhub.upload import (
    SoundHubUploadError,
    boto3_available,
    load_soundhub_config,
    upload_project,
    verify_project,
)

__all__ = [
    "SoundHubStagingError",
    "SoundHubSubmissionError",
    "SoundHubUploadError",
    "boto3_available",
    "build_deployment_rows",
    "build_recording_rows",
    "execute_soundhub_submission",
    "flac_available",
    "load_soundhub_config",
    "project_root",
    "plan_soundhub_submission",
    "read_bd_audio_rows",
    "refresh_project_csvs",
    "stage_deployment",
    "upload_project",
    "validate_deployment_id",
    "verify_project",
    "write_deployment_copy",
    "write_deployment_fragments",
]
