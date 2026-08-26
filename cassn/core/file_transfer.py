"""Crash-safe, hash-verified file transfer primitives.

SD cards and external drives can raise transient ``OSError``/I/O failures.  A
transfer must therefore never replace the staged destination until a complete
temporary copy has been hashed and verified.
"""

from __future__ import annotations

import os
import hashlib
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from cassn.core.hashing import sha256_sha1


@dataclass(frozen=True)
class FileHashes:
    sha256: str
    sha1: str
    attempts: int


@dataclass(frozen=True)
class VerifiedCopy:
    """Hashes from a one-source-read verified copy and its accept decision."""

    sha256: str
    sha1: str
    attempts: int
    accepted: bool


class FileTransferError(OSError):
    """A source could not be read or a verified destination could not be made."""


def copy_file_single_read_verified(
    source,
    destination,
    *,
    accept_hash=None,
    release_hash=None,
    max_attempts: int = 3,
    retry_delay: float = 0.5,
    chunk_size: int = 1024 * 1024,
) -> VerifiedCopy:
    """Copy while hashing the source, then verify the staged bytes once.

    The historical path first hashed the entire source and then asked
    ``shutil.copy2`` to read it again. This streams source bytes into the
    temporary destination while calculating both hashes, cutting healthy-card
    source I/O in half. ``accept_hash`` runs only after the temporary copy has
    verified and can atomically reject a session-wide duplicate before commit.
    """
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f".{destination.name}.{os.getpid()}.partial")
    last_error: OSError | None = None
    mismatch = False

    for attempt in range(1, max_attempts + 1):
        partial.unlink(missing_ok=True)
        reserved = False
        source_sha256 = hashlib.sha256()
        source_sha1 = hashlib.sha1()
        try:
            with source.open("rb") as src, partial.open("wb") as dst:
                while True:
                    chunk = src.read(chunk_size)
                    if not chunk:
                        break
                    source_sha256.update(chunk)
                    source_sha1.update(chunk)
                    dst.write(chunk)
            shutil.copystat(source, partial)

            sha256_value = source_sha256.hexdigest()
            sha1_value = source_sha1.hexdigest()
            staged_sha256, staged_sha1 = sha256_sha1(partial)
            if staged_sha256 != sha256_value or staged_sha1 != sha1_value:
                mismatch = True
                continue

            if accept_hash is not None:
                reserved = bool(accept_hash(sha256_value, sha1_value))
                if not reserved:
                    return VerifiedCopy(
                        sha256_value, sha1_value, attempt, accepted=False
                    )
            partial.replace(destination)
            return VerifiedCopy(sha256_value, sha1_value, attempt, accepted=True)
        except OSError as exc:
            last_error = exc
            if reserved and release_hash is not None:
                release_hash(source_sha256.hexdigest(), source_sha1.hexdigest())
        finally:
            partial.unlink(missing_ok=True)

        if attempt < max_attempts and retry_delay:
            time.sleep(retry_delay)

    if last_error is not None:
        raise FileTransferError(
            f"I/O failure copying {source} to {destination} after "
            f"{max_attempts} attempts: {last_error}"
        ) from last_error
    if mismatch:
        raise FileTransferError(
            f"Hash verification failed copying {source} to {destination} after "
            f"{max_attempts} attempts"
        )
    raise FileTransferError(f"Could not copy {source} to {destination}")


def hash_file_with_retries(
    path,
    *,
    max_attempts: int = 3,
    retry_delay: float = 0.5,
) -> FileHashes:
    """Hash ``path``, retrying transient OS-level read failures."""
    path = Path(path)
    last_error: OSError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            sha256, sha1 = sha256_sha1(path)
            return FileHashes(sha256, sha1, attempt)
        except OSError as exc:
            last_error = exc
            if attempt < max_attempts and retry_delay:
                time.sleep(retry_delay)

    detail = f": {last_error}" if last_error else ""
    raise FileTransferError(
        f"Could not read {path} after {max_attempts} attempts{detail}"
    ) from last_error


def copy_file_verified(
    source,
    destination,
    *,
    expected_sha256: str,
    expected_sha1: str,
    max_attempts: int = 3,
    retry_delay: float = 0.5,
) -> FileHashes:
    """Copy to a sibling temporary file and atomically commit after verification.

    An existing destination is never removed or modified unless the temporary
    copy is complete and both hashes match.  This makes an interrupted retry
    safe even when the destination belongs to an existing inventory record.
    """
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f".{destination.name}.{os.getpid()}.partial")
    last_error: OSError | None = None
    mismatch = False

    for attempt in range(1, max_attempts + 1):
        partial.unlink(missing_ok=True)
        try:
            shutil.copy2(source, partial)
            actual_sha256, actual_sha1 = sha256_sha1(partial)
            if actual_sha256 == expected_sha256 and actual_sha1 == expected_sha1:
                partial.replace(destination)
                return FileHashes(actual_sha256, actual_sha1, attempt)
            mismatch = True
        except OSError as exc:
            last_error = exc
        finally:
            partial.unlink(missing_ok=True)

        if attempt < max_attempts and retry_delay:
            time.sleep(retry_delay)

    if last_error is not None:
        raise FileTransferError(
            f"I/O failure copying {source} to {destination} after "
            f"{max_attempts} attempts: {last_error}"
        ) from last_error
    if mismatch:
        raise FileTransferError(
            f"Hash verification failed copying {source} to {destination} after "
            f"{max_attempts} attempts"
        )
    raise FileTransferError(f"Could not copy {source} to {destination}")
