from __future__ import annotations

import csv
import io
import struct

from utils.backfill_audio_durations import (
    BoxAudioCsv,
    apply_resolved_durations,
    plan_audio_documents,
    wav_duration_seconds_from_prefix,
)


FIELDS = [
    "filename",
    "deployment_id",
    "plot_number",
    "device_type",
    "file_type",
    "file_size_bytes",
    "file_hash_sha1",
    "duration",
    "recording_duration_sec",
    "is_uploaded_to_box",
    "notes",
]


def _wav_prefix(*, avg_bytes_per_sec: int, data_size: int) -> bytes:
    fmt_body = struct.pack("<HHIIHH", 1, 1, 48000, avg_bytes_per_sec, 2, 16)
    chunks = b"fmt " + struct.pack("<I", len(fmt_body)) + fmt_body
    chunks += b"data" + struct.pack("<I", data_size)
    return b"RIFF" + struct.pack("<I", len(chunks) + 4) + b"WAVE" + chunks


def _payload(rows: list[dict[str, str]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return b"\xef\xbb\xbf" + stream.getvalue().encode()


def _row(filename: str, *, duration: str = "", box: str = "True") -> dict[str, str]:
    return {
        "filename": filename,
        "deployment_id": "UC_Reserve_plot1_BD_20260101",
        "plot_number": "1",
        "device_type": "BD",
        "file_type": "audio",
        "file_size_bytes": "960044",
        "file_hash_sha1": "abc123",
        "duration": "12:00",
        "recording_duration_sec": duration,
        "is_uploaded_to_box": box,
        "notes": "preserve me",
    }


def test_wav_duration_reads_fmt_and_data_headers_without_audio_body():
    prefix = _wav_prefix(avg_bytes_per_sec=96_000, data_size=960_000)

    assert wav_duration_seconds_from_prefix(prefix, file_size=960_044) == 10


def test_wav_duration_rejects_header_only_or_invalid_files():
    assert wav_duration_seconds_from_prefix(b"not a wave") is None
    assert wav_duration_seconds_from_prefix(_wav_prefix(avg_bytes_per_sec=96_000, data_size=0)) is None


def test_plan_and_apply_change_only_blank_confirmed_audio_rows():
    source = BoxAudioCsv(
        "2026/Reserve/Event/audio_file_metadata.csv",
        "csv-id",
        "event-id",
        _payload(
            [
                _row("missing.wav"),
                _row("already.wav", duration="12"),
                _row("not-box.wav", box="False"),
            ]
        ),
    )

    plans, tasks = plan_audio_documents([source])
    apply_resolved_durations(plans, tasks, {"abc123": 10})

    assert not plans[0].errors
    assert len(tasks) == 1
    assert plans[0].changed_rows == 1
    rows = list(
        csv.DictReader(
            io.StringIO(plans[0].updated_payload().decode("utf-8-sig"), newline="")
        )
    )
    assert rows[0]["recording_duration_sec"] == "10"
    assert rows[0]["notes"] == "preserve me"
    assert rows[1]["recording_duration_sec"] == "12"
    assert rows[2]["recording_duration_sec"] == ""


def test_duplicate_hash_duration_can_update_rows_in_two_documents():
    sources = [
        BoxAudioCsv(f"event-{index}/audio_file_metadata.csv", str(index), str(index), _payload([_row("same.wav")]))
        for index in range(2)
    ]
    plans, tasks = plan_audio_documents(sources)

    apply_resolved_durations(plans, tasks, {"abc123": 10})

    assert [plan.changed_rows for plan in plans] == [1, 1]


def test_legacy_csv_adds_duration_column_and_does_not_require_sha1():
    legacy_fields = [field for field in FIELDS if field not in {"file_hash_sha1", "recording_duration_sec"}]
    row = _row("legacy.wav")
    row.pop("file_hash_sha1")
    row.pop("recording_duration_sec")
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=legacy_fields, lineterminator="\n")
    writer.writeheader()
    writer.writerow(row)
    source = BoxAudioCsv("legacy/audio_file_metadata.csv", "csv", "event", stream.getvalue().encode())

    plans, tasks = plan_audio_documents([source])
    apply_resolved_durations(plans, tasks, {tasks[0].cache_key: 10})

    assert not plans[0].errors
    assert plans[0].fieldnames.index("recording_duration_sec") == plans[0].fieldnames.index("duration") + 1
    assert plans[0].updated_rows[0]["recording_duration_sec"] == "10"
