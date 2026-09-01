"""Tests for AudioMoth metadata extraction, focused on gain normalization."""

from __future__ import annotations

import struct
import wave

import pytest

from cassn.core.audio_metadata import (
    GAIN_SETTINGS,
    normalize_gain,
    parse_audiomoth_config_file,
    parse_audiomoth_wav_comment,
    refresh_audiomoth_inventory_metadata,
)

# Verbatim from a real deployment's WAV ICMT chunk — AudioMoth writes the gain
# in lower case here, mid-sentence.
REAL_COMMENT = (
    "Recorded at 00:00:00 11/05/2026 (UTC-7) by AudioMoth 242A2605648965BB "
    "at high gain while battery was greater than 4.9V and temperature was 13.3C."
)

# Verbatim column layout from a real CONFIG.TXT — capitalized here.
REAL_CONFIG = """AudioMoth 242A2605648965BB
Firmware                        : AudioMoth-Firmware-Basic (1.11.1)
Sample rate (Hz)                : 48000
Gain                            : High
Filter                          : -
Recording period 1              : 00:00 - 09:00 (UTC-8)
"""


def test_refresh_inventory_metadata_uses_authoritative_staged_wav(
    tmp_path, monkeypatch
):
    wav = tmp_path / "verified.wav"
    wav.write_bytes(b"verified")
    entry = {
        "recorded_datetime": "2026-05-10T00:00:00-07:00",
        "recording_duration_sec": "",
        "battery_voltage": "",
        "temperature_c": "",
    }
    monkeypatch.setattr(
        "cassn.core.audio_metadata.parse_audiomoth_wav_comment",
        lambda path: {
            "recording_duration_sec": 32400,
            "sample_rate_hz": "48000",
            "gain_setting": "High",
            "device_id": "COMMENT-ID",
            "battery_voltage": "4.4",
            "temperature_c": "12.0",
        },
    )
    monkeypatch.setattr(
        "cassn.core.audio_metadata.parse_audiomoth_guano",
        lambda path: {
            "recorded_datetime": "2026-05-10T00:00:00-08:00",
            "device_id": "GUANO-ID",
            "ARU_make": "Open Acoustic Devices",
            "ARU_model": "AudioMoth-Firmware-Basic 1.11.1",
            "battery_voltage": "4.5",
            "temperature_c": "12.1",
        },
    )

    assert refresh_audiomoth_inventory_metadata(entry, wav)
    assert entry["recording_duration_sec"] == 32400
    assert entry["recorded_datetime"] == "2026-05-10T00:00:00-08:00"
    assert entry["device_id"] == "GUANO-ID"
    assert entry["battery_voltage"] == "4.5"
    assert entry["temperature_c"] == "12.1"
    assert entry["gain"] == "High"


def write_wav_with_comment(path, comment: str) -> None:
    """A minimal but real WAV carrying a LIST/INFO/ICMT comment chunk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(b"".join(struct.pack("<h", i % 100) for i in range(480)))

    payload = comment.encode("latin-1") + b"\x00"
    chunk = b"ICMT" + struct.pack("<I", len(payload)) + payload
    data = bytearray(path.read_bytes())
    # AudioMoth places the comment in the header; the reader only scans the
    # first 4 KiB, so splice it in just after the RIFF/WAVE preamble.
    data[12:12] = chunk
    struct.pack_into("<I", data, 4, len(data) - 8)
    path.write_bytes(bytes(data))


# ---------------------------------------------------------------------------
# normalize_gain
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("high", "High"),
    ("High", "High"),
    ("HIGH", "High"),
    ("medium", "Medium"),
    ("low", "Low"),
    ("low-medium", "Low-Medium"),
    ("medium-high", "Medium-High"),
    ("  high  ", "High"),
])
def test_normalize_gain_canonicalizes_the_known_vocabulary(raw, expected):
    assert normalize_gain(raw) == expected


@pytest.mark.parametrize("blank", ["", None])
def test_normalize_gain_leaves_blanks_blank(blank):
    assert normalize_gain(blank) == ""


@pytest.mark.parametrize("raw", ["12", "unknown-setting", "6 dB"])
def test_normalize_gain_passes_unknown_values_through(raw):
    """A numeric or future setting must not be reshaped by a guess."""
    assert normalize_gain(raw) == raw


def test_every_canonical_setting_is_its_own_normal_form():
    for value in GAIN_SETTINGS:
        assert normalize_gain(value) == value


# ---------------------------------------------------------------------------
# The two device sources agree after extraction
# ---------------------------------------------------------------------------

def test_config_file_gain_is_canonical(tmp_path):
    config = tmp_path / "CONFIG.TXT"
    config.write_text(REAL_CONFIG)
    assert parse_audiomoth_config_file(config)["gain_setting"] == "High"


def test_wav_comment_gain_is_canonical(tmp_path):
    """AudioMoth writes 'at high gain' here; it must not reach the CSV as 'high'."""
    wav = tmp_path / "UC_Site_plot1_BD_20260714_00001.wav"
    write_wav_with_comment(wav, REAL_COMMENT)
    assert parse_audiomoth_wav_comment(wav)["gain_setting"] == "High"


def test_both_device_sources_agree(tmp_path):
    """The regression: a deployment used to carry 'High' on its CONFIG row and
    'high' on every audio row, and the mismatch reached SoundHub's deployment.csv."""
    config = tmp_path / "CONFIG.TXT"
    config.write_text(REAL_CONFIG)
    wav = tmp_path / "UC_Site_plot1_BD_20260714_00001.wav"
    write_wav_with_comment(wav, REAL_COMMENT)

    from_config = parse_audiomoth_config_file(config)["gain_setting"]
    from_wav = parse_audiomoth_wav_comment(wav)["gain_setting"]
    assert from_config == from_wav == "High"


