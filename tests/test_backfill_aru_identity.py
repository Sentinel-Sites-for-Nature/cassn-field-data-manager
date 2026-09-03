from __future__ import annotations

import csv
import io

from utils.backfill_aru_identity import process


FIELDS = ["filename", "ARU_make", "ARU_model", "notes"]


def _legacy_payload() -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(
        [
            {
                "filename": "recording.wav",
                "ARU_make": "AudioMoth",
                "ARU_model": "AudioMoth-Firmware-Basic 1.11.1",
                "notes": "preserve recording note",
            },
            {
                "filename": "CONFIG.TXT",
                "ARU_make": "Open Acoustic Devices",
                "ARU_model": "AudioMoth",
                "notes": "preserve config note",
            },
        ]
    )
    return b"\xef\xbb\xbf" + stream.getvalue().encode("utf-8")


def test_backfill_is_dry_run_by_default_and_idempotent_when_applied(tmp_path):
    path = tmp_path / "audio_file_metadata.csv"
    original = _legacy_payload()
    path.write_bytes(original)

    dry_run = process(path, apply=False)

    assert path.read_bytes() == original
    assert dry_run.added_column
    assert dry_run.changed == 2
    assert dry_run.firmware_recovered == 1

    applied = process(path, apply=True)
    payload = path.read_bytes()
    rows = list(csv.DictReader(io.StringIO(payload.decode("utf-8-sig"), newline="")))

    assert applied.added_column
    assert payload.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in payload
    assert rows[0]["ARU_make"] == "Open Acoustic Devices"
    assert rows[0]["ARU_model"] == "AudioMoth"
    assert rows[0]["ARU_firmware"] == "AudioMoth-Firmware-Basic 1.11.1"
    assert rows[0]["notes"] == "preserve recording note"
    assert rows[1]["ARU_firmware"] == ""
    assert rows[1]["notes"] == "preserve config note"

    rerun = process(path, apply=True)

    assert path.read_bytes() == payload
    assert not rerun.added_column
    assert rerun.changed == 0
    assert rerun.firmware_recovered == 0
