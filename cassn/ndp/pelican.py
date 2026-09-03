"""Minimal subprocess boundary around the Pelican command-line client."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cassn.ndp.transfer import (
    NdpTransferError,
    TransferFile,
    normalize_destination_root,
)


@dataclass(frozen=True)
class PelicanResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""


class PelicanRunner:
    """Run Pelican without a shell, keeping authentication outside the app."""

    def __init__(
        self, executable: str = "pelican", token: Path | None = None, *, run=None
    ):
        self.executable = executable
        self.token = Path(token).expanduser() if token else None
        self._run = run or subprocess.run

    def require_available(self) -> None:
        if shutil.which(self.executable) is None:
            raise NdpTransferError(
                f"Pelican executable was not found: {self.executable}"
            )
        if self.token is not None and not self.token.is_file():
            raise NdpTransferError(f"Pelican token file does not exist: {self.token}")

    def _token_args(self) -> list[str]:
        return ["--token", str(self.token)] if self.token else []

    def sync_directory(self, source: Path, destination: str) -> PelicanResult:
        """Run an interactive sync so CILogon prompts remain visible."""
        source = Path(source)
        if not source.is_dir():
            raise NdpTransferError(f"Pelican sync source is not a directory: {source}")
        destination = normalize_destination_root(destination)
        command = [
            self.executable,
            "object",
            "sync",
            str(source),
            destination,
            *self._token_args(),
        ]
        completed = self._run(command, text=True)
        result = PelicanResult(tuple(command), int(completed.returncode))
        if result.returncode:
            raise NdpTransferError(
                "Pelican sync stopped before verification; rerun the same transfer to resume"
            )
        return result

    def stat_object(self, object_url: str) -> object:
        """Return raw JSON from ``object stat``; interpretation awaits the live spike."""
        # An object URL is allowed below a valid root, but must retain the same
        # scheme/path guarantees.  normalize_destination_root applies those
        # guarantees equally to collection and object paths.
        object_url = normalize_destination_root(object_url)
        command = [
            self.executable,
            "object",
            "stat",
            object_url,
            "--checksums",
            "sha",
            "--json",
            *self._token_args(),
        ]
        completed = self._run(command, text=True, capture_output=True)
        if completed.returncode:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise NdpTransferError(f"Pelican stat failed for {object_url}: {detail}")
        try:
            return json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise NdpTransferError(
                f"Pelican stat returned invalid JSON for {object_url}"
            ) from exc


def _stat_mapping(value: object) -> dict:
    """Return the single object-stat mapping without guessing through ambiguity."""
    if isinstance(value, list):
        if len(value) != 1:
            raise NdpTransferError(
                f"Pelican stat returned {len(value)} records for one object"
            )
        return _stat_mapping(value[0])
    if not isinstance(value, dict):
        raise NdpTransferError("Pelican stat JSON is not an object")
    for key in ("result", "object", "stat"):
        nested = value.get(key)
        if isinstance(nested, (dict, list)):
            return _stat_mapping(nested)
    return value


def verify_stat(source: TransferFile, raw: object) -> dict:
    """Check remote size and use a returned SHA digest when its meaning is clear.

    Pelican's live checksum response remains a protocol-spike item.  Until that
    is observed, an absent or unfamiliar checksum is reported as ``size_only``
    rather than being promoted to cryptographic verification.
    """
    record = _stat_mapping(raw)
    remote_size = record.get("size", record.get("Size"))
    try:
        remote_size = int(remote_size)
    except (TypeError, ValueError) as exc:
        raise NdpTransferError(
            f"Pelican stat returned no usable size for {source.filename}"
        ) from exc
    if remote_size != source.size:
        raise NdpTransferError(
            f"{source.filename}: Pelican size {remote_size} disagrees with metadata "
            f"size {source.size}"
        )

    checksum = ""
    checksums = record.get("checksums", record.get("Checksums", {}))
    if isinstance(checksums, dict):
        for key, value in checksums.items():
            if str(key).lower() in {"sha", "sha1", "sha-1", "sha256", "sha-256"}:
                checksum = str(value or "").strip().lower()
                break
    elif isinstance(checksums, list):
        candidates = []
        for item in checksums:
            if not isinstance(item, dict):
                continue
            algorithm = str(item.get("algorithm") or item.get("type") or "").lower()
            if algorithm in {"sha", "sha1", "sha-1", "sha256", "sha-256"}:
                candidates.append(str(item.get("value") or item.get("checksum") or ""))
        if len(candidates) == 1:
            checksum = candidates[0].strip().lower()

    expected = ""
    if len(checksum) == 64:
        expected = source.sha256
    elif len(checksum) == 40 and source.box_sha1:
        expected = source.box_sha1
    if expected and checksum != expected:
        raise NdpTransferError(f"{source.filename}: Pelican checksum does not match")
    return {
        "filename": source.filename,
        "size": remote_size,
        "verification": "checksum" if expected else "size_only",
        "checksum": checksum,
    }
