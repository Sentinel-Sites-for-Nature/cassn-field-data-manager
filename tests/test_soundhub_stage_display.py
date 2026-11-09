"""Progress evidence for local preparation, historical uploads, and cumulative manifests."""
import csv
import hashlib
import json

import pytest

from cassn.soundhub.export import build_recording_rows
from cassn.soundhub.stage_display import batch_bytes, event_preview, manifest_changes, manifest_rows
from cassn.soundhub.staging import project_root


def save_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)


def test_refresh_reports_changed_rows_independently_of_file_totals(tmp_path):
    root = project_root(tmp_path)
    save_rows(root / "deployment.csv", [dict(deployment_id="p1", schedule="old"), dict(deployment_id="p2", schedule="same")])
    save_rows(root / "recording.csv", [dict(path="p1/a.flac", start="old"), dict(path="p1/b.flac", start="removed")])
    old = manifest_rows(tmp_path)
    save_rows(root / "deployment.csv", [
        dict(deployment_id="p1", schedule="new"), dict(deployment_id="p2", schedule="same"),
        dict(deployment_id="p3", schedule="new")])
    save_rows(root / "recording.csv", [dict(path="p1/a.flac", start="old"), dict(path="p3/c.flac", start="new")])
    current = manifest_rows(tmp_path)
    assert manifest_changes(old, current) == {
        "deployment.csv": dict(added=1, updated=1, removed=0), "recording.csv": dict(added=1, updated=0, removed=1)}
    assert all(not any(counts.values()) for counts in manifest_changes(current, current).values())


def test_verified_event_receipt_excludes_staged_only_paths_and_other_projects(tmp_path, monkeypatch):
    event = tmp_path / "my-event"
    staging = tmp_path / "staging"
    root = project_root(staging)
    audio = [dict(filename=f"rec-{i}.wav", deployment_id="recorder", device_type="BD",
                  file_type="audio", sample_rate_hz="48000") for i in range(3)]
    expected = build_recording_rows(audio)
    save_rows(event / "audio_file_metadata.csv", audio)
    save_rows(event / "soundhub/deployment.csv", [{"deployment_id": "recorder"}])
    save_rows(root / "deployment.csv", [{"deployment_id": "recorder"}])
    save_rows(root / "recording.csv", expected[:2])
    for row in expected[:2]:
        flac = root / row["path"]
        flac.parent.mkdir(exist_ok=True)
        flac.write_bytes(b"local FLAC")
    assert batch_bytes(staging) == sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
    transfer = tmp_path / "transfers/first"
    save_rows(transfer / "recording.csv", expected)
    digest = hashlib.sha256((transfer / "recording.csv").read_bytes()).hexdigest()
    config = {"bucket": "test", "upload_prefix": "upload", "project_short_name": "project"}
    monkeypatch.setattr("cassn.soundhub.stage_display.load_soundhub_config", lambda: config)
    receipt = {"destination": "s3://test/upload/project/", "planned": {
        "events": [event.name], "objects": [dict(relative=expected[0]["path"], key="upload/project/" + expected[0]["path"])]},
        "verification": {"ok": False}, "manifests": {"recording.csv": {"sha256": digest}}}
    path = transfer / "transfer.json"
    path.write_text(json.dumps(receipt))
    assert event_preview(event, staging) == dict(uploaded=0, staged=2, waiting=1)
    receipt["verification"]["ok"] = True
    receipt["destination"] = "s3://different/upload/project/"
    path.write_text(json.dumps(receipt))
    assert event_preview(event, staging) == dict(uploaded=0, staged=2, waiting=1)
    receipt["destination"] = "s3://test/upload/project/"
    path.write_text(json.dumps(receipt))
    assert event_preview(event, staging) == dict(uploaded=1, staged=1, waiting=1)
    (transfer / "recording.csv").write_bytes(b"changed")
    with pytest.raises(ValueError, match="differs"):
        event_preview(event, staging)
