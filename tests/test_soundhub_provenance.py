"""Tests for exact staged-recording SoundHub provenance on Box metadata."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cassn.config import SOUNDHUB_DEPLOYMENT_FIELDS, SOUNDHUB_RECORDING_FIELDS
from cassn.soundhub.export import write_deployment_fragments
from cassn.soundhub.provenance import (
    SoundHubProvenanceError,
    apply_submission_provenance,
    default_box_year_root,
    infer_submission_year,
    plan_submission_provenance,
    write_submission_report,
)
from cassn.soundhub.submission import (
    SoundHubSubmissionError,
    execute_soundhub_submission,
    plan_soundhub_submission,
)
from cassn.soundhub.staging import project_root
from cassn.soundhub.upload import staged_objects


FIELDS = [
    "filename",
    "deployment_event_id",
    "deployment_id",
    "device_type",
    "file_type",
    "legacy_note",
    "is_submitted_to_soundhub",
    "soundhub_submitter",
    "soundhub_submission_datetime",
]
DEPLOYMENT_1 = "UC_Alpha_plot1_BD_20260610"
DEPLOYMENT_2 = "UC_Beta_plot2_BD_20260710"


def _write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        return list(reader.fieldnames or []), list(reader)


def _recording(deployment_id: str) -> dict:
    return {
        "filename": f"{deployment_id}_00001.flac",
        "deployment_id": deployment_id,
        "start": "2026-04-01 00:00:00-07:00",
        "end": "2026-04-01 01:00:00-07:00",
    }


def _deployment(deployment_id: str) -> dict:
    row = {field: "" for field in SOUNDHUB_DEPLOYMENT_FIELDS}
    row.update(
        project_short_name="UCNature-SSN",
        deployment_id=deployment_id,
        subproject="Alpha_2026",
        subproject_design="<Site>_<SamplingYear>",
        placename="Alpha_plot1",
        longitude="-121.12345678",
        latitude="36.12345678",
        date_installed="2026-03-31",
        deployment_start_date="2026-04-01",
        deployment_start_time="00:00",
        deployment_end_date="2026-04-01",
        deployment_end_time="23:59",
        frequency="daily",
        duration="12:59",
        gain="High",
        ARU_make="Open Acoustic Devices",
        ARU_model="AudioMoth-Firmware-Basic 1.11.1",
        ARU_container="polybag",
        ARU_microphone="internal",
        mounted_on="metal_pole",
        sensor_height_meters="2.5",
        recorded_by="Imperato, John",
    )
    return row


def _metadata(deployment_id: str, event_id: str, *, filename: str | None = None) -> dict:
    return {
        "filename": filename or f"{deployment_id}_00001.wav",
        "deployment_event_id": event_id,
        "deployment_id": deployment_id,
        "device_type": "BD",
        "file_type": "audio",
        "legacy_note": f"preserve-{deployment_id}",
        "is_submitted_to_soundhub": "False",
        "soundhub_submitter": "",
        "soundhub_submission_datetime": "",
    }


def _fixture(tmp_path: Path):
    staging = tmp_path / "staging"
    rows = [_recording(DEPLOYMENT_1), _recording(DEPLOYMENT_2)]
    _write_csv(
        project_root(staging) / "recording.csv",
        SOUNDHUB_RECORDING_FIELDS,
        rows,
    )
    for row in rows:
        deployment = _deployment(row["deployment_id"])
        audio = {
            **deployment,
            "filename": Path(row["filename"]).with_suffix(".wav").name,
            "recorded_datetime": row["start"].replace(" ", "T"),
            "recording_duration_sec": "3600",
        }
        write_deployment_fragments(staging, [audio])
    _write_csv(
        project_root(staging) / "deployment.csv",
        SOUNDHUB_DEPLOYMENT_FIELDS,
        [_deployment(DEPLOYMENT_1), _deployment(DEPLOYMENT_2)],
    )
    for row in rows:
        media = project_root(staging) / row["deployment_id"] / row["filename"]
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"test flac")

    box_year = tmp_path / "Box" / "field_data" / "2026"
    event_1 = box_year / "Reserve A" / "UC_Alpha_20260610" / "audio_file_metadata.csv"
    event_2 = box_year / "Reserve B" / "UC_Beta_20260710" / "audio_file_metadata.csv"
    _write_csv(
        event_1,
        FIELDS,
        [
            _metadata(DEPLOYMENT_1, "UC_Alpha_20260610"),
            _metadata(
                DEPLOYMENT_1,
                "UC_Alpha_20260610",
                filename=f"{DEPLOYMENT_1}_00002.wav",
            ),
        ],
    )
    _write_csv(event_2, FIELDS, [_metadata(DEPLOYMENT_2, "UC_Beta_20260710")])
    return staging, box_year, event_1, event_2


def test_plan_maps_exact_staged_rows_across_events(tmp_path):
    staging, box_year, _, _ = _fixture(tmp_path)

    plan = plan_submission_provenance(staging, box_year)

    assert plan.ok
    assert len(plan.target_keys) == 2
    assert len(plan.pending_keys) == 2
    assert not plan.submitted_keys
    assert plan.pending_deployment_ids == {DEPLOYMENT_1, DEPLOYMENT_2}
    assert plan.event_ids == {"UC_Alpha_20260610", "UC_Beta_20260710"}
    assert plan.matched_file_count == 2
    assert infer_submission_year(staging) == 2026
    assert default_box_year_root(2026).name == "2026"


def test_apply_preserves_header_unrelated_cells_and_excluded_row(tmp_path):
    staging, box_year, event_1, event_2 = _fixture(tmp_path)
    plan = plan_submission_provenance(staging, box_year)
    submitted_at = datetime(2026, 8, 25, 1, 2, 3, tzinfo=timezone.utc)

    result = apply_submission_provenance(
        plan, submitter="Imperato, John", submitted_at=submitted_at
    )

    assert result.changed_files == 2
    assert result.changed_rows == 2
    for path in (event_1, event_2):
        fields, rows = _read_csv(path)
        assert fields == FIELDS
        assert rows[0]["legacy_note"].startswith("preserve-")
        assert rows[0]["is_submitted_to_soundhub"] == "True"
        assert rows[0]["soundhub_submitter"] == "Imperato, John"
        assert rows[0]["soundhub_submission_datetime"] == "2026-08-25T01:02:03+00:00"
    _, rows = _read_csv(event_1)
    assert rows[1]["is_submitted_to_soundhub"] == "False"
    assert rows[1]["soundhub_submitter"] == ""

    second = plan_submission_provenance(staging, box_year)
    assert second.ok
    assert not second.pending_keys
    assert len(second.submitted_keys) == 2


def test_mixed_provenance_within_deployment_is_blocking(tmp_path):
    staging, box_year, event_1, _ = _fixture(tmp_path)
    rows = [_recording(DEPLOYMENT_1), _recording(DEPLOYMENT_2)]
    second = {
        **_recording(DEPLOYMENT_1),
        "filename": f"{DEPLOYMENT_1}_00002.flac",
    }
    rows.append(second)
    _write_csv(project_root(staging) / "recording.csv", SOUNDHUB_RECORDING_FIELDS, rows)
    media = project_root(staging) / DEPLOYMENT_1 / second["filename"]
    media.write_bytes(b"test flac 2")
    fields, metadata = _read_csv(event_1)
    metadata[0]["is_submitted_to_soundhub"] = "True"
    metadata[0]["soundhub_submitter"] = "Imperato, John"
    metadata[0]["soundhub_submission_datetime"] = "2026-08-20T00:00:00+00:00"
    _write_csv(event_1, fields, metadata)

    plan = plan_submission_provenance(staging, box_year)

    assert not plan.ok
    assert any("mixed submitted and pending" in error for error in plan.errors)


def test_apply_refuses_box_file_changed_after_preflight(tmp_path):
    staging, box_year, event_1, _ = _fixture(tmp_path)
    plan = plan_submission_provenance(staging, box_year)
    event_1.write_bytes(event_1.read_bytes() + b"\n")

    with pytest.raises(SoundHubProvenanceError, match="changed after preflight"):
        apply_submission_provenance(plan)


def test_selection_and_markdown_report_cover_planned_batch(tmp_path):
    staging, box_year, _, _ = _fixture(tmp_path)
    plan = plan_submission_provenance(staging, box_year)
    settings = {
        "bucket": "casoundhub",
        "upload_prefix": "upload",
        "project_short_name": "UCNature-SSN",
    }
    objects = staged_objects(
        staging, settings, deployment_ids={DEPLOYMENT_1}
    )
    relatives = {item["relative"] for item in objects}
    assert relatives == {
        "deployment.csv",
        "recording.csv",
        f"{DEPLOYMENT_1}/{DEPLOYMENT_1}_00001.flac",
    }

    provenance = apply_submission_provenance(
        plan,
        submitted_at=datetime(2026, 8, 25, 1, 2, 3, tzinfo=timezone.utc),
    )
    all_objects = staged_objects(
        staging, settings, deployment_ids=plan.pending_deployment_ids
    )
    reports = write_submission_report(
        plan,
        settings=settings,
        upload_result={"uploaded": 4, "skipped": 0},
        verification={
            "ok": True,
            "checked": 4,
            "present": 4,
            "missing": [],
            "mismatched": [],
        },
        provenance_result=provenance,
        planned_objects=all_objects,
    )
    assert len(reports) == 2
    assert all(path.parent.name == "soundhub" for path in reports)
    assert len({path.read_bytes() for path in reports}) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert "**Status:** Verified successfully" in text
    assert "Message for Brian" not in text
    assert "| FLAC recordings | 2 |" in text
    assert "| SoundHub deployments | 2 |" in text
    assert "Box metadata files updated | 2" in text


def test_submission_blocks_completed_and_pending_batches_mixed_together(tmp_path):
    staging, box_year, event_1, _ = _fixture(tmp_path)
    fields, rows = _read_csv(event_1)
    rows[0].update(
        is_submitted_to_soundhub="True",
        soundhub_submitter="Imperato, John",
        soundhub_submission_datetime="2026-08-20T00:00:00+00:00",
    )
    _write_csv(event_1, fields, rows)
    settings = {
        "bucket": "casoundhub",
        "upload_prefix": "upload",
        "project_short_name": "UCNature-SSN",
    }
    plan = plan_soundhub_submission(
        staging, settings=settings, box_year_root=box_year
    )

    assert not plan.ok
    assert plan.provenance.pending_keys == {
        (DEPLOYMENT_2, f"{DEPLOYMENT_2}_00001.flac")
    }
    assert plan.provenance.pending_event_ids == {"UC_Beta_20260710"}
    assert plan.provenance.pending_file_count == 1
    assert not plan.objects
    assert any("mixes recordings from a completed submission" in error for error in plan.errors)


def test_verification_failure_leaves_box_provenance_unchanged(tmp_path, monkeypatch):
    staging, box_year, event_1, event_2 = _fixture(tmp_path)
    settings = {
        "bucket": "casoundhub",
        "upload_prefix": "upload",
        "project_short_name": "UCNature-SSN",
    }
    plan = plan_soundhub_submission(
        staging, settings=settings, box_year_root=box_year
    )
    monkeypatch.setattr(
        "cassn.soundhub.submission.upload_project",
        lambda *args, **kwargs: {
            "cancelled": False,
            "uploaded": 4,
            "skipped": 0,
            "uploaded_bytes": plan.total_bytes,
            "total": 4,
        },
    )
    monkeypatch.setattr(
        "cassn.soundhub.submission.verify_project",
        lambda *args, **kwargs: {
            "ok": False,
            "checked": 4,
            "present": 3,
            "missing": ["missing.flac"],
            "mismatched": [],
        },
    )

    result = execute_soundhub_submission(plan)

    assert result["success"] is False
    for path in (event_1, event_2):
        _, rows = _read_csv(path)
        assert rows[0]["is_submitted_to_soundhub"] == "False"
        assert not (path.parent / "soundhub").exists()


def test_cancelled_upload_never_verifies_or_updates_box(tmp_path, monkeypatch):
    staging, box_year, event_1, event_2 = _fixture(tmp_path)
    settings = {
        "bucket": "casoundhub",
        "upload_prefix": "upload",
        "project_short_name": "UCNature-SSN",
    }
    plan = plan_soundhub_submission(
        staging, settings=settings, box_year_root=box_year
    )
    monkeypatch.setattr(
        "cassn.soundhub.submission.upload_project",
        lambda *args, **kwargs: {
            "cancelled": True,
            "uploaded": 1,
            "skipped": 0,
            "uploaded_bytes": 9,
            "total": 4,
        },
    )

    def should_not_verify(*args, **kwargs):
        raise AssertionError("verification must not run after cancellation")

    monkeypatch.setattr(
        "cassn.soundhub.submission.verify_project", should_not_verify
    )

    result = execute_soundhub_submission(plan)

    assert result["cancelled"] is True
    for path in (event_1, event_2):
        _, rows = _read_csv(path)
        assert rows[0]["is_submitted_to_soundhub"] == "False"


def test_year_inference_supports_2027_and_blocks_mixed_years(tmp_path):
    staging = tmp_path / "staging"
    for deployment_id in (
        "UC_Alpha_plot1_BD_20270610",
        "UC_Beta_plot1_BD_20260610",
    ):
        media = (
            project_root(staging)
            / deployment_id
            / f"{deployment_id}_00001.flac"
        )
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"test flac")
    _write_csv(
        project_root(staging) / "recording.csv",
        SOUNDHUB_RECORDING_FIELDS,
        [_recording("UC_Alpha_plot1_BD_20270610")],
    )
    assert infer_submission_year(staging) == 2027
    assert default_box_year_root(2027).name == "2027"

    _write_csv(
        project_root(staging) / "recording.csv",
        SOUNDHUB_RECORDING_FIELDS,
        [
            _recording("UC_Alpha_plot1_BD_20270610"),
            _recording("UC_Beta_plot1_BD_20260610"),
        ],
    )
    with pytest.raises(SoundHubProvenanceError, match="more than one deployment year"):
        infer_submission_year(staging)


def test_box_failure_after_s3_verification_has_distinct_recovery_state(
    tmp_path, monkeypatch
):
    staging, box_year, event_1, _ = _fixture(tmp_path)
    settings = {
        "bucket": "casoundhub",
        "upload_prefix": "upload",
        "project_short_name": "UCNature-SSN",
    }
    plan = plan_soundhub_submission(
        staging, settings=settings, box_year_root=box_year
    )
    event_1.write_bytes(event_1.read_bytes() + b"\n")
    monkeypatch.setattr(
        "cassn.soundhub.submission.upload_project",
        lambda *args, **kwargs: {
            "cancelled": False,
            "uploaded": 4,
            "skipped": 0,
            "uploaded_bytes": plan.total_bytes,
            "total": 4,
        },
    )
    monkeypatch.setattr(
        "cassn.soundhub.submission.verify_project",
        lambda *args, **kwargs: {
            "ok": True,
            "checked": 4,
            "present": 4,
            "missing": [],
            "mismatched": [],
        },
    )

    with pytest.raises(SoundHubSubmissionError) as caught:
        execute_soundhub_submission(plan)

    assert caught.value.phase == "box_provenance"
    assert caught.value.result["verification"]["ok"] is True


def test_cli_preflight_output_uses_operator_terms(tmp_path, capsys):
    from utils.prep_soundhub import _print_preflight

    staging, box_year, _, _ = _fixture(tmp_path)
    settings = {
        "bucket": "casoundhub",
        "upload_prefix": "upload",
        "project_short_name": "UCNature-SSN",
    }
    plan = plan_soundhub_submission(
        staging, settings=settings, box_year_root=box_year
    )

    _print_preflight(plan)
    output = capsys.readouterr().out
    assert "Deployment events in next batch" in output
    assert "Box metadata CSVs to update" in output
    assert "Box event files" not in output
