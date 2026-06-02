"""
AudioMoth metadata extraction.

Sources, in order of precedence for the fields each provides:

* the WAV ``guan`` (GUANO) chunk — present on firmware >= 1.10.0 — is the
  primary source for the recorded timestamp (which already carries the
  device's *configured* UTC offset), the serial/device id, battery voltage,
  and temperature;
* the recording filename encodes local Pacific time
  (``20251219_000000.WAV`` -> ``2025-12-19T00:00:00-08:00``) and is the
  fallback timestamp for pre-GUANO files;
* ``CONFIG.TXT`` holds the schedule, gain, filter cutoffs, and device ID;
* the WAV ``ICMT`` comment chunk holds per-file battery voltage, temperature,
  gain, and filter settings.
"""

from __future__ import annotations

import re
import struct
import wave
from datetime import datetime
from pathlib import Path

from cassn.config import PACIFIC_TZ


def hz_to_khz(val) -> str:
    """Convert a Hz value to a trimmed kHz string. Returns '' when blank."""
    if not val:
        return ""
    try:
        return str(round(float(val) / 1000, 4)).rstrip("0").rstrip(".")
    except (ValueError, TypeError):
        return str(val)


def parse_audiomoth_recorded_datetime(original_filename) -> str:
    """Return ISO 8601 datetime with UTC offset from an AudioMoth filename.

    Triggered recordings append a suffix letter (``20260108_000000T.WAV``);
    it is stripped before parsing. Empty string when the filename doesn't parse.
    """
    try:
        stem = Path(original_filename).stem.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        dt_naive = datetime.strptime(stem, "%Y%m%d_%H%M%S")
        return dt_naive.replace(tzinfo=PACIFIC_TZ).isoformat()
    except ValueError:
        return ""


def parse_audiomoth_device_id(source_dir) -> str:
    """Scan ``source_dir`` for a CONFIG.TXT and return its Device ID, or ''."""
    for candidate in Path(source_dir).rglob("*.TXT"):
        try:
            text = candidate.read_text(encoding="utf-8", errors="ignore")
            for line in text.splitlines():
                if line.strip().startswith("Device ID"):
                    parts = line.split(":", 1)
                    if len(parts) == 2:
                        return parts[1].strip()
        except Exception:
            continue
    return ""


def parse_audiomoth_config_file(config_path: Path) -> dict:
    """Parse a CONFIG.TXT file into a dict of AudioMoth settings."""
    result: dict = {}
    if not config_path.exists():
        return result
    try:
        lines = config_path.read_text(encoding="utf-8", errors="replace").splitlines()
        periods: list[tuple[str, str]] = []  # (start, end) per recording period
        for line in lines:
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()

            if key == "Firmware":
                # "AudioMoth-Firmware-Basic (1.11.0)" -> "AudioMoth-Firmware-Basic 1.11.0"
                result["ARU_model"] = re.sub(r"\((.+?)\)", r"\1", val).strip()
            elif key == "Sample rate (Hz)":
                result["sample_rate_hz"] = val if val != "-" else ""
            elif key == "Gain":
                result["gain_setting"] = val if val != "-" else ""
            elif key == "Filter":
                if val == "-":
                    result["low_pass_filter_hz"] = ""
                    result["high_pass_filter_hz"] = ""
                elif val.lower().startswith("high-pass"):
                    m = re.search(r"([\d.]+)\s*khz", val, re.IGNORECASE)
                    result["high_pass_filter_hz"] = str(int(float(m.group(1)) * 1000)) if m else ""
                    result["low_pass_filter_hz"] = ""
                elif val.lower().startswith("low-pass"):
                    m = re.search(r"([\d.]+)\s*khz", val, re.IGNORECASE)
                    result["low_pass_filter_hz"] = str(int(float(m.group(1)) * 1000)) if m else ""
                    result["high_pass_filter_hz"] = ""
                elif val.lower().startswith("bandpass"):
                    # "Bandpass (10.0kHz - 50.0kHz)"
                    nums = re.findall(r"([\d.]+)\s*khz", val, re.IGNORECASE)
                    if len(nums) == 2:
                        result["high_pass_filter_hz"] = str(int(float(nums[0]) * 1000))
                        result["low_pass_filter_hz"] = str(int(float(nums[1]) * 1000))
            elif key == "Threshold setting":
                result["filter_type_amplitude"] = val.rstrip("%") if val != "-" else ""
            elif key == "Minimum trigger duration (s)":
                result["filter_type_duration"] = val if val != "-" else ""
            elif re.match(r"Recording period \d+", key):
                # "00:00 - 09:00 (UTC-8)"
                m = re.match(r"(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})", val)
                if m:
                    periods.append((m.group(1), m.group(2)))
                    result["frequency"] = "daily"

        # Flatten all periods: first start, last end, total duration in HH:MM
        if periods:
            result["deployment_start_time"] = periods[0][0]
            result["deployment_end_time"] = periods[-1][1]
            total_minutes = 0
            for start, end in periods:
                try:
                    t1 = datetime.strptime(start, "%H:%M")
                    t2 = datetime.strptime(end, "%H:%M")
                    delta_mins = int((t2 - t1).seconds / 60)
                    if delta_mins < 0:
                        delta_mins += 24 * 60
                    total_minutes += delta_mins
                except Exception:
                    pass
            h, m = divmod(total_minutes, 60)
            result["duration"] = f"{h:02d}:{m:02d}"
            if len(periods) > 1:
                result["_schedule_note"] = ", ".join(f"{s}-{e}" for s, e in periods)
    except Exception as e:
        print(f"    WARNING: failed to parse CONFIG.TXT at {config_path}: {e}")
    return result


