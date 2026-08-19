"""
WAV to FLAC staging for SoundHub.

Produces a local tree that mirrors the S3 key layout one-for-one, so the upload
is a plain recursive walk with no path rewriting::

    <staging_root>/UCNature-SSN/deployment.csv
    <staging_root>/UCNature-SSN/recording.csv
    <staging_root>/UCNature-SSN/<deployment_id>/<filename>.flac

Everything that is *not* destined for S3 — the per-deployment CSV fragments the
project-level CSVs are rebuilt from — lives beside the project directory under
``.cassn_fragments/``, never inside it. That keeps the project directory an
exact mirror of the bucket prefix.

Source WAVs are never modified. Conversion is idempotent: an existing FLAC whose
recorded size matches the manifest is left alone, so an interrupted run is
finished by re-running it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from cassn.config import (
    FLAC_COMPRESSION_LEVEL,
    SOUNDHUB_DEVICE_TYPE,
    SOUNDHUB_PROJECT_SHORT_NAME,
)
from cassn.core import hashing


class SoundHubStagingError(Exception):
    """A staging precondition failed. Raised before any file is written."""


def flac_available() -> bool:
    """True when the ``flac`` encoder is on PATH."""
    return shutil.which("flac") is not None


def project_root(staging_root) -> Path:
    """The directory that mirrors ``s3://casoundhub/upload/UCNature-SSN/``."""
    return Path(staging_root) / SOUNDHUB_PROJECT_SHORT_NAME


def fragments_root(staging_root) -> Path:
    """Per-deployment CSV fragments, deliberately outside the project mirror."""
    return Path(staging_root) / ".cassn_fragments"


def validate_deployment_id(rows: list[dict]) -> str:
    """Return the single ``deployment_id`` shared by one group of BD rows.

    ``audio_file_metadata.csv`` carries the deployment id straight from the
    Survey123 device row, which is the only authority for it — this deliberately
    does not re-derive the id from folder or file names. What it does check is
    that the value is internally consistent and correctly shaped, because a
    malformed id becomes an S3 prefix that cannot be deleted afterwards.
    """
    ids = {r.get("deployment_id", "") for r in rows}
    if not ids:
        raise SoundHubStagingError("No BD audio rows to stage.")
    if len(ids) > 1:
        raise SoundHubStagingError(
            "Rows grouped under one deployment carry different deployment_id "
            "values: " + ", ".join(sorted(ids))
        )

    deployment_id = ids.pop()
    if not deployment_id:
        raise SoundHubStagingError(
            "Blank deployment_id in audio_file_metadata.csv — the Survey123 "
            "device row is missing. Refresh the lookups and regenerate metadata."
        )

    expected_suffix = f"_{SOUNDHUB_DEVICE_TYPE}_"
    if expected_suffix not in deployment_id:
        raise SoundHubStagingError(
            f"deployment_id {deployment_id!r} is not a {SOUNDHUB_DEVICE_TYPE} "
            "deployment. Only bird recorders are submitted to SoundHub."
        )

    # Every file in a deployment is named from the same components as the id, so
    # the id must prefix each filename. Catches rows spliced in from another
    # device far more reliably than re-parsing the folder name would.
    for row in rows:
        filename = row.get("filename", "")
        if not filename.startswith(deployment_id + "_"):
            raise SoundHubStagingError(
                f"File {filename!r} does not belong to deployment "
                f"{deployment_id!r}. Refusing to stage a mismatched inventory."
            )
    return deployment_id


def _source_wav(deployment_folder: Path, row: dict) -> Path:
    """Locate a row's WAV under the deployment folder.

    Device folders sit under ``raw_data/`` at most sites but directly under the
    deployment folder at others, and the WI split can nest camera files further
    down. Audio is never split, but the search stays general rather than assuming
    one layout.
    """
    filename = row.get("filename", "")
    if not filename:
        raise SoundHubStagingError("Inventory row with no filename.")
    matches = sorted(Path(deployment_folder).rglob(filename))
    if not matches:
        raise SoundHubStagingError(
            f"Source WAV not found under {deployment_folder}: {filename}"
        )
    return matches[0]


