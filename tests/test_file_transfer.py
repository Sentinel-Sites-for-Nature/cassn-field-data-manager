"""Tests for crash-safe SD-card/external-drive transfers."""

from pathlib import Path

import pytest

from cassn.core import file_transfer
from cassn.core.file_transfer import (
    FileTransferError,
    copy_file_single_read_verified,
    copy_file_verified,
    hash_file_with_retries,
)
from cassn.core.hashing import sha256_sha1


def test_verified_copy_commits_matching_bytes(tmp_path):
    source = tmp_path / "source.wav"
    destination = tmp_path / "staged.wav"
    source.write_bytes(b"field-data")
    sha256, sha1 = sha256_sha1(source)

    result = copy_file_verified(
        source,
        destination,
        expected_sha256=sha256,
        expected_sha1=sha1,
        retry_delay=0,
    )

    assert destination.read_bytes() == b"field-data"
    assert result.attempts == 1
    assert not list(tmp_path.glob("*.partial"))


def test_single_read_copy_verifies_and_commits_after_hash_acceptance(tmp_path):
    source = tmp_path / "source.jpg"
    destination = tmp_path / "staged.jpg"
    source.write_bytes(b"camera-data" * 100)
    accepted = []

    result = copy_file_single_read_verified(
        source,
        destination,
        accept_hash=lambda sha256, sha1: accepted.append((sha256, sha1)) or True,
        retry_delay=0,
    )

    assert result.accepted
    assert result.attempts == 1
    assert destination.read_bytes() == source.read_bytes()
    assert accepted == [(result.sha256, result.sha1)]


def test_single_read_copy_discards_verified_duplicate_without_destination(tmp_path):
    source = tmp_path / "source.jpg"
    destination = tmp_path / "staged.jpg"
    source.write_bytes(b"duplicate")

    result = copy_file_single_read_verified(
        source,
        destination,
        accept_hash=lambda _sha256, _sha1: False,
        retry_delay=0,
    )

    assert not result.accepted
    assert not destination.exists()
    assert not list(tmp_path.glob("*.partial"))


def test_failed_copy_never_damages_existing_destination(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"
    destination = tmp_path / "staged.wav"
    source.write_bytes(b"new bytes")
    destination.write_bytes(b"known good bytes")
    sha256, sha1 = sha256_sha1(source)

    def fail_copy(_source: Path, _destination: Path):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(file_transfer.shutil, "copy2", fail_copy)

    with pytest.raises(FileTransferError, match="I/O failure"):
        copy_file_verified(
            source,
            destination,
            expected_sha256=sha256,
            expected_sha1=sha1,
            max_attempts=3,
            retry_delay=0,
        )

    assert destination.read_bytes() == b"known good bytes"


def test_source_hash_retries_transient_io_error(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"
    source.write_bytes(b"recoverable")
    real_hasher = file_transfer.sha256_sha1
    calls = 0

    def flaky_hasher(path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(5, "Input/output error")
        return real_hasher(path)

    monkeypatch.setattr(file_transfer, "sha256_sha1", flaky_hasher)

    result = hash_file_with_retries(source, retry_delay=0)

    assert result.attempts == 2
    assert calls == 2
