"""Tests for safe Box publication of the validated Survey123 pair."""

from __future__ import annotations

import csv
from pathlib import Path
from types import SimpleNamespace

import pytest

from cassn.box.lookup_publisher import (
    LookupPublicationError,
    publish_validated_lookup_pair,
)


def _write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _canonical_files(root: Path) -> dict[str, bytes]:
    _write_csv(root / "devices.csv", ["device_record_id", "device_id", "device_type"], [{
        "device_record_id": "ML:CAM1", "device_id": "CAM1", "device_type": "ML",
    }])
    _write_csv(
        root / "deployments.csv",
        [
            "deployment_id", "deployment_event_id", "site_short_name", "plot_number",
            "device_type", "device_record_id", "device_id", "deployment_start_date",
            "deployment_end_date",
        ],
        [{
            "deployment_id": "UC_TestSite_plot1_ML_20260201", "deployment_event_id": "UC_TestSite_20260201",
            "site_short_name": "TestSite", "plot_number": "1", "device_type": "ML",
            "device_record_id": "ML:CAM1", "device_id": "CAM1",
            "deployment_start_date": "2026-01-01", "deployment_end_date": "2026-02-01",
        }],
    )
    return {path.name: path.read_bytes() for path in root.iterdir()}


class PublishingClient:
    def __init__(self, *, fail_devices: bool = False):
        self.by_name = {"deployments.csv": "deployments-id"}
        self.content = {"deployments-id": b"legacy,event-only\n"}
        self.versions = {"deployments-id": [b"legacy,event-only\n"]}
        self.fail_devices = fail_devices
        self.folders = SimpleNamespace(get_folder_items=self.get_folder_items)
        self.downloads = SimpleNamespace(download_file=self.download_file)
        self.uploads = SimpleNamespace(
            upload_file_version=self.upload_file_version,
            upload_file=self.upload_file,
        )
        self.files = SimpleNamespace(delete_file_by_id=self.delete_file_by_id)

    def get_folder_items(self, _folder_id, **_kwargs):
        return SimpleNamespace(entries=[
            SimpleNamespace(type="file", name=name, id=file_id)
            for name, file_id in self.by_name.items()
        ])

    def download_file(self, file_id):
        return [self.content[file_id]]

    def upload_file_version(self, file_id, *, attributes, file):
        content = file.read()
        self.content[file_id] = content
        self.versions.setdefault(file_id, []).append(content)

    def upload_file(self, *, attributes, file):
        name = attributes["name"]
        if name == "devices.csv" and self.fail_devices:
            raise OSError("simulated Box failure")
        file_id = f"new-{name}"
        self.by_name[name] = file_id
        self.content[file_id] = file.read()
        self.versions[file_id] = [self.content[file_id]]
        return SimpleNamespace(entries=[SimpleNamespace(id=file_id)])

    def delete_file_by_id(self, file_id):
        self.content.pop(file_id, None)
        for name, value in list(self.by_name.items()):
            if value == file_id:
                del self.by_name[name]


def test_publication_versions_obsolete_deployments_and_creates_devices(tmp_path):
    source = tmp_path / "candidate"
    canonical = _canonical_files(source)
    client = PublishingClient()

    published = publish_validated_lookup_pair(client, "folder-1", source)

    assert [(item.name, item.action) for item in published] == [
        ("deployments.csv", "versioned"),
        ("devices.csv", "created"),
    ]
    assert client.content["deployments-id"] == canonical["deployments.csv"]
    assert client.content["new-devices.csv"] == canonical["devices.csv"]
    assert client.versions["deployments-id"][0] == b"legacy,event-only\n"


def test_publication_restores_previous_deployments_if_devices_upload_fails(tmp_path):
    source = tmp_path / "candidate"
    _canonical_files(source)
    client = PublishingClient(fail_devices=True)

    with pytest.raises(LookupPublicationError, match="simulated Box failure"):
        publish_validated_lookup_pair(client, "folder-1", source)

    assert client.content["deployments-id"] == b"legacy,event-only\n"
    assert "devices.csv" not in client.by_name
