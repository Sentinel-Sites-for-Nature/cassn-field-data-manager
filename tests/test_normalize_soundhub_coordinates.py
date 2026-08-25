"""Tests for staged SoundHub coordinate normalization."""
from __future__ import annotations

import csv
from pathlib import Path

from cassn.config import SOUNDHUB_DEPLOYMENT_FIELDS
from cassn.soundhub.staging import fragments_root, project_root
from utils.normalize_soundhub_coordinates import normalize_soundhub_staging


def _row(deployment_id: str, *, longitude: str, latitude: str, placename: str = "plot") -> dict:
    row = {field: "" for field in SOUNDHUB_DEPLOYMENT_FIELDS}
    row.update(
        {
            "project_short_name": "UCNature-SSN",
            "deployment_id": deployment_id,
            "placename": placename,
            "longitude": longitude,
            "latitude": latitude,
        }
    )
    return row


def _write(path: Path, rows: list[dict], *, fields=None, bom: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig" if bom else "utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields or SOUNDHUB_DEPLOYMENT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _read(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


def _staging(tmp_path: Path, rows: list[dict]) -> Path:
    root = project_root(tmp_path)
    _write(root / "deployment.csv", rows)
    for row in rows:
        _write(
            fragments_root(tmp_path) / row["deployment_id"] / "deployment.csv",
            [row],
        )
    (root / "recording.csv").write_bytes(b"recording manifest sentinel\n")
    media = root / rows[0]["deployment_id"] / "recording.flac"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"FLAC media sentinel\n")
    return tmp_path


def test_dry_run_reconciles_fragments_without_writing(tmp_path):
    staging = _staging(
        tmp_path,
        [
            _row("UC_Test_plot1_BD_20260101", longitude="-120.1", latitude="34.123456789"),
            _row("UC_Test_plot2_BD_20260101", longitude="-121.1234", latitude="35.2"),
        ],
    )
    before = {path: path.read_bytes() for path in staging.rglob("*") if path.is_file()}

    result = normalize_soundhub_staging(staging)

    assert result.ok
    assert result.changed_deployments == 2
    assert result.changed_cells == 4
    assert result.changed_files == 3
    assert not result.applied
    assert {path: path.read_bytes() for path in staging.rglob("*") if path.is_file()} == before


def test_apply_normalizes_project_and_fragments_but_not_recording_or_media(tmp_path):
    deployment_id = "UC_Test_plot1_BD_20260101"
    staging = _staging(
        tmp_path,
        [_row(deployment_id, longitude="-120.1", latitude="34.123456789")],
    )
    root = project_root(staging)
    recording_before = (root / "recording.csv").read_bytes()
    media_path = root / deployment_id / "recording.flac"
    media_before = media_path.read_bytes()

    result = normalize_soundhub_staging(staging, apply=True)

    assert result.ok
    assert result.applied
    project_row = _read(root / "deployment.csv")[0]
    fragment_row = _read(fragments_root(staging) / deployment_id / "deployment.csv")[0]
    assert project_row == fragment_row
    assert project_row["longitude"] == "-120.10000000"
    assert project_row["latitude"] == "34.12345679"
    assert (root / "recording.csv").read_bytes() == recording_before
    assert media_path.read_bytes() == media_before

    second = normalize_soundhub_staging(staging, apply=True)
    assert second.ok
    assert not second.changed
    assert not second.applied


def test_invalid_coordinate_blocks_all_writes(tmp_path):
    staging = _staging(
        tmp_path,
        [_row("UC_Test_plot1_BD_20260101", longitude="-120.1", latitude="")],
    )
    before = {path: path.read_bytes() for path in staging.rglob("*") if path.is_file()}

    result = normalize_soundhub_staging(staging, apply=True)

    assert not result.ok
    assert any("latitude is blank" in error for error in result.errors)
    assert {path: path.read_bytes() for path in staging.rglob("*") if path.is_file()} == before


def test_out_of_range_coordinate_blocks_all_writes(tmp_path):
    staging = _staging(
        tmp_path,
        [_row("UC_Test_plot1_BD_20260101", longitude="-181", latitude="34.2")],
    )
    before = {path: path.read_bytes() for path in staging.rglob("*") if path.is_file()}

    result = normalize_soundhub_staging(staging, apply=True)

    assert not result.ok
    assert any("longitude is outside" in error for error in result.errors)
    assert {path: path.read_bytes() for path in staging.rglob("*") if path.is_file()} == before


def test_noncoordinate_project_fragment_difference_blocks_writes(tmp_path):
    deployment_id = "UC_Test_plot1_BD_20260101"
    staging = _staging(
        tmp_path,
        [_row(deployment_id, longitude="-120.1", latitude="34.2")],
    )
    fragment = fragments_root(staging) / deployment_id / "deployment.csv"
    row = _read(fragment)[0]
    row["placename"] = "different"
    _write(fragment, [row])
    before = {path: path.read_bytes() for path in staging.rglob("*") if path.is_file()}

    result = normalize_soundhub_staging(staging, apply=True)

    assert not result.ok
    assert any("placename" in error for error in result.errors)
    assert {path: path.read_bytes() for path in staging.rglob("*") if path.is_file()} == before


def test_partial_coordinate_run_is_resumable(tmp_path):
    deployment_id = "UC_Test_plot1_BD_20260101"
    staging = _staging(
        tmp_path,
        [_row(deployment_id, longitude="-120.1", latitude="34.2")],
    )
    fragment = fragments_root(staging) / deployment_id / "deployment.csv"
    row = _read(fragment)[0]
    row["longitude"] = "-120.10000000"
    row["latitude"] = "34.20000000"
    _write(fragment, [row])

    result = normalize_soundhub_staging(staging, apply=True)

    assert result.ok
    assert result.applied
    assert _read(project_root(staging) / "deployment.csv")[0] == _read(fragment)[0]


def test_wrong_schema_blocks_writes(tmp_path):
    deployment_id = "UC_Test_plot1_BD_20260101"
    staging = _staging(
        tmp_path,
        [_row(deployment_id, longitude="-120.1", latitude="34.2")],
    )
    project = project_root(staging) / "deployment.csv"
    _write(project, [{"deployment_id": deployment_id}], fields=["deployment_id"])
    before = project.read_bytes()

    result = normalize_soundhub_staging(staging, apply=True)

    assert not result.ok
    assert any("header does not match" in error for error in result.errors)
    assert project.read_bytes() == before


def test_apply_preserves_utf8_bom(tmp_path):
    deployment_id = "UC_Test_plot1_BD_20260101"
    staging = _staging(
        tmp_path,
        [_row(deployment_id, longitude="-120.1", latitude="34.2")],
    )
    project = project_root(staging) / "deployment.csv"
    fragment = fragments_root(staging) / deployment_id / "deployment.csv"
    row = _read(project)[0]
    _write(project, [row], bom=True)
    _write(fragment, [row], bom=True)

    result = normalize_soundhub_staging(staging, apply=True)

    assert result.ok
    assert project.read_bytes().startswith(b"\xef\xbb\xbf")
    assert fragment.read_bytes().startswith(b"\xef\xbb\xbf")
