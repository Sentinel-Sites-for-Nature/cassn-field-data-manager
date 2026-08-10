"""Tests for the non-destructive Survey123 legacy candidate transformer."""

from __future__ import annotations

import csv
import json
import stat
from pathlib import Path

from utils.survey123_legacy_transform import transform_legacy_snapshot


def write_csv(path: Path, fields: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        writer.writerows(rows)


def write_role(snapshot: Path, role: str, attributes: list[dict]) -> None:
    page = snapshot / role / "layers" / "0" / "pages" / "page-0001.json"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        json.dumps({"features": [{"attributes": row} for row in attributes]}),
        encoding="utf-8",
    )


def make_fixture(tmp_path: Path) -> tuple[Path, Path]:
    lookup = tmp_path / "lookups"
    write_csv(
        lookup / "sites.csv",
        ["site_name", "site_code", "label_code"],
        [["Test Reserve", "TestSite", "TST"]],
    )
    write_csv(
        lookup / "plots.csv",
        [
            "site_code",
            "plot_number",
            "plot_name",
            "plot_latitude",
            "plot_longitude",
            "plot_description",
        ],
        [
            ["TestSite", "1", "One", "38", "-122", ""],
            ["TestSite", "2", "Two", "39", "-123", ""],
        ],
    )
    write_csv(
        lookup / "cameras.csv",
        [
            "site_code",
            "plot_number",
            "device_type",
            "camera_id",
            "feature_type",
            "sensor_height",
            "sensor_orientation",
        ],
        [
            ["TestSite", "1", "ML", "CAM1", "Trail game", "Knee height", "Parallel"],
            ["TestSite", "2", "SA", "0000", "None", "Knee height", "Pointed Downward"],
        ],
    )
    write_csv(
        lookup / "ARUs.csv",
        [
            "deployment_event_id",
            "site_code",
            "plot_number",
            "device_type",
            "mounted_on",
            "sensor_height_meters",
            "ARU_status",
        ],
        [
            ["UC_TestSite_20260110", "TestSite", "1", "BD", "Pole", "1", ""],
            ["UC_TestSite_20260110", "TestSite", "1", "BT", "Pole", "2.5", ""],
        ],
    )
    write_csv(
        lookup / "deployments.csv",
        ["site_code", "deployment_start", "deployment_end", "deployment_event_id"],
        [["TestSite", "2026-01-01", "2026-01-10", "UC_TestSite_20260110"]],
    )
    (lookup / "wi_config.json").write_text(json.dumps({"project_id_ML": "p"}))
    (lookup / "soundhub_config.json").write_text(json.dumps({"ARU_make": "AudioMoth"}))

    snapshot = tmp_path / "snapshots" / "20260809T000000Z"
    snapshot.mkdir(parents=True)
    snapshot.joinpath("manifest.json").write_text(
        json.dumps(
            {
                "sources": [
                    {"role": role, "form_item_id": f"{role}-item"}
                    for role in ("ml_camera", "sa_camera", "aru", "retrieval")
                ]
            }
        )
    )
    base_time = 1767301200000  # 2026-01-01 Pacific
    write_role(
        snapshot,
        "ml_camera",
        [
            {
                "objectid": 1,
                "globalid": "ml-one",
                "siteID": "TST",
                "plot_number": "1",
                "camera1ID": "CAM1",
                "site_date": base_time,
                "deploymentEndDateTime": base_time + 60_000,
                "mlCameraLocation": "Trail_game",
            }
        ],
    )
    write_role(
        snapshot,
        "sa_camera",
        [
            {
                "objectid": 1,
                "globalid": "sa-one",
                "siteID": "TST",
                "plot_number": "2",
                "camera1ID": "0000",
                "site_date": base_time,
                "deploymentEndDateTime": base_time + 120_000,
            }
        ],
    )
    write_role(
        snapshot,
        "aru",
        [
            {
                "objectid": 1,
                "globalid": "aru-one",
                "siteID": "TST",
                "plot_number": "1",
                "single_aru": "no",
                "AM_bird_ID": "0000",
                "AM_bat_ID": "",
                "site_date": base_time,
                "deployment_endtime": base_time + 180_000,
            }
        ],
    )
    write_role(
        snapshot,
        "retrieval",
        [
            {
                "objectid": 1,
                "globalid": "ret-one",
                "siteID": "TST",
                "Plot": "1",
                "MethodsDeployed": None,
                "site_date": base_time + 5 * 86_400_000,
                "calcEndTime": base_time + 5 * 86_400_000 + 60_000,
            }
        ],
    )
    return snapshot, lookup


