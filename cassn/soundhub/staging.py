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

Source WAVs are never modified. Unfinished conversions stay in .cassn_work,
outside the project mirror. Re-running reuses completed FLACs there.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
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
_DEPLOYMENT_ID_RE = re.compile(
    rf"^(?P<identity>.+_{SOUNDHUB_DEVICE_TYPE})_(?P<date>\d{{8}})"
    r"(?:-seq\d{2,})?$"
)


class SoundHubStagingError(Exception):
    """Staging needs user attention before it can continue."""


def flac_executable() -> str | None:
    """Find FLAC even when a Finder-launched bundle has a minimal PATH."""
    encoder = shutil.which("flac")
    if encoder is None and sys.platform == "darwin":
        for candidate in ("/opt/homebrew/bin/flac", "/usr/local/bin/flac"):
            encoder = shutil.which(candidate)
            if encoder is not None:
                break
    return encoder


def flac_available() -> bool:
    """True when the encoder is on PATH or in a standard macOS Homebrew location."""
    return flac_executable() is not None


def project_root(staging_root) -> Path:
    """The directory that mirrors ``s3://casoundhub/upload/UCNature-SSN/``."""
    return Path(staging_root) / SOUNDHUB_PROJECT_SHORT_NAME


def fragments_root(staging_root) -> Path:
    """Per-deployment CSV fragments, deliberately outside the project mirror."""
    return Path(staging_root) / ".cassn_fragments"


def work_root(staging_root) -> Path:
    """Completed recordings from unfinished events, outside the upload tree."""
    return Path(staging_root) / ".cassn_work"


def staging_progress(deployment_folder, staging_root) -> tuple[int, int] | None:
    """Count resumable recordings without reading the audio or contacting Box."""
    from cassn.soundhub.export import read_bd_audio_rows

    work = work_root(staging_root)
    if not work.is_dir():
        return None
    try:
        rows = read_bd_audio_rows(deployment_folder)
    except FileNotFoundError:
        return None
    pending = work / "pending.json"
    pending_ids = set()
    if pending.exists():
        pending_ids = {
            row["deployment_id"] for row in json.loads(pending.read_text())["audio_rows"]
        }
    if not any((work / row["deployment_id"]).is_dir()
               or row["deployment_id"] in pending_ids for row in rows):
        return None
    completed = sum(
        any((base / row["deployment_id"] / (Path(row["filename"]).stem + ".flac")).is_file()
            for base in (work, project_root(staging_root)))
        for row in rows
    )
    return completed, len(rows)


def recover_staging(staging_root) -> dict | None:
    """Finish a recorded folder promotion and CSV rebuild, always going forward.

    Both normal completion and crash recovery use this sequence. A recorder
    folder moves in one rename. A previous version is retained until the new
    batch validates, allowing an existing deployment to be refreshed too.
    """
    from cassn.soundhub.export import (
        refresh_project_csvs, validate_staging_manifests, write_deployment_fragments,
    )

    work = work_root(staging_root)
    marker = work / "pending.json"
    if not marker.exists():
        return None
    try:
        if work.is_symlink() or marker.is_symlink() or project_root(staging_root).is_symlink():
            raise ValueError("staging recovery paths must not be symlinks")
        rows = json.loads(marker.read_text(encoding="utf-8"))["audio_rows"]
        groups: dict[str, list[dict]] = {}
        for row in rows:
            groups.setdefault(row["deployment_id"], []).append(row)
        if not groups:
            raise ValueError("empty recording list")
        for group in groups.values():
            deployment_id = validate_deployment_id(group)
            if Path(deployment_id).name != deployment_id:
                raise ValueError("invalid deployment folder")
        root = project_root(staging_root)
        root.mkdir(parents=True, exist_ok=True)
        for deployment_id, group in groups.items():
            source = work / deployment_id
            destination = root / deployment_id
            previous = work / (deployment_id + ".previous")
            media = source if source.exists() else destination
            expected = {Path(row["filename"]).stem + ".flac" for row in group}
            if media.is_symlink() or not media.is_dir():
                raise ValueError(f"completed recorder folder is missing or unsafe: {media}")
            if {path.name for path in media.iterdir() if not path.name.startswith(".")} != expected:
                raise ValueError(f"completed recordings differ from the inventory: {media}")
            if any(path.is_symlink() or not path.is_file() for path in media.iterdir()):
                raise ValueError(f"unsafe recording in {media}")
            if source.exists():
                if destination.exists():
                    if destination.is_symlink() or previous.exists():
                        raise ValueError(f"cannot replace recorder folder: {destination}")
                    destination.rename(previous)
                source.rename(destination)
        write_deployment_fragments(staging_root, rows)
        csvs = refresh_project_csvs(staging_root)
        validation = validate_staging_manifests(staging_root)
        for deployment_id in groups:
            previous = work / (deployment_id + ".previous")
            if previous.exists():
                shutil.rmtree(previous)
        marker.unlink()
        return {"csvs": csvs, "validation": validation}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SoundHubStagingError(
            f"Could not finish SoundHub staging. Retry after correcting this error: {exc}"
        ) from exc