def convert_wav_to_flac(src: Path, dst: Path, *, level: int = FLAC_COMPRESSION_LEVEL) -> None:
    """Encode one WAV to FLAC, verifying the result during the encode.

    ``--verify`` decodes each block as it is written and compares it against the
    input, so a corrupted encode fails here rather than silently reaching S3.
    ``--keep-foreign-metadata`` carries the AudioMoth RIFF comment across in an
    APPLICATION block; it does *not* preserve the GUANO chunk, which is why the
    WAVs remain the archival copy on Box.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    partial = dst.with_suffix(dst.suffix + ".partial")
    partial.unlink(missing_ok=True)

    result = subprocess.run(
        [
            "flac",
            f"-{level}",
            "--silent",
            "--verify",
            "--keep-foreign-metadata",
            "--output-name",
            str(partial),
            str(src),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        partial.unlink(missing_ok=True)
        raise SoundHubStagingError(
            f"flac failed on {src.name}: {result.stderr.strip() or 'unknown error'}"
        )
    # Publish atomically so an interrupted run never leaves a truncated .flac
    # that a later run would mistake for finished work.
    partial.replace(dst)


def stage_deployment(
    deployment_folder,
    audio_rows: list[dict],
    staging_root,
    *,
    progress=None,
    is_cancelled=None,
) -> dict:
    """Convert a deployment event's BD WAVs into the SoundHub staging tree.

    One CA-SSN deployment *event* covers several plots, and each plot's recorder
    is a separate SoundHub *deployment* with its own id and its own S3 folder —
    so ``audio_rows`` is grouped by ``deployment_id`` and each group staged
    independently.

    ``audio_rows`` are the BD audio rows of ``audio_file_metadata.csv``. Returns
    a summary carrying the staged deployment ids, each staged file with its FLAC
    SHA-256, and counts of converted/skipped files.
    """
    deployment_folder = Path(deployment_folder)
    if not flac_available():
        raise SoundHubStagingError(
            "The 'flac' encoder is not installed. Install it with: brew install flac"
        )
    if not audio_rows:
        raise SoundHubStagingError("No BD audio rows to stage.")

    groups: dict[str, list[dict]] = {}
    for row in audio_rows:
        groups.setdefault(row.get("deployment_id", ""), []).append(row)

    # Validate every group before converting anything, so a bad id in the last
    # plot cannot leave the first plot's FLACs half-written.
    deployment_ids = {key: validate_deployment_id(rows) for key, rows in groups.items()}

    staged: list[dict] = []
    converted = skipped = 0
    total = len(audio_rows)
    index = 0

    for key, rows in groups.items():
        deployment_id = deployment_ids[key]
        out_dir = project_root(staging_root) / deployment_id
        out_dir.mkdir(parents=True, exist_ok=True)

        for row in rows:
            if is_cancelled is not None and is_cancelled():
                return {
                    "deployment_ids": sorted(deployment_ids.values()),
                    "cancelled": True,
                    "converted": converted,
                    "skipped": skipped,
                    "staged": staged,
                    "staging_dir": project_root(staging_root),
                }

            index += 1
            src = _source_wav(deployment_folder, row)
            dst = out_dir / (src.stem + ".flac")

            if progress is not None:
                progress(index, total, src.name)

            if dst.exists():
                skipped += 1
            else:
                convert_wav_to_flac(src, dst)
                converted += 1

            staged.append(
                {
                    "filename": dst.name,
                    "source_filename": src.name,
                    "deployment_id": deployment_id,
                    "size_bytes": dst.stat().st_size,
                    "sha256": hashing.sha256(dst),
                }
            )

    return {
        "deployment_ids": sorted(deployment_ids.values()),
        "cancelled": False,
        "converted": converted,
        "skipped": skipped,
        "staged": staged,
        "staging_dir": project_root(staging_root),
    }
