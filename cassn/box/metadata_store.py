"""Version-pinned, checksum-verified access to small Box sidecars."""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass


@dataclass(frozen=True)
class BoxFileSnapshot:
    file_id: str
    parent_id: str
    name: str
    etag: str
    version_id: str
    sha1: str
    size: int
    can_upload: bool


class BoxFileStore:
    FILE_FIELDS = [
        "id", "name", "parent", "etag", "file_version", "sha1", "size", "permissions",
    ]

    def __init__(self, client):
        self.client = client

    def snapshot(self, file_id: str) -> BoxFileSnapshot:
        item = self.client.files.get_file_by_id(file_id, fields=self.FILE_FIELDS)
        if (item.etag is None or not item.sha_1 or not item.file_version
                or not item.parent or item.size is None):
            raise ValueError(f"Box file {file_id} is missing version/fixity information")
        return BoxFileSnapshot(
            item.id, item.parent.id, item.name, str(item.etag),
            item.file_version.id, item.sha_1.lower(), item.size,
            bool(item.permissions and item.permissions.can_upload),
        )

    def read(self, ref: BoxFileSnapshot) -> bytes:
        stream = self.client.downloads.download_file(ref.file_id, version=ref.version_id)
        if stream is None:
            raise ValueError(f"Box file {ref.file_id} returned no content")
        with stream:
            payload = stream.read()
        if len(payload) != ref.size or hashlib.sha1(payload).hexdigest() != ref.sha1:
            raise ValueError(f"Box file {ref.file_id} failed its download checksum/size check")
        return payload

    def assert_unchanged(self, ref: BoxFileSnapshot) -> None:
        if self.snapshot(ref.file_id) != ref:
            raise ValueError(f"Box file {ref.file_id} changed after preflight; rebuild the metadata plan")

    def _verify_write(self, file_id: str, payload: bytes, parent_id: str, name: str) -> None:
        current = self.snapshot(file_id)
        if (current.parent_id != parent_id or current.name != name
                or current.size != len(payload)
                or current.sha1 != hashlib.sha1(payload).hexdigest()):
            raise ValueError(f"Box file {file_id} failed verification after upload")

    def write_metadata(self, ref: BoxFileSnapshot, payload: bytes) -> None:
        from box_sdk_gen import UploadFileVersionAttributes

        self.client.uploads.upload_file_version(
            ref.file_id, UploadFileVersionAttributes(name=ref.name), io.BytesIO(payload),
            if_match=ref.etag,
        )
        self._verify_write(ref.file_id, payload, ref.parent_id, ref.name)
