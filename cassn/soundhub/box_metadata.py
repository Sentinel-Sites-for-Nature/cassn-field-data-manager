"""Small SoundHub sidecars accessed through the application's Box connection.

Only the year/reserve/event levels and event-sidecar folders are listed. Raw
media is never traversed. Paths are display labels, not Box Drive I/O targets.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path

from cassn.box.client import BoxStorage


# Retain these imports as public aliases for existing SoundHub callers.
from cassn.box.metadata_store import BoxFileSnapshot, BoxFileStore


class BoxMetadataStore(BoxFileStore):
    def __init__(self, client, data_folder_id: str):
        super().__init__(client)
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
