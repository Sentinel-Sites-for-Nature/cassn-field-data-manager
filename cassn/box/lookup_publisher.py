"""Publish a validated Survey123 lookup pair to canonical Box files."""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

from cassn.box.client import BoxStorage
from cassn.lookup_sync import DEVICE_PAIR_FILENAMES, validate_device_lookup_pair


class LookupPublicationError(RuntimeError):
    """The canonical Box pair could not be safely published."""


@dataclass(frozen=True)
class PublishedLookupFile:
    name: str
    file_id: str
    action: str


def _download_bytes(client, file_id: str) -> bytes:
    return b"".join(client.downloads.download_file(file_id))


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _upload_version(client, file_id: str, name: str, content: bytes) -> None:
    client.uploads.upload_file_version(
        file_id,
        attributes={"name": name},
        file=io.BytesIO(content),
    )


def _upload_new(client, folder_id: str, name: str, content: bytes) -> str:
    result = client.uploads.upload_file(
        attributes={"name": name, "parent": {"id": folder_id}},
        file=io.BytesIO(content),
    )
    try:
        return str(result.entries[0].id)
    except Exception as exc:
        raise LookupPublicationError(
            f"Box did not return a file ID for new {name}"
        ) from exc


def publish_validated_lookup_pair(
    client,
    folder_id: str,
    source_dir: Path,
) -> tuple[PublishedLookupFile, ...]:
    """Version/create the canonical Box pair and verify its downloaded hashes.

    The pair is validated before the first remote write. Existing canonical
    files are updated as new Box versions. If the second update or verification
    fails, prior bytes are restored as another version; newly created files are
    removed.
    """
    source_dir = source_dir.resolve()
    validation = validate_device_lookup_pair(
        source_dir / "devices.csv",
        source_dir / "deployments.csv",
    )
    content = {name: (source_dir / name).read_bytes() for name in DEVICE_PAIR_FILENAMES}

    storage = BoxStorage(client)
    existing = storage.folder_file_map(folder_id)
    previous = {
        name: _download_bytes(client, existing[name]) if name in existing else None
        for name in DEVICE_PAIR_FILENAMES
    }

    committed: list[PublishedLookupFile] = []
    try:
        # deployments.csv is normally the existing obsolete file. Version it
        # first so a subsequent devices.csv creation failure can be rolled back
        # without leaving the legacy deployment schema irrecoverable.
        for name in ("deployments.csv", "devices.csv"):
            prior_id = existing.get(name)
            if prior_id:
                _upload_version(client, prior_id, name, content[name])
                published = PublishedLookupFile(name, str(prior_id), "versioned")
            else:
                file_id = _upload_new(client, folder_id, name, content[name])
                published = PublishedLookupFile(name, file_id, "created")
            committed.append(published)

        for published in committed:
            downloaded = _download_bytes(client, published.file_id)
            if _sha256_bytes(downloaded) != validation.hashes[published.name]:
                raise LookupPublicationError(
                    f"Box hash verification failed for {published.name}"
                )
        return tuple(sorted(committed, key=lambda item: item.name))
    except Exception as publish_exc:
        rollback_errors: list[str] = []
        for published in reversed(committed):
            try:
                prior = previous[published.name]
                if prior is None:
                    client.files.delete_file_by_id(published.file_id)
                else:
                    _upload_version(
                        client,
                        published.file_id,
                        published.name,
                        prior,
                    )
            except Exception as rollback_exc:
                rollback_errors.append(f"{published.name}: {rollback_exc}")
        detail = f"Could not publish validated Box lookup pair: {publish_exc}"
        if rollback_errors:
            detail += "; rollback errors: " + "; ".join(rollback_errors)
        raise LookupPublicationError(detail) from publish_exc
