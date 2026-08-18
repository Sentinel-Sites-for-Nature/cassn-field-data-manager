"""Crash-safe, hash-verified file transfer primitives.

SD cards and external drives can raise transient ``OSError``/I/O failures.  A
transfer must therefore never replace the staged destination until a complete
temporary copy has been hashed and verified.
"""

from __future__ import annotations

import os
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


class FileTransferError(OSError):
    """A source could not be read or a verified destination could not be made."""


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
