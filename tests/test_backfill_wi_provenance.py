from __future__ import annotations

import csv
import io
from pathlib import Path

from cassn.reporting.data_collection_summary import WISubmissionTracker
from utils.backfill_wi_provenance import BoxCsv, plan_wi_csv


FIELDS = [
    "filename",
    "deployment_id",
    "file_type",
    "is_submitted_to_wi",
    "wi_submitter",
    "wi_submission_datetime",
]


def _payload(rows: list[dict[str, str]], *, bom: bool = False) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    data = stream.getvalue().encode()
    return (b"\xef\xbb\xbf" + data) if bom else data


def _rows(payload: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(payload.decode("utf-8-sig"), newline="")))


def test_plan_stamps_only_tracker_wi_rows_and_preserves_unknown_provenance():
    tracker = WISubmissionTracker(
        Path("tracker.xlsx"),
        {"wi": "WI - Validation not started", "box": "Box"},
    )
    source = BoxCsv(
        "2026/Reserve/Event/image_file_metadata.csv",
        "file-id",
        _payload(
            [
                {
                    "filename": "wi.jpg",
                    "deployment_id": "wi",
                    "file_type": "image",
                    "is_submitted_to_wi": "False",
                    "wi_submitter": "",
                    "wi_submission_datetime": "",
                },
                {
                    "filename": "box.jpg",
                    "deployment_id": "box",
                    "file_type": "image",
                    "is_submitted_to_wi": "False",
                    "wi_submitter": "",
                    "wi_submission_datetime": "",
                },
            ],
            bom=True,
        ),
    )

    plan = plan_wi_csv(source, tracker)

    assert not plan.errors
    assert plan.changed_rows == 1
    assert plan.changed_deployments == {"wi"}
    assert plan.updated_payload.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in plan.updated_payload
    rows = _rows(plan.updated_payload)
    assert rows[0]["is_submitted_to_wi"] == "True"
    assert rows[0]["wi_submitter"] == ""
    assert rows[0]["wi_submission_datetime"] == ""
    assert rows[1]["is_submitted_to_wi"] == "False"


def test_plan_never_clears_metadata_true_when_tracker_says_box():
    tracker = WISubmissionTracker(Path("tracker.xlsx"), {"deployment": "Box"})
    source = BoxCsv(
        "image_file_metadata.csv",
        "file-id",
        _payload(
            [
                {
                    "filename": "one.jpg",
                    "deployment_id": "deployment",
                    "file_type": "image",
                    "is_submitted_to_wi": "True",
                    "wi_submitter": "Someone",
                    "wi_submission_datetime": "2026-01-01T00:00:00Z",
                }
            ]
        ),
    )

    plan = plan_wi_csv(source, tracker)

    assert not plan.changed
    assert plan.tracker_box_metadata_true == {"deployment"}
    assert plan.updated_payload == source.payload


def test_plan_rejects_missing_submission_column():
    payload = b"filename,deployment_id,file_type\none.jpg,deployment,image\n"
    plan = plan_wi_csv(
        BoxCsv("image_file_metadata.csv", "file-id", payload),
        WISubmissionTracker(Path("tracker.xlsx"), {"deployment": "WI - Pending"}),
    )

    assert plan.errors
    assert "is_submitted_to_wi" in plan.errors[0]
