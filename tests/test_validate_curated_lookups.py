"""Tests for the read-only curated lookup validator CLI."""

from __future__ import annotations

from utils.validate_curated_lookups import main, validate_curated_lookup_directory

from tests.test_lookup_sync import _canonical_files


def test_validator_reports_counts_and_hashes(tmp_path):
    _canonical_files(tmp_path)

    report = validate_curated_lookup_directory(tmp_path)

    assert "deployments=1; deployment_events=1" in report
    assert "deployments.csv=" in report
    assert "deployment_events.csv=" in report


def test_validator_cli_is_nonzero_for_invalid_directory(tmp_path, capsys):
    assert main([str(tmp_path)]) == 1
    assert "Invalid curated lookup directory" in capsys.readouterr().err
