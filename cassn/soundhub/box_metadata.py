"""Small SoundHub sidecars accessed through the application's Box connection.

Only the year/reserve/event levels and event-sidecar folders are listed. Raw
media is never traversed. Paths are display labels, not Box Drive I/O targets.
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

from cassn.box.client import BoxStorage


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


class BoxMetadataStore:
    FILE_FIELDS = [
        "id", "name", "parent", "etag", "file_version", "sha1", "size", "permissions",
    ]

    def __init__(self, client, data_folder_id: str):
        self.client = client
        self.storage = BoxStorage(client)
        self.data_folder_id = data_folder_id

    @classmethod
    def connect(cls):
        from cassn.box.auth import get_box_client, hosted_config

        # SoundHub's UCNature submission always belongs to UC, including when
        # invoked by the standalone maintenance CLI with legacy settings paths.
        config = hosted_config("uc")
        client = get_box_client(config)
        if client is None:
            raise RuntimeError("Connect to Box in Field Data Manager before submitting to SoundHub.")
        return cls(client, config.field_data_folder_id)

    def reconnect(self):
        """Reload the saved connection after a potentially hours-long transfer."""
        fresh = type(self).connect()
        if fresh.data_folder_id != self.data_folder_id:
            raise RuntimeError("Box project changed after SoundHub preflight")
        self.client = fresh.client
        self.storage = fresh.storage

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

    def iter_metadata(self, year_root: Path):
        year_id = self.storage.find_child_folder(self.data_folder_id, year_root.name)
        if year_id is None:
            raise ValueError(f"Box data folder has no year {year_root.name}")
        fields = ["id", "name", "type"]
        for reserve in self.storage.iter_folder_items(year_id, fields=fields):
            if reserve.type != "folder":
                continue
            for event in self.storage.iter_folder_items(reserve.id, fields=fields):
                if event.type != "folder":
                    continue
                for item in self.storage.iter_folder_items(event.id, fields=fields):
                    if item.type == "file" and item.name == "audio_file_metadata.csv":
                        yield year_root / reserve.name / event.name / item.name, self.snapshot(item.id)

    def check_report_permission(self, event_id: str) -> None:
        # An existing report folder can have more restrictive permissions.
        report_id = self.storage.find_child_folder(event_id, "soundhub")
        target = report_id or event_id
        folder = self.client.folders.get_folder_by_id(target, fields=["id", "permissions"])
        if not folder.permissions or not folder.permissions.can_upload:
            raise ValueError(f"No Box upload permission for submission reports in folder {target}")

    def assert_unchanged(self, ref: BoxFileSnapshot) -> None:
        current = self.snapshot(ref.file_id)
        if current != ref:
            raise ValueError(f"Box file {ref.file_id} changed after preflight; rebuild the provenance plan")

    def _verify_write(self, file_id: str, payload: bytes, parent_id: str, name: str) -> None:
        current = self.snapshot(file_id)
        if (current.parent_id != parent_id or current.name != name
                or current.size != len(payload)
                or current.sha1 != hashlib.sha1(payload).hexdigest()):
            raise ValueError(f"Box file {file_id} failed verification after upload")

    def write_metadata(self, ref: BoxFileSnapshot, payload: bytes) -> None:
        from box_sdk_gen import UploadFileVersionAttributes

        # If-Match closes the race between the batch recheck and this write.
        self.client.uploads.upload_file_version(
            ref.file_id, UploadFileVersionAttributes(name=ref.name), io.BytesIO(payload),
            if_match=ref.etag,
        )
        self._verify_write(ref.file_id, payload, ref.parent_id, ref.name)

    def write_report(self, event_id: str, name: str, payload: bytes) -> None:
        folder_id = self.storage.find_or_create_folder(event_id, "soundhub")
        # Never overwrite an existing transfer record.
        files = self.storage.folder_file_map(folder_id)
        if name in files:
            ref = self.snapshot(files[name])
            if self.read(ref) != payload:
                raise ValueError(f"A different submission report already exists: {name}")
            return
        result = self.client.uploads.upload_file(
            attributes={"name": name, "parent": {"id": folder_id}}, file=io.BytesIO(payload),
        )
        if not result.entries or len(result.entries) != 1:
            raise ValueError("Box did not return the uploaded submission report")
        file_id = result.entries[0].id
        self._verify_write(file_id, payload, folder_id, name)
        files[name] = file_id

    def verified_reports(self, event_id: str, event_root: Path) -> dict[str, Path]:
        from cassn.soundhub.lifecycle import REPORT_GLOB, is_verified_submission_report

        folder_id = self.storage.find_child_folder(event_id, "soundhub")
        if folder_id is None:
            return {}
        reports = {}
        for item in self.storage.iter_folder_items(folder_id, fields=["id", "name", "type"]):
            if item.type == "file" and Path(item.name).match(REPORT_GLOB):
                payload = self.read(self.snapshot(item.id))
                if is_verified_submission_report(payload.decode("utf-8")):
                    reports[item.name] = event_root / "soundhub" / item.name
        return reports
