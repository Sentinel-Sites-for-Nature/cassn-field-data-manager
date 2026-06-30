"""Tests for camera timestamp parsing (cassn.core.image_metadata).

These pin the corrected behavior so the future cloud port must match it.
"""
from cassn.core.image_metadata import parse_camera_recorded_datetime


def test_timestamp_with_offset_is_preserved():
    # Camera recorded its zone — existing behavior must not change.
    exif = {"DateTimeOriginal": "2025:12:04 15:48:05", "OffsetTimeOriginal": "-08:00"}
    assert parse_camera_recorded_datetime(exif) == "2025-12-04T15:48:05-08:00"


def test_timestamp_without_offset_assumes_pacific():
    # Camera did NOT record a zone (the bug). Returned "" before the fix.
    exif = {"DateTimeOriginal": "2025:12:04 15:48:05"}
    assert parse_camera_recorded_datetime(exif) == "2025-12-04T15:48:05-08:00"


def test_missing_datetime_returns_empty():
    # No date at all — still returns an empty string, unchanged.
    assert parse_camera_recorded_datetime({}) == ""