def parse_audiomoth_wav_comment(wav_path: Path) -> dict:
    """Extract AudioMoth metadata from a WAV ICMT comment chunk."""
    result: dict = {}
    try:
        with open(wav_path, "rb") as f:
            data = f.read(4096)

        icmt_pos = data.find(b"ICMT")
        if icmt_pos == -1:
            icmt_pos = data.find(b"Recorded at")  # fallback: scan for comment text
        if icmt_pos == -1:
            return result

        try:
            if data[icmt_pos:icmt_pos + 4] == b"ICMT":
                length = struct.unpack_from("<I", data, icmt_pos + 4)[0]
                comment = data[icmt_pos + 8: icmt_pos + 8 + length].decode("latin-1", errors="replace").rstrip("\x00")
            else:
                comment = data[icmt_pos:icmt_pos + 500].decode("latin-1", errors="replace")
        except Exception:
            return result

        try:
            with wave.open(str(wav_path), "rb") as wf:
                result["sample_rate_hz"] = str(wf.getframerate())
        except Exception:
            pass

        m = re.search(r"at (\w[\w\s-]*?) gain", comment, re.IGNORECASE)
        if m:
            result["gain_setting"] = m.group(1).strip()

        m = re.search(r"by AudioMoth ([0-9A-Fa-f]+)", comment)
        if m:
            result["device_id"] = m.group(1)

        m = re.search(r"battery was (?:greater than )?([\d.]+)V", comment, re.IGNORECASE)
        if m:
            result["battery_voltage"] = m.group(1)

        m = re.search(r"temperature was ([\d.]+)C", comment, re.IGNORECASE)
        if m:
            result["temperature_c"] = m.group(1)

        m = re.search(r"High-pass filter with frequency of ([\d.]+)\s*kHz", comment, re.IGNORECASE)
        if m:
            result["high_pass_filter_hz"] = str(int(float(m.group(1)) * 1000))

        m = re.search(r"Low-pass filter with frequency of ([\d.]+)\s*kHz", comment, re.IGNORECASE)
        if m:
            result["low_pass_filter_hz"] = str(int(float(m.group(1)) * 1000))

        m = re.search(r"Amplitude threshold was (\d+)%", comment, re.IGNORECASE)
        if m:
            result["filter_type_amplitude"] = m.group(1)

        m = re.search(r"(\d+)s minimum trigger duration", comment, re.IGNORECASE)
        if m:
            result["filter_type_duration"] = m.group(1)
    except Exception as e:
        print(f"    WARNING: failed to parse WAV comment at {wav_path}: {e}")
    return result


