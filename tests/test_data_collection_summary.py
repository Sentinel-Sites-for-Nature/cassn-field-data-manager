from __future__ import annotations

import csv
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook, load_workbook
import pytest
from cassn.reporting import data_collection_summary as reporting

from cassn.reporting.data_collection_summary import (
    MetadataDocument,
    DataCollectionSummary,
    WISubmissionTracker,
    build_data_collection_summary,
    dated_output_path,
    default_box_year_root,
    discover_box_year_metadata,
    json_sidecar_path,
    load_wi_submission_tracker,
    publish_data_collection_summary,
    render_data_collection_summary_json,
    render_data_collection_summary_workbook,
)


def _image(
    deployment_id: str,
    filename: str,
    *,
    site_name: str = "Sedgwick Reserve",
    box: str = "True",
    wi: str = "False",
    size: str = "1000",
) -> dict[str, str]:
    return {
        "deployment_id": deployment_id,
        "filename": filename,
        "site_name": site_name,
        "file_type": "image",
        "file_size_bytes": size,
        "is_uploaded_to_box": box,
        "is_submitted_to_wi": wi,
    }


def _audio(
    deployment_id: str,
    filename: str,
    device_type: str,
    seconds: str,
    *,
    site_name: str = "Sedgwick Reserve",
    box: str = "True",
    soundhub: str = "False",
    size: str = "2000",
) -> dict[str, str]:
    return {
        "deployment_id": deployment_id,
        "filename": filename,
        "site_name": site_name,
        "file_type": "audio",
        "device_type": device_type,
        "recording_duration_sec": seconds,
        "file_size_bytes": size,
        "is_uploaded_to_box": box,
        "is_submitted_to_soundhub": soundhub,
    }


def test_summary_uses_full_site_name_box_provenance_and_wi_tracker(tmp_path):
    wi_deployment = "UC_Sedgwick_plot1_ML_20260610"
    box_only_deployment = "UC_Sedgwick_plot2_ML_20260610"
    documents = [
        MetadataDocument(
            tmp_path / "image_file_metadata.csv",
            "image",
            (
                _image(wi_deployment, "one.jpg"),
                _image(box_only_deployment, "two.jpg"),
                _image(wi_deployment, "not-confirmed.jpg", box="False"),
            ),
        ),
        MetadataDocument(
            tmp_path / "audio_file_metadata.csv",
            "audio",
            (
                _audio("bird", "bird.wav", "BD", "120", soundhub="True"),
                _audio("bat", "bat.wav", "BT", "60"),
            ),
        ),
    ]
    tracker = WISubmissionTracker(
        tmp_path / "tracker.xlsx",
        {
            wi_deployment: "WI - Validation not started",
            box_only_deployment: "Box",
        },
    )

    result = build_data_collection_summary(2026, documents, wi_tracker=tracker)

    assert result.ok
    assert result.site_count == 1
    assert list(result.by_site) == ["Sedgwick Reserve"]
    assert result.totals.images_captured == 2
    assert result.totals.images_classified_ai == 1
    assert result.totals.bird_minutes_recorded == Decimal("2")
    assert result.totals.bird_minutes_classified_ai == Decimal("2")
    assert result.totals.bat_minutes_recorded == Decimal("1")
    assert result.totals.data_bytes == 6000
    assert result.quality_counts["unconfirmed_box_upload_rows"] == 1
    assert result.quality_counts["wi_tracker_metadata_backfill_deployments"] == 1
    assert result.quality_counts["wi_tracker_metadata_conflicts"] == 0
    assert result.totals.images_validated_human == 0


