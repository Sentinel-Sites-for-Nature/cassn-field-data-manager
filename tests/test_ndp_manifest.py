"""Tests for the cassn.source.deployment manifest builder."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from cassn.lookups import Site
from cassn.ndp.manifest import (
    INVENTORY_REVISION,
    MANIFEST_TYPE,
    ORGANIZATION,
    SCHEMA_VERSION,
    build_manifest,
)


EVENT_ID = "UC_QuailRidge_20260108"
CAMERA_DEPLOYMENT = "UC_QuailRidge_plot1_ML_20260108"
AUDIO_DEPLOYMENT = "UC_QuailRidge_plot1_BD_20260108"
SITE = Site("Quail Ridge Reserve", "QuailRidge", "QRR")
PLOT_COORDINATES = {"latitude": "38.51695783", "longitude": "-122.1516454"}
SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "schemas"
    / "cassn-source-deployment-v1.schema.json"
)


def _hash(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _camera_lookup(**overrides) -> dict:
    row = {
        "deployment_id": CAMERA_DEPLOYMENT,
        "deployment_event_id": EVENT_ID,
        "deployment_sequence": "0",
        "site_short_name": "QuailRidge",
        "plot_number": "1",
        "device_type": "ML",
        "deployment_start_date": "2025-11-07",
        "deployment_end_date": "2026-01-08",
        "device_id": "08019434",
        "asset_tag": "",
    }
    row.update(overrides)
    return row


def _audio_lookup(**overrides) -> dict:
    row = _camera_lookup(
        deployment_id=AUDIO_DEPLOYMENT,
        device_type="BD",
        device_id="242A2605648875E8",
    )
    row.update(overrides)
    return row


def _image_row(filename: str, **overrides) -> dict:
    # The legacy site-column pair, as the QuailRidge documents on Box carry it.
    row = {
        "filename": filename,
        "deployment_id": CAMERA_DEPLOYMENT,
        "deployment_event_id": EVENT_ID,
        "organization": "UC",
        "site": "QuailRidge",
        "site_full_name": "Quail Ridge Reserve",
        "site_code": "QRR",
        "start_date": "2025-11-07",
        "end_date": "2026-01-08",
        "recorded_by": "Imperato, John",
        "plot_number": "1",
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
    }
    row.update(overrides)
    return row


def _audio_row(filename: str, **overrides) -> dict:
    row = {
        "filename": filename,
        "deployment_id": AUDIO_DEPLOYMENT,
        "deployment_event_id": EVENT_ID,
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
    }
    row.update(overrides)
    return row


def _build(
    rows,
    *,
    kind="image",
    lookup=None,
    site=SITE,
    plot_coordinates=PLOT_COORDINATES,
    **overrides,
):
    keywords = {
        "document_kind": kind,
        "deployment_event_id": EVENT_ID,
        "lookup_row": _camera_lookup() if lookup is None else lookup,
        "site": site,
        "plot_coordinates": plot_coordinates,
        "metadata_sha256": _hash("csv"),
    }
    keywords.update(overrides)
    deployment_id = rows[0]["deployment_id"] if rows else CAMERA_DEPLOYMENT
    return build_manifest(deployment_id, rows, **keywords)


def test_camera_manifest_carries_lookup_placement_and_media_rollup():
    rows = [_image_row(f"img_{index}.jpg") for index in range(3)]
    build = _build(rows)

    assert build.ok, build.errors
    assert build.warnings == ()
    manifest = build.manifest
    assert manifest["manifest_type"] == MANIFEST_TYPE
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["inventory_revision"] == INVENTORY_REVISION
    assert "generated" not in manifest
    assert "generator" not in manifest
    assert "access" not in manifest
    assert "verification" not in manifest

    deployment = manifest["deployment"]
    assert deployment["site"] == {
        "id": "QuailRidge",
        "name": "Quail Ridge Reserve",
    }
    assert deployment["organization"] == ORGANIZATION == "UC-Nature"
    assert deployment["plot_number"] == 1
    assert "device" not in deployment
    assert "recorded_by" not in deployment
    assert deployment["deployment_interval"] == {
        "start": "2025-11-07",
        "end": "2026-01-08",
    }
    assert deployment["coordinates"] == {
        "latitude": 38.51695783,
        "longitude": -122.1516454,
    }

    content = manifest["content"]
    assert content["media_type"] == "image"
    assert content["recording_interval"] == {
        "start": "2025-11-08T04:37:30-08:00",
        "end": "2025-11-08T04:37:30-08:00",
    }
    assert content["inventory"] == {
        "path": "metadata/file_metadata.csv",
        "sha256": _hash("csv"),
        "file_counts": {"image": 3},
        "total_bytes": 3000,
    }


def test_manifest_satisfies_the_published_json_schema():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    manifests = [
        _build([_image_row("img_1.jpg")]).manifest,
        _build(
            [_audio_row("rec_1.wav")],
            kind="audio",
            lookup=_audio_lookup(),
        ).manifest,
    ]

    for manifest in manifests:
        errors = sorted(
            validator.iter_errors(manifest),
            key=lambda error: list(error.absolute_path),
        )
        assert not errors, [error.message for error in errors]


def test_config_sidecars_count_and_hash_but_do_not_set_the_recording_window():
    rows = [
        _audio_row("rec_1.wav", recorded_datetime="2025-12-13T00:00:00-08:00"),
        _audio_row("rec_2.wav", recorded_datetime="2025-12-19T20:00:00-08:00"),
        _audio_row(
            "CONFIG_01.txt",
            file_type="config",
            file_size_bytes="500",
            recorded_datetime="",
        ),
    ]
    build = _build(rows, kind="audio", lookup=_audio_lookup())

    assert build.ok, build.errors
    content = build.manifest["content"]
    assert content["media_type"] == "audio"
    assert content["inventory"]["file_counts"] == {"audio": 2, "config": 1}
    assert content["inventory"]["total_bytes"] == 4500
    assert content["recording_interval"]["start"] == "2025-12-13T00:00:00-08:00"
    assert content["recording_interval"]["end"] == "2025-12-19T20:00:00-08:00"


def test_a_config_only_deployment_is_kept_with_a_warning():
    rows = [
        _audio_row(
            "CONFIG_01.txt",
            file_type="config",
            file_size_bytes="500",
            recorded_datetime="",
        )
    ]
    build = _build(rows, kind="audio", lookup=_audio_lookup())

    assert build.ok, build.errors
    assert build.manifest["content"]["inventory"]["file_counts"] == {"config": 1}
    assert build.manifest["content"]["recording_interval"]["start"] is None
    assert any("no media" in warning for warning in build.warnings)


def test_duplicate_flattened_filename_is_a_hard_error():
    rows = [_image_row("img_1.jpg"), _image_row("img_1.jpg"), _image_row("img_2.jpg")]
    build = _build(rows)

    assert not build.ok
    assert build.manifest is None
    assert any("duplicate filename" in error and "img_1.jpg" in error for error in build.errors)


def test_missing_lookup_row_refuses_to_guess_a_placement_window():
    build = _build([_image_row("img_1.jpg")], lookup={})

    assert not build.ok
    assert any("deployments.csv has no row" in error for error in build.errors)
    # The missing row is the only finding worth reporting; site identity hangs
    # off it and must not cascade a second error.
    assert not any("sites.csv" in error for error in build.errors)


def test_asset_tag_against_serial_warns_rather_than_failing():
    rows = [_audio_row("rec_1.wav")]
    build = _build(rows, kind="audio", lookup=_audio_lookup(device_id="0104"))

    assert build.ok, build.errors
    assert "device" not in build.manifest["deployment"]
    assert any("asset tag" in warning for warning in build.warnings)


def test_two_different_camera_serials_are_a_hard_error():
    build = _build([_image_row("img_1.jpg", camera_id="08021469")])

    assert not build.ok
    assert any("device_id disagrees" in error for error in build.errors)


def test_manifest_coordinates_come_from_the_plot_lookup():
    rows = [_image_row("img_1.jpg", latitude="", longitude="")]
    build = _build(
        rows,
        plot_coordinates={"latitude": "38.5", "longitude": "-122.1"},
    )

    assert build.ok, build.errors
    assert build.manifest["deployment"]["coordinates"] == {
        "latitude": 38.5,
        "longitude": -122.1,
    }
    assert any("plots.csv" in warning for warning in build.warnings)


def test_absent_coordinates_record_null_with_a_warning():
    rows = [_image_row("img_1.jpg")]
    build = _build(rows, plot_coordinates={})

    assert build.ok, build.errors
    assert build.manifest["deployment"]["coordinates"] is None
    assert any("plots.csv has no coordinates" in warning for warning in build.warnings)


def test_out_of_range_coordinate_is_a_hard_error():
    build = _build([_image_row("img_1.jpg", latitude="138.5")])

    assert not build.ok
    assert any("out of range" in error for error in build.errors)


def test_metadata_coordinates_that_disagree_with_plots_are_a_hard_error():
    rows = [_image_row("img_1.jpg", latitude="38.6")]
    build = _build(rows)

    assert not build.ok
    assert any("disagree with plots.csv" in error for error in build.errors)


def test_historical_coordinate_precision_does_not_create_a_false_disagreement():
    rows = [
        _image_row(
            "img_1.jpg",
            latitude="38.516957831234",
            longitude="-122.151645398765",
        )
    ]
    build = _build(rows)

    assert build.ok, build.errors
    assert build.manifest["deployment"]["coordinates"] == {
        "latitude": 38.51695783,
        "longitude": -122.1516454,
    }


def test_audio_rows_filed_in_the_image_document_are_a_hard_error():
    rows = [_audio_row("rec_1.wav")]
    build = _build(rows, kind="image", lookup=_audio_lookup())

    assert not build.ok
    assert any("filed in the image document" in error for error in build.errors)


def test_malformed_checksum_is_a_hard_error():
    build = _build([_image_row("img_1.jpg", file_hash_sha256="not-a-hash")])

    assert not build.ok
    assert any("64 hexadecimal characters" in error for error in build.errors)


def test_placement_disagreements_warn_and_the_lookup_still_wins():
    rows = [
        _image_row(
            "img_1.jpg",
            start_date="2025-11-04",
            recorded_datetime="2025-11-04T04:37:30-08:00",
        )
    ]
    build = _build(rows)

    assert build.ok, build.errors
    assert build.manifest["deployment"]["deployment_interval"]["start"] == "2025-11-07"
    assert any("disagrees with deployments.csv" in warning for warning in build.warnings)
    assert any("predates the curated placement start" in warning for warning in build.warnings)


def test_a_placement_datetime_naming_the_same_day_does_not_warn():
    # Some metadata generations write the image placement columns as
    # midnight-to-23:59:59 datetimes rather than bare dates.
    rows = [
        _image_row(
            "img_1.jpg",
            start_date="2025-11-07 00:00:00",
            end_date="2026-01-08 23:59:59",
        )
    ]
    build = _build(rows)

    assert build.ok, build.errors
    assert build.warnings == ()


def test_drifted_site_display_names_warn_but_sites_csv_is_recorded():
    rows = [_image_row("img_1.jpg", site_code="QuailRidge")]
    build = _build(rows)

    assert build.ok, build.errors
    assert build.manifest["deployment"]["site"] == {
        "id": "QuailRidge",
        "name": "Quail Ridge Reserve",
    }
    assert any("site_code" in warning for warning in build.warnings)


def test_current_site_column_pair_is_read_too():
    row = _image_row("img_1.jpg")
    del row["site"], row["site_full_name"]
    row["site_short_name"] = "QuailRidge"
    row["site_name"] = "Quail Ridge Reserve"
    build = _build([row])

    assert build.ok, build.errors
    assert build.warnings == ()


def test_no_rows_is_a_hard_error():
    build = _build([])

    assert not build.ok
    assert any("no rows staged" in error for error in build.errors)