def validate_deployment_id(rows: list[dict]) -> str:
    """Return the single ``deployment_id`` shared by one group of BD rows.

    ``audio_file_metadata.csv`` carries the deployment id straight from the
    curated device row, which is the only authority for it — this deliberately
    does not re-derive the id from folder or file names. What it does check is
    that the value is internally consistent and correctly shaped, because a
    malformed id becomes an S3 prefix that cannot be deleted afterwards.

    Only the *identity* half of the id is checked against the filenames. The
    trailing date is the device's curated retrieval date, while the
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
            "Blank deployment_id in audio_file_metadata.csv — the curated "
            "device row is missing. Refresh the lookups and regenerate metadata."
        )

    match = _DEPLOYMENT_ID_RE.match(deployment_id)
    if match is None:
        raise SoundHubStagingError(
            f"deployment_id {deployment_id!r} is not a well-formed "
            f"{SOUNDHUB_DEVICE_TYPE} deployment id "
            f"(expected <org>_<site>_plot<n>_{SOUNDHUB_DEVICE_TYPE}_<YYYYMMDD>"
            "[-seqNN]). "
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
    encoder = flac_executable()
    if encoder is None:
        raise SoundHubStagingError("Could not find the 'flac' encoder. Install it with: brew install flac")
    dst.parent.mkdir(parents=True, exist_ok=True)
    partial = dst.with_suffix(dst.suffix + ".partial")
    partial.unlink(missing_ok=True)

    # Dot-prefixed so a leftover from an interrupted run can never be staged.
    repaired = dst.parent / f".{src.name}.padded"
    encode_src = src
    try:
        if _wav_missing_final_pad(src):
            _padded_copy(src, repaired)
            encode_src = repaired
        result = subprocess.run(
            [
                encoder,
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
    recording_prepared=None,
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
    recover_staging(staging_root)
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
    if any(Path(value).name != value for value in deployment_ids.values()):
        raise SoundHubStagingError("Invalid SoundHub deployment folder name.")

    from cassn.soundhub.export import build_recording_rows, write_deployment_copy

    build_recording_rows(audio_rows)

    # Scan the device folders once up front rather than searching per file.
    wav_index = _index_source_wavs(deployment_folder)

    # FLAC size varies with the signal. Use WAV size as the output estimate,
    # plus one WAV and one FLAC for the largest remaining conversion.
    remaining_sizes = []
    for key, rows in groups.items():
        for row in rows:
            name = Path(row["filename"]).stem + ".flac"
            if not any((base / deployment_ids[key] / name).is_file()
                       for base in (work_root(staging_root), project_root(staging_root))):
                remaining_sizes.append(_source_wav(deployment_folder, row, wav_index).stat().st_size)
    work_root(staging_root).mkdir(parents=True, exist_ok=True)
    required = sum(remaining_sizes) + 2 * max(remaining_sizes, default=0)
    available = shutil.disk_usage(work_root(staging_root)).free
    if available < required:
        raise SoundHubStagingError(
            f"Not enough disk space for SoundHub staging. Estimated free space needed "
            f"is {required / 1e9:.2f} GB, including temporary files. "
            f"Available space is {available / 1e9:.2f} GB. "
            "Free disk space, then choose Continue SoundHub Staging."
        )

    staged: list[dict] = []
    converted = skipped = 0
    total = len(audio_rows)
    index = 0

    for key, rows in groups.items():
        deployment_id = deployment_ids[key]
        out_dir = work_root(staging_root) / deployment_id
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

            reused = dst.exists()
            if reused:
                skipped += 1
            else:
                existing = project_root(staging_root) / deployment_id / dst.name
                if existing.is_file():
                    # Preserve the current upload tree until the replacement
                    # folder is complete, without copying large recordings.
                    try:
                        os.link(existing, dst)
                    except OSError:
                        # Some removable disks cannot create hard links.
                        # Copy to a temporary name so a full disk is retryable.
                        partial = dst.with_suffix(".flac.partial")
                        shutil.copyfile(existing, partial)
                        partial.replace(dst)
                    reused = True
                    skipped += 1
                else:
                    convert_wav_to_flac(src, dst)
                    converted += 1

            if dst.is_symlink() or not dst.is_file():
                raise SoundHubStagingError(f"Unsafe staged recording: {dst}")
            if reused:
                check = subprocess.run(
                    [flac_executable(), "--test", "--silent", str(dst)],
                    capture_output=True, text=True,
                )
                if check.returncode != 0:
                    raise SoundHubStagingError(
                        f"Completed FLAC failed its integrity check: {dst.name}: {check.stderr.strip()}"
                    )
            dst.with_suffix(".flac.partial").unlink(missing_ok=True)
            (out_dir / f".{src.name}.padded").unlink(missing_ok=True)

            staged.append(
                {
                    "filename": dst.name,
                    "source_filename": src.name,
                    "deployment_id": deployment_id,
                    "size_bytes": dst.stat().st_size,
                    "sha256": hashing.sha256(dst),
                }
            )

            if recording_prepared is not None:
                recording_prepared(deployment_id, src.name)

    write_deployment_copy(deployment_folder, audio_rows)
    marker = work_root(staging_root) / "pending.json"
    temporary = marker.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump({"audio_rows": audio_rows}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(marker)
    completion = recover_staging(staging_root)
    return {
        "deployment_ids": sorted(deployment_ids.values()),
        "cancelled": False,
        "converted": converted,
        "skipped": skipped,
        "staged": staged,
        "staging_dir": project_root(staging_root),
        **completion,
    }
