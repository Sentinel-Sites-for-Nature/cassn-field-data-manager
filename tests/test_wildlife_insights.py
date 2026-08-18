"""Tests for Wildlife Insights deployment CSV generation."""

import csv

from cassn.export.wildlife_insights import (
    build_wi_rows,
    format_wi_coordinate,
    reshape_image_metadata_to_wi,
)


def test_format_wi_coordinate_pads_and_rounds_to_eight_places():
    assert format_wi_coordinate("34.1234") == "34.12340000"
    assert format_wi_coordinate("-120.1") == "-120.10000000"
    assert format_wi_coordinate("34.123456789") == "34.12345679"
    assert format_wi_coordinate("-120.123456785") == "-120.12345679"
    assert format_wi_coordinate("34.12345678") == "34.12345678"


def test_format_wi_coordinate_handles_blanks_invalid_values_and_zero():
    assert format_wi_coordinate(None) == ""
    assert format_wi_coordinate("  ") == ""
    assert format_wi_coordinate("not-a-coordinate") == "not-a-coordinate"
    assert format_wi_coordinate("NaN") == "NaN"
    assert format_wi_coordinate("-0.000000001") == "0.00000000"


def test_build_wi_rows_formats_lookup_coordinates():
    rows_by_type = build_wi_rows(
        {
            "organization": "UC",
            "site_short_name": "TEST",
            "deployment_start": "2026-01-01",
            "deployment_end": "2026-02-01",
        },
        [{"device_type": "ML", "plot_number": "1"}],
        {("TEST", 1, "ML"): {"camera_id": "camera-1"}},
        {("TEST", 1): {"latitude": "34.1234", "longitude": "-120.123456789"}},
        {},
    )

    row = rows_by_type["ML"][0]
    assert row["latitude"] == "34.12340000"
    assert row["longitude"] == "-120.12345679"


def test_reshape_image_metadata_formats_coordinates(tmp_path):
    image_csv = tmp_path / "image_file_metadata.csv"
    with image_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "file_type",
                "device_type",
                "deployment_id",
                "latitude",
                "longitude",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "file_type": "image",
                "device_type": "SA",
                "deployment_id": "deployment-1",
                "latitude": "34.12",
                "longitude": "-120.123456789",
            }
        )

    row = reshape_image_metadata_to_wi(image_csv)["SA"][0]
    assert row["latitude"] == "34.12000000"
    assert row["longitude"] == "-120.12345679"
