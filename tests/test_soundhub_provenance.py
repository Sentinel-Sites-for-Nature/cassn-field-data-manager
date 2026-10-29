"""Tests for exact staged-recording SoundHub provenance on Box metadata."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
from types import SimpleNamespace
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
from cassn.soundhub.box_metadata import BoxMetadataStore


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
        "path": f"{deployment_id}/{deployment_id}_00001.flac",
        "sample_rate": "48000",
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
        recording_schedule="daily",
        recording_duration="12:59",
        gain="High",
        ARU_make="Open Acoustic Devices",
        ARU_model="AudioMoth-Firmware-Basic 1.11.1",
        ARU_container="polybag",
        ARU_microphone="internal",
        mounted_on="pole",
        aru_status="Functioning",
        feature_type="None",
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
            "frequency": deployment["recording_schedule"],
            "duration": deployment["recording_duration"],
            "filename": Path(row["path"]).with_suffix(".wav").name,
            "recorded_datetime": row["start"].replace(" ", "T"),
            "recording_duration_sec": "3600",
            "sample_rate_hz": row["sample_rate"],
        }
        write_deployment_fragments(staging, [audio])
    _write_csv(
        project_root(staging) / "deployment.csv",
        SOUNDHUB_DEPLOYMENT_FIELDS,
        [_deployment(DEPLOYMENT_1), _deployment(DEPLOYMENT_2)],
    )
    for row in rows:
        media = project_root(staging) / row["path"]
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"test flac")

    box_year = tmp_path / "Box" / "data" / "2026"
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
    assert default_box_year_root(2026).parts[-3:] == ("CASSN", "data", "2026")


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
        "path": f"{DEPLOYMENT_1}/{DEPLOYMENT_1}_00002.flac",
    }
    rows.append(second)
    _write_csv(project_root(staging) / "recording.csv", SOUNDHUB_RECORDING_FIELDS, rows)
    media = project_root(staging) / second["path"]
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
    record = json.loads((result["transfer_directory"] / "transfer.json").read_text())
    assert record["status"] == "failed"
    assert record["phase"] == "verification"
    assert record["verification"]["missing"] == ["missing.flac"]
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
    record = json.loads((result["transfer_directory"] / "transfer.json").read_text())
    assert record["status"] == "cancelled"
    assert record["upload"]["uploaded"] == 1
    assert record["verification"] is None
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
    assert default_box_year_root(2027).parts[-3:] == ("CASSN", "data", "2027")

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
    record = json.loads((caught.value.result["transfer_directory"] / "transfer.json").read_text())
    assert record["status"] == "failed"
    assert record["phase"] == "box_provenance"
    assert record["verification"]["ok"] is True
    assert record["finished_at"]


def _archive_test_plan(tmp_path):
    staging, box_year, _, _ = _fixture(tmp_path)
    plan = plan_soundhub_submission(staging, box_year_root=box_year, settings={
        "bucket": "casoundhub", "upload_prefix": "upload",
        "project_short_name": "UCNature-SSN",
    })
    assert plan.ok
    return plan


def _mock_verified_upload(monkeypatch):
    def upload(*args, **kwargs):
        kwargs["log_path"].write_text("upload: test object\n")
        return dict(cancelled=False, uploaded=4, skipped=0, uploaded_bytes=18, total=4)
    monkeypatch.setattr("cassn.soundhub.submission.upload_project", upload)
    monkeypatch.setattr("cassn.soundhub.submission.verify_project", lambda *a, **kw: {
        "ok": True, "checked": 4, "present": 4, "missing": [], "mismatched": []})


def test_transfer_archive_preserves_manifests_logs_and_report_after_cleanup(tmp_path, monkeypatch):
    plan = _archive_test_plan(tmp_path)
    originals = {n: (project_root(plan.staging_root) / n).read_bytes()
                 for n in ("deployment.csv", "recording.csv")}
    _mock_verified_upload(monkeypatch)
    result = execute_soundhub_submission(plan)
    directory = result["transfer_directory"]
    record = json.loads((directory / "transfer.json").read_text())
    assert directory.parent == plan.staging_root.parent / "transfers"
    assert record["status"] == "complete"
    assert record["verification"]["ok"] is True
    assert record["provenance"]["changed_rows"] == 2
    assert record["soundhub_ingestion"] == "unconfirmed"
    assert record["destination"] == "s3://casoundhub/upload/UCNature-SSN/"
    assert len(record["planned"]["objects"]) == 4
    assert (directory / "submission_report.md").read_bytes() == result["reports"][0].read_bytes()
    shutil.rmtree(plan.staging_root)
    assert (directory / "upload.log").read_text() == "upload: test object\n"
    for name, payload in originals.items():
        assert (directory / name).read_bytes() == payload


def test_failed_attempt_and_retry_keep_separate_records(tmp_path, monkeypatch):
    plan = _archive_test_plan(tmp_path)
    def fail(*args, **kwargs):
        kwargs["log_path"].write_text("AccessDenied\n")
        raise RuntimeError("AccessDenied")
    monkeypatch.setattr("cassn.soundhub.submission.upload_project", fail)
    with pytest.raises(SoundHubSubmissionError) as caught:
        execute_soundhub_submission(plan)
    first = caught.value.result["transfer_directory"]
    first_record = (first / "transfer.json").read_bytes()
    parsed = json.loads(first_record)
    assert parsed["status"] == "failed"
    assert parsed["phase"] == "upload"
    assert parsed["verification"] is None
    _mock_verified_upload(monkeypatch)
    result = execute_soundhub_submission(plan)
    assert result["transfer_directory"] != first
    assert (first / "transfer.json").read_bytes() == first_record
    assert (first / "upload.log").read_text() == "AccessDenied\n"


def test_keyboard_interrupt_is_retained_without_claiming_s3_success(tmp_path, monkeypatch):
    plan = _archive_test_plan(tmp_path)
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr("cassn.soundhub.submission.upload_project", interrupt)
    with pytest.raises(KeyboardInterrupt):
        execute_soundhub_submission(plan)
    path, = (plan.staging_root.parent / "transfers").glob('*/transfer.json')
    record = json.loads(path.read_text())
    assert record["status"] == "interrupted"
    assert record["verification"] is None


def test_archive_write_failure_prevents_network_upload(tmp_path, monkeypatch):
    plan = _archive_test_plan(tmp_path)
    (plan.staging_root.parent / "transfers").write_text("not a directory")
    def should_not_upload(*args, **kwargs):
        raise AssertionError("upload must not start without durable records")
    monkeypatch.setattr("cassn.soundhub.submission.upload_project", should_not_upload)
    with pytest.raises(SoundHubSubmissionError) as caught:
        execute_soundhub_submission(plan)
    assert caught.value.phase == "local_record"


class _BoxApi:
    """A versioned Box server fake, using the real SDK response schema."""

    def __init__(self, source_paths):
        self.folders = self.files = self.downloads = self.uploads = self
        self.items = {}
        self.payloads = {}
        self.listed = []
        self.writes = []
        self.race_on_write = False
        self.corrupt_write = False
        self.add_folder("data", "data", "0")
        self.add_folder("year", "2026", "data")
        self.add_folder("reserve", "Reserve", "year")
        for index, path in enumerate(source_paths):
            event_id = f"event{index}"
            self.add_folder(event_id, path.parent.name, "reserve")
            self.add_folder(f"raw{index}", "raw_data", event_id)
            self.add_file(f"file{index}", path.name, event_id, path.read_bytes())

    def add_folder(self, item_id, name, parent):
        self.items[item_id] = dict(id=item_id, type="folder", name=name,
                                  parent={"id": parent}, permissions={"can_upload": True})

    def add_file(self, item_id, name, parent, payload):
        permissions = {name: True for name in (
            "can_delete", "can_download", "can_invite_collaborator", "can_rename",
            "can_set_share_access", "can_share", "can_annotate", "can_comment",
            "can_preview", "can_upload", "can_view_annotations_all", "can_view_annotations_self",
        )}
        self.items[item_id] = dict(
            id=item_id, type="file", name=name, parent={"id": parent, "type": "folder"},
            etag="0", file_version={"id": f"v{item_id}"}, sha1=hashlib.sha1(payload).hexdigest(),
            size=len(payload), permissions=permissions,
        )
        self.payloads[item_id] = payload

    def get_folder_items(self, folder_id, *, fields=None, limit=1000, offset=0):
        self.listed.append(folder_id)
        assert not folder_id.startswith("raw"), "must never enumerate raw media"
        items = [SimpleNamespace(**item) for item in self.items.values()
                 if item["parent"]["id"] == folder_id]
        return SimpleNamespace(entries=items[offset:offset + limit])

    def get_folder_by_id(self, folder_id, *, fields):
        return SimpleNamespace(id=folder_id, permissions=SimpleNamespace(
            **self.items[folder_id]["permissions"]))

    def get_file_by_id(self, file_id, *, fields):
        from box_sdk_gen.schemas.file_full import FileFull
        return FileFull.from_dict(self.items[file_id])

    def download_file(self, file_id, *, version):
        assert version == self.items[file_id]["file_version"]["id"]
        return io.BytesIO(self.payloads[file_id])

    def upload_file_version(self, file_id, attributes, file, *, if_match):
        if self.race_on_write:
            self.items[file_id]["etag"] = "changed"
        if self.items[file_id]["etag"] != if_match:
            raise RuntimeError("412 precondition failed")
        payload = file.read()
        self.writes.append((file_id, if_match, payload))
        self.add_file(file_id, attributes.name, self.items[file_id]["parent"]["id"], payload)
        self.items[file_id]["etag"] = "1"
        if self.corrupt_write:
            self.items[file_id]["sha1"] = "wrong"

    def create_folder(self, name, parent):
        item_id = f"reports-{parent.id}"
        self.add_folder(item_id, name, parent.id)
        self.writes.append((item_id, "folder"))
        return SimpleNamespace(id=item_id)

    def upload_file(self, *, attributes, file):
        item_id = f"report-{attributes['parent']['id']}"
        payload = file.read()
        self.add_file(item_id, attributes["name"], attributes["parent"]["id"], payload)
        self.writes.append((item_id, "report", payload))
        return SimpleNamespace(entries=[SimpleNamespace(id=item_id)])


def _api_fixture(tmp_path, monkeypatch):
    staging, _, event_1, event_2 = _fixture(tmp_path)
    api = _BoxApi([event_1, event_2])
    store = BoxMetadataStore(api, "data")
    # Deliberately use a nonexistent Drive path; any Drive access would fail.
    monkeypatch.setattr("cassn.soundhub.provenance.default_box_year_root",
                        lambda year: tmp_path / "no-box-drive" / str(year))
    monkeypatch.setattr(BoxMetadataStore, "connect", lambda: store)
    return staging, api, store


def test_api_preflight_reads_exact_versions_without_drive_or_raw_media(tmp_path, monkeypatch):
    staging, api, store = _api_fixture(tmp_path, monkeypatch)
    # Exercise pagination at all levels, not just single-page fake responses.
    store.storage.PAGE_LIMIT = 1
    plan = plan_soundhub_submission(staging)
    assert plan.ok, plan.errors
    assert len(plan.provenance.pending_keys) == 2
    assert not api.writes
    assert "raw0" not in api.listed
    assert all(document.box_file.version_id for document in plan.provenance.documents)


def test_api_duplicate_rows_remain_blocking(tmp_path, monkeypatch):
    staging, api, _ = _api_fixture(tmp_path, monkeypatch)
    api.add_folder("duplicate", "Duplicate event", "reserve")
    api.add_file("dup", "audio_file_metadata.csv", "duplicate", api.payloads["file0"])
    plan = plan_soundhub_submission(staging)
    assert not plan.ok
    assert any("appears in both" in error for error in plan.errors)
    assert not api.writes


def test_api_unreadable_metadata_cannot_silently_pass(tmp_path, monkeypatch):
    staging, api, _ = _api_fixture(tmp_path, monkeypatch)
    api.payloads["file1"] += b"corrupt download"
    plan = plan_soundhub_submission(staging)
    assert not plan.ok
    assert any("checksum/size" in error for error in plan.errors)


@pytest.mark.parametrize("denied_item", ["file0", "event0", "restricted-reports"])
def test_api_preflight_requires_permissions_for_later_records(tmp_path, monkeypatch, denied_item):
    staging, api, _ = _api_fixture(tmp_path, monkeypatch)
    api.add_folder("restricted-reports", "soundhub", "event1")
    api.items[denied_item]["permissions"]["can_upload"] = False
    plan = plan_soundhub_submission(staging)
    assert not plan.ok
    assert any("permission" in error.lower() for error in plan.errors)
    assert not api.writes


def test_api_change_after_preflight_stops_all_metadata_writes(tmp_path, monkeypatch):
    staging, api, _ = _api_fixture(tmp_path, monkeypatch)
    plan = plan_submission_provenance(staging)
    api.items["file1"]["etag"] = "new-edit"
    with pytest.raises(SoundHubProvenanceError, match="changed after preflight"):
        apply_submission_provenance(plan)
    assert not api.writes


def test_api_write_uses_if_match_to_protect_last_moment_edits(tmp_path, monkeypatch):
    staging, api, _ = _api_fixture(tmp_path, monkeypatch)
    plan = plan_submission_provenance(staging)
    api.race_on_write = True
    with pytest.raises(RuntimeError, match="412"):
        apply_submission_provenance(plan)
    assert not api.writes


def test_api_failed_read_after_write_is_not_success(tmp_path, monkeypatch):
    staging, api, _ = _api_fixture(tmp_path, monkeypatch)
    plan = plan_soundhub_submission(staging)
    _mock_verified_upload(monkeypatch)
    api.corrupt_write = True
    with pytest.raises(SoundHubSubmissionError) as caught:
        execute_soundhub_submission(plan)
    assert caught.value.phase == "box_provenance"
    record = json.loads((caught.value.result["transfer_directory"] / "transfer.json").read_text())
    assert record["status"] == "failed"
    assert record["verification"]["ok"]
    assert not any(item[1] == "report" for item in api.writes)


def test_api_submission_preserves_unstaged_rows_reports_and_cleanup(tmp_path, monkeypatch):
    from cassn.soundhub.lifecycle import plan_completed_batch_cleanup, clear_completed_batch

    staging, api, _ = _api_fixture(tmp_path, monkeypatch)
    plan = plan_soundhub_submission(staging)
    _mock_verified_upload(monkeypatch)
    result = execute_soundhub_submission(plan)
    assert result["success"]
    assert result["provenance"].changed_rows == 2
    rows = list(csv.DictReader(io.StringIO(api.payloads["file0"].decode())))
    assert rows[0]["is_submitted_to_soundhub"] == "True"
    assert rows[1]["is_submitted_to_soundhub"] == "False"
    assert rows[0]["legacy_note"] == f"preserve-{DEPLOYMENT_1}"
    assert len([item for item in api.writes if item[1] == "report"]) == 2
    record = json.loads((result["transfer_directory"] / "transfer.json").read_text())
    assert record["box_metadata_access"] == "api"
    assert record["source_metadata"][0]["box_file"]["file_id"] == "file0"
    cleanup = plan_completed_batch_cleanup(staging)
    assert cleanup.clearable, cleanup.errors
    writes_before = list(api.writes)
    clear_completed_batch(cleanup)
    assert not project_root(staging).exists()
    assert result["transfer_directory"].is_dir()
    assert api.writes == writes_before


def test_api_report_conflict_preserves_existing_record(tmp_path, monkeypatch):
    staging, api, store = _api_fixture(tmp_path, monkeypatch)
    api.add_folder("reports-event0", "soundhub", "event0")
    api.add_file("report", "existing_soundhub_submission.md", "reports-event0", b"old report")
    with pytest.raises(ValueError, match="different submission report"):
        store.write_report("event0", "existing_soundhub_submission.md", b"new report")
    assert api.payloads["report"] == b"old report"
    assert not api.writes


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


def test_verified_batch_with_failed_box_records_cannot_be_uploaded_twice(tmp_path, monkeypatch):
    plan = _archive_test_plan(tmp_path)
    _mock_verified_upload(monkeypatch)
    monkeypatch.setattr('cassn.soundhub.submission.apply_submission_provenance',
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('connection removed')))
    with pytest.raises(SoundHubSubmissionError) as first:
        execute_soundhub_submission(plan)
    summary = (first.value.result['transfer_directory'] / 'upload_summary.md').read_text()
    assert 'PASSED' in summary
    assert 'Not confirmed saved' in summary
    assert 'connection removed' in summary
    assert 'Do not upload' in summary
    monkeypatch.setattr('cassn.soundhub.submission.upload_project',
                        lambda *a, **kw: pytest.fail('must not upload verified batch twice'))
    with pytest.raises(SoundHubSubmissionError, match='already passed S3 verification'):
        execute_soundhub_submission(plan)


def test_box_connection_is_reloaded_after_transfer_before_writes(tmp_path, monkeypatch):
    staging, api, store = _api_fixture(tmp_path, monkeypatch)
    plan = plan_soundhub_submission(staging)
    _mock_verified_upload(monkeypatch)
    reconnected = []
    original = store.reconnect
    def reconnect():
        assert not api.writes
        reconnected.append(True)
        original()
    monkeypatch.setattr(store, 'reconnect', reconnect)
    result = execute_soundhub_submission(plan)
    assert result['success']
    assert reconnected == [True]
    summary = (result['transfer_directory'] / 'upload_summary.md').read_text()
    assert 'Box submission flags: Saved' in summary
    assert 'ingestion/processing: unconfirmed' in summary


def test_soundhub_connection_selects_hosted_uc_without_legacy_credentials(monkeypatch):
    from cassn.box import auth
    selected = []
    def client(config):
        selected.append(config)
        return object()
    monkeypatch.setattr(auth, 'get_box_client', client)
    monkeypatch.setattr(auth, 'load_box_config', lambda: pytest.fail('must not read legacy credentials'))
    store = BoxMetadataStore.connect()
    assert selected[0].provider == 'hosted'
    assert selected[0].profile == 'uc'
    assert store.data_folder_id == selected[0].field_data_folder_id


def test_reconnect_cannot_change_box_project(monkeypatch):
    store = BoxMetadataStore(object(), 'uc-root')
    monkeypatch.setattr(BoxMetadataStore, 'connect',
                        lambda: BoxMetadataStore(object(), 'other-root'))
    with pytest.raises(RuntimeError, match='project changed'):
        store.reconnect()
