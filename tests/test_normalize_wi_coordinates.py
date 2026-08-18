"""Tests for the standalone WI coordinate-normalization utility."""

import csv

from utils.normalize_wi_coordinates import discover_wi_csvs, normalize_wi_csv


FIELDS = ["deployment_id", "longitude", "latitude", "placename"]


def _write_csv(path, rows, *, fieldnames=FIELDS):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _read_rows(path):
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def test_dry_run_reports_changes_without_writing(tmp_path):
    path = tmp_path / "WI_metadata" / "wildlife_insights_ML_deployments.csv"
    _write_csv(
        path,
        [
            {
                "deployment_id": "one",
                "longitude": "-120.1",
                "latitude": "34.123456789",
                "placename": "test",
            }
        ],
    )
    before = path.read_bytes()

    result = normalize_wi_csv(path)

    assert result.ok
    assert result.changed_rows == 1
    assert result.changed_cells == 2
    assert path.read_bytes() == before


def test_apply_normalizes_valid_values_and_preserves_invalid_values(tmp_path):
    path = tmp_path / "WI_metadata" / "wildlife_insights_SA_deployments.csv"
    _write_csv(
        path,
        [
            {
                "deployment_id": "one",
                "longitude": "-120.1",
                "latitude": "34.123456789",
                "placename": "valid",
            },
            {
                "deployment_id": "two",
                "longitude": "not-a-coordinate",
                "latitude": "",
                "placename": "invalid",
            },
        ],
    )

    result = normalize_wi_csv(path, apply=True)
    rows = _read_rows(path)

    assert result.ok
    assert result.changed_rows == 1
    assert result.changed_cells == 2
    assert result.invalid_cells == 1
    assert result.blank_cells == 1
    assert rows[0]["longitude"] == "-120.10000000"
    assert rows[0]["latitude"] == "34.12345679"
    assert rows[1]["longitude"] == "not-a-coordinate"
    assert rows[1]["latitude"] == ""
    assert list(rows[0]) == FIELDS


def test_apply_is_idempotent(tmp_path):
    path = tmp_path / "already.csv"
    _write_csv(
        path,
        [
            {
                "deployment_id": "one",
                "longitude": "-120.10000000",
                "latitude": "34.12345679",
                "placename": "test",
            }
        ],
    )

    result = normalize_wi_csv(path, apply=True)

    assert result.ok
    assert not result.changed


def test_missing_coordinate_column_is_an_error_and_does_not_write(tmp_path):
    path = tmp_path / "missing.csv"
    fields = ["deployment_id", "longitude"]
    _write_csv(path, [{"deployment_id": "one", "longitude": "-120.1"}], fieldnames=fields)
    before = path.read_bytes()

    result = normalize_wi_csv(path, apply=True)

    assert not result.ok
    assert result.errors == ["missing required column(s): latitude"]
    assert path.read_bytes() == before


def test_directory_discovery_only_selects_canonical_wi_csvs(tmp_path):
    first = tmp_path / "Reserve" / "Deployment" / "WI_metadata" / "wildlife_insights_ML_deployments.csv"
    second = tmp_path / "Reserve2" / "Deployment2" / "WI_metadata" / "wildlife_insights_SA_deployments.csv"
    unrelated = tmp_path / "Reserve" / "Deployment" / "image_file_metadata.csv"
    wrong_folder = tmp_path / "wildlife_insights_ML_deployments.csv"
    for path in (first, second, unrelated, wrong_folder):
        _write_csv(path, [])

    assert discover_wi_csvs(tmp_path) == sorted([first, second])
    assert discover_wi_csvs(unrelated) == [unrelated]