def _read_guano_chunk(wav_path: Path) -> bytes | None:
    """Return the raw bytes of the WAV ``guan`` chunk, or ``None`` if absent.

    Walks the RIFF chunk table from the front, seeking over each chunk body
    (chunk bodies are padded to an even length) so the multi-gigabyte ``data``
    chunk is never read into memory. The ``guan`` chunk normally trails it.
    """
    with open(wav_path, "rb") as f:
        header = f.read(12)
        if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            return None
        while True:
            chunk_header = f.read(8)
            if len(chunk_header) < 8:
                return None
            chunk_id = chunk_header[:4]
            chunk_size = struct.unpack_from("<I", chunk_header, 4)[0]
            if chunk_id == b"guan":
                return f.read(chunk_size)
            # Skip the body, padded to an even number of bytes.
            f.seek(chunk_size + (chunk_size & 1), 1)


def _normalize_guano_timestamp(value: str) -> str:
    """Normalize a GUANO ``Timestamp`` to an ISO 8601 string.

    GUANO timestamps are ISO 8601 with the device's configured UTC offset
    already applied (``2025-12-28T00:00:00-08:00``), so none is attached here.
    A trailing ``Z`` is accepted as ``+00:00``. The raw value is returned
    unchanged if it does not parse.
    """
    raw = value.strip()
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        return datetime.fromisoformat(candidate).isoformat()
    except ValueError:
        return raw


def parse_audiomoth_guano(wav_path: Path) -> dict:
    """Extract the authoritative fields from a WAV GUANO (``guan``) chunk.

    AudioMoth firmware >= 1.10.0 appends a GUANO metadata chunk (expanded to
    all recording settings in 1.11.0). It is the preferred source for the few
    fields it carries unambiguously — most importantly ``Timestamp``, which
    already encodes the device's *configured* UTC offset, so no DST guesswork
    is needed (unlike the filename-derived datetime).

    Returns a dict with whichever of ``recorded_datetime``, ``device_id``,
    ``battery_voltage``, and ``temperature_c`` the chunk provides. Returns an
    empty dict for pre-1.10.0 files (no ``guan`` chunk) or on any parse error,
    so callers transparently fall back to the ICMT/CONFIG values.
    """
    result: dict = {}
    try:
        raw = _read_guano_chunk(wav_path)
        if not raw:
            return result
        fields: dict[str, str] = {}
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")  # split on the FIRST colon only
            fields[key.strip()] = val.strip()

        if fields.get("Timestamp"):
            result["recorded_datetime"] = _normalize_guano_timestamp(fields["Timestamp"])
        if fields.get("Serial"):
            result["device_id"] = fields["Serial"]
        # Hardware identity, straight from the device. GUANO is authoritative:
        # "Make: Open Acoustic Devices", "Model: AudioMoth". The firmware string
        # is stored as ARU_model to match the CONFIG.TXT-derived value, e.g.
        # "AudioMoth-Firmware-Basic (1.11.0)" -> "AudioMoth-Firmware-Basic 1.11.0".
        if fields.get("Make"):
            result["ARU_make"] = fields["Make"]
        if fields.get("Firmware Version"):
            result["ARU_model"] = re.sub(r"\((.+?)\)", r"\1", fields["Firmware Version"]).strip()
        elif fields.get("Model"):
            result["ARU_model"] = fields["Model"]
        if fields.get("OAD|Battery Voltage"):
            result["battery_voltage"] = fields["OAD|Battery Voltage"]
        if fields.get("Temperature Int"):
            result["temperature_c"] = fields["Temperature Int"]
    except Exception as e:
        print(f"    WARNING: failed to parse GUANO chunk at {wav_path}: {e}")
    return result