def test_transform_preserves_contract_and_allows_legacy_ids(tmp_path):
    snapshot, lookup = make_fixture(tmp_path)

    output, manifest = transform_legacy_snapshot(snapshot, lookup, tmp_path / "candidates")

    with (output / "deployments.csv").open() as handle:
        deployments = list(csv.DictReader(handle))
    with (output / "devices.csv").open() as handle:
        devices = list(csv.DictReader(handle))

    assert len(deployments) == 4
    assert manifest["counts"]["blocking_issues"] == 0
    assert {row["device_id"] for row in devices} >= {"CAM1", "0000", ""}
    ml = next(row for row in deployments if row["device_type"] == "ML")
    assert ml["plot_number"] == "1"
    assert ml["plot_resolution"] == "survey"
    assert ml["feature_type"] == "Trail game"
    assert ml["sensor_height"] == "Knee height"
    bd = next(row for row in deployments if row["device_type"] == "BD")
    assert bd["deployment_end_reason"] == "retrieved"
    assert bd["retrieval_methods"] == ""
    assert bd["mounted_on"] == "Pole"
    assert (output / "wi_config.json").is_file()
    assert (output / "soundhub_config.json").is_file()
    if __import__("os").name != "nt":
        assert stat.S_IMODE(output.stat().st_mode) == 0o700
        assert all(
            stat.S_IMODE(path.stat().st_mode) == 0o600
            for path in output.iterdir()
            if path.is_file()
        )


def test_transform_reports_unresolvable_plot_without_aborting_other_rows(tmp_path):
    snapshot, lookup = make_fixture(tmp_path)
    ml_page = snapshot / "ml_camera" / "layers" / "0" / "pages" / "page-0001.json"
    payload = json.loads(ml_page.read_text())
    payload["features"][0]["attributes"]["camera1ID"] = "UNKNOWN"
    payload["features"][0]["attributes"]["plot_number"] = None
    ml_page.write_text(json.dumps(payload))

    output, manifest = transform_legacy_snapshot(snapshot, lookup, tmp_path / "candidates")

    assert manifest["counts"]["blocking_issues"] == 1
    with (output / "issues.csv").open() as handle:
        issues = list(csv.DictReader(handle))
    assert issues[0]["code"] == "unknown_plot"
    with (output / "deployments.csv").open() as handle:
        deployments = list(csv.DictReader(handle))
    assert len(deployments) == 3


def test_same_day_retrieval_closes_prior_device_not_replacement(tmp_path):
    snapshot, lookup = make_fixture(tmp_path)
    ml_page = snapshot / "ml_camera" / "layers" / "0" / "pages" / "page-0001.json"
    payload = json.loads(ml_page.read_text())
    first = payload["features"][0]["attributes"]
    replacement = dict(first)
    replacement.update(
        {
            "objectid": 2,
            "globalid": "ml-replacement",
            "camera1ID": "CAM2",
            "site_date": first["site_date"] + 5 * 86_400_000,
            "deploymentEndDateTime": first["site_date"] + 5 * 86_400_000 + 30_000,
        }
    )
    payload["features"].append({"attributes": replacement})
    ml_page.write_text(json.dumps(payload))

    output, _manifest = transform_legacy_snapshot(snapshot, lookup, tmp_path / "candidates")

    with (output / "deployments.csv").open() as handle:
        deployments = list(csv.DictReader(handle))
    first_row = next(row for row in deployments if row["device_id"] == "CAM1")
    replacement_row = next(row for row in deployments if row["device_id"] == "CAM2")
    assert first_row["deployment_end_reason"] == "redeployed"
    assert first_row["deployment_end_date"] == replacement_row["deployment_start_date"]
    assert replacement_row["deployment_end_reason"] == "current_event_end"
    assert replacement_row["deployment_end_date"] == "2026-01-10"