def test_only_validation_complete_counts_as_human_validated(tmp_path):
    statuses = {
        "complete": "WI - Validation Complete",
        "in-progress": "WI - Validation in Progress",
        "not-started": "WI - Validation not started",
        "box-only": "Box",
    }
    documents = [
        MetadataDocument(
            tmp_path / "image_file_metadata.csv",
            "image",
            tuple(_image(deployment, f"{deployment}-{n}.jpg") for deployment in statuses for n in (1, 2))
            + (_image("complete", "other-site.jpg", site_name="Angelo Coast Range Reserve"),),
        )
    ]
    tracker = WISubmissionTracker(tmp_path / "tracker.xlsx", statuses)

    result = build_data_collection_summary(2026, documents, wi_tracker=tracker)

    assert result.ok
    assert result.totals.images_captured == 9
    assert result.totals.images_classified_ai == 7
    assert result.totals.images_validated_human == 3
    assert result.by_site["Sedgwick Reserve"].images_validated_human == 2
    assert result.by_site["Angelo Coast Range Reserve"].images_validated_human == 1
    assert result.quality_counts["wi_tracker_validated_deployments"] == 1

    path = tmp_path / "summary.xlsx"
    render_data_collection_summary_workbook(result, path)
    workbook = load_workbook(path, data_only=True)
    try:
        assert workbook["Summary"]["A8"].value == "Images validated by humans"
        assert workbook["Summary"]["B8"].value == 3
        assert workbook["By Site"]["D4"].value == "Images Validated by Humans"
    finally:
        workbook.close()
    payload = reporting.data_collection_summary_dict(result)
    assert payload["totals"]["images_validated_human"] == 3


def test_human_validation_needs_tracker(tmp_path):
    documents = [
        MetadataDocument(tmp_path / "image_file_metadata.csv", "image", (_image("d", "one.jpg", wi="True"),))
    ]

    result = build_data_collection_summary(2026, documents)

    assert result.totals.images_classified_ai == 1
    assert result.totals.images_validated_human == 0
    assert any("human-validated images cannot be counted" in warning for warning in result.warnings)


def test_missing_duration_does_not_use_schedule_duration(tmp_path):
    row = _audio("bird", "bird.wav", "BD", "")
    row["duration"] = "12:00"
    result = build_data_collection_summary(
        2026,
        [MetadataDocument(tmp_path / "audio_file_metadata.csv", "audio", (row,))],
    )

    assert result.totals.bird_minutes_recorded == 0
    assert result.quality_counts["missing_audio_duration_rows"] == 1


def test_duplicate_key_with_same_hash_is_counted_once(tmp_path):
    first = _audio("bird", "bird.wav", "BD", "120")
    first["file_hash_sha256"] = "abc123"
    second = dict(first)
    second["legacy_extra_column"] = "historical copy"
    result = build_data_collection_summary(
        2026,
        [MetadataDocument(tmp_path / "audio_file_metadata.csv", "audio", (first, second))],
    )

    assert result.ok
    assert result.totals.bird_minutes_recorded == 2
    assert result.quality_counts["exact_duplicate_rows"] == 1


def test_duplicate_blank_site_name_is_only_backfilled_once(tmp_path):
    first = _audio("bird", "bird.wav", "BD", "120", site_name="")
    first["file_hash_sha256"] = "abc123"
    second = dict(first)
    result = build_data_collection_summary(
        2026,
        [
            MetadataDocument(
                tmp_path / "audio_file_metadata.csv",
                "audio",
                (first, second),
                "Quail Ridge Reserve",
            )
        ],
    )

    assert result.ok
    assert result.quality_counts["exact_duplicate_rows"] == 1
    assert result.quality_counts["site_name_from_box_folder_rows"] == 1


