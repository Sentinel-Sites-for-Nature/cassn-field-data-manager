"""Tests for concise application-startup reporting."""

from types import SimpleNamespace

from cassn.__main__ import _lookup_status_message


def test_lookup_status_reports_successful_box_sync() -> None:
    result = SimpleNamespace(
        source="box",
        synced_files=("sites.csv", "plots.csv"),
    )

    assert _lookup_status_message(result) == (
        "Lookup tables: downloaded 2 files from Box and validated successfully."
    )


def test_lookup_status_reports_validated_offline_cache() -> None:
    result = SimpleNamespace(source="offline-cache", synced_files=())

    assert _lookup_status_message(result) == (
        "Lookup tables: using the last validated offline cache."
    )