def test_config_dash_gain_stays_blank(tmp_path):
    config = tmp_path / "CONFIG.TXT"
    config.write_text(REAL_CONFIG.replace(": High", ": -"))
    assert parse_audiomoth_config_file(config)["gain_setting"] == ""


def test_wav_comment_medium_gain(tmp_path):
    """Bat recorders run at medium gain; same lower-case problem."""
    wav = tmp_path / "UC_Site_plot1_BT_20260714_00001.wav"
    write_wav_with_comment(wav, REAL_COMMENT.replace("at high gain", "at medium gain"))
    assert parse_audiomoth_wav_comment(wav)["gain_setting"] == "Medium"


# ---------------------------------------------------------------------------
# Historical inventory records
# ---------------------------------------------------------------------------

def test_csv_projection_normalizes_legacy_inventory_gain():
    """Gain is written into session.json as each file is copied, so a deployment
    part-copied under an older build carries the raw lower-case spelling in
    records the current run never produced. The CSV must still come out uniform."""
    from cassn.export.metadata_csv import normalize_gain as projection_normalize

    assert projection_normalize("high") == "High"
    assert projection_normalize("medium") == "Medium"


# ---------------------------------------------------------------------------
# Duration is independent of the comment chunk
# ---------------------------------------------------------------------------

def write_wav(path, *, comment: str | None = None, junk_bytes: int = 0) -> None:
    """A real WAV, optionally with an ICMT comment and optional leading padding.

    ``junk_bytes`` inserts a JUNK chunk ahead of the comment, pushing ICMT past
    the 4 KiB the reader scans — the layout a repaired pad byte can produce.
    Every chunk is padded to even length, as RIFF requires.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(b"\x00\x00" * 48000)  # exactly one second

    def chunk(cid: bytes, body: bytes) -> bytes:
        return cid + struct.pack("<I", len(body)) + body + (b"\x00" if len(body) % 2 else b"")

    splice = b""
    if junk_bytes:
        splice += chunk(b"JUNK", b"\x00" * junk_bytes)
    if comment is not None:
        splice += chunk(b"ICMT", comment.encode("latin-1") + b"\x00")

    data = bytearray(path.read_bytes())
    data[12:12] = splice
    struct.pack_into("<I", data, 4, len(data) - 8)
    path.write_bytes(bytes(data))


def test_duration_is_read_when_the_comment_chunk_is_absent(tmp_path):
    """Duration comes from the RIFF headers, so a missing ICMT must not hide it.

    It previously did: the duration read sat below the comment-parsing bail-out,
    so a WAV with no ICMT yielded no ``recording_duration_sec`` — which is what
    left 48 AnzaBorrego rows with a blank SoundHub ``end`` timestamp.
    """
    wav = tmp_path / "UC_Site_plot1_BD_20260714_00001.wav"
    write_wav(wav, comment=None)

    result = parse_audiomoth_wav_comment(wav)
    assert result["recording_duration_sec"] == 1
    assert result["sample_rate_hz"] == "48000"
    assert "gain_setting" not in result  # nothing to parse, and nothing invented


def test_duration_is_read_when_the_comment_sits_past_the_scan_window(tmp_path):
    """Only the first 4 KiB is scanned for ICMT; duration must not depend on that."""
    wav = tmp_path / "UC_Site_plot1_BD_20260714_00002.wav"
    write_wav(wav, comment=REAL_COMMENT, junk_bytes=8000)

    assert parse_audiomoth_wav_comment(wav)["recording_duration_sec"] == 1


def test_comment_fields_still_parse_alongside_duration(tmp_path):
    """The ungating must not cost the comment-derived fields when ICMT is present."""
    wav = tmp_path / "UC_Site_plot1_BD_20260714_00003.wav"
    write_wav(wav, comment=REAL_COMMENT)

    result = parse_audiomoth_wav_comment(wav)
    assert result["recording_duration_sec"] == 1
    assert result["gain_setting"] == "High"
