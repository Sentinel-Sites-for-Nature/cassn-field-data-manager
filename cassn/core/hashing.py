"""
File hashing — one primitive instead of three.

The original carried ``compute_file_hash`` (SHA-256), ``compute_file_hashes``
(both, single pass) and ``compute_file_sha1`` (SHA-1). They read each file up
to three times and could drift apart. This collapses them into one streaming
pass over the file with thin, well-named wrappers for the original call sites.

Block size does not affect a digest, so unifying every reader to 64 KiB blocks
produces byte-identical hashes to the original code.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK_SIZE = 65536  # 64 KiB — faster than 4 KiB on large files


def hash_file(filepath, algorithms: tuple[str, ...] = ("sha256", "sha1")) -> dict[str, str]:
    """Compute one or more digests of a file in a single streaming pass.

    Returns ``{algorithm_name: hexdigest}`` for each requested algorithm.
    """
    hashers = {name: hashlib.new(name) for name in algorithms}
    with open(filepath, "rb") as f:
        for block in iter(lambda: f.read(_CHUNK_SIZE), b""):
            for h in hashers.values():
                h.update(block)
    return {name: h.hexdigest() for name, h in hashers.items()}


def sha256_sha1(filepath) -> tuple[str, str]:
    """Return ``(sha256_hex, sha1_hex)`` computed in one read pass."""
    digests = hash_file(filepath, ("sha256", "sha1"))
    return digests["sha256"], digests["sha1"]


def sha256(filepath) -> str:
    """SHA-256 hex digest (primary archival checksum)."""
    return hash_file(filepath, ("sha256",))["sha256"]


def sha1(filepath) -> str:
    """SHA-1 hex digest (matches Box's server-side hash for fixity checks)."""
    return hash_file(filepath, ("sha1",))["sha1"]
