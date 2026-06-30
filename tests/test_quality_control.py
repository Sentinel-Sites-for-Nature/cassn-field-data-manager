"""Tests for quality-control helpers (cassn.core.quality_control)."""
from cassn.core.quality_control import (
    build_box_verification_record,
    is_duplicate_media,
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
