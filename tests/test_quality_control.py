"""Tests for duplicate-detection policy (cassn.core.quality_control)."""
from cassn.core.quality_control import is_duplicate_media


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
