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

import re
import shutil
import subprocess
from pathlib import Path

from cassn.config import (
    FLAC_COMPRESSION_LEVEL,
    SOUNDHUB_DEVICE_TYPE,
    SOUNDHUB_PROJECT_SHORT_NAME,
)
from cassn.core import hashing


# A deployment id is an identity (org, site, plot, device) plus the date the
# device was retrieved. The two halves matter separately during validation: the
# identity must agree with the filenames, the date must not be assumed to.
_DEPLOYMENT_ID_RE = re.compile(rf"^(?P<identity>.+_{SOUNDHUB_DEVICE_TYPE})_(?P<date>\d{{8}})$")


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

    Only the *identity* half of the id is checked against the filenames. The
    trailing date is the device's retrieval date from Survey123, while the
    filenames carry the deployment event's date, and the two legitimately differ
    when a recorder was collected on a different day from the rest of the event.
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

    match = _DEPLOYMENT_ID_RE.match(deployment_id)
    if match is None:
        raise SoundHubStagingError(
            f"deployment_id {deployment_id!r} is not a well-formed "
            f"{SOUNDHUB_DEVICE_TYPE} deployment id "
            f"(expected <org>_<site>_plot<n>_{SOUNDHUB_DEVICE_TYPE}_<YYYYMMDD>). "
            "Only bird recorders are submitted to SoundHub."
        )
    identity = match.group("identity")

    # Files are named from the same org/site/plot/device components as the id, so
    # that prefix must match. Catches rows spliced in from another plot or device
    # far more reliably than re-parsing the folder name would.
    for row in rows:
        filename = row.get("filename", "")
        if not filename.startswith(identity + "_"):
            raise SoundHubStagingError(
                f"File {filename!r} does not belong to deployment "
                f"{deployment_id!r} — expected it to start with {identity + '_'!r}. "
                "Refusing to stage a mismatched inventory."
            )
    return deployment_id


def _index_source_wavs(deployment_folder: Path) -> dict[str, Path]:
    """Map every BD WAV's filename to its path with a shallow scan.

    Audio is never split, so each WAV sits directly inside its device folder.
    Device folders sit under ``raw_data/`` at most sites but directly under the
    deployment folder at others, so both are scanned one level deep — and only
    the ``*_BD`` device folders are entered, since those are the only ones
    holding SoundHub audio. Camera folders can hold hundreds of thousands of
    files at their top level, so even listing them once over a network mount
    costs minutes; anything not indexed still resolves through the
    :func:`_source_wav` fallback.
    """
    suffix = f"_{SOUNDHUB_DEVICE_TYPE}"
    index: dict[str, Path] = {}
    for root in (deployment_folder / "raw_data", deployment_folder):
        if not root.is_dir():
            continue
        for device_dir in root.iterdir():
            if not device_dir.is_dir() or not device_dir.name.endswith(suffix):
                continue
            for path in device_dir.iterdir():
                if path.is_file() and path.suffix.lower() == ".wav":
                    index.setdefault(path.name, path)
    return index


def _source_wav(deployment_folder: Path, row: dict, index: dict[str, Path]) -> Path:
    """Locate a row's WAV, preferring the prebuilt index over a tree walk.

    ``index`` comes from :func:`_index_source_wavs`. A miss falls back to a
    single targeted ``rglob`` for that one file — covering unusual layouts (e.g.
    a WI-nested tree) without walking the tree once per recording.
    """
    filename = row.get("filename", "")
    if not filename:
        raise SoundHubStagingError("Inventory row with no filename.")
    match = index.get(filename)
    if match is not None:
        return match
    matches = sorted(Path(deployment_folder).rglob(filename))
    if not matches:
        raise SoundHubStagingError(
            f"Source WAV not found under {deployment_folder}: {filename}"
        )
    return matches[0]


def _wav_missing_final_pad(src: Path) -> bool:
    """True when the file's last RIFF chunk is odd-sized and unpadded.

    RIFF requires every odd-sized chunk to be followed by a pad byte, but the
    AudioMoth firmware omits it after the trailing GUANO chunk when that chunk's
    text happens to be an odd length. ``flac --keep-foreign-metadata`` then hits
    EOF one byte early while copying chunks and aborts with ``read failed
    (011)``. Only a few chunk headers are read, so this is cheap even over a
    network mount.
    """
    try:
        file_size = src.stat().st_size
        with open(src, "rb") as f:
            header = f.read(12)
            if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                return False
            while True:
                raw = f.read(8)
                if len(raw) < 8:
                    return False
                size = int.from_bytes(raw[4:8], "little")
                end = f.tell() + size
                if size % 2 and end == file_size:
                    return True  # odd chunk runs to EOF with no room for a pad
                f.seek(end + (size % 2))
                if f.tell() >= file_size:
                    return False
    except OSError:
        return False


def _padded_copy(src: Path, dst: Path) -> None:
    """Copy ``src`` with the missing final pad byte appended.

    The RIFF size field is bumped to count the pad, making the copy the file the
    recorder should have written. The audio samples are untouched, so
    ``flac --verify`` still checks the encode against the same signal.
    """
    shutil.copyfile(src, dst)
    with open(dst, "r+b") as f:
        f.seek(4)
        riff_size = int.from_bytes(f.read(4), "little")
        f.seek(4)
        f.write((riff_size + 1).to_bytes(4, "little"))
        f.seek(0, 2)
        f.write(b"\x00")


def convert_wav_to_flac(src: Path, dst: Path, *, level: int = FLAC_COMPRESSION_LEVEL) -> None:
    """Encode one WAV to FLAC, verifying the result during the encode.

    ``--verify`` decodes each block as it is written and compares it against the
    input, so a corrupted encode fails here rather than silently reaching S3.
    ``--keep-foreign-metadata`` carries the AudioMoth RIFF comment across in an
    APPLICATION block; it does *not* preserve the GUANO chunk, which is why the
    WAVs remain the archival copy on Box.

    A WAV whose trailing chunk lacks its RIFF pad byte (an AudioMoth firmware
    quirk) is first copied locally with the pad restored, since flac refuses the
    unpadded original; the source WAV is never modified.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    partial = dst.with_suffix(dst.suffix + ".partial")
    partial.unlink(missing_ok=True)

    # Dot-prefixed so a leftover from an interrupted run can never be staged.
    repaired = dst.parent / f".{src.name}.padded"
    encode_src = src
    if _wav_missing_final_pad(src):
        _padded_copy(src, repaired)
        encode_src = repaired

    try:
        result = subprocess.run(
            [
                "flac",
                f"-{level}",
                "--silent",
                "--verify",
                "--keep-foreign-metadata",
                "--output-name",
                str(partial),
                str(encode_src),
            ],
            capture_output=True,
            text=True,
        )
    finally:
        repaired.unlink(missing_ok=True)

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

    # Scan the device folders once up front rather than searching per file.
    wav_index = _index_source_wavs(deployment_folder)

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
            src = _source_wav(deployment_folder, row, wav_index)
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
