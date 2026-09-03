"""Streaming, hash-verified downloads from Box into NDP scratch space."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cassn.ndp.transfer import NdpTransferError, TransferFile


class TransferCancelled(NdpTransferError):
    """The caller requested cancellation between durable transfer steps."""


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    bytes_written: int
    skipped: bool


def _sha256(path: Path, *, chunk_size: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_box_file(
    client,
    source: TransferFile,
    destination: Path,
    *,
    chunk_size: int = 8 * 1024 * 1024,
    progress=None,
    is_cancelled=None,
) -> DownloadResult:
    """Stream one Box file to scratch and publish it only after SHA-256 matches."""
    destination = Path(destination)
    if destination.exists():
        if not destination.is_file():
            raise NdpTransferError(f"scratch destination is not a file: {destination}")
        if destination.stat().st_size != source.size:
            raise NdpTransferError(
                f"existing scratch file has the wrong size: {destination}"
            )
        if _sha256(destination, chunk_size=chunk_size) != source.sha256:
            raise NdpTransferError(
                f"existing scratch file has the wrong SHA-256: {destination}"
            )
        return DownloadResult(destination, 0, True)

    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".partial", dir=destination.parent
    )
    partial = Path(tmp_name)
    digest = hashlib.sha256()
    written = 0
    stream = None
    try:
        stream = client.downloads.download_file(source.file_id)
        if stream is None:
            raise NdpTransferError(f"Box did not return content for {source.filename}")
        with os.fdopen(fd, "wb") as output:
            while True:
                if is_cancelled and is_cancelled():
                    raise TransferCancelled(
                        f"cancelled while downloading {source.filename}"
                    )
                chunk = stream.read(chunk_size)
                if not chunk:
                    break
                output.write(chunk)
                digest.update(chunk)
                written += len(chunk)
                if progress:
                    progress(source.filename, written, source.size)
            output.flush()
            os.fsync(output.fileno())
        if written != source.size:
            raise NdpTransferError(
                f"{source.filename}: downloaded {written} bytes; expected {source.size}"
            )
        if digest.hexdigest() != source.sha256:
            raise NdpTransferError(
                f"{source.filename}: downloaded SHA-256 does not match metadata"
            )
        os.replace(partial, destination)
        return DownloadResult(destination, written, False)
    finally:
        if stream is not None:
            try:
                stream.close()
            except AttributeError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass
        partial.unlink(missing_ok=True)
