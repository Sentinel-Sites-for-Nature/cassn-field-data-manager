"""Tests for synchronized Anza SoundHub duration recovery."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from cassn.config import SOUNDHUB_RECORDING_FIELDS
from cassn.soundhub.staging import fragments_root, project_root
from utils.backfill_anza_soundhub_durations import (
    ANZA_DEPLOYMENT_IDS,
    DURATION_COLUMN,
    backfill_anza_soundhub_durations,
    read_flac_duration,
)


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        return list(reader.fieldnames or []), list(reader)


def _write_flac_header(path: Path, sample_rate: int, total_samples: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    streaminfo = bytearray(34)
    packed = (sample_rate << 44) | total_samples
    streaminfo[10:18] = packed.to_bytes(8, "big")
    path.write_bytes(b"fLaC" + bytes([0x80, 0, 0, 34]) + streaminfo)


def _fixture(tmp_path: Path):
    staging = tmp_path / "staging"
    box_event = tmp_path / "box" / "UC_AnzaBorrego_20260516"
    starts = ["2026-04-13 00:00:00-07:00", "2026-04-13 20:00:00-07:00"]
    recording_rows = []
    audio_rows = []
    for index, deployment_id in enumerate(sorted(ANZA_DEPLOYMENT_IDS)):
        filename = f"{deployment_id}_00001.flac"
        recording_rows.append(
            {
                "filename": filename,
                "deployment_id": deployment_id,
                "start": starts[index % 2],
                "end": "",
            }
        )
        audio_rows.append(
            {
                "filename": Path(filename).with_suffix(".wav").name,
                "deployment_id": deployment_id,
                "duration": "legacy schedule",
                "notes": f"keep-{index}",
            }
        )
        _write_flac_header(
            project_root(staging) / deployment_id / filename,
            48_000,
            48_000 * 10 + index,
        )
        _write_csv(
            fragments_root(staging) / deployment_id / "recording.csv",
            SOUNDHUB_RECORDING_FIELDS,
            [recording_rows[-1]],
        )

    other = {
        "filename": "UC_Other_plot1_BD_20260501_00001.flac",
        "deployment_id": "UC_Other_plot1_BD_20260501",
        "start": "2026-04-01 00:00:00-07:00",
        "end": "2026-04-01 01:00:00-07:00",
    }
    _write_csv(
        project_root(staging) / "recording.csv",
        SOUNDHUB_RECORDING_FIELDS,
        [other, *recording_rows],
    )
    _write_csv(
        box_event / "soundhub" / "recording.csv",
        SOUNDHUB_RECORDING_FIELDS,
        recording_rows,
    )
    audio_rows.append(
        {
            "filename": "UC_AnzaBorrego_plot4_BD_20260516_00010.wav",
            "deployment_id": "UC_AnzaBorrego_plot4_BD_20260516",
            "duration": "legacy schedule",
            "notes": "header-only failure",
        }
    )
    _write_csv(
        box_event / "audio_file_metadata.csv",
        ["filename", "deployment_id", "duration", "notes"],
        audio_rows,
    )
    return staging, box_event, other


def test_reads_streaminfo_without_external_decoder(tmp_path):
    path = tmp_path / "sample.flac"
    _write_flac_header(path, 48_000, 48_000 * 12 + 3)

    duration = read_flac_duration(path)

    assert duration.sample_rate_hz == 48_000
    assert duration.total_samples == 576_003
    assert duration.seconds == "12.0000625"
    assert duration.microseconds == 12_000_063


def test_dry_run_reports_synchronized_changes_without_writing(tmp_path):
    staging, box_event, _ = _fixture(tmp_path)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*.csv")}

    result = backfill_anza_soundhub_durations(
        staging, box_event, expected_recordings=4
    )

    assert result.ok
    assert result.recording_count == 4
    assert result.deployment_count == 4
    assert result.changed_files == 7
    assert result.changed_cells == 16
    assert not result.applied
    assert {path: path.read_bytes() for path in tmp_path.rglob("*.csv")} == before


def test_apply_preserves_other_rows_and_header_only_failure(tmp_path):
    staging, box_event, other = _fixture(tmp_path)

    result = backfill_anza_soundhub_durations(
        staging, box_event, apply=True, expected_recordings=4
    )

    assert result.ok
    assert result.applied
    fields, audio_rows = _read_csv(box_event / "audio_file_metadata.csv")
    assert fields == ["filename", "deployment_id", "duration", DURATION_COLUMN, "notes"]
    assert audio_rows[-1][DURATION_COLUMN] == ""
    assert audio_rows[-1]["notes"] == "header-only failure"
    assert all(row[DURATION_COLUMN] for row in audio_rows[:-1])

    _, project_rows = _read_csv(project_root(staging) / "recording.csv")
    assert project_rows[0] == other
    target_project = {row["filename"]: row for row in project_rows[1:]}
    assert all(row["end"] for row in target_project.values())
    _, box_rows = _read_csv(box_event / "soundhub" / "recording.csv")
    assert {row["filename"]: row for row in box_rows} == target_project
    for deployment_id in ANZA_DEPLOYMENT_IDS:
        _, rows = _read_csv(fragments_root(staging) / deployment_id / "recording.csv")
        assert rows == [target_project[rows[0]["filename"]]]

    provenance = json.loads(result.provenance_path.read_text(encoding="utf-8"))
    assert provenance["recordings_updated"] == 4
    assert len(provenance["files"]) == 7
    assert all(item["before_sha256"] != item["after_sha256"] for item in provenance["files"])

    second = backfill_anza_soundhub_durations(
        staging, box_event, apply=True, expected_recordings=4
    )
    assert second.ok
    assert not second.changed
    assert not second.applied


def test_conflicting_existing_end_blocks_every_write(tmp_path):
    staging, box_event, _ = _fixture(tmp_path)
    box_csv = box_event / "soundhub" / "recording.csv"
    fields, rows = _read_csv(box_csv)
    rows[0]["end"] = "2026-04-14 00:00:00-07:00"
    _write_csv(box_csv, fields, rows)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*.csv")}

    result = backfill_anza_soundhub_durations(
        staging, box_event, apply=True, expected_recordings=4
    )

    assert not result.ok
    assert any("conflicts with derived" in error for error in result.errors)
    assert {path: path.read_bytes() for path in tmp_path.rglob("*.csv")} == before
