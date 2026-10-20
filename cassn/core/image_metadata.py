"""
EXIF and Reconyx MakerNote extraction.

Two extraction paths, exactly as in the original:

* Pillow reads the standard EXIF block (datetime, make/model) and the raw
  MakerNote bytes; :func:`extract_reconyx_sequence` decodes the Reconyx
  HYPERFIRE HP4K sequence triple straight from those bytes.
* :func:`parse_reconyx_exiftool` shells out to ``exiftool`` for the richer
  Reconyx tags (temperature, moon phase, battery).

``EXIF_AVAILABLE`` reflects whether Pillow/piexif imported successfully; when
False, :func:`extract_exif_data` returns empties and the app degrades the same
way the original did.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from cassn.config import PACIFIC_TZ

try:
    from PIL import Image
    from PIL.ExifTags import TAGS
    import piexif  # noqa: F401  (imported for availability parity with the original)

    EXIF_AVAILABLE = True
except ImportError:
    EXIF_AVAILABLE = False
    print("Warning: PIL/piexif not available. Install with: pip install pillow piexif")

try:
    from exiftool import ExifToolHelper  # PyExifTool — persistent exiftool process

    PYEXIFTOOL_AVAILABLE = True
except ImportError:
    ExifToolHelper = None
    PYEXIFTOOL_AVAILABLE = False


def extract_exif_data(image_path) -> tuple[dict, dict]:
    """Return ``(name_mapped_exif, raw_exif)`` for an image.

    ``name_mapped_exif`` maps human-readable EXIF tag names to values;
    ``raw_exif`` keeps numeric tag ids (needed to reach the MakerNote at 0x927c).
    Returns ``({}, {})`` on any failure or when EXIF support is unavailable.
    """
    if not EXIF_AVAILABLE:
        return {}, {}
    try:
        img = Image.open(image_path)
        raw_exif = img._getexif() or {}
        exif_data = {TAGS.get(tag_id, tag_id): value for tag_id, value in raw_exif.items()}
        return exif_data, raw_exif
    except Exception:
        return {}, {}


def extract_reconyx_sequence(raw_exif: dict):
    """Decode ``(trigger_type, position, total)`` from the Reconyx HP4K MakerNote.

    e.g. ``('M', 1, 3)``; returns ``(None, None, None)`` when unavailable.
    Offsets verified against the HYPERFIRE HP4K MakerNote binary structure.
    """
    try:
        mn = raw_exif.get(0x927C)
        if mn and len(mn) > 42:
            trigger = chr(mn[40])
            pos = mn[41]
            total = mn[42]
            if trigger in ("M", "T", "L") and 1 <= pos <= total:
                return trigger, pos, total
    except Exception:
        pass
    return None, None, None


CAMERA_TIMESTAMP_SOURCE = "pacific_local_v1"


@dataclass(frozen=True)
class CameraTimezoneCheck:
    severity: str
    reported_offset: str
    expected_offsets: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class CameraTimestamp:
    recorded_datetime: str
    recorded_datetime_utc: str
    effective_offset: str
    status: str
    check: CameraTimezoneCheck


def resolve_camera_timestamp(
    exif_data: dict, expected_timezone: str = PACIFIC_TZ.key,
) -> CameraTimestamp:
    """Resolve the original wall clock under the local-civil-time protocol.

    The reported offset is QC evidence only, including during the repeated
    autumn hour. Round trips reject nonexistent times; distinct UTC candidates
    detect ambiguity. No converted timestamp is ever used as input.
    """
    clock = exif_data.get("DateTimeOriginal") or exif_data.get("DateTime")
    reported = str(exif_data.get("OffsetTimeOriginal") or "").strip()
    expected = ()

    def unresolved(status, message):
        return CameraTimestamp("", "", "", status,
                               CameraTimezoneCheck("error", reported, expected, message))

    try:
        wall = datetime.strptime(str(clock), "%Y:%m:%d %H:%M:%S")
    except (TypeError, ValueError):
        return unresolved("invalid_capture_time", "Missing or invalid camera capture time")
    zone = ZoneInfo(expected_timezone)
    candidates = {}
    for fold in (0, 1):
        local = wall.replace(tzinfo=zone, fold=fold)
        try:
            utc = local.astimezone(timezone.utc)
            round_trip = utc.astimezone(zone).replace(tzinfo=None)
        except (OverflowError, ValueError):
            return unresolved("invalid_capture_time", "Camera capture time is outside the supported UTC range")
        if round_trip == wall:
            candidates[utc] = local
    expected = tuple(sorted({local.isoformat()[19:] for local in candidates.values()}))
    if not candidates:
        return unresolved("nonexistent_capture_time",
                          f"Camera capture time does not exist in {expected_timezone} during the daylight-saving transition")
    if len(candidates) > 1:
        return unresolved("ambiguous_capture_time",
                          "Capture falls in the repeated daylight-saving hour; actual clock occurrence needs verification")

    utc, local = next(iter(candidates.items()))
    effective = expected[0]
    severity, status = "pass", "offset_matches"
    message = "Camera timezone offset matches local civil time (clock accuracy not verified)"
    if not reported:
        severity, status = "warning", "offset_missing"
        message = "Camera timezone offset missing; resolved using local civil time"
    elif not re.fullmatch(r"[+-](?:[01]\d|2[0-3]):[0-5]\d", reported):
        severity, status = "warning", "offset_invalid"
        message = "Invalid camera timezone offset; resolved using local civil time"
    elif reported != effective and {reported, effective} != {"-00:00", "+00:00"}:
        severity, status = "warning", "offset_mismatch_corrected"
        message = f"Camera timezone offset mismatch: reported {reported}; expected {effective} for {expected_timezone}; resolved using local civil time"
    if not exif_data.get("DateTimeOriginal"):
        severity = "warning"
        status += ";datetime_fallback"
        message += "; DateTime fallback used because DateTimeOriginal is absent"
    return CameraTimestamp(
        local.isoformat(), utc.isoformat().replace("+00:00", "Z"), effective, status,
        CameraTimezoneCheck(severity, reported, expected, message),
    )


def camera_timestamp_fields(exif_data: dict) -> dict:
    """Evidence and resolution for a newly processed image inventory row."""
    result = resolve_camera_timestamp(exif_data)
    source = next((tag for tag in ("DateTimeOriginal", "DateTime") if exif_data.get(tag)), "")
    return {
        "recorded_datetime": result.recorded_datetime,
        "recorded_datetime_utc": result.recorded_datetime_utc,
        "camera_datetime_original": exif_data.get(source, ""),
        "camera_datetime_source": source,
        "camera_offset_time_original": exif_data.get("OffsetTimeOriginal") or "",
        "camera_effective_offset": result.effective_offset,
        "camera_timestamp_source": CAMERA_TIMESTAMP_SOURCE,
        "camera_timestamp_status": result.status,
        "camera_timestamp_severity": result.check.severity,
    }


def validate_camera_timezone(
    exif_data: dict, expected_timezone: str = PACIFIC_TZ.key,
) -> CameraTimezoneCheck:
    """Use the same wall-clock resolution for validation and new ingestion."""
    return resolve_camera_timestamp(exif_data, expected_timezone).check


def parse_camera_recorded_datetime(exif_data: dict) -> str:
    """Return resolved local ISO time, or empty when the instant is unresolved."""
    return resolve_camera_timestamp(exif_data).recorded_datetime


def _reconyx_sequence_from_exiftool_tags(tags: dict):
    """Decode ``(trigger_type, position, total)`` from ExifTool Reconyx tags.

    Model-agnostic, unlike :func:`extract_reconyx_sequence`: ExifTool selects
    the correct MakerNote table (HyperFire, HyperFire2, UltraFire) from the
    note's signature, so the burst triple is read from the right place
    regardless of camera model. ExifTool emits ``Sequence`` as an ``int16u[2]``
    rendered like ``"1 5"`` (frame 1 of 5); ``TriggerMode`` may be a code
    (``"M"``) or a human string (``"Motion Detection"``), so the first letter
    is taken. Returns ``(None, None, None)`` when absent or unparseable.
    """
    try:
        seq = tags.get("Sequence")
        if seq is None:
            return None, None, None
        if isinstance(seq, (list, tuple)):
            nums = [int(x) for x in seq]
        else:
            nums = [int(n) for n in re.findall(r"\d+", str(seq))]
        if len(nums) < 2:
            return None, None, None
        pos, total = nums[0], nums[1]
        if not (1 <= pos <= total):
            return None, None, None
        tm = str(tags.get("TriggerMode", "")).strip()
        trigger = tm[0].upper() if tm else None
        return trigger, pos, total
    except Exception:
        return None, None, None


def _parse_reconyx_tags(tags: dict) -> dict:
    """Map an ExifTool ``-Reconyx:All`` tag dict to the app's field names.

    Shared by both extraction paths (persistent session and one-shot subprocess)
    so they always produce identical output. Returns temperature, moon phase, and
    battery readings, plus — when present — the sequence triple under
    ``sequence_trigger_type`` / ``sequence_position`` / ``sequence_total``.
    """
    result: dict = {}
    if not tags:
        return result

    # Normalize keys: PyExifTool (ExifToolHelper) returns group-prefixed names
    # ("MakerNotes:SerialNumber") while the one-shot subprocess returns bare names
    # ("SerialNumber"). Strip the group so both paths produce identical lookups —
    # without this, every Reconyx field is silently empty on the PyExifTool path.
    tags = {str(k).split(":")[-1]: v for k, v in tags.items()}

    temp = tags.get("AmbientTemperature", "")
    m = re.match(r"([-\d.]+)", str(temp))
    if m:
        result["temperature_c"] = m.group(1)

    result["moon_phase"] = tags.get("MoonPhase", "")
    result["battery_voltage"] = str(tags.get("BatteryVoltage", ""))
    result["battery_voltage_avg"] = str(tags.get("BatteryVoltageAvg", ""))
    result["battery_type"] = tags.get("BatteryType", "")
    # The camera's hardware serial, straight from the Reconyx MakerNote. Captured
    # for provenance and cross-checked against deployments.csv device_id in QC.
    result["camera_serial_exif"] = str(tags.get("SerialNumber", ""))

    trigger, pos, total = _reconyx_sequence_from_exiftool_tags(tags)
    if pos is not None:
        result["sequence_trigger_type"] = trigger or ""
        result["sequence_position"] = pos
        result["sequence_total"] = total
    return result


def parse_reconyx_exiftool(image_path: Path) -> dict:
    """Extract Reconyx fields via a one-shot ``exiftool`` subprocess.

    The fallback path used when PyExifTool isn't installed. For batch ingest
    prefer :class:`ReconyxExtractor`, which keeps a single exiftool process open
    across many files instead of spawning one per image. See
    :func:`_parse_reconyx_tags` for the fields returned.
    """
    try:
        proc = subprocess.run(
            ["exiftool", "-Reconyx:All", "-json", str(image_path)],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            return {}
        data = json.loads(proc.stdout)
        return _parse_reconyx_tags(data[0]) if data else {}
    except Exception as e:
        print(f"    WARNING: ExifTool failed on {image_path}: {e}")
        return {}


class ReconyxExtractor:
    """Batch Reconyx extractor backed by a persistent ExifTool process.

    Spawning ``exiftool`` once per image is slow on a card of thousands (Perl
    startup dominates). PyExifTool keeps one exiftool process alive in
    ``-stay_open`` mode and feeds it files one after another, so the startup
    cost is paid once per ingest run rather than once per file.

    Lifecycle: call :meth:`start` once before the file loop and :meth:`close`
    after it (or use it as a context manager); call :meth:`parse` per image.
    When PyExifTool is unavailable — or the exiftool binary can't be launched —
    it transparently falls back to the per-file :func:`parse_reconyx_exiftool`
    subprocess, so behavior degrades rather than disappears.
    """

    def __init__(self) -> None:
        self._et = None

    def start(self) -> "ReconyxExtractor":
        if ExifToolHelper is not None:
            try:
                # Drop PyExifTool's default "-n": with it, print-conversion is off and
                # tags come back as numeric codes (MoonPhase=3, BatteryType=2) instead
                # of the human labels ("Waxing Gibbous", "Lithium") the one-shot
                # subprocess path returns. "-G" keys are normalized in _parse_reconyx_tags.
                et = ExifToolHelper(common_args=["-G"])
                et.run()
                self._et = et
            except Exception as e:
                print(
                    "    WARNING: could not start a persistent ExifTool session "
                    f"({e}); falling back to per-file subprocess."
                )
                self._et = None
        return self

    def parse(self, image_path) -> dict:
        if self._et is None:
            return parse_reconyx_exiftool(image_path)  # one-shot fallback
        try:
            data = self._et.execute_json("-Reconyx:All", str(image_path))
            return _parse_reconyx_tags(data[0]) if data else {}
        except Exception as e:
            print(f"    WARNING: ExifTool failed on {image_path}: {e}")
            return {}

    def close(self) -> None:
        if self._et is not None:
            try:
                self._et.terminate()
            except Exception:
                pass
            self._et = None

    def __enter__(self) -> "ReconyxExtractor":
        return self.start()

    def __exit__(self, *exc) -> bool:
        self.close()
        return False

    def __del__(self):
        # Safety net if a caller forgets close() (e.g. exception mid-loop).
        self.close()
