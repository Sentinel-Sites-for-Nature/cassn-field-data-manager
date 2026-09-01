"""Tests for guarded rollover of completed SoundHub staging batches."""
from __future__ import annotations

import csv
from pathlib import Path
from types import SimpleNamespace

import pytest

from cassn.config import SOUNDHUB_RECORDING_FIELDS
from cassn.soundhub.lifecycle import (
    SoundHubLifecycleError,
    clear_completed_batch,
    plan_completed_batch_cleanup,
    staging_extension_blockers,
)
from cassn.soundhub.provenance import (
    apply_submission_provenance,
    plan_submission_provenance,
)
from cassn.soundhub.staging import fragments_root, project_root
from utils.prep_soundhub import cmd_clear_completed


FIELDS = [
    "filename",
    "deployment_event_id",
    "deployment_id",
    "device_type",
    "file_type",
    "is_submitted_to_soundhub",
    "soundhub_submitter",
    "soundhub_submission_datetime",
]
DEPLOYMENT_1 = "UC_Alpha_plot1_BD_20260610"
DEPLOYMENT_2 = "UC_Beta_plot2_BD_20260710"
REPORT_NAME = "2026-08-25_075742Z_soundhub_submission.md"


def _write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _fixture(tmp_path: Path):
    staging = tmp_path / "staging"
    root = project_root(staging)
    recordings = []
    for deployment_id in (DEPLOYMENT_1, DEPLOYMENT_2):
        filename = f"{deployment_id}_00001.flac"
        recordings.append(
            {
                "filename": filename,
                "deployment_id": deployment_id,
                "start": "2026-04-01 00:00:00-07:00",
                "end": "2026-04-01 01:00:00-07:00",
            }
        )
        media = root / deployment_id / filename
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"test flac")
    _write_csv(root / "recording.csv", SOUNDHUB_RECORDING_FIELDS, recordings)
    _write_csv(
        root / "deployment.csv",
        ["deployment_id"],
        [{"deployment_id": DEPLOYMENT_1}, {"deployment_id": DEPLOYMENT_2}],
    )
    fragments = fragments_root(staging)
    fragments.mkdir()
    (fragments / "durable.csv").write_text("fragment\n")

    box_year = tmp_path / "Box" / "data" / "2026"
    event_roots = [
        box_year / "Reserve A" / "UC_Alpha_20260610",
        box_year / "Reserve B" / "UC_Beta_20260710",
    ]
    for event_root, deployment_id in zip(
        event_roots, (DEPLOYMENT_1, DEPLOYMENT_2), strict=True
    ):
        _write_csv(
            event_root / "audio_file_metadata.csv",
            FIELDS,
            [
                {
                    "filename": f"{deployment_id}_00001.wav",
                    "deployment_event_id": event_root.name,
                    "deployment_id": deployment_id,
                    "device_type": "BD",
                    "file_type": "audio",
                    "is_submitted_to_soundhub": "False",
                    "soundhub_submitter": "",
                    "soundhub_submission_datetime": "",
                }
            ],
        )
        (event_root / "source_wavs_untouched.txt").write_text("source sentinel\n")
    return staging, box_year, event_roots


def _complete(staging: Path, box_year: Path, event_roots: list[Path]) -> None:
    provenance = plan_submission_provenance(staging, box_year)
    apply_submission_provenance(provenance)
    report = "# SoundHub submission report\n\n**Status:** Verified successfully\n"
    for event_root in event_roots:
        path = event_root / "soundhub" / REPORT_NAME
        path.parent.mkdir()
        path.write_text(report)


def test_active_batch_is_not_closed_or_clearable(tmp_path):
    staging, box_year, _ = _fixture(tmp_path)

    plan = plan_completed_batch_cleanup(staging, box_year)

    assert not plan.closed
    assert not plan.clearable
    assert plan.pending_count == 2
    assert not plan.errors


def test_missing_box_metadata_does_not_block_local_staging_extension(tmp_path):
    staging, box_year, event_roots = _fixture(tmp_path)
    for event_root in event_roots:
        (event_root / "audio_file_metadata.csv").unlink()

    plan = plan_completed_batch_cleanup(staging, box_year)

    assert plan.provenance is not None
    assert any("Box metadata is missing" in error for error in plan.errors)
    assert staging_extension_blockers(plan) == []


def test_completed_batch_requires_one_shared_verified_report(tmp_path):
    staging, box_year, event_roots = _fixture(tmp_path)
    provenance = plan_submission_provenance(staging, box_year)
    apply_submission_provenance(provenance)
    first_report = event_roots[0] / "soundhub" / REPORT_NAME
    first_report.parent.mkdir()
    first_report.write_text("**Status:** Verified successfully\n")

    plan = plan_completed_batch_cleanup(staging, box_year)

    assert plan.closed
    assert not plan.clearable
    assert any("no verified SoundHub submission report" in error for error in plan.errors)


def test_cleanup_revalidates_if_staging_changes(tmp_path):
    staging, box_year, event_roots = _fixture(tmp_path)
    _complete(staging, box_year, event_roots)
    plan = plan_completed_batch_cleanup(staging, box_year)
    assert plan.clearable
    (project_root(staging) / "changed-after-preflight.txt").write_text("changed\n")

    with pytest.raises(SoundHubLifecycleError, match="changed after cleanup preflight"):
        clear_completed_batch(plan)


def test_cleanup_removes_only_derived_project_and_preserves_box(tmp_path):
    staging, box_year, event_roots = _fixture(tmp_path)
    _complete(staging, box_year, event_roots)
    plan = plan_completed_batch_cleanup(staging, box_year)

    result = clear_completed_batch(plan)

    assert result.removed_project_root == project_root(staging)
    assert result.removed_fragments_root == fragments_root(staging)
    assert result.removed_files == plan.local_file_count
    assert result.removed_bytes == plan.local_bytes
    assert not project_root(staging).exists()
    assert not fragments_root(staging).exists()
    assert staging.is_dir()
    for event_root in event_roots:
        assert (event_root / "audio_file_metadata.csv").is_file()
        assert (event_root / "source_wavs_untouched.txt").is_file()
        assert (event_root / "soundhub" / REPORT_NAME).is_file()


def test_cleanup_refuses_symlink_anywhere_in_project(tmp_path):
    staging, box_year, event_roots = _fixture(tmp_path)
    _complete(staging, box_year, event_roots)
    outside = tmp_path / "outside.txt"
    outside.write_text("do not touch\n")
    (project_root(staging) / "outside-link").symlink_to(outside)

    plan = plan_completed_batch_cleanup(staging, box_year)

    assert plan.closed
    assert not plan.clearable
    assert any("symlink" in error for error in plan.errors)
    assert outside.read_text() == "do not touch\n"


def test_clear_cli_is_dry_run_by_default_then_applies(
    tmp_path, monkeypatch, capsys
):
    staging, box_year, event_roots = _fixture(tmp_path)
    _complete(staging, box_year, event_roots)
    monkeypatch.setattr(
        "utils.prep_soundhub.load_soundhub_config",
        lambda: {"staging_root": str(staging)},
    )

    args = SimpleNamespace(
        staging=str(staging), box_year_root=str(box_year), apply=False
    )
    assert cmd_clear_completed(args) == 0
    assert project_root(staging).is_dir()
    assert "DRY RUN PASSED" in capsys.readouterr().out

    args.apply = True
    assert cmd_clear_completed(args) == 0
    assert not project_root(staging).exists()
    output = capsys.readouterr().out
    assert "COMPLETED SOUNDHUB BATCH CLEARED" in output
    assert "Box data, source WAVs, submission reports, and S3 were not changed" in output
