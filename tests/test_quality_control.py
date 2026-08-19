"""Tests for quality-control helpers (cassn.core.quality_control)."""
from cassn.core.quality_control import (
    build_box_verification_record,
    check_required_lookups,
    is_duplicate_media,
    validate_coordinates,
)


def test_media_duplicate_is_detected():
    seen = {"abc123"}
    assert is_duplicate_media("abc123", "image", seen) is True
    assert is_duplicate_media("abc123", "audio", seen) is True


def test_config_file_is_never_a_duplicate():
    # The bug: identical CONFIG.TXT across devices was dropped. Must be kept now.
    seen = {"abc123"}
    assert is_duplicate_media("abc123", "config", seen) is False


def test_new_media_hash_is_not_duplicate():
    assert is_duplicate_media("new999", "image", set()) is False


def test_box_verification_record_all_clear():
    rec = build_box_verification_record(
        hash_ok=True, hash_summary="all match", hash_issues=[],
        box_summary="file lists match", box_issues=[],
        verified_at="2026-06-30T12:00:00",
    )
    assert rec["verified"] is True
    assert rec["verified_at"] == "2026-06-30T12:00:00"
    assert rec["hash_verification"]["ok"] is True
    assert rec["box_file_list"]["missing_from_box"] == []


def test_box_verification_record_flags_issues():
    rec = build_box_verification_record(
        hash_ok=False, hash_summary="1 mismatch",
        hash_issues=[{"type": "sha1_mismatch", "filename": "a.jpg"}],
        box_summary="1 missing",
        box_issues=[
            {"type": "missing_from_box", "filename": "b.jpg"},
            {"type": "extra_on_box", "filename": "c.jpg"},
        ],
        verified_at="2026-06-30T12:00:00",
    )
    assert rec["verified"] is False
    assert rec["box_file_list"]["missing_from_box"] == ["b.jpg"]
    assert rec["box_file_list"]["extra_on_box"] == ["c.jpg"]
    assert rec["hash_verification"]["issues"] == [
        {"type": "sha1_mismatch", "filename": "a.jpg"}
    ]


def test_required_lookups_blocks_missing_camera_identity():
    findings = check_required_lookups(
        "p1_ML", is_audio=False,
        has_device_identity=False, has_coordinates=True, has_aru_row=True,
    )
    assert findings[0][0] == "lookup_device_identity"
    assert findings[0][1] == "error"
    assert "cameras.csv" in findings[0][2]


def test_required_lookups_blocks_missing_coordinates():
    findings = check_required_lookups(
        "p1_ML", is_audio=False,
        has_device_identity=True, has_coordinates=False, has_aru_row=True,
    )
    assert findings == [(
        "lookup_plot_coordinates",
        "error",
        "p1_ML: plot coordinates missing — plot not in plots.csv",
    )]


def test_required_lookups_warns_on_missing_aru_for_audio():
    findings = check_required_lookups(
        "p1_BD", is_audio=True,
        has_device_identity=True, has_coordinates=True, has_aru_row=False,
    )
    assert len(findings) == 1
    assert findings[0][0] == "lookup_aru_install"
    assert findings[0][1] == "warning"


def test_required_lookups_no_findings_when_all_present():
    findings = check_required_lookups(
        "p1_BD", is_audio=True,
        has_device_identity=True, has_coordinates=True, has_aru_row=True,
    )
    assert findings == []


def test_required_lookups_ignores_aru_for_cameras():
    # Cameras have no ARU; a missing ARU row must not warn for an image device.
    findings = check_required_lookups(
        "p1_ML", is_audio=False,
        has_device_identity=True, has_coordinates=True, has_aru_row=False,
    )
    assert findings == []


def _plot_entry(plot="1", lat="34.5", lon="-120.1", elevation="128"):
    return {
        "plot_number": plot,
        "latitude": lat,
        "longitude": lon,
        "elevation_m": elevation,
    }


def test_coordinates_and_elevation_in_range_produce_no_warnings():
    assert validate_coordinates([_plot_entry()]) == []


def test_missing_elevation_warns_without_blocking_coordinates():
    warnings = validate_coordinates([_plot_entry(elevation="")])
    assert warnings == ["Plot 1: elevation is missing"]


def test_non_numeric_elevation_warns():
    warnings = validate_coordinates([_plot_entry(elevation="128 m")])
    assert warnings == ["Plot 1: elevation is not numeric ('128 m')"]


def test_out_of_range_elevation_warns():
    # A plausible unit mix-up: feet entered into a metres column.
    warnings = validate_coordinates([_plot_entry(elevation="9500")])
    assert warnings == ["Plot 1: elevation 9500.0 m is outside expected study area bounds"]


def test_elevation_is_reported_alongside_a_bad_coordinate_pair():
    warnings = validate_coordinates([_plot_entry(lat="", lon="", elevation="")])
    assert warnings == [
        "Plot 1: coordinates are missing",
        "Plot 1: elevation is missing",
    ]


def test_elevation_warning_is_deduplicated_per_plot():
    entries = [_plot_entry(elevation=""), _plot_entry(elevation=""), _plot_entry(plot="2")]
    assert validate_coordinates(entries) == ["Plot 1: elevation is missing"]
