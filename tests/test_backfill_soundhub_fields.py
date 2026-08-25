"""Tests for synchronized staged/Box SoundHub field backfill."""
from __future__ import annotations

import csv
from pathlib import Path

from cassn.config import AUDIO_FIELDS, SOUNDHUB_DEPLOYMENT_FIELDS
from cassn.export.wildlife_insights import SUBPROJECT_DESIGN
from cassn.soundhub.staging import fragments_root, project_root
from utils.backfill_soundhub_fields import backfill_soundhub_fields


ANZA = "UC_AnzaBorrego_plot1_BD_20260516"
OTHER = "UC_TestSite_plot2_BD_20260610"


def _write(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _read(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


def _deployment_row(deployment_id: str) -> dict:
    row = {field: "" for field in SOUNDHUB_DEPLOYMENT_FIELDS}
    row.update(
        {
            "project_short_name": "UCNature-SSN",
            "deployment_id": deployment_id,
            "subproject": "legacy-event-id",
            "longitude": "-120.00000000",
            "latitude": "35.00000000",
        }
    )
    return row


def _audio_row(deployment_id: str) -> dict:
    row = {field: "" for field in AUDIO_FIELDS}
    row.update(
        {
            "filename": f"{deployment_id}_00001.wav",
            "deployment_id": deployment_id,
            "device_type": "BD",
            "file_type": "audio",
            "subproject": "legacy-event-id",
        }
    )
    return row


def _fixture(tmp_path: Path):
    staging = tmp_path / "staging"
    rows = [_deployment_row(ANZA), _deployment_row(OTHER)]
    _write(project_root(staging) / "deployment.csv", SOUNDHUB_DEPLOYMENT_FIELDS, rows)
    for row in rows:
        _write(
            fragments_root(staging) / row["deployment_id"] / "deployment.csv",
            SOUNDHUB_DEPLOYMENT_FIELDS,
            [row],
        )
    box_year = tmp_path / "box" / "2026"
    for event, row in [
        ("UC_AnzaBorrego_20260516", rows[0]),
        ("UC_TestSite_20260610", rows[1]),
    ]:
        folder = box_year / "Reserve" / event
        _write(
            folder / "audio_file_metadata.csv",
            AUDIO_FIELDS,
            [_audio_row(row["deployment_id"])],
        )
        _write(
            folder / "soundhub" / "deployment.csv",
            SOUNDHUB_DEPLOYMENT_FIELDS,
            [row],
        )
    return staging, box_year


def test_dry_run_is_non_mutating_and_reports_all_sources(tmp_path):
    staging, box_year = _fixture(tmp_path)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*.csv")}

    result = backfill_soundhub_fields(staging, box_year)

    assert result.ok
    assert result.deployment_count == 2
    assert result.changed_files == 7
    assert not result.applied
    assert {path: path.read_bytes() for path in tmp_path.rglob("*.csv")} == before


def test_apply_keeps_staging_fragments_and_box_metadata_in_sync(tmp_path):
    staging, box_year = _fixture(tmp_path)

    result = backfill_soundhub_fields(staging, box_year, apply=True)

    assert result.ok
    assert result.applied
    project_rows = {
        row["deployment_id"]: row
        for row in _read(project_root(staging) / "deployment.csv")
    }
    assert project_rows[ANZA]["subproject"] == "AnzaBorrego_2026"
    assert project_rows[ANZA]["subproject_design"] == SUBPROJECT_DESIGN
    assert project_rows[ANZA]["mounted_on"] == "metal_pole"
    assert project_rows[ANZA]["sensor_height_meters"] == "2.5"
    assert project_rows[OTHER]["subproject"] == "TestSite_2026"
    assert project_rows[OTHER]["sensor_height_meters"] == ""

    for deployment_id, project_row in project_rows.items():
        fragment = _read(
            fragments_root(staging) / deployment_id / "deployment.csv"
        )[0]
        assert fragment == project_row

    for audio_path in box_year.rglob("audio_file_metadata.csv"):
        row = _read(audio_path)[0]
        expected = project_rows[row["deployment_id"]]
        for column in ("subproject", "subproject_design", "mounted_on"):
            assert row[column] == expected[column]
        if row["deployment_id"] == ANZA:
            assert row["sensor_height_meters"] == "2.5"

    second = backfill_soundhub_fields(staging, box_year, apply=True)
    assert second.ok
    assert not second.changed
    assert not second.applied


def test_missing_box_source_blocks_every_write(tmp_path):
    staging, box_year = _fixture(tmp_path)
    missing = box_year / "Reserve" / "UC_TestSite_20260610" / "audio_file_metadata.csv"
    missing.unlink()
    before = {path: path.read_bytes() for path in tmp_path.rglob("*.csv")}

    result = backfill_soundhub_fields(staging, box_year, apply=True)

    assert not result.ok
    assert any("Box audio metadata missing" in error for error in result.errors)
    assert {path: path.read_bytes() for path in tmp_path.rglob("*.csv")} == before