def test_discovers_only_event_root_metadata(tmp_path):
    event = tmp_path / "Reserve" / "Event"
    raw = event / "raw_data" / "p1_ML"
    raw.mkdir(parents=True)
    fields = list(_image("deployment", "one.jpg"))
    with (event / "image_file_metadata.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerow(_image("deployment", "one.jpg"))
    (raw / "image_file_metadata.csv").write_text("should,not,be,read\n")

    documents = discover_box_year_metadata(tmp_path)

    assert len(documents) == 1
    assert documents[0].path == event / "image_file_metadata.csv"
    assert documents[0].site_name_hint == "Reserve"


def test_blank_legacy_site_name_uses_full_box_reserve_folder_name(tmp_path):
    row = _image("deployment", "one.jpg", site_name="")
    result = build_data_collection_summary(
        2026,
        [
            MetadataDocument(
                tmp_path / "image_file_metadata.csv",
                "image",
                (row,),
                "Angelo Coast Range Reserve",
            )
        ],
    )

    assert result.ok
    assert list(result.by_site) == ["Angelo Coast Range Reserve"]
    assert result.quality_counts["site_name_from_box_folder_rows"] == 1


def test_loads_exported_wi_tracker_workbook(tmp_path):
    path = tmp_path / "tracker.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Deployment Tracker"
    sheet.append(["Project", "Deployment", "Status"])
    sheet.append(["UCNRS Mammal", "deployment-1", "WI - Partial Validation"])
    sheet.append(["UCNRS Mammal", "deployment-2", "Box"])
    workbook.save(path)
    workbook.close()

    tracker = load_wi_submission_tracker(path)

    assert tracker.submitted("deployment-1") is True
    assert tracker.submitted("deployment-2") is False
    assert tracker.submitted("missing") is None


def test_renders_only_the_three_approved_sheets_in_requested_style(tmp_path):
    result = build_data_collection_summary(
        2026,
        [
            MetadataDocument(
                tmp_path / "image_file_metadata.csv",
                "image",
                (_image("deployment", "one.jpg", wi="True"),),
            )
        ],
    )
    path = tmp_path / "summary.xlsx"

    render_data_collection_summary_workbook(
        result,
        path,
        generated_at=datetime.fromisoformat("2026-08-31T12:00:00-07:00"),
    )

    workbook = load_workbook(path, data_only=True)
    try:
        assert workbook.sheetnames == ["Summary", "By Site", "Data Quality"]
        summary_sheet = workbook["Summary"]
        by_site_sheet = workbook["By Site"]
        quality_sheet = workbook["Data Quality"]

        assert summary_sheet["A1"].value == "CA-SSN 2026 Data Collection Summary"
        assert summary_sheet["A2"].value == (
            "Box-uploaded media stored in CASSN/data/2026 as of 2026-08-31."
        )
        assert summary_sheet["A5"].value == "Sites with collected data"
        assert summary_sheet["B5"].value == 1
        assert "; no contractor-classification total yet" not in summary_sheet["D10"].value

        assert by_site_sheet["A1"].value == "CA-SSN 2026 Data Collection by Site"
        assert by_site_sheet["A2"].value is None
        assert by_site_sheet["A3"].value is None
        assert by_site_sheet["A4"].value == "Site Name"
        assert by_site_sheet["A5"].value == "Sedgwick Reserve"

        for sheet, last_column in (
            (summary_sheet, 4),
            (by_site_sheet, 8),
            (quality_sheet, 4),
        ):
            header_cell = sheet.cell(4, 1)
            assert header_cell.fill.fill_type == "solid"
            assert header_cell.fill.fgColor.rgb == "FF000000"
            assert header_cell.font.bold
            assert header_cell.font.color.rgb.endswith("FFFFFF")
            for row in range(4, sheet.max_row + 1):
                for column in range(1, last_column + 1):
                    cell = sheet.cell(row, column)
                    assert all(
                        edge.style == "thin"
                        for edge in (
                            cell.border.left,
                            cell.border.right,
                            cell.border.top,
                            cell.border.bottom,
                        )
                    )

            outside_cell = sheet.cell(4, last_column + 1)
            assert outside_cell.fill.fill_type is None
            assert all(
                edge.style is None
                for edge in (
                    outside_cell.border.left,
                    outside_cell.border.right,
                    outside_cell.border.top,
                    outside_cell.border.bottom,
                )
            )

        assert summary_sheet["A5"].fill.fgColor.rgb.endswith("D9D9D9")
        assert summary_sheet["A6"].fill.fgColor.rgb.endswith("FFFFFF")
    finally:
        workbook.close()


def test_dated_output_path_uses_generation_date_folder(tmp_path):
    path = dated_output_path(tmp_path, 2026, datetime(2026, 8, 31).date())
    assert path == tmp_path / "2026-08-31" / "CA-SSN_data_collection_summary_2026.xlsx"


def test_default_box_year_root_uses_renamed_data_folder():
    assert default_box_year_root(2026).parts[-3:] == ("CASSN", "data", "2026")


def test_json_sidecar_matches_workbook_figures(tmp_path):
    result = build_data_collection_summary(
        2026,
        [
            MetadataDocument(
                tmp_path / "image_file_metadata.csv",
                "image",
                (
                    _image("deployment", "one.jpg", wi="True", size="2000"),
                    _image("deployment", "two.jpg", size="3000"),
                ),
            )
        ],
    )
    workbook_path = tmp_path / "CA-SSN_data_collection_summary_2026.xlsx"
    path = json_sidecar_path(workbook_path)

    render_data_collection_summary_json(
        result,
        path,
        generated_at=datetime.fromisoformat("2026-08-31T12:00:00-07:00"),
    )

    assert path == tmp_path / "CA-SSN_data_collection_summary_2026.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["year"] == 2026
    assert payload["generated_at"] == "2026-08-31T12:00:00-07:00"
    assert payload["site_count"] == 1
    assert payload["totals"]["images_captured"] == 2
    assert payload["totals"]["images_classified_ai"] == 1
    assert payload["totals"]["data_bytes"] == 5000
    assert [site["site_name"] for site in payload["by_site"]] == ["Sedgwick Reserve"]
    assert payload["quality_counts"]["confirmed_box_uploaded_media_rows"] == 2


def _publication_fixture(tmp_path):
    summary = DataCollectionSummary(2026)
    summary.totals.images_captured = 2
    output = tmp_path / "report.xlsx"
    sidecar = output.with_suffix(".json")
    return summary, output, sidecar


def test_publish_workbook_and_json_share_totals_and_timestamp(tmp_path):
    summary, output, sidecar = _publication_fixture(tmp_path)
    timestamp = datetime.fromisoformat("2026-10-09T12:00:00-07:00")
    assert publish_data_collection_summary(summary, output, generated_at=timestamp) == (output, sidecar)
    workbook = load_workbook(output, read_only=True, data_only=True)
    try:
        assert workbook["Summary"]["B6"].value == 2
        assert timestamp.strftime("%Y-%m-%d") in workbook["Summary"]["A2"].value
    finally:
        workbook.close()
    data = json.loads(sidecar.read_text())
    assert data["totals"]["images_captured"] == 2
    assert data["generated_at"] == timestamp.isoformat()
    assert not list(tmp_path.glob(".summary-publish-*"))


@pytest.mark.parametrize("renderer", ["render_data_collection_summary_workbook", "render_data_collection_summary_json"])
def test_render_failure_preserves_previous_report_pair(tmp_path, monkeypatch, renderer):
    summary, output, sidecar = _publication_fixture(tmp_path)
    output.write_bytes(b"old workbook")
    sidecar.write_bytes(b"old JSON")

    def fail(*_args, **_kwargs):
        raise OSError("render failed")

    monkeypatch.setattr(reporting, renderer, fail)
    with pytest.raises(OSError, match="render failed"):
        publish_data_collection_summary(summary, output)
    assert output.read_bytes() == b"old workbook"
    assert sidecar.read_bytes() == b"old JSON"
    assert not list(tmp_path.glob(".summary-publish-*"))


@pytest.mark.parametrize("previous", [True, False])
@pytest.mark.parametrize("failed_output", ["workbook", "json"])
def test_replacement_failure_restores_previous_outputs(tmp_path, monkeypatch, previous, failed_output):
    summary, output, sidecar = _publication_fixture(tmp_path)
    if previous:
        output.write_bytes(b"old workbook")
        sidecar.write_bytes(b"old JSON")
    target = output if failed_output == "workbook" else sidecar
    original_replace = reporting.os.replace

    def replace(source, destination):
        if Path(destination) == target and Path(source).name == target.name:
            raise OSError("replacement failed")
        return original_replace(source, destination)

    monkeypatch.setattr(reporting.os, "replace", replace)
    with pytest.raises(OSError, match="replacement failed"):
        publish_data_collection_summary(summary, output)
    if previous:
        assert output.read_bytes() == b"old workbook"
        assert sidecar.read_bytes() == b"old JSON"
    else:
        assert not output.exists()
        assert not sidecar.exists()
    assert not list(tmp_path.glob(".summary-publish-*"))


def test_backup_failure_does_not_change_either_output(tmp_path, monkeypatch):
    summary, output, sidecar = _publication_fixture(tmp_path)
    output.write_bytes(b"old workbook")
    sidecar.write_bytes(b"old JSON")

    def fail(*_args):
        raise OSError("backup failed")

    monkeypatch.setattr(reporting.shutil, "copy2", fail)
    with pytest.raises(OSError, match="backup failed"):
        publish_data_collection_summary(summary, output)
    assert output.read_bytes() == b"old workbook"
    assert sidecar.read_bytes() == b"old JSON"


def test_failed_rollback_retains_recovery_backups(tmp_path, monkeypatch):
    summary, output, sidecar = _publication_fixture(tmp_path)
    output.write_bytes(b"old workbook")
    sidecar.write_bytes(b"old JSON")
    original_replace = reporting.os.replace

    def replace(source, destination):
        if Path(destination) == sidecar or (
            Path(destination) == output and Path(source).name.startswith("previous-")
        ):
            raise OSError("disk failed")
        return original_replace(source, destination)

    monkeypatch.setattr(reporting.os, "replace", replace)
    with pytest.raises(RuntimeError, match="Recovery files retained at") as error:
        publish_data_collection_summary(summary, output)
    recovery = next(tmp_path.glob(".summary-publish-*"))
    assert str(recovery) in str(error.value)
    assert (recovery / "previous-report.xlsx").read_bytes() == b"old workbook"
    assert (recovery / "previous-report.json").read_bytes() == b"old JSON"
    assert sidecar.read_bytes() == b"old JSON"


def test_cli_failure_does_not_claim_success_or_replace_existing_pair(tmp_path, monkeypatch, capsys):
    from utils import generate_data_collection_summary as cli

    summary, _output, _sidecar = _publication_fixture(tmp_path)
    output = tmp_path / "2026-10-09/CA-SSN_data_collection_summary_2026.xlsx"
    output.parent.mkdir()
    sidecar = output.with_suffix(".json")
    output.write_bytes(b"old workbook")
    sidecar.write_bytes(b"old JSON")
    monkeypatch.setattr(cli, "discover_box_year_metadata", lambda _root: [])
    monkeypatch.setattr(cli, "build_data_collection_summary", lambda *_args, **_kwargs: summary)

    def fail(*_args, **_kwargs):
        raise OSError("JSON failed")

    monkeypatch.setattr(reporting, "render_data_collection_summary_json", fail)
    assert cli.main([
        "--year", "2026", "--box-year-root", str(tmp_path),
        "--output-root", str(tmp_path), "--generation-date", "2026-10-09", "--apply",
        "--no-wi-tracker",
    ]) == 2
    captured = capsys.readouterr()
    assert "Wrote:" not in captured.out
    assert "JSON failed" in captured.err
    assert output.read_bytes() == b"old workbook"
    assert sidecar.read_bytes() == b"old JSON"


def test_publish_archives_wi_tracker_snapshot_beside_report(tmp_path):
    summary, output, sidecar = _publication_fixture(tmp_path)
    export = tmp_path / "downloads" / "WI Processing Tracker (3).XLSX"
    export.parent.mkdir()
    export.write_bytes(b"tracker export")
    summary.wi_tracker_source = str(export)
    stale = tmp_path / "WI Processing Tracker.csv"
    stale.write_bytes(b"earlier same-day csv")

    publish_data_collection_summary(summary, output)

    snapshot = tmp_path / "WI Processing Tracker.xlsx"
    assert snapshot.read_bytes() == b"tracker export"
    assert not stale.exists()
    assert json.loads(sidecar.read_text())["wi_tracker_source"] == str(snapshot)
    assert summary.wi_tracker_source == str(export)
    assert not list(tmp_path.glob(".summary-publish-*"))


def test_publish_without_tracker_removes_stale_snapshot(tmp_path):
    summary, output, sidecar = _publication_fixture(tmp_path)
    stale = tmp_path / "WI Processing Tracker.xlsx"
    stale.write_bytes(b"earlier same-day export")

    publish_data_collection_summary(summary, output)

    assert not stale.exists()
    assert json.loads(sidecar.read_text())["wi_tracker_source"] == ""


def test_publish_can_reuse_archived_snapshot_as_tracker(tmp_path):
    summary, output, _sidecar = _publication_fixture(tmp_path)
    snapshot = tmp_path / "WI Processing Tracker.csv"
    snapshot.write_bytes(b"archived export")
    summary.wi_tracker_source = str(snapshot)

    publish_data_collection_summary(summary, output)

    assert snapshot.read_bytes() == b"archived export"


def test_snapshot_replacement_failure_restores_previous_folder(tmp_path, monkeypatch):
    summary, output, sidecar = _publication_fixture(tmp_path)
    output.write_bytes(b"old workbook")
    sidecar.write_bytes(b"old JSON")
    snapshot = tmp_path / "WI Processing Tracker.csv"
    snapshot.write_bytes(b"old tracker")
    export = tmp_path / "export.csv"
    export.write_bytes(b"new tracker")
    summary.wi_tracker_source = str(export)
    original_replace = reporting.os.replace

    def replace(source, destination):
        if Path(destination) == snapshot and Path(source).name.startswith("tracker-"):
            raise OSError("snapshot failed")
        return original_replace(source, destination)

    monkeypatch.setattr(reporting.os, "replace", replace)
    with pytest.raises(OSError, match="snapshot failed"):
        publish_data_collection_summary(summary, output)
    assert output.read_bytes() == b"old workbook"
    assert sidecar.read_bytes() == b"old JSON"
    assert snapshot.read_bytes() == b"old tracker"
    assert not list(tmp_path.glob(".summary-publish-*"))


def test_finds_newest_wi_tracker_download(tmp_path):
    import os

    older = tmp_path / "WI Processing Tracker - Deployment Tracker.csv"
    newest = tmp_path / "WI Processing Tracker (1).xlsx"
    (tmp_path / "Unrelated Tracker.xlsx").write_bytes(b"other")
    older.write_bytes(b"old")
    newest.write_bytes(b"new")
    os.utime(older, (1_000, 1_000))
    os.utime(newest, (2_000, 2_000))

    assert reporting.find_latest_wi_tracker_download(tmp_path) == newest
    assert reporting.find_latest_wi_tracker_download(tmp_path / "missing") is None


def _cli_tracker_fixture(tmp_path, monkeypatch):
    from utils import generate_data_collection_summary as cli

    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    export = downloads / "WI Processing Tracker.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Deployment Tracker"
    sheet.append(["Project", "Deployment", "Status"])
    sheet.append(["UCNRS Mammal", "deployment-1", "WI - Partial Validation"])
    workbook.save(export)
    workbook.close()
    documents = [
        MetadataDocument(tmp_path / "image_file_metadata.csv", "image", (_image("deployment-1", "one.jpg"),))
    ]
    monkeypatch.setattr(cli, "discover_box_year_metadata", lambda _root: documents)
    args = [
        "--year", "2026", "--box-year-root", str(tmp_path),
        "--output-root", str(tmp_path / "reports"), "--generation-date", "2026-10-14",
        "--downloads-dir", str(downloads),
    ]
    return cli, export, args, tmp_path / "reports" / "2026-10-14"


def test_cli_moves_downloaded_tracker_into_report_folder(tmp_path, monkeypatch, capsys):
    cli, export, args, folder = _cli_tracker_fixture(tmp_path, monkeypatch)

    assert cli.main(args) == 0
    assert export.exists()
    assert "WI tracker download:" in capsys.readouterr().out

    assert cli.main(args + ["--apply"]) == 0
    snapshot = folder / "WI Processing Tracker.xlsx"
    assert snapshot.exists()
    assert not export.exists()
    payload = json.loads((folder / "CA-SSN_data_collection_summary_2026.json").read_text())
    assert payload["wi_tracker_source"] == str(snapshot)


def test_cli_copies_explicit_tracker_without_removing_it(tmp_path, monkeypatch):
    cli, export, args, folder = _cli_tracker_fixture(tmp_path, monkeypatch)

    assert cli.main(args + ["--wi-tracker", str(export), "--apply"]) == 0
    assert export.exists()
    assert (folder / "WI Processing Tracker.xlsx").exists()


def test_cli_requires_tracker_download_or_explicit_opt_out(tmp_path, monkeypatch, capsys):
    cli, export, args, folder = _cli_tracker_fixture(tmp_path, monkeypatch)
    export.unlink()

    assert cli.main(args + ["--apply"]) == 2
    assert "--no-wi-tracker" in capsys.readouterr().err
    assert not folder.exists()
    assert cli.main(args + ["--no-wi-tracker", "--apply"]) == 0


def test_cli_warns_about_stale_tracker_download(tmp_path, monkeypatch, capsys):
    import os

    cli, export, args, _folder = _cli_tracker_fixture(tmp_path, monkeypatch)
    os.utime(export, (1_000, 1_000))

    assert cli.main(args) == 0
    assert "WARNING: this tracker download is more than 6 hours old" in capsys.readouterr().out
