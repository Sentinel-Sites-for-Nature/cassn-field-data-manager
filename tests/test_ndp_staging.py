"""Tests for splitting a deployment event into NDP staging trees."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from cassn.lookups import LookupTables, Site
from cassn.ndp.manifest import METADATA_FILENAME
from cassn.ndp.staging import (
    MANIFEST_FILENAME,
    REQUIRED_COLUMNS_BY_KIND,
    NdpStagingError,
    PlannedDeployment,
    apply_plan,
    plan_event,
    read_event_documents,
)


EVENT_ID = "UC_QuailRidge_20260108"
CAMERA_DEPLOYMENT = "UC_QuailRidge_plot1_ML_20260108"
AUDIO_DEPLOYMENT = "UC_QuailRidge_plot1_BD_20260108"
HEADER_FIXTURES = Path(__file__).parent / "data" / "ndp_headers"

IMAGE_FIELDS = [
    "filename",
    "original_filename",
    "deployment_event_id",
    "deployment_id",
    "organization",
    "site",
    "site_full_name",
    "site_code",
    "start_date",
    "end_date",
    "recorded_by",
    "plot_number",
    "device_type",
    "camera_id",
    "camera_make",
    "camera_model",
    "file_type",
    "file_size_bytes",
    "file_hash_sha256",
    "recorded_datetime",
    "latitude",
    "longitude",
    "notes",
]
AUDIO_FIELDS = [
    "filename",
    "original_filename",
    "deployment_event_id",
    "deployment_id",
    "organization",
    "site",
    "site_full_name",
    "site_code",
    "deployment_start_date",
    "deployment_end_date",
    "recorded_by",
    "plot_number",
    "device_type",
    "device_id",
    "ARU_make",
    "ARU_model",
    "file_type",
    "file_size_bytes",
    "file_hash_sha256",
    "recorded_datetime",
    "latitude",
    "longitude",
    "notes",
]


def _hash(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _image_row(filename: str, deployment_id: str = CAMERA_DEPLOYMENT, **overrides) -> dict:
    row = {
        "filename": filename,
        "original_filename": filename.upper(),
        "deployment_event_id": EVENT_ID,
        "deployment_id": deployment_id,
        "organization": "UC",
        "site": "QuailRidge",
        "site_full_name": "Quail Ridge Reserve",
        "site_code": "QRR",
        "start_date": "2025-11-07",
        "end_date": "2026-01-08",
        "recorded_by": "Imperato, John",
        "plot_number": deployment_id.split("_plot")[1][0],
        "device_type": "ML",
        "camera_id": "08019434",
        "camera_make": "RECONYX",
        "camera_model": "HYPERFIRE HP4K",
        "file_type": "image",
        "file_size_bytes": "1000",
        "file_hash_sha256": _hash(filename),
        "recorded_datetime": "2025-11-08T04:37:30-08:00",
        "latitude": "38.51695783",
        "longitude": "-122.1516454",
        "notes": "",
    }
    row.update(overrides)
    return row


def _audio_row(filename: str, **overrides) -> dict:
    row = {
        "filename": filename,
        "original_filename": filename.upper(),
        "deployment_event_id": EVENT_ID,
        "deployment_id": AUDIO_DEPLOYMENT,
        "organization": "UC",
        "site": "QuailRidge",
        "site_full_name": "Quail Ridge Reserve",
        "site_code": "QRR",
        "deployment_start_date": "2025-11-07",
        "deployment_end_date": "2026-01-08",
        "recorded_by": "Imperato, John",
        "plot_number": "1",
        "device_type": "BD",
        "device_id": "242A2605648875E8",
        "ARU_make": "AudioMoth",
        "ARU_model": "AudioMoth-Firmware-Basic 1.11.0",
        "file_type": "audio",
        "file_size_bytes": "2000",
        "file_hash_sha256": _hash(filename),
        "recorded_datetime": "2025-12-13T00:00:00-08:00",
        "latitude": "38.51695783",
        "longitude": "-122.1516454",
        "notes": "",
    }
    row.update(overrides)
    return row


def _write_csv(path: Path, fields: list[str], rows: list[dict], *, newline="\r\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator=newline)
        writer.writeheader()
        writer.writerows(rows)


def _lookup_row(deployment_id: str, device_type: str, device_id: str, plot: str) -> dict:
    return {
        "deployment_id": deployment_id,
        "deployment_event_id": EVENT_ID,
        "deployment_sequence": "0",
        "site_short_name": "QuailRidge",
        "plot_number": plot,
        "device_type": device_type,
        "deployment_start_date": "2025-11-07",
        "deployment_end_date": "2026-01-08",
        "device_id": device_id,
        "asset_tag": "",
    }


@pytest.fixture
def lookups() -> LookupTables:
    """A minimal in-memory lookup snapshot; no Box and no lookup files needed."""
    tables = LookupTables()
    tables.sites = [Site("Quail Ridge Reserve", "QuailRidge", "QRR")]
    tables.device_deployments = [
        _lookup_row(CAMERA_DEPLOYMENT, "ML", "08019434", "1"),
        _lookup_row(AUDIO_DEPLOYMENT, "BD", "242A2605648875E8", "1"),
    ]
    tables.deployments_by_id = {
        row["deployment_id"]: row for row in tables.device_deployments
    }
    tables.plot_coords = {
        ("QuailRidge", 1): {
            "latitude": "38.51695783",
            "longitude": "-122.1516454",
        }
    }
    return tables


@pytest.fixture
def event_dir(tmp_path) -> Path:
    event = tmp_path / "box" / EVENT_ID
    _write_csv(
        event / "image_file_metadata.csv",
        IMAGE_FIELDS,
        [_image_row(f"img_{index}.jpg") for index in range(3)],
    )
    _write_csv(
        event / "audio_file_metadata.csv",
        AUDIO_FIELDS,
        [
            _audio_row("rec_1.wav"),
            _audio_row("CONFIG_01.txt", file_type="config", recorded_datetime=""),
        ],
    )
    return event


def test_every_filed_header_generation_satisfies_the_required_columns():
    """Column count never identifies a schema; the required columns do.

    The fixtures are the header lines of every distinct generation filed on Box,
    including three pairs that share a column count but differ in content.
    """
    fixtures = sorted(HEADER_FIXTURES.glob("*.csv"))
    assert len(fixtures) >= 10
    for fixture in fixtures:
        kind = fixture.name.split("_", 1)[0]
        with fixture.open(encoding="utf-8-sig", newline="") as stream:
            fields = set(next(csv.reader(stream)))
        missing = sorted(REQUIRED_COLUMNS_BY_KIND[kind] - fields)
        assert not missing, f"{fixture.name} is missing {missing}"


def test_plan_splits_an_event_into_one_directory_per_deployment(event_dir, lookups, tmp_path):
    plan = plan_event(event_dir, tmp_path / "staging", lookups)

    assert plan.ok, plan.errors
    assert plan.deployment_event_id == EVENT_ID
    assert [d.deployment_id for d in plan.deployments] == [
        AUDIO_DEPLOYMENT,
        CAMERA_DEPLOYMENT,
    ]
    for deployment in plan.deployments:
        assert set(deployment.files) == {
            MANIFEST_FILENAME,
            METADATA_FILENAME,
        }
    # No data/ directory is planned; media movement is a later phase.
    assert not any(
        name.startswith("data/") for d in plan.deployments for name in d.files
    )


def test_staged_csv_preserves_source_fields_order_and_row_values(event_dir, lookups, tmp_path):
    plan = plan_event(event_dir, tmp_path / "staging", lookups)
    source_path = event_dir / "image_file_metadata.csv"
    staged = next(
        d for d in plan.deployments if d.deployment_id == CAMERA_DEPLOYMENT
    ).files[METADATA_FILENAME]

    with source_path.open(encoding="utf-8", newline="") as stream:
        source_reader = csv.DictReader(stream)
        source_fields = source_reader.fieldnames
        source_rows = list(source_reader)
    staged_reader = csv.DictReader(staged.decode("utf-8").splitlines())
    assert staged_reader.fieldnames == source_fields
    assert list(staged_reader) == source_rows


def test_a_source_written_with_unix_newlines_keeps_them(tmp_path, lookups):
    event = tmp_path / "box" / EVENT_ID
    _write_csv(
        event / "audio_file_metadata.csv",
        AUDIO_FIELDS,
        [_audio_row("rec_1.wav")],
        newline="\n",
    )
    plan = plan_event(event, tmp_path / "staging", lookups)

    assert plan.ok, plan.errors
    staged = plan.deployments[0].files[METADATA_FILENAME]
    assert b"\r\n" not in staged


def test_manifest_records_the_hash_of_the_staged_csv_bytes(event_dir, lookups, tmp_path):
    plan = plan_event(event_dir, tmp_path / "staging", lookups)
    for deployment in plan.deployments:
        manifest = json.loads(deployment.files[MANIFEST_FILENAME])
        digest = hashlib.sha256(deployment.files[METADATA_FILENAME]).hexdigest()
        assert manifest["content"]["inventory"]["sha256"] == digest
        assert manifest["content"]["inventory"]["path"] == METADATA_FILENAME


def test_apply_writes_the_tree_and_is_idempotent(event_dir, lookups, tmp_path):
    staging_root = tmp_path / "staging"
    result = apply_plan(plan_event(event_dir, staging_root, lookups))

    assert len(result.written) == 4
    assert result.unchanged == ()
    event_root = staging_root / EVENT_ID
    assert (event_root / CAMERA_DEPLOYMENT / MANIFEST_FILENAME).is_file()
    assert (event_root / CAMERA_DEPLOYMENT / METADATA_FILENAME).is_file()
    assert not (event_root / CAMERA_DEPLOYMENT / "data").exists()

    # The manifest is content-deterministic, so a rerun changes nothing.
    rerun = plan_event(event_dir, staging_root, lookups)
    second = apply_plan(rerun)
    assert second.written == ()
    assert len(second.unchanged) == 4


def test_apply_finishes_an_interrupted_run(event_dir, lookups, tmp_path):
    staging_root = tmp_path / "staging"
    apply_plan(plan_event(event_dir, staging_root, lookups))
    (staging_root / EVENT_ID / CAMERA_DEPLOYMENT / MANIFEST_FILENAME).unlink()

    result = apply_plan(plan_event(event_dir, staging_root, lookups))

    assert len(result.written) == 1
    assert len(result.unchanged) == 3


def test_apply_refuses_to_replace_a_file_whose_bytes_differ(event_dir, lookups, tmp_path):
    staging_root = tmp_path / "staging"
    apply_plan(plan_event(event_dir, staging_root, lookups))
    staged = staging_root / EVENT_ID / CAMERA_DEPLOYMENT / METADATA_FILENAME
    staged.write_bytes(staged.read_bytes() + b"tampered\r\n")

    with pytest.raises(NdpStagingError, match="Refusing to replace"):
        apply_plan(plan_event(event_dir, staging_root, lookups))


def test_apply_refuses_a_plan_with_validation_errors(event_dir, lookups, tmp_path):
    plan = plan_event(event_dir, tmp_path / "staging", lookups)
    plan.errors.append("something is wrong")

    with pytest.raises(NdpStagingError, match="validation errors"):
        apply_plan(plan)


def test_one_deployment_in_both_documents_is_a_hard_error(tmp_path, lookups):
    event = tmp_path / "box" / EVENT_ID
    _write_csv(event / "image_file_metadata.csv", IMAGE_FIELDS, [_image_row("img_1.jpg")])
    _write_csv(
        event / "audio_file_metadata.csv",
        AUDIO_FIELDS,
        [_audio_row("rec_1.wav", deployment_id=CAMERA_DEPLOYMENT)],
    )
    plan = plan_event(event, tmp_path / "staging", lookups)

    assert not plan.ok
    assert any("both the image and audio documents" in error for error in plan.errors)


def test_event_folder_name_is_the_authoritative_event_id(tmp_path, lookups):
    event = tmp_path / "box" / "UC_QuailRidge_20260109"
    _write_csv(event / "image_file_metadata.csv", IMAGE_FIELDS, [_image_row("img_1.jpg")])
    plan = plan_event(event, tmp_path / "staging", lookups)

    assert not plan.ok
    assert any("does not match the event folder" in error for error in plan.errors)


def test_unsafe_deployment_id_is_rejected_before_staging(tmp_path, lookups):
    event = tmp_path / "box" / EVENT_ID
    row = _image_row("img_1.jpg")
    row["deployment_id"] = "../escaped"
    lookup = _lookup_row("../escaped", "ML", "08019434", "1")
    lookups.deployments_by_id["../escaped"] = lookup
    _write_csv(event / "image_file_metadata.csv", IMAGE_FIELDS, [row])

    plan = plan_event(event, tmp_path / "staging", lookups)

    assert not plan.ok
    assert any("safe directory name" in error for error in plan.errors)


def test_apply_rechecks_deployment_id_path_safety(event_dir, lookups, tmp_path):
    plan = plan_event(event_dir, tmp_path / "staging", lookups)
    original = plan.deployments[0]
    plan.deployments[0] = PlannedDeployment(
        "../escaped", original.files, original.warnings
    )

    with pytest.raises(NdpStagingError, match="Unsafe deployment_id"):
        apply_plan(plan)

    assert not (tmp_path / "staging" / "escaped").exists()


def test_a_document_missing_a_required_column_is_rejected(tmp_path):
    event = tmp_path / "box" / EVENT_ID
    fields = [name for name in IMAGE_FIELDS if name != "file_hash_sha256"]
    rows = [{key: value for key, value in _image_row("img_1.jpg").items() if key in fields}]
    _write_csv(event / "image_file_metadata.csv", fields, rows)

    with pytest.raises(NdpStagingError, match="file_hash_sha256"):
        read_event_documents(event)


def test_an_event_with_no_metadata_documents_is_rejected(tmp_path):
    event = tmp_path / "box" / EVENT_ID
    event.mkdir(parents=True)

    with pytest.raises(NdpStagingError, match="No image_file_metadata"):
        read_event_documents(event)


def test_a_raw_data_folder_no_deployment_covers_is_reported(event_dir, lookups, tmp_path):
    (event_dir / "raw_data" / "p1_ML").mkdir(parents=True)
    (event_dir / "raw_data" / "p1_BD").mkdir()
    (event_dir / "raw_data" / "p2_SA").mkdir()

    plan = plan_event(event_dir, tmp_path / "staging", lookups)

    assert plan.ok, plan.errors
    assert any("raw_data/p2_SA" in warning for warning in plan.warnings)
    assert not any("p1_ML" in warning for warning in plan.warnings)
