#!/usr/bin/env python3

import sys
import csv
import os
import re
import struct
import subprocess
import wave
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict, Counter
import shutil
import json
import hashlib

# Credentials always live in ~/.cassn_config/ regardless of how the app is launched.
# When frozen (bundled .app): assets live in sys._MEIPASS.
# When running as a script: assets live next to the script.
_CONFIG_DIR = Path.home() / ".cassn_config"
_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
if getattr(sys, 'frozen', False):
    _BUNDLE_DIR = Path(sys._MEIPASS)
else:
    _BUNDLE_DIR = Path(__file__).parent
_LOCAL_DATA_DIR = Path.home() / ".cassn_config" / "lookup_tables"
_LOCAL_DATA_DIR.mkdir(parents=True, exist_ok=True)

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QFormLayout, QLabel, QLineEdit, QComboBox, QDateEdit, QCheckBox,
    QPushButton, QTabWidget, QTreeWidget, QTreeWidgetItem, QTextEdit,
    QFileDialog, QMessageBox, QGroupBox, QGridLayout, QScrollArea,
    QCompleter, QFrame, QSizePolicy, QProgressBar,
    QDialog, QDialogButtonBox, QListWidget, QListWidgetItem, QInputDialog,
)
from PySide6.QtCore import Qt, QDate, QStringListModel, QThread, Signal
from PySide6.QtGui import QFont, QPixmap

# Box SDK
try:
    from box_sdk_gen import BoxClient, BoxOAuth, OAuthConfig
    BOX_AVAILABLE = True
except ImportError:
    BOX_AVAILABLE = False
    print("Warning: box-sdk-gen not available. Install with: pip install box-sdk-gen")

# EXIF handling
try:
    from PIL import Image
    from PIL.ExifTags import TAGS
    import piexif
    EXIF_AVAILABLE = True
except ImportError:
    EXIF_AVAILABLE = False
    print("Warning: PIL/piexif not available. Install with: pip install pillow piexif")

APP_TITLE = "CA-SSN Field Data Manager"
VERSION = "3.0"

# Load Box credentials from config.json
def load_box_config():
    """Load Box configuration from config.json"""
    config_path = _CONFIG_DIR / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}\n"
            "Please copy config.json.example to config.json and add your Box credentials."
        )

    with open(config_path, 'r') as f:
        config = json.load(f)

    return (
        config['box']['client_id'],
        config['box']['client_secret'],
        config['box']['field_data_folder_id'],
        config['box'].get('app_config_folder_id')
    )

try:
    BOX_CLIENT_ID, BOX_CLIENT_SECRET, BOX_TARGET_FOLDER_ID, BOX_APP_CONFIG_FOLDER_ID = load_box_config()
except FileNotFoundError as e:
    print(f"Warning: {e}")
    BOX_CLIENT_ID = BOX_CLIENT_SECRET = BOX_TARGET_FOLDER_ID = BOX_APP_CONFIG_FOLDER_ID = None

CHUNKED_UPLOAD_THRESHOLD = 20 * 1024 * 1024  # 20 MB — use chunked upload above this size
MIN_IMAGE_SIZE_BYTES = 50 * 1024              # 50 KB — files below this are likely corrupted writes

# Subfolder under each deployment that holds QC/audit sidecar files
# (qc_report.json, box_upload_manifest.json, box_upload_verification.json,
# deployment_summary.txt). Keeps the deployment root clean.
QC_SUBFOLDER = "qc"
QC_SIDECAR_FILES = (
    "qc_report.json",
    "box_upload_manifest.json",
    "box_upload_verification.json",
    "deployment_summary.txt",
)


def qc_path_for(deployment_folder: Path, filename: str) -> Path:
    """Path to a QC sidecar file inside the qc/ subfolder of a deployment.
    Creates the subfolder if missing."""
    qc_dir = deployment_folder / QC_SUBFOLDER
    qc_dir.mkdir(parents=True, exist_ok=True)
    return qc_dir / filename


def migrate_qc_sidecars(deployment_folder: Path):
    """One-shot migration: move pre-existing QC sidecars from the deployment
    root into qc/. Safe to call repeatedly; no-op if files are already in qc/."""
    if not deployment_folder.is_dir():
        return
    qc_dir = deployment_folder / QC_SUBFOLDER
    for name in QC_SIDECAR_FILES:
        old = deployment_folder / name
        new = qc_dir / name
        if old.exists() and not new.exists():
            try:
                qc_dir.mkdir(parents=True, exist_ok=True)
                old.rename(new)
            except Exception:
                pass  # best-effort; don't block on migration failure

def _required_local_csv_path(filename):
    """Return the required repo-local CSV path."""
    return _LOCAL_DATA_DIR / filename


def load_reserves_from_csv():
    """Load reserves from local_data/sites.csv."""
    reserves = []
    csv_path = _required_local_csv_path("sites.csv")

    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                site_code = row['site_code'].strip()
                site_name = row['site_name'].strip()
                if site_code and site_name:
                    reserves.append((site_code, site_name))
        if not reserves:
            raise ValueError(f"No site rows found in {csv_path}")
    except Exception as e:
        print(
            "Warning: Could not load site lookup data — "
            "lookup tables will be synced from Box on startup. "
            f"({e})"
        )
        reserves = []

    return reserves


def load_plot_names_from_csv():
    """Load plot names from local_data/plots.csv.

    Returns:
      plot_names: dict[site_code -> dict[plot_number_int -> plot_name_str]]
      plot_metadata: dict[(site_code, plot_number) -> full row dict]

    Plot count per reserve is unbounded — Quail Ridge can have 5 plots while
    Bodega has 4. The UI rebuilds the plot grid based on the chosen reserve.
    """
    plot_names: dict[str, dict[int, str]] = {}
    plot_metadata = {}
    csv_path = _required_local_csv_path("plots.csv")

    try:
        for encoding in ("utf-8", "utf-8-sig", "latin-1"):
            try:
                with open(csv_path, 'r', encoding=encoding) as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        site_code = row['site_code'].strip()
                        plot_number = int(row['plot_number'])
                        plot_name = row['plot_name'].strip()
                        plot_latitude = row.get('plot_latitude', '').strip()
                        plot_longitude = row.get('plot_longitude', '').strip()
                        plot_description = row.get('plot_description', '').strip()

                        if site_code not in plot_names:
                            plot_names[site_code] = {}

                        if plot_number >= 1 and plot_name:
                            plot_names[site_code][plot_number] = plot_name
                        plot_metadata[(site_code, plot_number)] = {
                            'plot_name': plot_name,
                            'plot_latitude': plot_latitude,
                            'plot_longitude': plot_longitude,
                            'plot_description': plot_description,
                        }
                break  # successfully read — stop trying encodings
            except UnicodeDecodeError:
                plot_names = {}
                plot_metadata = {}
                continue
    except Exception as e:
        print(
            "Warning: Could not load plot lookup data — "
            "lookup tables will be synced from Box on startup. "
            f"({e})"
        )
        plot_names = {}
        plot_metadata = {}

    return plot_names, plot_metadata


# Organization and reserve lists
ORGANIZATIONS = ["UC"]
RESERVES = load_reserves_from_csv()
PLOT_NAMES, PLOT_METADATA = load_plot_names_from_csv()

# Device type definitions
DEVICE_TYPES = {
    "ML": "Medium-Large Animal Camera",
    "SA": "Small Animal Camera",
    "BD": "Acoustic Recorder Birds",
    "BT": "Acoustic Recorder Bats",
}

# People list for data downloader
DOWNLOADERS = [
    "Bloom, Ryan",
    "Imperato, John",
    "Kaplan-Zenk, Samara",
    "Other"
]

# File type classification
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.bmp', '.gif', '.cr2', '.nef', '.arw', '.dng'}
AUDIO_EXTENSIONS = {'.wav', '.mp3', '.flac', '.m4a', '.aac', '.wma', '.ogg'}
CONFIG_EXTENSIONS = {'.txt'}


def classify_file(filename):
    """Classify file as image, audio, config, or other."""
    ext = Path(filename).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    elif ext in AUDIO_EXTENSIONS:
        return "audio"
    elif ext in CONFIG_EXTENSIONS:
        return "config"
    else:
        return "other"


def extract_exif_data(image_path):
    """Extract EXIF data from image file. Returns name-mapped dict and raw exif dict."""
    if not EXIF_AVAILABLE:
        return {}, {}

    try:
        img = Image.open(image_path)
        raw_exif = img._getexif() or {}
        exif_data = {TAGS.get(tag_id, tag_id): value for tag_id, value in raw_exif.items()}
        return exif_data, raw_exif
    except Exception:
        return {}, {}


def extract_reconyx_sequence(raw_exif):
    """
    Returns (trigger_type, position, total) from Reconyx HP4K MakerNote.
    e.g. ('M', 1, 3) or (None, None, None) if unavailable.
    Offsets verified against HYPERFIRE HP4K MakerNote binary structure.
    """
    try:
        mn = raw_exif.get(0x927c)
        if mn and len(mn) > 42:
            trigger = chr(mn[40])
            pos = mn[41]
            total = mn[42]
            if trigger in ('M', 'T', 'L') and 1 <= pos <= total:
                return trigger, pos, total
    except Exception:
        pass
    return None, None, None


_PACIFIC = ZoneInfo('America/Los_Angeles')


def parse_camera_recorded_datetime(exif_data):
    """
    Returns ISO 8601 datetime with UTC offset from Reconyx EXIF.
    e.g. '2025-12-04T15:48:05-08:00'
    """
    try:
        dt_str = exif_data.get('DateTimeOriginal') or exif_data.get('DateTime')
        offset_str = exif_data.get('OffsetTimeOriginal', '')
        if dt_str:
            dt = datetime.strptime(f'{dt_str}{offset_str}', '%Y:%m:%d %H:%M:%S%z')
            return dt.astimezone(_PACIFIC).isoformat()
    except Exception:
        pass
    return ''


def parse_audiomoth_recorded_datetime(original_filename):
    """
    Returns ISO 8601 datetime with UTC offset from AudioMoth filename.
    AudioMoth filenames encode local Pacific time: '20251219_000000.WAV' -> '2025-12-19T00:00:00-08:00'
    Triggered recordings append a suffix letter e.g. '20260108_000000T.WAV' — stripped before parsing.
    """
    try:
        stem = Path(original_filename).stem.rstrip('ABCDEFGHIJKLMNOPQRSTUVWXYZ')
        dt_naive = datetime.strptime(stem, '%Y%m%d_%H%M%S')
        return dt_naive.replace(tzinfo=_PACIFIC).isoformat()
    except ValueError:
        return ''


def parse_audiomoth_device_id(source_dir):
    """
    Scans source_dir for an AudioMoth CONFIG.TXT and extracts the Device ID.
    Returns the ID string (e.g. '24F31904648873F9') or '' if not found.
    """
    for candidate in Path(source_dir).rglob('*.TXT'):
        try:
            text = candidate.read_text(encoding='utf-8', errors='ignore')
            for line in text.splitlines():
                if line.strip().startswith('Device ID'):
                    parts = line.split(':', 1)
                    if len(parts) == 2:
                        return parts[1].strip()
        except Exception:
            continue
    return ''


def load_soundhub_config(path: Path) -> dict:
    if not path.exists():
        print(f"  WARNING: soundhub_config.json not found at {path}")
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_arus(path: Path) -> dict:
    """Load ARUs.csv → dict keyed (site_code, plot_number_int, device_type).

    ARUs are physical assets at a fixed location — `mounted_on`,
    `sensor_height_meters`, and `ARU_status` describe the install, not the
    deployment event. So one row per (site, plot, device); no duplication
    across deployment events.

    Older CSVs may include a `deployment_event_id` column for back-compat —
    it's accepted but ignored. If multiple rows share the same (site, plot,
    device) key, the last one wins.
    """
    result = {}
    if not path.exists():
        print(f"  WARNING: ARUs.csv not found at {path}")
        return result
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                key = (
                    row["site_code"].strip(),
                    int(row["plot_number"]),
                    row["device_type"].strip(),
                )
                result[key] = {k: v.strip() for k, v in row.items()}
            except (KeyError, ValueError):
                continue
    return result


def parse_audiomoth_config_file(config_path: Path) -> dict:
    """Parse a CONFIG.TXT file. Returns dict of AudioMoth settings."""
    result = {}
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
                # "AudioMoth-Firmware-Basic (1.11.0)" → "AudioMoth-Firmware-Basic 1.11.0"
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
                    # Bandpass: "Bandpass (10.0kHz - 50.0kHz)"
                    nums = re.findall(r"([\d.]+)\s*khz", val, re.IGNORECASE)
                    if len(nums) == 2:
                        result["high_pass_filter_hz"] = str(int(float(nums[0]) * 1000))
                        result["low_pass_filter_hz"] = str(int(float(nums[1]) * 1000))
            elif key == "Threshold setting":
                result["filter_type_amplitude"] = val.rstrip("%") if val != "-" else ""
            elif key == "Minimum trigger duration (s)":
                result["filter_type_duration"] = val if val != "-" else ""
            elif re.match(r"Recording period \d+", key):
                # "00:00 - 09:00 (UTC-8)" — collect all periods
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
            # If multiple periods, record full schedule in notes-friendly format
            if len(periods) > 1:
                result["_schedule_note"] = ", ".join(f"{s}-{e}" for s, e in periods)

    except Exception as e:
        print(f"    WARNING: failed to parse CONFIG.TXT at {config_path}: {e}")
    return result


def parse_audiomoth_wav_comment(wav_path: Path) -> dict:
    """Extract AudioMoth metadata from WAV ICMT comment chunk."""
    result = {}
    try:
        with open(wav_path, "rb") as f:
            data = f.read(4096)

        # Find ICMT chunk
        icmt_pos = data.find(b"ICMT")
        if icmt_pos == -1:
            # Fallback: scan for AudioMoth comment pattern
            icmt_pos = data.find(b"Recorded at")
        if icmt_pos == -1:
            return result

        # Read length (4 bytes after ICMT tag) then content
        try:
            if data[icmt_pos:icmt_pos+4] == b"ICMT":
                length = struct.unpack_from("<I", data, icmt_pos + 4)[0]
                comment = data[icmt_pos + 8: icmt_pos + 8 + length].decode("latin-1", errors="replace").rstrip("\x00")
            else:
                # Raw text
                comment = data[icmt_pos:icmt_pos + 500].decode("latin-1", errors="replace")
        except Exception:
            return result

        # Get sample rate from WAV header
        try:
            with wave.open(str(wav_path), "rb") as wf:
                result["sample_rate_hz"] = str(wf.getframerate())
        except Exception:
            pass

        # Parse comment string
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

        result["ARU_make"] = "AudioMoth"

    except Exception as e:
        print(f"    WARNING: failed to parse WAV comment at {wav_path}: {e}")
    return result


def parse_reconyx_exiftool(image_path: Path) -> dict:
    """Use ExifTool to extract Reconyx-specific fields."""
    result = {}
    try:
        proc = subprocess.run(
            ["exiftool", "-Reconyx:All", "-json", str(image_path)],
            capture_output=True, text=True, timeout=15
        )
        if proc.returncode != 0:
            return result
        data = json.loads(proc.stdout)
        if not data:
            return result
        tags = data[0]

        # AmbientTemperature: "4 C" → "4"
        temp = tags.get("AmbientTemperature", "")
        m = re.match(r"([-\d.]+)", str(temp))
        if m:
            result["temperature_c"] = m.group(1)

        result["moon_phase"] = tags.get("MoonPhase", "")
        result["battery_voltage"] = str(tags.get("BatteryVoltage", ""))
        result["battery_voltage_avg"] = str(tags.get("BatteryVoltageAvg", ""))
        result["battery_type"] = tags.get("BatteryType", "")

    except Exception as e:
        print(f"    WARNING: ExifTool failed on {image_path}: {e}")
    return result


def _hz_to_khz(val) -> str:
    """Convert Hz value to kHz string. Returns '' if blank."""
    if not val:
        return ''
    try:
        return str(round(float(val) / 1000, 4)).rstrip('0').rstrip('.')
    except (ValueError, TypeError):
        return str(val)


IMAGE_FIELDS = [
    'filename', 'original_filename', 'deployment_event_id', 'deployment_id',
    'organization', 'site', 'site_full_name', 'site_code',
    'start_date', 'end_date', 'recorded_by',
    'subproject', 'subproject_design', 'placename', 'event_name', 'event_description',
    'plot_number', 'device_type', 'camera_id', 'file_type',
    'file_size_bytes', 'file_hash_sha256', 'recorded_datetime',
    'latitude', 'longitude',
    'camera_make', 'camera_model',
    'sequence_trigger_type', 'sequence_event_num', 'sequence_position', 'sequence_total',
    'temperature_c', 'moon_phase', 'battery_voltage', 'battery_voltage_avg', 'battery_type',
    'project_id', 'bait_type', 'bait_description', 'event_type', 'quiet_period',
    'camera_functioning', 'feature_type', 'feature_type_methodology',
    'sensor_height', 'height_other', 'sensor_orientation', 'orientation_other',
    'plot_treatment', 'plot_treatment_description', 'detection_distance',
    'app_version', 'processing_datetime',
    'is_uploaded_to_box', 'box_uploader', 'box_upload_datetime',
    'is_uploaded_to_pelican', 'pelican_uploader', 'pelican_upload_datetime',
    'is_submitted_to_wi', 'wi_submitter', 'wi_submission_datetime',
    'notes',
]

AUDIO_FIELDS = [
    'filename', 'original_filename', 'deployment_event_id', 'deployment_id',
    'organization', 'site', 'site_full_name', 'site_code',
    'deployment_start_date', 'deployment_end_date', 'recorded_by',
    'subproject', 'subproject_design', 'placename', 'event_name', 'event_description',
    'plot_number', 'device_type', 'device_id', 'file_type',
    'file_size_bytes', 'file_hash_sha256', 'recorded_datetime',
    'latitude', 'longitude',
    'ARU_make', 'ARU_model', 'sample_rate_hz', 'gain', 'filter_type_khz',
    'battery_voltage', 'temperature_c',
    'date_installed', 'deployment_start_time', 'deployment_end_time',
    'frequency', 'duration', 'filter_type_duration', 'filter_type_amplitude',
    'feature_type', 'feature_type_details', 'ARU_container', 'ARU_microphone',
    'mounted_on', 'sensor_height_meters', 'ARU_status',
    'app_version', 'processing_datetime',
    'is_uploaded_to_box', 'box_uploader', 'box_upload_datetime',
    'is_uploaded_to_pelican', 'pelican_uploader', 'pelican_upload_datetime',
    'is_submitted_to_soundhub', 'soundhub_submitter', 'soundhub_submission_datetime',
    'is_submitted_to_nabat', 'nabat_submitter', 'nabat_submission_datetime',
    'notes',
]


def compute_file_hash(filepath):
    """Compute SHA-256 hash of file"""
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def compute_file_sha1(filepath):
    """Compute SHA-1 hash of file. Used for end-to-end verification against Box,
    which exposes SHA-1 in file metadata."""
    sha1 = hashlib.sha1()
    with open(filepath, "rb") as f:
        for byte_block in iter(lambda: f.read(65536), b""):  # 64 KB blocks — faster than 4 KB on large files
            sha1.update(byte_block)
    return sha1.hexdigest()


def write_device_manifest(device_label: str, device_dir: Path, entries: list):
    """
    Write a fixity manifest sidecar for a completed device.
    Records file count plus {filename, size_bytes, sha256} for every file.
    Written before the SD card is ejected — last chance for a tamper-evident record.
    """
    manifest = {
        "device_label": device_label,
        "generated": datetime.now().isoformat(),
        "file_count": len(entries),
        "files": [
            {
                "filename": e.get("new_filename", ""),
                "size_bytes": e.get("file_size_bytes", 0),
                "sha256": e.get("file_hash_sha256", ""),
            }
            for e in entries
        ],
    }
    manifest_path = device_dir / f"{device_label}_manifest.json"
    try:
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)
    except Exception as exc:
        print(f"Warning: could not write manifest for {device_label}: {exc}")


def scan_orphans(deployment_folder: Path, file_inventory: list) -> dict:
    """
    Detect orphaned files in both directions:
      - 'missing_from_disk': inventory entries whose file no longer exists on disk
      - 'orphaned_on_disk': media files on disk with no matching inventory entry
    Returns a dict with both lists (entries are dicts with 'path' and optionally 'device').
    """
    raw_data = deployment_folder / "raw_data"
    if not raw_data.exists():
        return {"missing_from_disk": [], "orphaned_on_disk": []}

    # Build set of paths that inventory says should exist
    inventoried_new = {entry["new_filename"] for entry in file_inventory if entry.get("new_filename")}

    missing_from_disk = []
    for entry in file_inventory:
        new_name = entry.get("new_filename", "")
        device_label = entry.get("device_label", "")
        if not new_name:
            continue
        expected = raw_data / device_label / new_name
        if not expected.exists():
            missing_from_disk.append({"path": str(expected), "device": device_label, "filename": new_name})

    orphaned_on_disk = []
    for device_dir in raw_data.iterdir():
        if not device_dir.is_dir():
            continue
        for fpath in device_dir.iterdir():
            if fpath.is_file() and not fpath.name.startswith(".") and fpath.suffix.lower() in (".jpg", ".wav", ".txt"):
                if fpath.name not in inventoried_new:
                    orphaned_on_disk.append({"path": str(fpath), "device": device_dir.name, "filename": fpath.name})

    return {"missing_from_disk": missing_from_disk, "orphaned_on_disk": orphaned_on_disk}


def generate_session_summary(
    deployment_folder: Path,
    metadata: dict,
    file_inventory: list,
    devices: list,
) -> Path:
    """
    Write deployment_summary.txt to deployment_folder.
    Returns the path to the written file.
    """
    lines = []
    lines.append("=" * 60)
    lines.append("CASSN DEPLOYMENT SUMMARY")
    lines.append("=" * 60)
    lines.append(f"Generated:        {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"Reserve:          {metadata.get('reserve_name', '?')}")
    lines.append(f"Organization:     {metadata.get('organization', '?')}")
    lines.append(f"Observer:         {metadata.get('observer', '?')}")
    lines.append(f"Deployment start: {metadata.get('deployment_start', '?')}")
    lines.append(f"Deployment end:   {metadata.get('deployment_end', '?')}")
    lines.append("")

    # Device summary
    device_counts: dict[str, int] = {}
    for entry in file_inventory:
        dl = entry.get("device_label", "unknown")
        device_counts[dl] = device_counts.get(dl, 0) + 1
    total_files = len(file_inventory)

    lines.append(f"Total files inventoried: {total_files}")
    lines.append(f"Devices configured:      {len(devices)}")
    lines.append("")
    lines.append("Per-device file counts:")
    for label, count in sorted(device_counts.items()):
        lines.append(f"  {label}: {count} files")
    lines.append("")

    # Coordinate list (unique plot → first seen lat/lon)
    plot_coords: dict[str, tuple] = {}
    for entry in file_inventory:
        p = entry.get("plot_number", "")
        lat = entry.get("latitude", "")
        lon = entry.get("longitude", "")
        if p and lat and lon and p not in plot_coords:
            plot_coords[p] = (lat, lon)
    if plot_coords:
        lines.append("Plot coordinates (first seen):")
        for plot, (lat, lon) in sorted(plot_coords.items()):
            lines.append(f"  Plot {plot}: {lat}, {lon}")
        lines.append("")

    # QC summary
    qc_path = qc_path_for(deployment_folder, "qc_report.json")
    if qc_path.exists():
        try:
            with open(qc_path) as f:
                qc_data = json.load(f)
            current = qc_data.get("current_state", [])
            errors = [c for c in current if c.get("severity") == "error"]
            warnings_list = [c for c in current if c.get("severity") == "warning"]
            lines.append(f"QC report: {len(errors)} error(s), {len(warnings_list)} warning(s) — current state")
            if errors:
                lines.append("  Errors:")
                for c in errors[:10]:
                    lines.append(f"    [{c.get('_device', '')}] {c.get('message', '')}")
            if warnings_list:
                lines.append("  Warnings:")
                for c in warnings_list[:10]:
                    lines.append(f"    [{c.get('_device', '')}] {c.get('message', '')}")
        except Exception:
            lines.append("QC report: could not read qc_report.json")
    else:
        lines.append("QC report: none")
    lines.append("")
    lines.append("=" * 60)

    summary_path = qc_path_for(deployment_folder, "deployment_summary.txt")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return summary_path


QC_CHECK_DESCRIPTIONS = {
    "hash_verification": (
        "Per-file SHA-256 hash verification: source hash computed before copy, destination "
        "hash computed after copy, and the two must match. Mismatches abort the copy and "
        "remove the destination file."
    ),
    "file_size_floor": (
        "Files copied below the 50 KB threshold are flagged as possible corrupted writes. "
        "Real RECONYX images are >1 MB; AudioMoth WAVs are at least several MB."
    ),
    "duplicate_detection": (
        "Files whose SHA-256 matches an already-inventoried file in this session are flagged "
        "and skipped. Indicates the same physical file was copied twice (e.g., re-running a card)."
    ),
    "expected_file_count": (
        "User-supplied expected file count (auto-counted from source) is compared against the "
        "number of files actually copied. Mismatch means files were skipped or missed."
    ),
    "sequence_gap": (
        "RECONYX burst integrity: each event_num should have exactly sequence_total frames, "
        "and event numbers should be contiguous with no missing events."
    ),
    "temporal_plausibility": (
        "Recorded timestamps should fall within the deployment window. Flags files dated "
        "before deployment start, more than 48h after collection, clock-reset clusters "
        "(many files at the same second), and >30-day gaps within a device."
    ),
    "field_completeness": (
        "Required metadata fields (recorded_datetime, latitude, longitude, sequence_trigger_type) "
        "should be populated for at least 90% of files. Low completeness suggests EXIF read errors."
    ),
    "box_upload": (
        "After Box upload, the deployment folder is recursively listed and reconciled against "
        "the pre-upload manifest. Reports any uploaded file that doesn't appear on Box."
    ),
    "box_verify": (
        "On-demand Box reconciliation: list every file under the deployment's Box folder via "
        "the Box API and diff against the local file_inventory. Catches uploads that finished "
        "with no error but left files behind."
    ),
    "file_hash_verification_run": (
        "On-demand end-to-end check: compute SHA-1 of every local file in raw_data/ and "
        "compare against the SHA-1 Box reports for the same file. Box computes SHA-1 "
        "server-side from the stored bytes, so a match proves the file on Box is "
        "byte-identical to the file locally — catches any corruption introduced during upload."
    ),
    "file_hash_mismatch": (
        "Local file's SHA-1 does not match the SHA-1 Box has on file. Indicates either "
        "local corruption since copy time, or that the upload did not deliver byte-identical "
        "contents to Box."
    ),
    "file_hash_missing": (
        "File listed in session inventory but no longer present on disk. Indicates accidental "
        "deletion or a drive that has gone offline."
    ),
    "orphan_scan": (
        "On-demand: compares file_inventory against files actually present on disk. Reports "
        "inventoried files missing from disk and disk files missing from inventory."
    ),
    "session_health": (
        "Each session.json found in staging is parsed at app launch. Truncated or malformed "
        "files are flagged so they can be repaired before being silently lost."
    ),
    "pre_departure": (
        "Aggregate readiness check before closing or switching deployments: all devices "
        "complete, manifests written, fixity check run, Box upload done, no QC errors."
    ),
}


def _compute_qc_current_state(history_session: list, history_devices: dict) -> list:
    """
    Given the append-only history, compute the current-state view.

    Rules:
      1. For each unique (check_name, device) pair, take the entry with the
         latest timestamp.
      2. Per-file detail entries (`file_hash_mismatch`, `file_hash_missing`)
         are dropped from current state if a `file_hash_verification_run`
         entry exists with a newer timestamp — that run supersedes them.

    Returns a list of entries sorted: errors first, then warnings, then passes;
    within each severity, sorted by check name then device.
    """
    # Walk history, tagging each entry with its scope (device or "")
    all_entries: list[dict] = []
    for e in history_session:
        e2 = dict(e)
        e2["_device"] = ""
        all_entries.append(e2)
    for dev, items in (history_devices or {}).items():
        for e in items:
            e2 = dict(e)
            e2["_device"] = dev
            all_entries.append(e2)

    # Most recent file_hash_verification_run timestamp (for filtering stale per-file entries)
    runs = [e for e in all_entries if e.get("check") == "file_hash_verification_run"]
    latest_run_ts = max((e["timestamp"] for e in runs), default="")

    # Group by (check, device); keep latest per group
    by_key: dict[tuple, dict] = {}
    for e in all_entries:
        key = (e["check"], e["_device"])
        prev = by_key.get(key)
        if prev is None or e["timestamp"] > prev["timestamp"]:
            by_key[key] = e

    current: list[dict] = []
    for (check, _), e in by_key.items():
        # Drop stale per-file fixity entries
        if check in ("file_hash_mismatch", "file_hash_missing") and latest_run_ts:
            if e["timestamp"] < latest_run_ts:
                continue
        current.append(e)

    severity_rank = {"error": 0, "warning": 1, "pass": 2}
    current.sort(key=lambda e: (
        severity_rank.get(e.get("severity"), 9),
        e.get("check", ""),
        e.get("_device", ""),
    ))
    return current


def append_qc_report(deployment_folder: Path, check: str, device: str, severity: str, message: str):
    """Append one finding to qc_report.json in the deployment folder.

    Structure:
      {
        "generated": "...",
        "current_state": [...],      # computed: latest entry per (check, device)
        "history": {
          "session_checks": [...],   # full chronological log of session-level checks
          "devices": { "p1_ML": [...], ... }
        }
      }

    History is append-only; current_state is rebuilt on every call.
    """
    report_path = qc_path_for(deployment_folder, "qc_report.json")
    if report_path.exists():
        try:
            with open(report_path) as f:
                report = json.load(f)
        except Exception:
            report = {}
    else:
        report = {}

    # Migrate legacy flat structure (session_checks/devices at top level) to nested history
    if "history" not in report:
        report["history"] = {
            "session_checks": report.pop("session_checks", []),
            "devices": report.pop("devices", {}),
        }
    history = report["history"]
    history.setdefault("session_checks", [])
    history.setdefault("devices", {})

    entry = {
        "check": check,
        "description": QC_CHECK_DESCRIPTIONS.get(check, ""),
        "severity": severity,
        "message": message,
        "timestamp": datetime.now().isoformat(),
    }

    if device:
        history["devices"].setdefault(device, []).append(entry)
    else:
        history["session_checks"].append(entry)

    report["generated"] = datetime.now().isoformat()
    report["current_state"] = _compute_qc_current_state(
        history["session_checks"], history["devices"]
    )

    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)


def check_sequence_integrity(entries: list, device_label: str) -> list[str]:
    """
    Check RECONYX sequence completeness for one device's file_inventory entries.
    Returns a list of warning strings (empty = all clear).
    Checks:
      (a) for each sequence_event_num, actual file count vs sequence_total
      (b) gaps in sequence_event_num across the device
    """
    warnings = []
    image_entries = [e for e in entries if e.get('file_type') == 'image' and e.get('sequence_event_num') not in ('', None)]
    if not image_entries:
        return warnings

    bursts = defaultdict(list)
    for e in image_entries:
        try:
            bursts[int(e['sequence_event_num'])].append(e)
        except (ValueError, TypeError):
            pass

    if not bursts:
        return warnings

    # (a) check each burst: actual count vs sequence_total
    for event_num, files in sorted(bursts.items()):
        totals = {e.get('sequence_total') for e in files if e.get('sequence_total') not in ('', None)}
        if totals:
            try:
                expected = int(next(iter(totals)))
                actual = len(files)
                if actual != expected:
                    warnings.append(
                        f"Burst #{event_num}: expected {expected} frames, found {actual} "
                        f"(missing {expected - actual})"
                    )
            except (ValueError, TypeError):
                pass

    # (b) check for gaps in event_num sequence
    event_nums = sorted(bursts.keys())
    for i in range(1, len(event_nums)):
        if event_nums[i] != event_nums[i - 1] + 1:
            gap_start = event_nums[i - 1] + 1
            gap_end = event_nums[i] - 1
            warnings.append(
                f"Gap in event sequence: events {gap_start}–{gap_end} missing "
                f"(jumped from {event_nums[i-1]} to {event_nums[i]})"
            )

    return warnings


def validate_datetimes(entries: list, deployment_start: str, collection_date: str) -> list[str]:
    """
    Temporal plausibility checks for one device's entries.
    Returns a list of warning strings (empty = all clear).
    Checks: pre-deployment dates, post-collection dates, clock-reset clusters, 30-day mid-deployment gaps.

    Note: EXIF datetimes are timezone-aware (e.g. with -08:00 offset). Deployment
    bounds are stripped to naive dates for comparison so we don't mix aware/naive.
    """
    warnings = []
    datetimes = []  # list of NAIVE datetimes for safe comparison
    for e in entries:
        dt_str = e.get('recorded_datetime', '')
        if not dt_str:
            continue
        try:
            dt = datetime.fromisoformat(str(dt_str))
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)
            datetimes.append(dt)
        except Exception:
            pass

    if not datetimes:
        return warnings

    try:
        deploy_start = datetime.strptime(deployment_start, "%Y-%m-%d")
    except Exception:
        deploy_start = None
    try:
        collect_end = datetime.strptime(collection_date, "%Y-%m-%d") + timedelta(hours=48)
    except Exception:
        collect_end = None

    pre_deploy = [dt for dt in datetimes if deploy_start and dt < deploy_start]
    if pre_deploy:
        warnings.append(f"{len(pre_deploy)} file(s) recorded before deployment start ({deployment_start}); earliest: {min(pre_deploy).isoformat()}")

    post_collect = [dt for dt in datetimes if collect_end and dt > collect_end]
    if post_collect:
        warnings.append(f"{len(post_collect)} file(s) recorded >48h after collection date ({collection_date}); latest: {max(post_collect).isoformat()}")

    # Clock-reset cluster: ≥3 files with same timestamp to the second
    ts_counts = Counter(dt.replace(microsecond=0) for dt in datetimes)
    resets = [(ts, cnt) for ts, cnt in ts_counts.items() if cnt >= 3]
    if resets:
        worst = max(resets, key=lambda x: x[1])
        warnings.append(f"Possible clock reset: {worst[1]} files share timestamp {worst[0].isoformat()} — camera clock may have reset to factory default")

    # >30-day gap within deployment
    sorted_dts = sorted(datetimes)
    for i in range(1, len(sorted_dts)):
        gap = (sorted_dts[i] - sorted_dts[i - 1]).days
        if gap > 30:
            warnings.append(f">30-day gap mid-deployment: {sorted_dts[i-1].date()} → {sorted_dts[i].date()} ({gap} days)")

    return warnings


REQUIRED_FIELDS_BY_TYPE = {
    'image': (
        'recorded_datetime', 'latitude', 'longitude', 'device_id',
        'file_hash_sha256', 'file_size_bytes', 'sequence_trigger_type',
    ),
    'audio': (
        'recorded_datetime', 'latitude', 'longitude', 'device_id',
        'file_hash_sha256', 'file_size_bytes', 'sample_rate_hz',
    ),
}

def check_field_completeness(file_inventory: list) -> dict[str, float]:
    """
    Compute % population for required fields per file_type. Config files (.txt sidecars)
    have no required fields and are excluded.
    Returns {"<file_type> <field>": pct_populated} for every required field with at least
    one relevant file in the inventory.
    """
    if not file_inventory:
        return {}
    result = {}
    for ft, fields in REQUIRED_FIELDS_BY_TYPE.items():
        relevant = [e for e in file_inventory if e.get('file_type') == ft]
        if not relevant:
            continue
        for field in fields:
            populated = sum(1 for e in relevant if e.get(field) not in ('', None))
            result[f"{ft} {field}"] = 100.0 * populated / len(relevant)
    return result


def validate_coordinates(file_inventory: list, bounds: dict | None = None) -> list[str]:
    """
    Validate (latitude, longitude) across all inventory entries.
    bounds: dict with optional keys lat_min, lat_max, lon_min, lon_max.
    Checks: non-zero, within bounds, plot always maps to same coords.
    Returns list of warning strings.
    """
    if bounds is None:
        bounds = {}
    lat_min = float(bounds.get('lat_min', 32.0))
    lat_max = float(bounds.get('lat_max', 42.5))
    lon_min = float(bounds.get('lon_min', -125.0))
    lon_max = float(bounds.get('lon_max', -114.0))

    warnings = []
    plot_coords: dict[str, set] = defaultdict(set)

    for e in file_inventory:
        lat_raw = e.get('latitude', '')
        lon_raw = e.get('longitude', '')
        plot = str(e.get('plot_number', ''))
        if lat_raw == '' or lon_raw == '':
            continue
        try:
            lat = float(lat_raw)
            lon = float(lon_raw)
        except (ValueError, TypeError):
            continue

        if lat == 0.0 and lon == 0.0:
            warnings.append(f"Plot {plot}: coordinates are (0, 0) — likely unset")
            continue

        if not (lat_min <= lat <= lat_max) or not (lon_min <= lon <= lon_max):
            warnings.append(f"Plot {plot}: ({lat}, {lon}) is outside expected study area bounds")

        if plot:
            plot_coords[plot].add((round(lat, 6), round(lon, 6)))

    for plot, coord_set in plot_coords.items():
        if len(coord_set) > 1:
            warnings.append(f"Plot {plot}: inconsistent coordinates across devices — {coord_set}")

    return warnings


def get_box_client():
    """Get authenticated Box client with automatic token refresh"""
    token_file = _CONFIG_DIR / "box_tokens.json"

    if not token_file.exists():
        return None
    
    try:
        # Simple custom token storage that works with our JSON format
        class SimpleTokenStorage:
            def __init__(self, token_file_path):
                self.token_file = token_file_path
                
            def store(self, token):
                """Store token - called automatically on refresh"""
                tokens = {
                    'access_token': token.access_token,
                    'refresh_token': token.refresh_token
                }
                with open(self.token_file, 'w') as f:
                    json.dump(tokens, f, indent=2)
            
            def get(self):
                """Get current token"""
                try:
                    with open(self.token_file, 'r') as f:
                        data = json.load(f)
                    # Return a token-like object
                    from box_sdk_gen import AccessToken
                    return AccessToken(
                        access_token=data['access_token'],
                        refresh_token=data.get('refresh_token')
                    )
                except:
                    return None
            
            def clear(self):
                """Clear tokens"""
                if self.token_file.exists():
                    self.token_file.unlink()
        
        # Create OAuth config with our custom storage
        config = OAuthConfig(
            client_id=BOX_CLIENT_ID,
            client_secret=BOX_CLIENT_SECRET,
            token_storage=SimpleTokenStorage(token_file)
        )
        
        # Create OAuth instance - it will load tokens from storage
        auth = BoxOAuth(config)
        
        # Create and return client - it will auto-refresh tokens as needed
        return BoxClient(auth)
        
    except Exception as e:
        print(f"Box authentication error: {e}")
        import traceback
        traceback.print_exc()
        return None


# Initialized after function definitions so loaders are available
SOUNDHUB_CONFIG = load_soundhub_config(_LOCAL_DATA_DIR / "soundhub_config.json")
ARUS = load_arus(_LOCAL_DATA_DIR / "ARUs.csv")


class BoxUploadThread(QThread):
    """Background thread for uploading files to Box"""
    progress = Signal(int, int, str)  # current, total, message
    finished = Signal(bool, str)  # success, message

    UPLOAD_WORKERS = 8  # parallel upload workers

    def __init__(self, deployment_folder, metadata, file_inventory=None):
        super().__init__()
        self.deployment_folder = deployment_folder
        self.metadata = metadata
        self.file_inventory = file_inventory or []
        self.client = None
        self.deploy_folder_id = None  # set during run(); used for provenance re-upload
        self._box_folder_files: dict[str, dict[str, str]] = {}  # folder_id → {filename: file_id}
        # Cache of (parent_folder_id, child_folder_name) → child_folder_id, so we
        # don't re-resolve the same intermediate folder once per file.
        self._resolved_folders: dict[tuple, str] = {}
        # Cooperative cancellation flag — set from the GUI thread via cancel()
        import threading as _threading
        self._cancel_event = _threading.Event()
        self._state_lock = _threading.Lock()  # protects shared upload counters

    def cancel(self):
        """Request cooperative cancellation. In-flight uploads finish; no new ones start."""
        self._cancel_event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()
    
    def run(self):
        """Upload deployment folder to Box"""
        try:
            self.client = get_box_client()
            if not self.client:
                self.finished.emit(False, "Could not authenticate with Box")
                return
            
            # Validate reserve name against canonical list before building path
            valid_names = {name for _, name in RESERVES}
            reserve_name = self.metadata.get('reserve_name', '')
            if reserve_name not in valid_names:
                self.finished.emit(
                    False,
                    f"Reserve name '{reserve_name}' not found in sites.csv. "
                    "Cannot determine Box folder path."
                )
                return

            # Build nested path: root → year → reserve → deployment
            TARGET_FOLDER_ID = BOX_TARGET_FOLDER_ID
            year = self.metadata['deployment_end'][:4]
            deploy_name = self.deployment_folder.name

            year_folder   = self.find_or_create_folder(self.client, TARGET_FOLDER_ID, year)
            site_folder   = self.find_or_create_folder(self.client, year_folder.id, reserve_name)
            deploy_folder = self.find_or_create_folder(self.client, site_folder.id, deploy_name)
            self.deploy_folder_id = deploy_folder.id  # saved for provenance re-upload

            uploadable_files = [
                path for path in self.deployment_folder.rglob('*')
                if path.is_file() and self.should_upload_file(path)
            ]
            total_files = len(uploadable_files)
            uploaded = 0

            # Build hash lookup from file_inventory (name → sha256)
            hash_lookup = {
                e.get('new_filename', ''): e.get('file_hash_sha256', '')
                for e in (self.file_inventory or [])
            }

            # Write pre-upload manifest
            manifest_entries = [
                {
                    "relative_path": str(fp.relative_to(self.deployment_folder)),
                    "filename": fp.name,
                    "sha256": hash_lookup.get(fp.name, ""),
                }
                for fp in uploadable_files
            ]
            manifest_data = {
                "generated": datetime.now().isoformat(),
                "deployment": self.deployment_folder.name,
                "file_count": len(manifest_entries),
                "files": manifest_entries,
            }
            manifest_path = qc_path_for(self.deployment_folder, "box_upload_manifest.json")
            with open(manifest_path, "w") as f:
                json.dump(manifest_data, f, indent=2)

            # PHASE 1 — Pre-resolve all unique destination folders and prime caches.
            # Doing this once up-front (instead of per file) eliminates ~2 API
            # roundtrips per file, which dominates wall-clock time when files are
            # small and the network is the bottleneck.
            unique_parent_paths = sorted({
                tuple(fp.relative_to(self.deployment_folder).parts[:-1])
                for fp in uploadable_files
            })
            parent_id_for_path: dict[tuple, str] = {(): deploy_folder.id}
            for path_parts in unique_parent_paths:
                if not path_parts:
                    continue
                # Resolve each segment, building up the parent_id chain.
                current_id = deploy_folder.id
                for i, segment in enumerate(path_parts):
                    sub_key = path_parts[: i + 1]
                    if sub_key in parent_id_for_path:
                        current_id = parent_id_for_path[sub_key]
                        continue
                    folder = self.find_or_create_folder(self.client, current_id, segment)
                    parent_id_for_path[sub_key] = folder.id
                    current_id = folder.id

            # Pre-populate file-existence cache for every unique parent folder.
            for path_parts, folder_id in parent_id_for_path.items():
                if folder_id in self._box_folder_files:
                    continue
                existing: dict[str, str] = {}
                offset = 0
                while True:
                    page = self.client.folders.get_folder_items(folder_id, limit=1000, offset=offset)
                    entries = page.entries or []
                    for item in entries:
                        if item.type == 'file':
                            existing[item.name] = item.id
                    if len(entries) < 1000:
                        break
                    offset += 1000
                self._box_folder_files[folder_id] = existing

            # PHASE 2 — Parallel upload via ThreadPoolExecutor. Each upload is
            # network I/O so the GIL releases during socket ops; threading is
            # effective. State mutation is protected by self._state_lock.
            from concurrent.futures import ThreadPoolExecutor, as_completed

            failed_uploads: list[tuple[str, str]] = []  # (relative_path, error_message)
            cancelled_count = 0

            def _upload_one(file_path):
                """Worker: upload one file with up to 3 retries. Honors cancellation."""
                if self._cancel_event.is_set():
                    return ("cancelled", str(file_path.relative_to(self.deployment_folder)), None)
                rel_path = file_path.relative_to(self.deployment_folder)
                last_err = None
                for attempt in range(1, 4):
                    if self._cancel_event.is_set():
                        return ("cancelled", str(rel_path), None)
                    try:
                        self.upload_file_with_path(file_path, deploy_folder.id, rel_path)
                        return ("ok", str(rel_path), file_path.name)
                    except Exception as e:
                        last_err = e
                return ("fail", str(rel_path), str(last_err)[:200] if last_err else "unknown error")

            with ThreadPoolExecutor(max_workers=self.UPLOAD_WORKERS) as ex:
                futures = {ex.submit(_upload_one, fp): fp for fp in uploadable_files}
                for fut in as_completed(futures):
                    try:
                        status, rel_path, payload = fut.result()
                    except Exception as e:
                        # Defensive: shouldn't happen since _upload_one catches its own
                        rel_path = str(futures[fut].relative_to(self.deployment_folder))
                        status, payload = "fail", str(e)[:200]

                    with self._state_lock:
                        if status == "ok":
                            uploaded += 1
                            self.progress.emit(uploaded, total_files, payload or "")
                        elif status == "cancelled":
                            cancelled_count += 1
                        else:
                            failed_uploads.append((rel_path, payload or ""))
                            # Still bump the progress so the UI doesn't appear stuck
                            self.progress.emit(uploaded, total_files, "")

            if self._cancel_event.is_set():
                # User-requested cancellation — exit before verification.
                append_qc_report(self.deployment_folder, "box_upload", "", "warning",
                                 f"Upload cancelled by user. {uploaded}/{total_files} uploaded, "
                                 f"{cancelled_count} not attempted, {len(failed_uploads)} failed.")
                self.finished.emit(False,
                    f"Upload cancelled. {uploaded} of {total_files} files uploaded; "
                    f"{cancelled_count} not yet attempted; {len(failed_uploads)} failed. "
                    "Re-run the upload to resume — already-uploaded files will be skipped.")
                return

            # Post-upload verification: recursively list Box folder and compare against manifest.
            # Paginates each folder — without this, large device folders (>1000 files)
            # were being undercounted, causing huge false-positive "missing from Box" reports.
            try:
                box_file_names: set[str] = set()

                def _list_all_paginated(folder_id):
                    offset = 0
                    limit = 1000
                    while True:
                        page = self.client.folders.get_folder_items(folder_id, limit=limit, offset=offset)
                        entries = page.entries or []
                        for it in entries:
                            yield it
                        if len(entries) < limit:
                            return
                        offset += limit

                def _collect(folder_id):
                    for sub in _list_all_paginated(folder_id):
                        if sub.type == "file":
                            box_file_names.add(sub.name)
                        elif sub.type == "folder":
                            _collect(sub.id)

                _collect(deploy_folder.id)

                expected_names = {e["filename"] for e in manifest_entries}
                missing = expected_names - box_file_names
                if missing:
                    missing_str = ", ".join(sorted(missing)[:10])
                    append_qc_report(self.deployment_folder, "box_upload", "", "warning",
                                     f"{len(missing)} file(s) uploaded but missing from Box: {missing_str}")
                    self.finished.emit(failed_uploads == [],
                        f"Uploaded {uploaded} of {total_files} files. "
                        f"{len(missing)} appear missing from Box after verification: {missing_str}. "
                        f"{len(failed_uploads)} per-file upload error(s). "
                        "Run 'Verify Box Upload' to investigate. Re-running the upload will retry failed/missing files.")
                    return
                append_qc_report(self.deployment_folder, "box_upload", "", "pass",
                                 f"All {uploaded} file(s) uploaded and verified on Box")
            except Exception:
                pass  # verification failure is non-fatal; upload succeeded

            if failed_uploads:
                # Surface per-file failures even if Box-side reconciliation passed
                fail_lines = "\n".join(f"  • {p}: {err}" for p, err in failed_uploads[:10])
                more = f"\n... +{len(failed_uploads) - 10} more" if len(failed_uploads) > 10 else ""
                append_qc_report(self.deployment_folder, "box_upload", "", "warning",
                                 f"{len(failed_uploads)} file(s) failed to upload after retries")
                self.finished.emit(False,
                    f"Uploaded {uploaded} of {total_files} files. "
                    f"{len(failed_uploads)} file(s) failed after 3 retries each:\n{fail_lines}{more}\n\n"
                    "Re-run the upload — successfully uploaded files will be skipped, only failed ones retry.")
                return

            self.finished.emit(True, f"Successfully uploaded {uploaded} files to Box (verified)")

        except Exception as e:
            self.finished.emit(False, f"Upload error: {str(e)}")

    @staticmethod
    def should_upload_file(file_path):
        """Skip macOS/Finder junk files and internal app files from Box uploads."""
        name = file_path.name
        SKIP_NAMES = {'session.json'}
        if name.startswith('.') or name.startswith('._') or name in SKIP_NAMES:
            return False
        # Per-device manifests are local-only fixity sidecars — don't upload
        if name.endswith('_manifest.json') and 'raw_data' in file_path.parts:
            return False
        return True
    
    def find_or_create_folder(self, client, parent_id, folder_name):
        """Find existing folder or create new one. Resolution is cached so the same
        (parent_id, folder_name) pair is only resolved once per upload session."""
        # Check resolution cache first — avoids per-file API roundtrips for the
        # same intermediate folder hierarchy.
        cache_key = (parent_id, folder_name)
        cached_id = self._resolved_folders.get(cache_key)
        if cached_id is not None:
            # Return a lightweight namespace that mimics the .id attribute callers expect.
            class _CachedFolder:
                pass
            f = _CachedFolder()
            f.id = cached_id
            return f

        try:
            # Search for existing folder. limit=1000 avoids the Box default of 100,
            # which could miss folders in busy parents and cause duplicate-folder bugs.
            offset = 0
            while True:
                page = client.folders.get_folder_items(parent_id, limit=1000, offset=offset)
                entries = page.entries or []
                for item in entries:
                    if item.type == 'folder' and item.name == folder_name:
                        self._resolved_folders[cache_key] = item.id
                        return item
                if len(entries) < 1000:
                    break
                offset += 1000

            # Not found — create it
            from box_sdk_gen import CreateFolderParent
            parent = CreateFolderParent(id=parent_id)
            new_folder = client.folders.create_folder(folder_name, parent)
            self._resolved_folders[cache_key] = new_folder.id
            return new_folder
        except Exception as e:
            raise Exception(f"Error creating folder '{folder_name}': {e}")
    
    def upload_file_with_path(self, local_path, parent_folder_id, relative_path):
        """Upload file maintaining directory structure.

        For files under raw_data/ (immutable media), skip if already on Box.
        For everything else (mutable metadata sidecars: CSVs, JSONs), upload
        as a new file version when the file already exists, so re-uploads
        after adding a device or regenerating CSVs actually refresh Box.
        """
        # Create any intermediate folders
        current_folder_id = parent_folder_id

        if len(relative_path.parts) > 1:
            for folder_name in relative_path.parts[:-1]:
                folder = self.find_or_create_folder(
                    self.client, current_folder_id, folder_name
                )
                current_folder_id = folder.id

        # Build a paginated {filename -> file_id} cache per folder on first access
        file_name = relative_path.name
        if current_folder_id not in self._box_folder_files:
            existing: dict[str, str] = {}
            offset = 0
            while True:
                page = self.client.folders.get_folder_items(
                    current_folder_id, limit=1000, offset=offset
                )
                for item in page.entries:
                    if item.type == 'file':
                        existing[item.name] = item.id
                if len(page.entries) < 1000:
                    break
                offset += 1000
            self._box_folder_files[current_folder_id] = existing

        existing_id = self._box_folder_files[current_folder_id].get(file_name)

        # Immutable raw_data files: skip if already on Box
        is_raw_data = "raw_data" in relative_path.parts
        if existing_id and is_raw_data:
            return

        file_size = local_path.stat().st_size

        with open(local_path, 'rb') as file_stream:
            if existing_id and not is_raw_data:
                # Mutable sidecar already on Box — upload as a new version so the
                # current local copy replaces the stale one. Box keeps version history.
                from box_sdk_gen import UploadFileVersionAttributes
                self.client.uploads.upload_file_version(
                    existing_id,
                    attributes=UploadFileVersionAttributes(name=file_name),
                    file=file_stream,
                )
            elif file_size >= CHUNKED_UPLOAD_THRESHOLD:
                self.client.chunked_uploads.upload_big_file(
                    file=file_stream,
                    file_name=file_name,
                    file_size=file_size,
                    parent_folder_id=current_folder_id,
                )
            else:
                result = self.client.uploads.upload_file(
                    attributes={'name': file_name, 'parent': {'id': current_folder_id}},
                    file=file_stream,
                )
                # Cache the new file's id so subsequent calls in this session
                # see it as an existing file.
                try:
                    new_id = result.entries[0].id  # type: ignore[attr-defined]
                    self._box_folder_files[current_folder_id][file_name] = new_id
                except Exception:
                    # Fall back to a placeholder if the SDK shape ever changes.
                    self._box_folder_files[current_folder_id].setdefault(file_name, "")


class BoxVerifyThread(QThread):
    """List the Box deployment folder and compare against local file_inventory.
    Reports files missing from Box and unexpected files on Box."""
    finished = Signal(bool, str, list)  # ok, summary, issues

    def __init__(self, deployment_folder: Path, file_inventory: list, metadata: dict):
        super().__init__()
        self.deployment_folder = deployment_folder
        self.file_inventory = file_inventory
        self.metadata = metadata

    def run(self):
        try:
            client = get_box_client()
            if not client:
                self.finished.emit(False, "Could not authenticate with Box", [])
                return

            valid_names = {name for _, name in RESERVES}
            reserve_name = self.metadata.get("reserve_name", "")
            if reserve_name not in valid_names:
                self.finished.emit(False, f"Reserve '{reserve_name}' not in sites.csv", [])
                return

            year = self.metadata.get("deployment_end", "")[:4]
            deploy_name = self.deployment_folder.name

            # Helper: paginated folder listing (Box default page size is small)
            def _list_all(folder_id):
                offset = 0
                limit = 1000
                while True:
                    page = client.folders.get_folder_items(folder_id, limit=limit, offset=offset)
                    entries = page.entries or []
                    for it in entries:
                        yield it
                    if len(entries) < limit:
                        return
                    offset += limit

            # Walk Box: root -> year -> reserve -> deployment
            def _find_child(parent_id, name):
                for it in _list_all(parent_id):
                    if it.type == "folder" and it.name == name:
                        return it.id
                return None

            year_id = _find_child(BOX_TARGET_FOLDER_ID, year)
            if not year_id:
                self.finished.emit(False, f"Year folder '{year}' not found on Box", [])
                return
            reserve_id = _find_child(year_id, reserve_name)
            if not reserve_id:
                self.finished.emit(False, f"Reserve folder '{reserve_name}' not found on Box", [])
                return
            deploy_id = _find_child(reserve_id, deploy_name)
            if not deploy_id:
                self.finished.emit(False, f"Deployment folder '{deploy_name}' not found on Box", [])
                return

            # Recursively collect all filenames under deployment (with pagination)
            box_filenames: set[str] = set()
            def _collect(folder_id):
                for it in _list_all(folder_id):
                    if it.type == "file":
                        box_filenames.add(it.name)
                    elif it.type == "folder":
                        _collect(it.id)
            _collect(deploy_id)

            local_filenames = {e["new_filename"] for e in self.file_inventory if e.get("new_filename")}
            missing = local_filenames - box_filenames
            extra = box_filenames - local_filenames

            # Filter out known deployment metadata files from the "extra" list — these
            # are generated by the app and legitimately uploaded but never appear in
            # file_inventory (which only tracks raw media files).
            EXPECTED_METADATA = {
                "image_file_metadata.csv",
                "audio_file_metadata.csv",
                "deployment_event_record.json",
                "qc_report.json",
                "box_upload_manifest.json",
                "box_upload_verification.json",
                "deployment_summary.txt",
            }
            extra = {
                name for name in extra
                if name not in EXPECTED_METADATA
                and not name.startswith("wildlife_insights_")
                and not name.endswith("_manifest.json")
            }

            issues = (
                [{"type": "missing_from_box", "filename": n} for n in sorted(missing)]
                + [{"type": "extra_on_box", "filename": n} for n in sorted(extra)]
            )
            ok = not missing  # extras are informational, not failures
            if not missing and not extra:
                summary = "All files are present on Box."
            else:
                summary = f"Box verify: {len(missing)} missing from Box, {len(extra)} unexpected."
            self.finished.emit(ok, summary, issues)
        except Exception as e:
            self.finished.emit(False, f"Box verify error: {e}", [])


class ProvenanceUploadThread(QThread):
    """Re-uploads the two provenance-updated metadata CSVs to Box after upload completion."""
    finished = Signal(bool, str)

    def __init__(self, csv_paths, deploy_folder_id):
        super().__init__()
        self.csv_paths = csv_paths          # list of Path objects
        self.deploy_folder_id = deploy_folder_id

    def run(self):
        try:
            client = get_box_client()
            if not client:
                self.finished.emit(False, "Could not authenticate with Box for provenance upload")
                return

            for csv_path in self.csv_paths:
                if not csv_path.exists():
                    continue
                # Check if file already exists in the folder; if so, upload new version
                items = client.folders.get_folder_items(self.deploy_folder_id).entries
                existing = next((item for item in items if item.type == 'file' and item.name == csv_path.name), None)
                with open(csv_path, 'rb') as f:
                    if existing:
                        from box_sdk_gen import UploadFileVersionAttributes
                        client.uploads.upload_file_version(
                            existing.id,
                            attributes=UploadFileVersionAttributes(name=csv_path.name),
                            file=f,
                        )
                    else:
                        client.uploads.upload_file(
                            attributes={'name': csv_path.name, 'parent': {'id': self.deploy_folder_id}},
                            file=f,
                        )

            self.finished.emit(True, "Provenance CSVs uploaded to Box")
        except Exception as e:
            self.finished.emit(False, f"Provenance upload error: {e}")


class FixityCheckThread(QThread):
    """End-to-end file-hash verification: compute SHA-1 of each local file and
    compare against the SHA-1 Box reports for the same file. Box's SHA-1 is
    computed server-side from the bytes that actually arrived and are stored,
    so a match proves the file on Box is byte-identical to the file locally."""
    progress = Signal(int, int, str)   # checked, total, current_filename
    finished = Signal(bool, str, list)  # ok, summary_message, mismatch_list

    def __init__(self, deployment_folder: Path, file_inventory: list, metadata: dict):
        super().__init__()
        self.deployment_folder = deployment_folder
        self.file_inventory = file_inventory
        self.metadata = metadata
        import threading as _threading
        self._cancel_event = _threading.Event()

    def cancel(self):
        self._cancel_event.set()

    def run(self):
        try:
            # 1) Authenticate with Box
            client = get_box_client()
            if not client:
                self.finished.emit(False, "Could not authenticate with Box.", [])
                return

            # 2) Walk to the deployment folder on Box
            valid_names = {name for _, name in RESERVES}
            reserve_name = self.metadata.get('reserve_name', '')
            if reserve_name not in valid_names:
                self.finished.emit(False, f"Reserve '{reserve_name}' not in sites.csv.", [])
                return

            year = self.metadata.get('deployment_end', '')[:4]
            deploy_name = self.deployment_folder.name

            def _list_all(folder_id):
                offset = 0
                limit = 1000
                while True:
                    page = client.folders.get_folder_items(folder_id, limit=limit, offset=offset)
                    entries = page.entries or []
                    for it in entries:
                        yield it
                    if len(entries) < limit:
                        return
                    offset += limit

            def _find_child(parent_id, name):
                for it in _list_all(parent_id):
                    if it.type == "folder" and it.name == name:
                        return it.id
                return None

            year_id = _find_child(BOX_TARGET_FOLDER_ID, year)
            if not year_id:
                self.finished.emit(False, f"Year folder '{year}' not found on Box.", [])
                return
            reserve_id = _find_child(year_id, reserve_name)
            if not reserve_id:
                self.finished.emit(False, f"Reserve folder '{reserve_name}' not found on Box.", [])
                return
            deploy_id = _find_child(reserve_id, deploy_name)
            if not deploy_id:
                self.finished.emit(False, f"Deployment folder '{deploy_name}' not found on Box.", [])
                return

            # 3) Recursively collect every Box file's SHA-1, keyed by (parent_folder_name, filename)
            # This mirrors the local layout: raw_data/<device_label>/<filename>
            box_hashes: dict[tuple[str, str], str] = {}  # (parent_folder_name, filename) -> sha1
            box_names_only: set[str] = set()             # for fallback name-only lookups
            self.progress.emit(0, 0, "Listing Box folder contents…")

            def _collect(folder_id, parent_name):
                if self._cancel_event.is_set():
                    return
                for it in _list_all(folder_id):
                    if it.type == "file":
                        sha1 = getattr(it, 'sha1', None) or ''
                        box_hashes[(parent_name, it.name)] = sha1.lower()
                        box_names_only.add(it.name)
                    elif it.type == "folder":
                        _collect(it.id, it.name)

            _collect(deploy_id, deploy_name)

            if self._cancel_event.is_set():
                self.finished.emit(False, "Verification cancelled.", [])
                return

            # 4) Walk local raw_data/ and verify each file's bytes against Box
            raw_data_dir = self.deployment_folder / "raw_data"
            if not raw_data_dir.is_dir():
                self.finished.emit(False, "raw_data/ directory not found.", [])
                return

            files_to_check = [
                f for f in raw_data_dir.rglob("*")
                if f.is_file() and not f.name.startswith(".")
            ]
            total = len(files_to_check)
            mismatches = []          # local SHA-1 != Box SHA-1
            missing_from_box = []    # local file has no counterpart on Box
            checked = 0
            seen_keys: set[tuple[str, str]] = set()

            for fp in files_to_check:
                if self._cancel_event.is_set():
                    self.finished.emit(False, f"Verification cancelled after {checked}/{total} files.", [])
                    return
                self.progress.emit(checked, total, fp.name)
                # Local path: raw_data/<device_label>/<filename>
                key = (fp.parent.name, fp.name)
                seen_keys.add(key)
                box_sha1 = box_hashes.get(key)
                if box_sha1 is None:
                    # Box doesn't have this file at this path
                    missing_from_box.append(f"{fp.parent.name}/{fp.name}")
                else:
                    local_sha1 = compute_file_sha1(fp)
                    if local_sha1.lower() != box_sha1:
                        mismatches.append({
                            "filename": f"{fp.parent.name}/{fp.name}",
                            "local_sha1": local_sha1,
                            "box_sha1": box_sha1,
                        })
                checked += 1

            self.progress.emit(checked, total, "")

            # 5) Compose result
            all_ok = not mismatches and not missing_from_box
            summary = (
                f"End-to-end hash verification: {checked} local file(s) checked against Box. "
                f"{len(mismatches)} hash mismatch(es), {len(missing_from_box)} file(s) missing from Box."
            )
            issues = (
                [{"type": "mismatch", "filename": m["filename"]} for m in mismatches]
                + [{"type": "missing", "filename": n} for n in missing_from_box]
            )
            self.finished.emit(all_ok, summary, issues)

        except Exception as e:
            self.finished.emit(False, f"Verification error: {e}", [])


class FieldDataWizard(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(900, 700)

        # Data storage
        self.metadata = {}
        self.devices = []
        self.staging_root = Path.home() / "Desktop" / "CASSN_field_data_staging"
        self.current_deployment_folder = None
        self.file_inventory = []
        self.seen_file_hashes = set()  # session-wide duplicate detection
        self.upload_thread = None
        self.provenance_thread = None
        self.fixity_thread = None
        self.fixity_check_run = False  # set True once fixity check completes this session
        self.box_upload_complete = False  # set True once BoxUploadThread finishes successfully
        
        # Load saved config
        self.config_file = Path.home() / ".cassn_config" / "config.json"
        self.load_config()

        # Load Wildlife Insights lookup tables
        self.wi_camera_metadata = self._load_wi_camera_metadata()
        self.wi_config = self._load_wi_config()

        # Check Box authentication
        self.box_authenticated = self.check_box_auth()

        # Sync lookup tables from Box app_config folder (falls back to cache if offline)
        self.sync_lookup_tables()

        # Build UI
        self.init_ui()

        # Offer to resume an in-progress session
        sessions = self.find_all_sessions()
        if sessions:
            self.offer_resume_session(sessions)

    def check_box_auth(self):
        """Check if Box is authenticated"""
        if not BOX_AVAILABLE:
            return False
        
        # Look for tokens next to .app (or script folder when not bundled)
        token_file = _CONFIG_DIR / "box_tokens.json"
        if not token_file.exists():
            return False
        
        try:
            client = get_box_client()
            if client:
                user = client.users.get_user_me()
                return True
        except:
            pass
        
        return False
    
    def sync_lookup_tables(self):
        """Download latest lookup tables from Box app_config folder.

        On success: saves a sync timestamp and reloads all lookup data.
        On failure: checks for cached files.
          - If cache exists: warns user with last-synced date and asks to confirm.
          - If no cache: hard blocks with an error and quits.
        """
        if not BOX_APP_CONFIG_FOLDER_ID:
            return

        timestamp_path = _LOCAL_DATA_DIR / ".last_synced"

        try:
            client = get_box_client()
            if not client:
                return

            _LOCAL_DATA_DIR.mkdir(parents=True, exist_ok=True)
            items = client.folders.get_folder_items(BOX_APP_CONFIG_FOLDER_ID)
            synced = []

            for item in items.entries:
                if item.type != 'file':
                    continue
                local_path = _LOCAL_DATA_DIR / item.name
                content = client.downloads.download_file(item.id)
                with open(local_path, 'wb') as f:
                    for chunk in content:
                        f.write(chunk)
                synced.append(item.name)

            if synced:
                # Save sync timestamp
                timestamp_path.write_text(datetime.now().isoformat())
                print(f"Synced lookup tables from Box: {', '.join(synced)}")

            # Reload all lookup data with fresh files
            global RESERVES, PLOT_NAMES, PLOT_METADATA, SOUNDHUB_CONFIG, ARUS
            RESERVES = load_reserves_from_csv()
            PLOT_NAMES, PLOT_METADATA = load_plot_names_from_csv()
            SOUNDHUB_CONFIG = load_soundhub_config(_LOCAL_DATA_DIR / "soundhub_config.json")
            ARUS = load_arus(_LOCAL_DATA_DIR / "ARUs.csv")
            self.wi_camera_metadata = self._load_wi_camera_metadata()
            self.wi_config = self._load_wi_config()

            # Rebuild the plot grid in case the chosen reserve gained or lost plots.
            # Safe to call even if the UI hasn't been built yet — guarded below.
            try:
                if hasattr(self, 'grid_layout') and hasattr(self, 'site_code_edit'):
                    self._rebuild_plot_grid(self.site_code_edit.text() or None)
            except Exception:
                pass

        except Exception as e:
            print(f"Could not sync lookup tables from Box: {e}")
            self._handle_sync_failure(timestamp_path)

    def _handle_sync_failure(self, timestamp_path):
        """Handle a failed sync — warn user or hard block depending on cache state."""
        sites_cache = _LOCAL_DATA_DIR / "sites.csv"

        # No cache at all — hard block
        if not sites_cache.exists():
            QMessageBox.critical(
                self,
                "Lookup Tables Missing",
                "Could not connect to Box and no cached lookup tables were found.\n\n"
                "An internet connection is required on first launch to download "
                "the lookup tables needed to run the app.\n\n"
                "Please connect to the internet and relaunch."
            )
            sys.exit(1)

        # Cache exists — show last synced date and ask to confirm
        if timestamp_path.exists():
            try:
                synced_at = datetime.fromisoformat(timestamp_path.read_text().strip())
                date_str = synced_at.strftime("%B %d, %Y at %I:%M %p")
                age_msg = f"Lookup tables were last synced on {date_str}."
            except Exception:
                age_msg = "Lookup table sync date is unknown."
        else:
            age_msg = "Lookup tables have never been synced from Box."

        reply = QMessageBox.warning(
            self,
            "Could Not Sync Lookup Tables",
            f"Could not connect to Box to sync the latest lookup tables.\n\n"
            f"{age_msg}\n\n"
            f"Proceeding with outdated tables could result in incorrect metadata.\n\n"
            f"Proceed with cached lookup tables anyway?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )

        if reply == QMessageBox.No:
            sys.exit(0)

    def load_config(self):
        """Load saved configuration"""
        try:
            if self.config_file.exists():
                with open(self.config_file, 'r') as f:
                    config = json.load(f)
                    if 'staging_root' in config:
                        self.staging_root = Path(config['staging_root'])
        except:
            pass
    
    def save_config(self):
        """Save configuration"""
        try:
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_file, 'w') as f:
                json.dump({'staging_root': str(self.staging_root)}, f)
        except:
            pass

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------

    def save_session(self):
        """Persist current app state to session.json in the deployment folder."""
        try:
            if self.current_deployment_folder is None:
                return

            device_statuses = {}
            for i in range(self.device_tree.topLevelItemCount()):
                item = self.device_tree.topLevelItem(i)
                if i < len(self.devices):
                    device_label = self.devices[i][3]
                    device_statuses[device_label] = {
                        "status": item.text(2),
                        "files_copied": item.text(3),
                    }

            session = {
                "schema_version": 1,
                "saved_at": datetime.now().isoformat(),
                "metadata": self.metadata,
                "devices": self.devices,
                "device_statuses": device_statuses,
                "file_inventory": self.file_inventory,
                "deployment_folder": str(self.current_deployment_folder),
            }

            session_path = self.current_deployment_folder / "session.json"
            with open(session_path, "w") as f:
                json.dump(session, f, indent=2, default=str)
        except Exception:
            pass  # never crash the app on a failed save

    def find_all_sessions(self) -> list:
        """
        Scan staging_root for all session.json files.
        Returns a list of dicts: {path, data, status, saved_at, error_msg}
          status is 'ok' or 'corrupted'.
        Sorted newest first (by saved_at for parseable, by file mtime for corrupted).
        """
        sessions = []
        if not self.staging_root.exists():
            return sessions
        try:
            subdirs = [d for d in self.staging_root.iterdir() if d.is_dir()]
        except Exception:
            return sessions

        for subdir in subdirs:
            session_file = subdir / "session.json"
            if not session_file.exists():
                continue
            try:
                with open(session_file, "r") as f:
                    data = json.load(f)
                if data.get("schema_version") != 1:
                    continue
                folder = Path(data.get("deployment_folder", ""))
                if not folder.exists():
                    continue
                saved_at = datetime.fromisoformat(data.get("saved_at", "2000-01-01"))
                sessions.append({"path": session_file, "data": data, "status": "ok",
                                 "saved_at": saved_at, "error_msg": ""})
            except json.JSONDecodeError as e:
                mtime = datetime.fromtimestamp(session_file.stat().st_mtime)
                sessions.append({"path": session_file, "data": None, "status": "corrupted",
                                 "saved_at": mtime, "error_msg": str(e)})
                # Record the corruption in that deployment's qc_report.json so the audit
                # trail captures it even if the user never opens the deployment again.
                try:
                    append_qc_report(session_file.parent, "session_health", "", "error",
                                     f"session.json could not be parsed: {e}")
                except Exception:
                    pass
            except Exception:
                continue

        sessions.sort(key=lambda x: x["saved_at"], reverse=True)
        return sessions

    def offer_resume_session(self, sessions: list):
        """Show a selection dialog listing all deployments in staging. Lets user open any of them
        — whether to resume in-progress work or to inspect/verify a completed deployment."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Open Deployment")
        dlg.setMinimumWidth(560)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(
            "Select a deployment to open. You can resume in-progress sessions or reopen\n"
            "completed deployments to run integrity checks (Verify Box Upload, etc.)."
        ))

        list_widget = QListWidget()
        for s in sessions:
            if s["status"] == "ok":
                meta = s["data"].get("metadata", {})
                statuses = s["data"].get("device_statuses", {})
                total = len(statuses)
                completed = sum(1 for v in statuses.values() if v.get("status") in ("Complete", "Skipped"))
                reserve = meta.get("reserve_name", "Unknown Reserve")
                start = meta.get("deployment_start", "?")
                end = meta.get("deployment_end", "?")
                label = f"✓  {reserve}  |  {start} → {end}  |  {completed}/{total} devices done"
            else:
                folder_name = s["path"].parent.name
                label = f"⚠  CORRUPTED — {folder_name}  (truncated file, may be repairable)"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, s)
            list_widget.addItem(item)

        list_widget.setCurrentRow(0)
        layout.addWidget(list_widget)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Open Selected")
        buttons.button(QDialogButtonBox.Cancel).setText("Start New")
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        if dlg.exec() != QDialog.Accepted:
            return

        selected = list_widget.currentItem()
        if not selected:
            return
        chosen = selected.data(Qt.UserRole)

        if chosen["status"] == "corrupted":
            QMessageBox.warning(self, "Corrupted Session",
                f"The session file at:\n{chosen['path']}\n\ncould not be parsed "
                f"({chosen['error_msg']}).\n\n"
                "You can try opening it in a text editor to manually repair the JSON, "
                "or discard it and start a new session.")
            return

        self.restore_session(chosen["data"])

    def restore_session(self, session_data):
        """Restore app state from a saved session dict."""
        self.metadata = session_data.get("metadata", {})
        self.devices = [tuple(d) for d in session_data.get("devices", [])]
        self.file_inventory = session_data.get("file_inventory", [])
        self.seen_file_hashes = {e['file_hash_sha256'] for e in self.file_inventory if e.get('file_hash_sha256')}
        self.current_deployment_folder = Path(session_data["deployment_folder"])

        # Migrate any pre-existing QC sidecars from the deployment root into qc/.
        # No-op for fresh sessions or already-migrated deployments.
        migrate_qc_sidecars(self.current_deployment_folder)

        # Restore Tab 0 UI widgets
        idx = self.org_combo.findText(self.metadata.get("organization", ""))
        if idx >= 0:
            self.org_combo.setCurrentIndex(idx)

        idx = self.reserve_combo.findText(self.metadata.get("reserve_name", ""))
        if idx >= 0:
            self.reserve_combo.setCurrentIndex(idx)

        self.site_code_edit.setText(self.metadata.get("site", ""))

        from PySide6.QtCore import QDate
        start = QDate.fromString(self.metadata.get("deployment_start", ""), "yyyy-MM-dd")
        if start.isValid():
            self.deploy_start_date.setDate(start)
        end = QDate.fromString(self.metadata.get("deployment_end", ""), "yyyy-MM-dd")
        if end.isValid():
            self.deploy_end_date.setDate(end)

        observer = self.metadata.get("observer", "")
        idx = self.observer_combo.findText(observer)
        if idx >= 0:
            self.observer_combo.setCurrentIndex(idx)
            self.observer_other_edit.hide()
        else:
            other_idx = self.observer_combo.findText("Other")
            if other_idx >= 0:
                self.observer_combo.setCurrentIndex(other_idx)
            self.observer_other_edit.setText(observer)
            self.observer_other_edit.show()

        self.clear_all_devices()
        for plot_num, _plot_label, dev_code, _dev_label in self.devices:
            if plot_num in self.device_checkboxes and dev_code in self.device_checkboxes[plot_num]:
                self.device_checkboxes[plot_num][dev_code].setChecked(True)

        # Rebuild collection tree and restore per-device statuses
        self.populate_collection_list()
        statuses = session_data.get("device_statuses", {})
        for i in range(self.device_tree.topLevelItemCount()):
            if i < len(self.devices):
                device_label = self.devices[i][3]
                if device_label in statuses:
                    self.device_tree.topLevelItem(i).setText(2, statuses[device_label].get("status", "Pending"))
                    self.device_tree.topLevelItem(i).setText(3, str(statuses[device_label].get("files_copied", "0")))

        self.tabs.setCurrentIndex(1)
        self.log(f"Resumed session: {len(self.file_inventory)} files already in inventory.")
    
    def init_ui(self):
        """Initialize the user interface"""
        # Set window icon
        icon_path = _BUNDLE_DIR / "assets" / "cassn_icon.png"
        if icon_path.exists():
            from PySide6.QtGui import QIcon
            self.setWindowIcon(QIcon(str(icon_path)))
        
        # Central widget and main layout
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        
        # Logo banner at top - logo + title
        banner_layout = QHBoxLayout()
        banner_layout.setContentsMargins(0, 0, 0, 0)
        banner_layout.addStretch()

        # CA-SSN Logo
        cassn_logo_path = _BUNDLE_DIR / "assets" / "cassn_icon.png"
        if cassn_logo_path.exists():
            cassn_label = QLabel()
            cassn_pixmap = QPixmap(str(cassn_logo_path))
            cassn_scaled = cassn_pixmap.scaledToHeight(140, Qt.SmoothTransformation)
            cassn_label.setPixmap(cassn_scaled)
            cassn_label.setAlignment(Qt.AlignCenter)
            banner_layout.addWidget(cassn_label)

        banner_layout.addSpacing(10)

        # App title
        title_label = QLabel("CA-SSN Field Data Manager")
        title_font = QFont("Arial", 24, QFont.Bold)
        title_label.setFont(title_font)
        title_label.setAlignment(Qt.AlignCenter)
        banner_layout.addWidget(title_label)

        banner_layout.addStretch()
        main_layout.addLayout(banner_layout)
        main_layout.addSpacing(10)
        
        # Tab widget
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)
        
        # Create tabs
        self.create_metadata_tab()
        self.create_collection_tab()
        self.create_review_tab()
    
    def create_metadata_tab(self):
        """Create the deployment metadata entry tab"""
        tab = QWidget()
        self.tabs.addTab(tab, "1. Deployment Metadata")
        
        # Scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_widget = QWidget()
        layout = QVBoxLayout(scroll_widget)
        
        # Title
        title = QLabel("Deployment Metadata")
        title.setFont(QFont("Arial", 14, QFont.Bold))
        layout.addWidget(title)
        
        # Form layout for metadata fields
        form_group = QGroupBox("Deployment Information")
        form_layout = QFormLayout()
        
        # Organization
        self.org_combo = QComboBox()
        self.org_combo.addItems(ORGANIZATIONS)
        form_layout.addRow("Organization:", self.org_combo)
        
        # Site (with autocomplete)
        self.reserve_combo = QComboBox()
        self.reserve_combo.setEditable(True)
        reserve_names = [name for code, name in RESERVES]
        self.reserve_combo.addItems(reserve_names)
        
        completer = QCompleter(reserve_names)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        self.reserve_combo.setCompleter(completer)
        self.reserve_combo.currentTextChanged.connect(self.on_reserve_changed)
        form_layout.addRow("Site:", self.reserve_combo)
        
        # Site code
        self.site_code_edit = QLineEdit()
        self.site_code_edit.setReadOnly(True)
        form_layout.addRow("Site Code:", self.site_code_edit)
        
        # Deployment dates
        self.deploy_start_date = QDateEdit()
        self.deploy_start_date.setCalendarPopup(True)
        self.deploy_start_date.setDate(QDate.currentDate())
        form_layout.addRow("Deployment Start Date:", self.deploy_start_date)
        
        self.deploy_end_date = QDateEdit()
        self.deploy_end_date.setCalendarPopup(True)
        self.deploy_end_date.setDate(QDate.currentDate())
        form_layout.addRow("Deployment End Date:", self.deploy_end_date)
        
        # Observer/Downloader
        self.observer_combo = QComboBox()
        self.observer_combo.setEditable(True)
        self.observer_combo.addItems(DOWNLOADERS)
        self.observer_combo.currentTextChanged.connect(self.on_observer_changed)
        form_layout.addRow("Who is downloading data?", self.observer_combo)
        
        # Other observer entry
        self.observer_other_edit = QLineEdit()
        self.observer_other_edit.setPlaceholderText("Enter name...")
        self.observer_other_edit.hide()
        form_layout.addRow("", self.observer_other_edit)
        
        form_group.setLayout(form_layout)
        layout.addWidget(form_group)
        
        # Device selection
        device_group = QGroupBox("Select Devices for Each Plot")
        device_layout = QVBoxLayout()
        
        instructions = QLabel("Check which devices you are downloading data from.")
        device_layout.addWidget(instructions)
        
        # Device grid — header row only here; plot rows are built/rebuilt
        # dynamically in _rebuild_plot_grid() based on the chosen reserve.
        self.grid_layout = QGridLayout()

        self.grid_layout.addWidget(QLabel("Plot"), 0, 0)
        col = 1
        for dev_code, dev_name in DEVICE_TYPES.items():
            label = QLabel(dev_name)
            label.setWordWrap(True)
            self.grid_layout.addWidget(label, 0, col)
            col += 1

        self.device_checkboxes = {}
        self.plot_labels = {}

        # Seed with a generic 4-plot grid; replaced on first reserve selection.
        self._rebuild_plot_grid(reserve_code=None)

        device_layout.addLayout(self.grid_layout)
        
        # Quick select buttons
        button_layout = QHBoxLayout()
        select_all_btn = QPushButton("Select All Devices")
        select_all_btn.clicked.connect(self.select_all_devices)
        clear_all_btn = QPushButton("Clear All Devices")
        clear_all_btn.clicked.connect(self.clear_all_devices)
        button_layout.addWidget(select_all_btn)
        button_layout.addWidget(clear_all_btn)
        button_layout.addStretch()
        device_layout.addLayout(button_layout)
        
        device_group.setLayout(device_layout)
        layout.addWidget(device_group)
        
        # Staging location
        staging_group = QGroupBox("Local Staging Location")
        staging_layout = QHBoxLayout()
        
        self.staging_label = QLineEdit(str(self.staging_root))
        self.staging_label.setReadOnly(True)
        staging_layout.addWidget(self.staging_label)
        
        change_btn = QPushButton("Change...")
        change_btn.clicked.connect(self.choose_staging_location)
        staging_layout.addWidget(change_btn)
        
        self.set_default_cb = QCheckBox("Set as default")
        staging_layout.addWidget(self.set_default_cb)
        
        staging_group.setLayout(staging_layout)
        layout.addWidget(staging_group)
        
        # Box upload option
        box_group = QGroupBox("Box Cloud Storage")
        box_layout = QVBoxLayout()
        
        # Box connection status indicator
        box_status_layout = QHBoxLayout()
        box_status_label = QLabel()
        if self.box_authenticated:
            box_status_label.setText("✓ Box Connected")
            box_status_label.setStyleSheet("color: green; font-weight: bold;")
        else:
            box_status_label.setText("⚠ Box Not Connected")
            box_status_label.setStyleSheet("color: orange; font-weight: bold;")
        box_status_layout.addWidget(box_status_label)
        box_status_layout.addStretch()
        box_layout.addLayout(box_status_layout)
        
        self.upload_to_box_cb = QCheckBox("Upload to Box after processing")
        self.upload_to_box_cb.setChecked(self.box_authenticated)
        self.upload_to_box_cb.setEnabled(self.box_authenticated)
        box_layout.addWidget(self.upload_to_box_cb)
        
        if not self.box_authenticated:
            auth_note = QLabel("⚠ Box not connected. Run box_auth_setup.py to authenticate.")
            auth_note.setStyleSheet("color: orange;")
            box_layout.addWidget(auth_note)
        
        box_group.setLayout(box_layout)
        layout.addWidget(box_group)
        
        # Navigation button
        nav_layout = QHBoxLayout()
        nav_layout.addStretch()
        next_btn = QPushButton("Next: Collect SD Card Data →")
        next_btn.clicked.connect(self.validate_and_next)
        nav_layout.addWidget(next_btn)
        layout.addLayout(nav_layout)
        
        layout.addStretch()
        
        scroll.setWidget(scroll_widget)
        
        tab_layout = QVBoxLayout(tab)
        tab_layout.addWidget(scroll)
    
    def create_collection_tab(self):
        """Create the SD card data collection tab"""
        tab = QWidget()
        self.tabs.addTab(tab, "2. Collect SD Card Data")
        
        layout = QVBoxLayout(tab)
        
        # Title
        title = QLabel("SD Card Data Collection")
        title.setFont(QFont("Arial", 14, QFont.Bold))
        layout.addWidget(title)
        
        instructions = QLabel(
            "Insert SD card for each device, select it below, and click 'Copy Files'.\n"
            "Files will be automatically renamed and organized in the staging folder."
        )
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        
        # Device tree
        device_group = QGroupBox("Devices")
        device_layout = QVBoxLayout()
        
        self.device_tree = QTreeWidget()
        self.device_tree.setHeaderLabels(["Plot", "Device Type", "Status", "Files Copied"])
        self.device_tree.setColumnWidth(0, 150)
        self.device_tree.setColumnWidth(1, 200)
        self.device_tree.setColumnWidth(2, 100)
        device_layout.addWidget(self.device_tree)
        
        device_group.setLayout(device_layout)
        layout.addWidget(device_group)
        
        # Control buttons
        control_layout = QHBoxLayout()
        copy_btn = QPushButton("Select SD Card && Copy Files")
        copy_btn.clicked.connect(self.copy_sd_card_data)
        skip_btn = QPushButton("Skip Selected Device")
        skip_btn.clicked.connect(self.skip_device)
        add_device_btn = QPushButton("Add Device…")
        add_device_btn.clicked.connect(self.add_device)
        add_device_btn.setToolTip("Add a new device (plot + type) to this deployment, e.g. a 5th plot or a missed device.")
        control_layout.addWidget(copy_btn)
        control_layout.addWidget(skip_btn)
        control_layout.addWidget(add_device_btn)
        control_layout.addStretch()
        layout.addLayout(control_layout)
        
        # Progress log
        log_group = QGroupBox("Progress Log")
        log_layout = QVBoxLayout()
        
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        log_layout.addWidget(self.log_text)
        
        log_group.setLayout(log_layout)
        layout.addWidget(log_group)
        
        # Navigation
        nav_layout = QHBoxLayout()
        back_btn = QPushButton("← Back")
        back_btn.clicked.connect(lambda: self.tabs.setCurrentIndex(0))
        next_btn = QPushButton("Next: Review && Finalize →")
        next_btn.clicked.connect(self.validate_and_next_collection)
        nav_layout.addWidget(back_btn)
        nav_layout.addStretch()
        nav_layout.addWidget(next_btn)
        layout.addLayout(nav_layout)
    
    def create_review_tab(self):
        """Create the review and finalize tab"""
        tab = QWidget()
        self.tabs.addTab(tab, "3. Review & Finalize")
        
        layout = QVBoxLayout(tab)
        
        # Title
        title = QLabel("Deployment Summary")
        title.setFont(QFont("Arial", 14, QFont.Bold))
        layout.addWidget(title)
        
        # Summary text
        summary_group = QGroupBox("Summary")
        summary_layout = QVBoxLayout()
        
        self.summary_text = QTextEdit()
        self.summary_text.setReadOnly(True)
        summary_layout.addWidget(self.summary_text)
        
        summary_group.setLayout(summary_layout)
        layout.addWidget(summary_group)
        
        # Upload progress (hidden by default)
        self.upload_group = QGroupBox("Box Upload Progress")
        upload_layout = QVBoxLayout()
        
        self.upload_progress_bar = QProgressBar()
        self.upload_progress_bar.setMinimum(0)
        self.upload_progress_bar.setMaximum(100)
        upload_layout.addWidget(self.upload_progress_bar)
        
        self.upload_status_label = QLabel("")
        upload_layout.addWidget(self.upload_status_label)

        # Cancel button (shown only while an upload is running)
        self.cancel_upload_btn = QPushButton("Cancel Upload")
        self.cancel_upload_btn.clicked.connect(self.cancel_upload)
        self.cancel_upload_btn.setToolTip(
            "Stop after the in-flight files finish. Already-uploaded files stay on Box; "
            "re-running the upload resumes from where it stopped."
        )
        self.cancel_upload_btn.hide()
        upload_layout.addWidget(self.cancel_upload_btn)

        self.upload_group.setLayout(upload_layout)
        self.upload_group.hide()
        layout.addWidget(self.upload_group)
        
        # Action buttons
        button_layout = QHBoxLayout()
        
        self.open_btn = QPushButton("Open Staging Folder")
        self.open_btn.clicked.connect(self.open_staging_folder)

        self.switch_deployment_btn = QPushButton("Open Different Deployment…")
        self.switch_deployment_btn.clicked.connect(self.open_different_deployment)
        self.switch_deployment_btn.setToolTip("Switch to a different deployment in staging — useful for re-verifying past uploads.")

        self.upload_now_btn = QPushButton("Upload to Box Now")
        self.upload_now_btn.clicked.connect(self.upload_to_box_manual)
        self.upload_now_btn.setEnabled(self.box_authenticated)

        self.orphan_scan_btn = QPushButton("Compare Inventory ↔ Disk")
        self.orphan_scan_btn.clicked.connect(self.run_orphan_scan)
        self.orphan_scan_btn.setToolTip("Fast check: list files in inventory missing from disk and files on disk missing from inventory. No hashing.")

        self.fixity_btn = QPushButton("Verify File Hashes")
        self.fixity_btn.clicked.connect(self.run_fixity_check)
        self.fixity_btn.setToolTip("End-to-end verification: compute SHA-1 of each local file and compare against the SHA-1 Box reports for the corresponding file on Box. Proves the bytes on Box are byte-identical to the bytes locally. Slow on large datasets — reads every file.")

        self.box_verify_btn = QPushButton("Verify Box Upload")
        self.box_verify_btn.clicked.connect(self.run_box_verify)
        self.box_verify_btn.setToolTip("List the deployment folder on Box and reconcile against local inventory. Reports missing or unexpected files.")
        self.box_verify_btn.setEnabled(self.box_authenticated)

        self.new_btn = QPushButton("Start New Deployment")
        self.new_btn.clicked.connect(self.start_new_deployment)

        exit_btn = QPushButton("Exit")
        exit_btn.clicked.connect(self.close)

        button_layout.addWidget(self.open_btn)
        button_layout.addWidget(self.switch_deployment_btn)
        button_layout.addWidget(self.upload_now_btn)
        button_layout.addWidget(self.orphan_scan_btn)
        button_layout.addWidget(self.fixity_btn)
        button_layout.addWidget(self.box_verify_btn)
        button_layout.addWidget(self.new_btn)
        button_layout.addStretch()
        button_layout.addWidget(exit_btn)
        layout.addLayout(button_layout)
    
    # =========================================================================
    # Event Handlers
    # =========================================================================
    
    def on_reserve_changed(self, text):
        """Update site code and plot names when reserve changes"""
        for code, name in RESERVES:
            if name == text:
                self.site_code_edit.setText(code)
                self.update_plot_labels(code)
                return
        self.site_code_edit.setText("")
    
    def on_observer_changed(self, text):
        """Show/hide other observer entry"""
        if text == "Other":
            self.observer_other_edit.show()
        else:
            self.observer_other_edit.hide()
    
    def update_plot_labels(self, reserve_code):
        """Rebuild the plot/device grid for the chosen reserve."""
        self._rebuild_plot_grid(reserve_code)

    def _rebuild_plot_grid(self, reserve_code):
        """Tear down existing plot rows and rebuild them based on plots.csv for the
        selected reserve. Reserves can have any number of plots (4, 5, etc.); the
        grid sizes itself to the data."""
        # Tear down existing plot rows (everything below row 0, which is the header)
        for plot_label in self.plot_labels.values():
            plot_label.setParent(None)
            plot_label.deleteLater()
        for cb_row in self.device_checkboxes.values():
            for cb in cb_row.values():
                cb.setParent(None)
                cb.deleteLater()
        self.plot_labels = {}
        self.device_checkboxes = {}

        # Determine plot numbers to show
        plot_names_for_reserve = PLOT_NAMES.get(reserve_code, {}) if reserve_code else {}
        if plot_names_for_reserve:
            plot_numbers = sorted(plot_names_for_reserve.keys())
        else:
            # Fallback: generic 1–4 grid for unknown reserves or initial seed
            plot_numbers = [1, 2, 3, 4]

        # Build a row per plot. row_idx maps to grid row (header is row 0).
        for row_idx, plot_num in enumerate(plot_numbers, start=1):
            name = plot_names_for_reserve.get(plot_num, "")
            label_text = f"Plot {plot_num}: {name}" if name else f"Plot {plot_num}"
            plot_label = QLabel(label_text)
            self.plot_labels[plot_num] = plot_label
            self.grid_layout.addWidget(plot_label, row_idx, 0)

            self.device_checkboxes[plot_num] = {}
            col = 1
            for dev_code in DEVICE_TYPES.keys():
                cb = QCheckBox()
                self.device_checkboxes[plot_num][dev_code] = cb
                self.grid_layout.addWidget(cb, row_idx, col, Qt.AlignCenter)
                col += 1

    def select_all_devices(self):
        """Select all device checkboxes"""
        for plot_num in self.device_checkboxes:
            for dev_code in DEVICE_TYPES.keys():
                self.device_checkboxes[plot_num][dev_code].setChecked(True)

    def clear_all_devices(self):
        """Clear all device checkboxes"""
        for plot_num in self.device_checkboxes:
            for dev_code in DEVICE_TYPES.keys():
                self.device_checkboxes[plot_num][dev_code].setChecked(False)
    
    def choose_staging_location(self):
        """Choose staging directory"""
        directory = QFileDialog.getExistingDirectory(
            self, "Choose Staging Location", str(self.staging_root)
        )
        if directory:
            self.staging_root = Path(directory)
            self.staging_label.setText(str(self.staging_root))
    
    def validate_and_next(self):
        """Validate metadata and proceed to collection tab"""
        # Validation
        if not self.reserve_combo.currentText():
            QMessageBox.warning(self, "Missing Information", "Please select a site.")
            return
        
        if not self.site_code_edit.text():
            QMessageBox.warning(self, "Missing Information", "Please select a valid site.")
            return
        
        observer = self.observer_combo.currentText()
        if not observer:
            QMessageBox.warning(self, "Missing Information", "Please select who is downloading data.")
            return
        
        if observer == "Other" and not self.observer_other_edit.text().strip():
            QMessageBox.warning(self, "Missing Information", "Please enter a name for 'Other' option.")
            return
        
        # Store metadata
        reserve_name = self.reserve_combo.currentText()
        reserve_code = self.site_code_edit.text()
        
        self.metadata = {
            'organization': self.org_combo.currentText(),
            'reserve_name': reserve_name,
            'site': reserve_code,
            'deployment_start': self.deploy_start_date.date().toString("yyyy-MM-dd"),
            'deployment_end': self.deploy_end_date.date().toString("yyyy-MM-dd"),
            'observer': self.observer_other_edit.text() if observer == "Other" else observer,
        }
        
        # Build device list. Iterate over the plot numbers actually shown in the
        # grid (which were sized from plots.csv when the reserve was selected),
        # so a 5-plot reserve produces 5 plot entries and a 4-plot reserve produces 4.
        plot_names = PLOT_NAMES.get(reserve_code, {}) or {}

        self.devices = []
        for plot_num in sorted(self.device_checkboxes.keys()):
            plot_label = plot_names.get(plot_num, str(plot_num))
            
            for dev_code in DEVICE_TYPES.keys():
                if self.device_checkboxes[plot_num][dev_code].isChecked():
                    device_label = f"p{plot_num}_{dev_code}"
                    self.devices.append((plot_num, plot_label, dev_code, device_label))
        
        if not self.devices:
            QMessageBox.warning(self, "No Devices Selected", "Please select at least one device.")
            return
        
        # Save config if requested
        if self.set_default_cb.isChecked():
            self.save_config()
        
        # Create deployment folder
        self.create_deployment_folder()
        
        # Populate collection list
        self.populate_collection_list()
        
        # Go to collection tab
        self.tabs.setCurrentIndex(1)
    
    def create_deployment_folder(self):
        """Create deployment folder in staging location based on deployment end date"""
        folder_name = f"{self.metadata['organization']}_{self.metadata['site']}_{self.metadata['deployment_end'].replace('-', '')}"

        self.current_deployment_folder = self.staging_root / folder_name
        self.current_deployment_folder.mkdir(parents=True, exist_ok=True)
        
        (self.current_deployment_folder / "raw_data").mkdir(exist_ok=True)
        
        self.save_session()

    def populate_collection_list(self):
        """Populate device tree with selected devices"""
        self.device_tree.clear()
        
        for plot_num, plot_label, dev_code, device_label in self.devices:
            item = QTreeWidgetItem([
                f"Plot {plot_num} ({plot_label})",
                DEVICE_TYPES[dev_code],
                "Pending",
                "0"
            ])
            self.device_tree.addTopLevelItem(item)
    
    def log(self, message):
        """Add message to log. QC warnings/errors are colored red so they stand out."""
        from PySide6.QtGui import QColor
        timestamp = datetime.now().strftime("%H:%M:%S")
        full = f"[{timestamp}] {message}"
        # Detect QC findings, errors, and warnings (use red); everything else is default
        stripped = message.lstrip()
        is_alert = (
            "QC [" in message
            or stripped.startswith(("ERROR:", "✗", "⚠", "Warning:", "WARNING:"))
        )
        if is_alert:
            self.log_text.setTextColor(QColor("#cc0000"))
        else:
            self.log_text.setTextColor(QColor("#000000"))
        self.log_text.append(full)
        # Restore default color for any subsequent direct writes to the widget
        self.log_text.setTextColor(QColor("#000000"))
        QApplication.processEvents()
    
    def copy_sd_card_data(self):
        """Copy data from SD card for selected device"""
        selected = self.device_tree.currentItem()
        if not selected:
            QMessageBox.information(self, "No Selection", "Please select a device from the list.")
            return
        
        index = self.device_tree.indexOfTopLevelItem(selected)
        plot_num, plot_label, dev_code, device_label = self.devices[index]
        
        # Check if already complete
        if selected.text(2) == "Complete":
            reply = QMessageBox.question(
                self, "Already Complete",
                "This device is already complete. Copy again?",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.No:
                return
        
        # Select SD card
        sd_path = QFileDialog.getExistingDirectory(
            self, f"Select SD Card for Plot {plot_num} - {DEVICE_TYPES[dev_code]}"
        )

        if not sd_path:
            return

        # Auto-count valid media files in source (filters out .DS_Store, hidden files,
        # and resource forks). Used silently as expected_file_count for post-copy
        # comparison — catches files that were skipped during copy (hash mismatch,
        # duplicate, etc.). No user prompt.
        media_exts = {".jpg", ".jpeg"} if dev_code not in ("BD", "BT") else {".wav", ".txt"}
        try:
            expected_file_count = sum(
                1 for f in Path(sd_path).rglob("*")
                if f.is_file()
                and not f.name.startswith(".")
                and not f.name.startswith("_")
                and f.suffix.lower() in media_exts
            )
        except Exception:
            expected_file_count = None
        if expected_file_count is not None:
            self.log(f"  Auto-counted {expected_file_count} media file(s) in source folder.")

        self.log(f"Starting copy for Plot {plot_num} ({plot_label}) - {DEVICE_TYPES[dev_code]}...")
        self.log(f"Source: {sd_path}")
        
        # Create device folder
        device_folder = self.current_deployment_folder / "raw_data" / device_label
        device_folder.mkdir(parents=True, exist_ok=True)
        
        try:
            files_copied = self.process_sd_card_files(
                Path(sd_path), device_folder, plot_num, plot_label, dev_code, device_label
            )
            
            selected.setText(2, "Complete")
            device_entries = [e for e in self.file_inventory if e['device_label'] == device_label]
            total_for_device = len(device_entries)
            selected.setText(3, str(total_for_device))
            self.save_session()

            write_device_manifest(device_label, device_folder, device_entries)
            try:
                self._write_metadata_csvs()
            except Exception:
                pass  # never block device completion on a CSV write error

            if expected_file_count is not None:
                # Compare against total inventoried files for this device, not files
                # copied in this run alone — otherwise resume runs falsely warn because
                # already-copied files are skipped and don't count toward files_copied.
                if total_for_device != expected_file_count:
                    self.log(f"  ⚠ File count mismatch: expected {expected_file_count}, inventory has {total_for_device}. Check SD card!")
                    append_qc_report(self.current_deployment_folder, "expected_file_count", device_label, "warning",
                                     f"Expected {expected_file_count}, inventory has {total_for_device}")
                    QMessageBox.warning(self, "File Count Mismatch",
                        f"Expected {expected_file_count} files in source but device inventory has {total_for_device}.\n\n"
                        f"Please verify the SD card for missing files before ejecting.")
                else:
                    append_qc_report(self.current_deployment_folder, "expected_file_count", device_label, "pass",
                                     f"Expected {expected_file_count}, inventory has {total_for_device}")

            # Run QC checks and log findings to qc_report.json
            self._run_device_qc_checks(device_entries, device_label)

            self.log(f"✓ Completed! {total_for_device} files copied. Manifest written.\n")
            
        except Exception as e:
            self.log(f"✗ Error: {str(e)}\n")
            QMessageBox.critical(self, "Copy Error", f"Error copying files: {str(e)}")
    
    def process_sd_card_files(self, source_dir, dest_dir, plot_num, plot_label, dev_code, device_label):
        """Process files from SD card."""
        files_copied = 0
        small_file_count = 0
        hash_mismatch_count = 0
        duplicate_count = 0
        small_file_names: list[str] = []
        hash_mismatch_names: list[str] = []
        duplicate_names: list[str] = []

        # Resume support: skip files already in inventory for this device
        already_copied = {
            entry['original_filename']
            for entry in self.file_inventory
            if entry['device_label'] == device_label
        }
        prior_media_count = sum(
            1 for entry in self.file_inventory
            if entry['device_label'] == device_label and entry['file_type'] != 'config'
        )
        file_sequence = prior_media_count + 1

        # Event counter for sequence-aware camera naming (increments per trigger, not per photo)
        prior_event_nums = [
            entry['sequence_event_num'] for entry in self.file_inventory
            if entry['device_label'] == device_label
            and entry.get('sequence_event_num') not in (None, '')
        ]
        event_sequence = (max(prior_event_nums) + 1) if prior_event_nums else 1
        current_event_num = None

        if already_copied:
            self.log(f"  {len(already_copied)} files already copied for {device_label}, resuming...")

        # Deployment end date for media filenames (YYYYMMDD)
        deploy_date = datetime.strptime(self.metadata['deployment_end'], "%Y-%m-%d")
        date_str = deploy_date.strftime("%Y%m%d")

        # Deployment start date for config filenames (YYYYMMDD)
        deploy_start = datetime.strptime(self.metadata['deployment_start'], "%Y-%m-%d")
        start_date_str = deploy_start.strftime("%Y%m%d")

        org = self.metadata['organization']
        site = self.metadata['site']
        plot_metadata = PLOT_METADATA.get((site, plot_num), {})

        # Resolve physical device identifier
        audio_device_types = {'BD', 'BT'}
        if dev_code in audio_device_types:
            device_id = parse_audiomoth_device_id(source_dir)
            if not device_id:
                self.log(f"  Warning: could not find AudioMoth Device ID in {source_dir}")
        else:
            cam_key = (site, int(plot_num) if str(plot_num).isdigit() else plot_num, dev_code)
            cam_meta = self.wi_camera_metadata.get(cam_key, {})
            device_id = cam_meta.get('camera_id', '')
            if not device_id:
                self.log(f"  Warning: camera_id missing for {site} plot {plot_num} {dev_code} — update cameras.csv")

        # CONFIG.TXT — parse once per device folder (audio only; fast, critical for schedule fields)
        config_data = {}
        if dev_code in audio_device_types:
            config_files = sorted(
                f for pattern in ("*CONFIG*.txt", "*CONFIG*.TXT")
                for f in Path(source_dir).glob(pattern)
            )
            if config_files:
                config_data = parse_audiomoth_config_file(config_files[0])

        # Sort files alphabetically within each directory so RECONYX RCNX*.JPG
        # bursts arrive in capture order (frame 1, 2, 3). os.walk returns
        # files in filesystem-dependent order, which scrambles event numbering.
        for root, dirs, files in os.walk(source_dir):
            files.sort()
            for filename in files:
                if filename.startswith('.') or filename.startswith('_'):
                    continue

                source_path = Path(root) / filename
                file_ext = source_path.suffix.lower()
                file_type = classify_file(filename)

                if file_type == "other":
                    continue

                # Skip files already successfully copied in a prior session
                if filename in already_copied:
                    continue

                # For images: read EXIF and sequence from source before naming
                exif_data = {}
                trigger_type, seq_pos, seq_total = None, None, None
                if file_type == "image" and EXIF_AVAILABLE:
                    exif_data, raw_exif = extract_exif_data(source_path)
                    trigger_type, seq_pos, seq_total = extract_reconyx_sequence(raw_exif)

                if file_type == "config":
                    # Rename: ORG_SITE_plotN_DEVCODE_YYYYMMDD_CONFIG_01.txt
                    # plotN must be in the name so each plot's CONFIG is uniquely identifiable
                    # (otherwise BD plots 1, 2, 3, 4 all collide on the same filename).
                    new_filename = f"{org}_{site}_plot{plot_num}_{dev_code}_{start_date_str}_CONFIG_01{file_ext}"
                    dest_path = dest_dir / new_filename
                elif file_type == "image" and seq_pos is not None:
                    # Sequence-aware naming: {EVENTNO:05d}_{POS}
                    if seq_pos == 1 or current_event_num is None:
                        current_event_num = event_sequence
                        event_sequence += 1
                    seq_str = f"{current_event_num:05d}_{seq_pos}"
                    new_filename = f"{org}_{site}_plot{plot_num}_{dev_code}_{date_str}_{seq_str}{file_ext}"
                    dest_path = dest_dir / new_filename
                    file_sequence += 1
                else:
                    # Audio or image without sequence data: standard sequential naming
                    seq_str = f"{file_sequence:05d}"
                    new_filename = f"{org}_{site}_plot{plot_num}_{dev_code}_{date_str}_{seq_str}{file_ext}"
                    dest_path = dest_dir / new_filename
                    file_sequence += 1

                # Remove any partial file left by a previously interrupted copy
                if dest_path.exists():
                    try:
                        dest_path.unlink()
                    except Exception as e:
                        self.log(f"  Warning: could not remove partial file {dest_path.name}: {e}")

                source_hash = compute_file_hash(source_path)
                shutil.copy2(source_path, dest_path)
                file_hash = compute_file_hash(dest_path)

                # Post-copy hash verification: destination must match source
                if file_hash != source_hash:
                    try:
                        dest_path.unlink()
                    except Exception:
                        pass
                    self.log(f"  ERROR: hash mismatch after copy — {filename} skipped (source hash {source_hash[:8]}… ≠ dest {file_hash[:8]}…)")
                    hash_mismatch_count += 1
                    hash_mismatch_names.append(filename)
                    append_qc_report(self.current_deployment_folder, "hash_verification", device_label, "error",
                                     f"Hash mismatch on {filename} (source ≠ dest)")
                    QMessageBox.critical(self, "Copy Verification Failed",
                        f"Hash mismatch detected for:\n{filename}\n\nThe destination file does not match the source. "
                        f"The file has been removed from staging. Check the SD card and retry.")
                    continue

                # File size floor: flag suspiciously small files
                dest_size = dest_path.stat().st_size
                if file_type in ("image", "audio") and dest_size < MIN_IMAGE_SIZE_BYTES:
                    self.log(f"  Warning: {new_filename} is only {dest_size} bytes — possible corrupted write")
                    small_file_count += 1
                    small_file_names.append(new_filename)

                # Recorded datetime: authoritative time from device
                if file_type == "image":
                    recorded_datetime = parse_camera_recorded_datetime(exif_data)
                elif file_type == "audio":
                    recorded_datetime = parse_audiomoth_recorded_datetime(filename)
                else:
                    recorded_datetime = ''

                # Reconyx extras via ExifTool (best-effort)
                reconyx_data = {}
                if file_type == "image":
                    reconyx_data = parse_reconyx_exiftool(source_path)

                # AudioMoth WAV comment (per-file, overrides CONFIG.TXT where redundant)
                wav_data = {}
                if file_type == "audio":
                    wav_data = parse_audiomoth_wav_comment(source_path)
                    # WAV comment is authoritative for device_id if found
                    if wav_data.get('device_id'):
                        device_id = wav_data['device_id']

                file_info = {
                    'original_filename': filename,
                    'new_filename': new_filename,
                    'plot_number': plot_num,
                    'plot_label': plot_label,
                    'device_type': dev_code,
                    'device_label': device_label,
                    'device_id': device_id,
                    'file_type': file_type,
                    'file_size_bytes': dest_path.stat().st_size,
                    'file_hash_sha256': file_hash,
                    'recorded_datetime': recorded_datetime,
                    'latitude': plot_metadata.get('plot_latitude') or '',
                    'longitude': plot_metadata.get('plot_longitude') or '',
                    'camera_make': exif_data.get('Make', ''),
                    'camera_model': exif_data.get('Model', ''),
                    'sequence_trigger_type': trigger_type or '',
                    'sequence_event_num': current_event_num if seq_pos is not None else '',
                    'sequence_position': seq_pos if seq_pos is not None else '',
                    'sequence_total': seq_total if seq_total is not None else '',
                    'source_path': str(source_path),
                    # AudioMoth fields (audio rows only; blank for images)
                    'ARU_make':    config_data.get('ARU_make', '') or wav_data.get('ARU_make', ''),
                    'ARU_model':   config_data.get('ARU_model', '') or wav_data.get('ARU_model', ''),
                    'sample_rate_hz': wav_data.get('sample_rate_hz', '') or config_data.get('sample_rate_hz', ''),
                    'gain':        wav_data.get('gain_setting', '') or config_data.get('gain_setting', ''),
                    'filter_type_khz': _hz_to_khz(wav_data.get('high_pass_filter_hz', '') or wav_data.get('low_pass_filter_hz', '') or config_data.get('high_pass_filter_hz', '') or config_data.get('low_pass_filter_hz', '')),
                    'battery_voltage': wav_data.get('battery_voltage', ''),
                    'temperature_c': wav_data.get('temperature_c', '') or reconyx_data.get('temperature_c', ''),
                    'date_installed': self.metadata.get('deployment_start', ''),
                    'deployment_start_time': config_data.get('deployment_start_time', '') or SOUNDHUB_CONFIG.get(f'deployment_start_time_{dev_code}', ''),
                    'deployment_end_time':   config_data.get('deployment_end_time', '')   or SOUNDHUB_CONFIG.get(f'deployment_end_time_{dev_code}', ''),
                    'frequency':   config_data.get('frequency', '') or SOUNDHUB_CONFIG.get('frequency', ''),
                    'duration':    config_data.get('duration', '')   or SOUNDHUB_CONFIG.get(f'duration_{dev_code}', ''),
                    'filter_type_duration':  wav_data.get('filter_type_duration', '')  or config_data.get('filter_type_duration', ''),
                    'filter_type_amplitude': wav_data.get('filter_type_amplitude', '') or config_data.get('filter_type_amplitude', ''),
                    # Reconyx extras (image rows only; blank for audio)
                    'moon_phase':          reconyx_data.get('moon_phase', ''),
                    'battery_voltage_avg': reconyx_data.get('battery_voltage_avg', ''),
                    'battery_type':        reconyx_data.get('battery_type', ''),
                }

                # Duplicate detection: skip files whose hash already exists in this session
                if file_hash in self.seen_file_hashes:
                    self.log(f"  Warning: duplicate file skipped — {new_filename} matches an already-inventoried file")
                    duplicate_count += 1
                    duplicate_names.append(new_filename)
                    append_qc_report(self.current_deployment_folder, "duplicate_detection", device_label, "warning",
                                     f"Duplicate hash: {new_filename} matches an already-inventoried file")
                    dest_path.unlink(missing_ok=True)
                    continue

                self.seen_file_hashes.add(file_hash)
                self.file_inventory.append(file_info)
                files_copied += 1

                if file_type == "audio":
                    size_mb = dest_path.stat().st_size / (1024 * 1024)
                    self.log(f"  [{files_copied}] {new_filename}  ({size_mb:.1f} MB)")
                elif files_copied % 10 == 0:
                    self.log(f"  ...{files_copied} files processed")

                if files_copied % 10 == 0:
                    self.save_session()

        if small_file_count:
            self.log(f"  Warning: {small_file_count} file(s) below {MIN_IMAGE_SIZE_BYTES // 1024} KB — possible corrupted writes. Review before proceeding.")

        # Per-file aggregate summaries for qc_report.json
        if files_copied > 0:
            append_qc_report(self.current_deployment_folder, "hash_verification", device_label, "pass",
                             f"{files_copied} file(s) copied; all source/dest hashes matched")
        if hash_mismatch_count == 0 and files_copied == 0:
            # No copy attempts logged — don't write a hash_verification entry
            pass
        if small_file_count > 0:
            append_qc_report(self.current_deployment_folder, "file_size_floor", device_label, "warning",
                             f"{small_file_count} file(s) under {MIN_IMAGE_SIZE_BYTES // 1024} KB threshold")
        else:
            append_qc_report(self.current_deployment_folder, "file_size_floor", device_label, "pass",
                             f"All {files_copied} file(s) above {MIN_IMAGE_SIZE_BYTES // 1024} KB threshold")
        if duplicate_count > 0:
            append_qc_report(self.current_deployment_folder, "duplicate_detection", device_label, "warning",
                             f"{duplicate_count} duplicate file(s) skipped")
        else:
            append_qc_report(self.current_deployment_folder, "duplicate_detection", device_label, "pass",
                             "No duplicate file hashes encountered")

        return files_copied
    
    def skip_device(self):
        """Skip selected device"""
        selected = self.device_tree.currentItem()
        if not selected:
            QMessageBox.information(self, "No Selection", "Please select a device from the list.")
            return

        index = self.device_tree.indexOfTopLevelItem(selected)
        plot_num, plot_label, dev_code, device_label = self.devices[index]

        selected.setText(2, "Skipped")
        selected.setText(3, "0")
        self.save_session()

        self.log(f"Skipped Plot {plot_num} - {DEVICE_TYPES[dev_code]}\n")

    def add_device(self):
        """Add a new device (plot + device type) to the current deployment.

        Useful when a deployment was originally configured with N plots and a new
        plot or device type needs to be added later (e.g., a 5th experimental plot,
        or a device that was missed during initial setup). The new device appears
        in the collection tree as Pending and can be processed normally.
        """
        if not self.metadata or not self.current_deployment_folder:
            QMessageBox.information(self, "No Deployment",
                "Start or open a deployment before adding a device.")
            return

        reserve_code = self.metadata.get('site', '')
        plot_names_for_reserve = PLOT_NAMES.get(reserve_code, {}) or {}

        # Build the dialog
        dlg = QDialog(self)
        dlg.setWindowTitle("Add Device to Deployment")
        dlg.setMinimumWidth(420)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(
            f"Add a new device to deployment <b>{self.current_deployment_folder.name}</b>.\n"
            "Select the plot and device type. The new device will appear in the\n"
            "collection list as Pending and can be processed normally."
        ))

        # Plot picker — show all known plots for this reserve, plus an option to type a new one
        plot_row = QHBoxLayout()
        plot_row.addWidget(QLabel("Plot:"))
        plot_combo = QComboBox()
        plot_combo.setEditable(True)
        if plot_names_for_reserve:
            for num in sorted(plot_names_for_reserve.keys()):
                plot_combo.addItem(f"{num} — {plot_names_for_reserve[num]}", userData=num)
        else:
            for num in (1, 2, 3, 4):
                plot_combo.addItem(str(num), userData=num)
        plot_row.addWidget(plot_combo, stretch=1)
        layout.addLayout(plot_row)

        # Device-type picker
        dev_row = QHBoxLayout()
        dev_row.addWidget(QLabel("Device type:"))
        dev_combo = QComboBox()
        for code, name in DEVICE_TYPES.items():
            dev_combo.addItem(f"{code} — {name}", userData=code)
        dev_row.addWidget(dev_combo, stretch=1)
        layout.addLayout(dev_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Add Device")
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        if dlg.exec() != QDialog.Accepted:
            return

        # Resolve plot number — accept either the combo's userData (known plot)
        # or whatever the user typed (free-form integer for a brand-new plot).
        plot_data = plot_combo.currentData()
        if plot_data is not None:
            try:
                plot_num = int(plot_data)
            except (TypeError, ValueError):
                plot_num = None
        else:
            plot_num = None
        if plot_num is None:
            text = plot_combo.currentText().strip().split()[0] if plot_combo.currentText().strip() else ""
            try:
                plot_num = int(text)
            except (TypeError, ValueError):
                QMessageBox.warning(self, "Invalid Plot",
                    f"'{plot_combo.currentText()}' is not a valid plot number. Enter an integer.")
                return
        if plot_num < 1:
            QMessageBox.warning(self, "Invalid Plot", "Plot number must be 1 or greater.")
            return

        dev_code = dev_combo.currentData()
        device_label = f"p{plot_num}_{dev_code}"

        # Reject duplicates
        if any(existing[3] == device_label for existing in self.devices):
            QMessageBox.warning(self, "Already Exists",
                f"Device {device_label} is already in this deployment.")
            return

        plot_label = plot_names_for_reserve.get(plot_num, str(plot_num))

        # Append to devices list and the device tree
        self.devices.append((plot_num, plot_label, dev_code, device_label))
        item = QTreeWidgetItem([
            f"Plot {plot_num} ({plot_label})",
            DEVICE_TYPES[dev_code],
            "Pending",
            "0",
        ])
        self.device_tree.addTopLevelItem(item)
        self.device_tree.setCurrentItem(item)

        self.save_session()
        self.log(f"Added device: Plot {plot_num} ({plot_label}) - {DEVICE_TYPES[dev_code]} → {device_label}\n")
    
    def validate_and_next_collection(self):
        """Validate collection and proceed to review"""
        pending = False
        for i in range(self.device_tree.topLevelItemCount()):
            item = self.device_tree.topLevelItem(i)
            if item.text(2) == "Pending":
                pending = True
                break
        
        if pending:
            reply = QMessageBox.question(
                self, "Incomplete Collection",
                "Some devices are still pending. Continue anyway?",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.No:
                return
        
        # Required field completeness check (per file type — image and audio have different required sets)
        completeness = check_field_completeness(self.file_inventory)
        low_fields = {f: pct for f, pct in completeness.items() if pct < 90.0}
        if low_fields:
            detail = "\n".join(f"  {f}: {pct:.0f}% populated" for f, pct in sorted(low_fields.items()))
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Field Completeness Warning")
            msg_box.setIcon(QMessageBox.Warning)
            msg_box.setText("Some required fields are below 90% populated. Review before generating CSVs.")
            msg_box.setDetailedText(detail)
            msg_box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
            msg_box.button(QMessageBox.Ok).setText("Continue Anyway")
            if msg_box.exec() == QMessageBox.Cancel:
                return
            for f, pct in low_fields.items():
                append_qc_report(self.current_deployment_folder, "field_completeness", "", "warning",
                                 f"Field '{f}' is only {pct:.0f}% populated")
        else:
            append_qc_report(self.current_deployment_folder, "field_completeness", "", "pass",
                             "All required fields populated above 90% threshold")

        self.generate_metadata_files()
        self.update_review_tab()
        self.tabs.setCurrentIndex(2)
        
        # Auto-upload if enabled
        if self.upload_to_box_cb.isChecked() and self.box_authenticated:
            self.upload_to_box()
    
    def _write_metadata_csvs(self):
        """
        Build and write image_file_metadata.csv, audio_file_metadata.csv, and
        deployment_event_record.json from the current file_inventory.
        Safe to call incrementally after each device completes.
        """
        org  = self.metadata.get('organization', '')
        site = self.metadata.get('site', '')
        start = self.metadata.get('deployment_start', '')
        end   = self.metadata.get('deployment_end', '')
        observer = self.metadata.get('observer', '')
        end_nodash = end.replace('-', '')

        # Site full name and code from RESERVES (list of (site_code, site_name))
        site_full_name = ''
        site_code = site  # fallback
        for sc, sn in RESERVES:
            if sc == site:
                site_full_name = sn
                break

        deployment_event_id = f"{org}_{site}_{end_nodash}"
        subproject = deployment_event_id

        # event_name: "YYYYMMMstart-YYYYMMMend" e.g. "2026JAN-2026APR"
        try:
            s_dt = datetime.strptime(start, "%Y-%m-%d")
            e_dt = datetime.strptime(end,   "%Y-%m-%d")
            event_name = f"{s_dt.strftime('%Y%b').upper()}-{e_dt.strftime('%Y%b').upper()}"
        except Exception:
            event_name = ''

        now_iso = datetime.now(timezone.utc).isoformat()

        image_rows = []
        audio_rows = []

        for entry in self.file_inventory:
            plot_num = entry.get('plot_number', '')
            dev_type = entry.get('device_type', '')
            file_type = entry.get('file_type', '')

            try:
                plot_num_int = int(plot_num)
            except (TypeError, ValueError):
                plot_num_int = None

            deployment_id = f"{org}_{site}_plot{plot_num}_{dev_type}_{end_nodash}"
            placename = f"{site}_plot{plot_num}"

            # SoundHub physical lookup from ARUs.csv — keyed by (site, plot, device).
            # Physical install info doesn't change between deployments at the same plot.
            aru_key = (site_code, plot_num_int, dev_type)
            aru_row = ARUS.get(aru_key, {})

            # SoundHub config lookup (keyed by device type suffix)
            aru_container = SOUNDHUB_CONFIG.get(f'ARU_container_{dev_type}', '')
            aru_microphone = SOUNDHUB_CONFIG.get('ARU_microphone', '')
            sh_feature_type = SOUNDHUB_CONFIG.get('feature_type', '')

            base = {
                'filename':           entry.get('new_filename', ''),
                'original_filename':  entry.get('original_filename', ''),
                'deployment_event_id': deployment_event_id,
                'deployment_id':      deployment_id,
                'organization':       org,
                'site':               site,
                'site_full_name':     site_full_name,
                'site_code':          site_code,
                'subproject':         subproject,
                'subproject_design':  '',
                'placename':          placename,
                'event_name':         event_name,
                'event_description':  '',
                'plot_number':        plot_num,
                'device_type':        dev_type,
                'file_type':          file_type,
                'file_size_bytes':    entry.get('file_size_bytes', ''),
                'file_hash_sha256':   entry.get('file_hash_sha256', ''),
                'recorded_datetime':  entry.get('recorded_datetime', ''),
                'latitude':           entry.get('latitude', ''),
                'longitude':          entry.get('longitude', ''),
                'app_version':        VERSION,
                'processing_datetime': now_iso,
                'is_uploaded_to_box': False,
                'box_uploader':       '',
                'box_upload_datetime': '',
                'is_uploaded_to_pelican': False,
                'pelican_uploader':   '',
                'pelican_upload_datetime': '',
                'notes':              '',
            }

            if file_type == 'image':
                cam_key = (site, plot_num_int, dev_type) if plot_num_int else None
                cam = self.wi_camera_metadata.get(cam_key, {}) if cam_key else {}
                row = {**base,
                    'start_date':    f"{start} 00:00:00" if start else '',
                    'end_date':      f"{end} 23:59:59"   if end   else '',
                    'recorded_by':   observer,
                    'camera_id':     entry.get('device_id', ''),
                    'camera_make':   entry.get('camera_make', ''),
                    'camera_model':  entry.get('camera_model', ''),
                    'sequence_trigger_type': entry.get('sequence_trigger_type', ''),
                    'sequence_event_num':    entry.get('sequence_event_num', ''),
                    'sequence_position':     entry.get('sequence_position', ''),
                    'sequence_total':        entry.get('sequence_total', ''),
                    'temperature_c':         entry.get('temperature_c', ''),
                    'moon_phase':            entry.get('moon_phase', ''),
                    'battery_voltage':       entry.get('battery_voltage', ''),
                    'battery_voltage_avg':   entry.get('battery_voltage_avg', ''),
                    'battery_type':          entry.get('battery_type', ''),
                    # WI fields
                    'project_id':            self.wi_config.get(f'project_id_{dev_type}', ''),
                    'bait_type':             self.wi_config.get(f'bait_type_{dev_type}', ''),
                    'bait_description':      self.wi_config.get(f'bait_description_{dev_type}', ''),
                    'event_type':            self.wi_config.get('event_type', 'Temporal'),
                    'quiet_period':          self.wi_config.get('quiet_period', 0),
                    'camera_functioning':    self.wi_config.get('camera_functioning_default', 'Camera Functioning'),
                    'feature_type':          cam.get('feature_type', ''),
                    'feature_type_methodology': '',
                    'sensor_height':         cam.get('sensor_height', ''),
                    'height_other':          '',
                    'sensor_orientation':    cam.get('sensor_orientation', ''),
                    'orientation_other':     '',
                    'plot_treatment':        cam.get('plot_treatment', ''),
                    'plot_treatment_description': cam.get('plot_treatment_description', ''),
                    'detection_distance':    cam.get('detection_distance', ''),
                    'is_submitted_to_wi':    False,
                    'wi_submitter':          '',
                    'wi_submission_datetime': '',
                }
                image_rows.append(row)

            else:  # audio or config
                row = {**base,
                    'deployment_start_date': start,
                    'deployment_end_date':   end,
                    'recorded_by':           observer,
                    'device_id':             entry.get('device_id', ''),
                    'ARU_make':              entry.get('ARU_make', '') or SOUNDHUB_CONFIG.get('ARU_make', ''),
                    'ARU_model':             entry.get('ARU_model', '') or SOUNDHUB_CONFIG.get('ARU_model', ''),
                    'sample_rate_hz':        entry.get('sample_rate_hz', '') or SOUNDHUB_CONFIG.get(f'sample_rate_hz_{dev_type}', ''),
                    'gain':                  entry.get('gain', ''),
                    'filter_type_khz':       entry.get('filter_type_khz', ''),
                    'battery_voltage':       entry.get('battery_voltage', ''),
                    'temperature_c':         entry.get('temperature_c', ''),
                    'date_installed':        start,
                    'deployment_start_time': entry.get('deployment_start_time', '') or SOUNDHUB_CONFIG.get(f'deployment_start_time_{dev_type}', ''),
                    'deployment_end_time':   entry.get('deployment_end_time', '')   or SOUNDHUB_CONFIG.get(f'deployment_end_time_{dev_type}', ''),
                    'frequency':             entry.get('frequency', '') or SOUNDHUB_CONFIG.get('frequency', ''),
                    'duration':              entry.get('duration', '')   or SOUNDHUB_CONFIG.get(f'duration_{dev_type}', ''),
                    'filter_type_duration':  entry.get('filter_type_duration', ''),
                    'filter_type_amplitude': entry.get('filter_type_amplitude', ''),
                    'feature_type':          sh_feature_type,
                    'feature_type_details':  '',
                    'ARU_container':         aru_container,
                    'ARU_microphone':        aru_microphone,
                    'mounted_on':            aru_row.get('mounted_on', ''),
                    'sensor_height_meters':  aru_row.get('sensor_height_meters', ''),
                    'ARU_status':            aru_row.get('ARU_status', ''),
                    'is_submitted_to_soundhub': False,
                    'soundhub_submitter':    '',
                    'soundhub_submission_datetime': '',
                    'is_submitted_to_nabat': False,
                    'nabat_submitter':       '',
                    'nabat_submission_datetime': '',
                }
                audio_rows.append(row)

        def _row_count_existing(path: Path) -> int:
            if not path.exists():
                return -1  # sentinel: file did not previously exist
            try:
                with open(path, newline='', encoding='utf-8') as f:
                    return sum(1 for _ in csv.DictReader(f))
            except Exception:
                return -1

        def _log_csv_write(path: Path, prev_count: int, new_count: int):
            name = path.name
            if prev_count < 0:
                self.log(f"Generated: {name} ({new_count} rows)")
            elif new_count == prev_count:
                self.log(f"Updated: {name} (no change, {new_count} rows)")
            else:
                delta = new_count - prev_count
                sign = "+" if delta > 0 else ""
                self.log(f"Updated: {name} ({sign}{delta} rows, {new_count} total)")

        # Write image_file_metadata.csv
        img_path = self.current_deployment_folder / "image_file_metadata.csv"
        prev_img = _row_count_existing(img_path)
        with open(img_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=IMAGE_FIELDS, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(image_rows)
        _log_csv_write(img_path, prev_img, len(image_rows))

        # Write audio_file_metadata.csv
        aud_path = self.current_deployment_folder / "audio_file_metadata.csv"
        prev_aud = _row_count_existing(aud_path)
        with open(aud_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=AUDIO_FIELDS, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(audio_rows)
        _log_csv_write(aud_path, prev_aud, len(audio_rows))

        # deployment_event_record.json (unchanged)
        manifest = {
            'deployment_info': self.metadata,
            'devices': [
                {'plot_number': pn, 'plot_label': pl, 'device_type': dc, 'device_label': dl}
                for pn, pl, dc, dl in self.devices
            ],
            'file_count': len(self.file_inventory),
            'generated': datetime.now().isoformat(),
            'version': VERSION
        }
        manifest_path = self.current_deployment_folder / "deployment_event_record.json"
        existed = manifest_path.exists()
        with open(manifest_path, 'w') as f:
            json.dump(manifest, f, indent=2)
        self.log(f"{'Updated' if existed else 'Generated'}: deployment_event_record.json")

    def generate_metadata_files(self):
        """Generate CSVs, deployment_event_record.json, and WI deployment exports."""
        self._write_metadata_csvs()
        self._wi_status_lines = self.generate_wi_deployments()

    # ------------------------------------------------------------------
    # Wildlife Insights helpers
    # ------------------------------------------------------------------

    def _load_wi_camera_metadata(self) -> dict:
        """Load local_data/cameras.csv → dict keyed (site_code, plot_number_int, device_type)."""
        path = _LOCAL_DATA_DIR / "cameras.csv"
        if not path.exists():
            return {}
        result = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    try:
                        key = (row["site_code"].strip(), int(row["plot_number"]), row["device_type"].strip())
                        result[key] = row
                    except (KeyError, ValueError):
                        continue
        except Exception as e:
            self.log(f"Warning: could not load cameras.csv: {e}")
        return result

    def _load_wi_config(self) -> dict:
        """Load local_data/wi_config.json."""
        path = _LOCAL_DATA_DIR / "wi_config.json"
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            self.log(f"Warning: could not load wi_config.json: {e}")
            return {}

    def _load_wi_plot_coords(self) -> dict:
        """Load local_data/plots.csv → dict keyed (site_code, plot_number_int)."""
        path = _LOCAL_DATA_DIR / "plots.csv"
        if not path.exists():
            return {}
        result = {}
        for encoding in ("utf-8", "utf-8-sig", "latin-1"):
            try:
                with open(path, "r", encoding=encoding) as f:
                    for row in csv.DictReader(f):
                        try:
                            key = (row["site_code"].strip(), int(row["plot_number"]))
                            result[key] = {
                                "latitude": row.get("plot_latitude", "").strip(),
                                "longitude": row.get("plot_longitude", "").strip(),
                            }
                        except (KeyError, ValueError):
                            continue
                return result
            except UnicodeDecodeError:
                result = {}
        return result

    def generate_wi_deployments(self) -> list[str]:
        """Generate Wildlife Insights deployment CSVs from image_file_metadata.csv."""
        WI_COLUMNS = [
            "project_id", "deployment_id", "subproject_name", "subproject_design",
            "placename", "longitude", "latitude", "start_date", "end_date",
            "event_name", "event_description", "event_type", "bait_type", "bait_description",
            "feature_type", "feature_type_methodology", "camera_id", "quiet_period",
            "camera_functioning", "sensor_height", "height_other", "sensor_orientation",
            "orientation_other", "recorded_by", "plot_treatment", "plot_treatment_description",
            "detection_distance",
        ]
        CAMERA_DEVICE_TYPES = {"ML", "SA"}
        status_lines = []

        img_path = self.current_deployment_folder / "image_file_metadata.csv"
        if not img_path.exists():
            msg = "Skipping WI export: image_file_metadata.csv not found"
            self.log(msg)
            status_lines.append(msg)
            return status_lines

        # Load image rows and deduplicate by deployment_id
        seen = set()
        rows_by_type: dict[str, list[dict]] = {}
        try:
            with open(img_path, newline='', encoding='utf-8') as f:
                for row in csv.DictReader(f):
                    if row.get('file_type') != 'image':
                        continue
                    dev_type = row.get('device_type', '')
                    if dev_type not in CAMERA_DEVICE_TYPES:
                        continue
                    dep_id = row.get('deployment_id', '')
                    if dep_id in seen:
                        continue
                    seen.add(dep_id)
                    wi_row = {
                        "project_id":              row.get('project_id', ''),
                        "deployment_id":           dep_id,
                        "subproject_name":         row.get('subproject', ''),
                        "subproject_design":       row.get('subproject_design', ''),
                        "placename":               row.get('placename', ''),
                        "longitude":               row.get('longitude', ''),
                        "latitude":                row.get('latitude', ''),
                        "start_date":              row.get('start_date', ''),
                        "end_date":                row.get('end_date', ''),
                        "event_name":              row.get('event_name', ''),
                        "event_description":       row.get('event_description', ''),
                        "event_type":              row.get('event_type', ''),
                        "bait_type":               row.get('bait_type', ''),
                        "bait_description":        row.get('bait_description', ''),
                        "feature_type":            row.get('feature_type', ''),
                        "feature_type_methodology": row.get('feature_type_methodology', ''),
                        "camera_id":               row.get('camera_id', ''),
                        "quiet_period":            row.get('quiet_period', ''),
                        "camera_functioning":      row.get('camera_functioning', ''),
                        "sensor_height":           row.get('sensor_height', ''),
                        "height_other":            row.get('height_other', ''),
                        "sensor_orientation":      row.get('sensor_orientation', ''),
                        "orientation_other":       row.get('orientation_other', ''),
                        "recorded_by":             row.get('recorded_by', ''),
                        "plot_treatment":          row.get('plot_treatment', ''),
                        "plot_treatment_description": row.get('plot_treatment_description', ''),
                        "detection_distance":      row.get('detection_distance', ''),
                    }
                    rows_by_type.setdefault(dev_type, []).append(wi_row)
        except Exception as e:
            msg = f"Error reading image_file_metadata.csv for WI export: {e}"
            self.log(msg)
            status_lines.append(msg)
            return status_lines

        wi_dir = self.current_deployment_folder / "WI_metadata"
        wi_dir.mkdir(exist_ok=True)

        for dev_type in sorted(rows_by_type):
            rows = rows_by_type[dev_type]
            filename = f"wildlife_insights_{dev_type}_deployments.csv"
            out_path = wi_dir / filename
            prev_count = -1
            if out_path.exists():
                try:
                    with open(out_path, newline="", encoding="utf-8") as f:
                        prev_count = sum(1 for _ in csv.DictReader(f))
                except Exception:
                    prev_count = -1
            with open(out_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=WI_COLUMNS)
                writer.writeheader()
                writer.writerows(rows)
            new_count = len(rows)
            if prev_count < 0:
                msg = f"Generated: WI_metadata/{filename} ({new_count} rows)"
            elif new_count == prev_count:
                msg = f"Updated: WI_metadata/{filename} (no change, {new_count} rows)"
            else:
                delta = new_count - prev_count
                sign = "+" if delta > 0 else ""
                msg = f"Updated: WI_metadata/{filename} ({sign}{delta} rows, {new_count} total)"
            self.log(msg)
            status_lines.append(f"  - {filename} ({new_count} rows)")

        return status_lines

    def update_review_tab(self):
        """Update review summary"""
        summary = []
        summary.append("=" * 60)
        summary.append("DEPLOYMENT SUMMARY")
        summary.append("=" * 60)
        summary.append("")
        
        summary.append("DEPLOYMENT INFORMATION")
        summary.append("-" * 60)
        summary.append(f"Organization: {self.metadata['organization']}")
        summary.append(f"Reserve: {self.metadata['reserve_name']}")
        summary.append(f"Site: {self.metadata['site']}")
        summary.append(f"Deployment Period: {self.metadata['deployment_start']} to {self.metadata['deployment_end']}")
        summary.append(f"Observer: {self.metadata['observer']}")
        summary.append("")
        
        summary.append("DEVICES COLLECTED")
        summary.append("-" * 60)
        for plot_num, plot_label, dev_code, device_label in self.devices:
            device_files = [f for f in self.file_inventory if f['device_label'] == device_label]
            summary.append(f"  Plot {plot_num} ({plot_label}) - {DEVICE_TYPES[dev_code]}: {len(device_files)} files")
        summary.append("")
        
        summary.append("FILES PROCESSED")
        summary.append("-" * 60)
        total_files = len(self.file_inventory)
        total_size = sum(f['file_size_bytes'] for f in self.file_inventory)
        total_size_mb = total_size / (1024 * 1024)
        
        summary.append(f"Total Files: {total_files}")
        summary.append(f"Total Size: {total_size_mb:.2f} MB")
        summary.append("")
        
        file_types = {}
        for f in self.file_inventory:
            ftype = f['file_type']
            file_types[ftype] = file_types.get(ftype, 0) + 1
        
        summary.append("File Types:")
        for ftype, count in file_types.items():
            summary.append(f"  {ftype}: {count}")
        summary.append("")
        
        summary.append("OUTPUT LOCATION")
        summary.append("-" * 60)
        summary.append(f"Local Staging: {self.current_deployment_folder}")
        summary.append("")
        summary.append("Files generated:")
        summary.append("  - deployment_event_record.json")
        summary.append("  - file_metadata.csv")
        summary.append(f"  - raw_data/ ({total_files} files in device subfolders)")
        summary.append("")

        summary.append("WILDLIFE INSIGHTS")
        summary.append("-" * 60)
        wi_lines = getattr(self, "_wi_status_lines", [])
        if wi_lines:
            for line in wi_lines:
                summary.append(line)
        else:
            summary.append("  Skipped (wi_config.json not found in local_data/)")
        summary.append("")

        if self.upload_to_box_cb.isChecked():
            summary.append("Box Upload: In progress or will be uploaded...")
        
        summary.append("")
        summary.append("=" * 60)
        summary.append("NEXT STEPS")
        summary.append("=" * 60)
        summary.append("1. Review the files in the staging folder")
        summary.append("2. Verify Box upload completed successfully")
        summary.append("3. Keep a backup of the original SD cards until transfer is verified")
        
        self.summary_text.setText('\n'.join(summary))
    
    def upload_to_box(self):
        """Upload deployment folder to Box"""
        if not self.box_authenticated:
            QMessageBox.warning(self, "Box Not Connected", "Please authenticate with Box first.")
            return
        
        self.upload_group.show()
        self.upload_status_label.setText("Starting upload to Box...")
        self.upload_progress_bar.setValue(0)

        # Disable buttons during upload, show the cancel button
        self.open_btn.setEnabled(False)
        self.upload_now_btn.setEnabled(False)
        self.new_btn.setEnabled(False)
        self.cancel_upload_btn.setEnabled(True)
        self.cancel_upload_btn.setText("Cancel Upload")
        self.cancel_upload_btn.show()

        # Start upload thread
        self.upload_thread = BoxUploadThread(
            self.current_deployment_folder,
            self.metadata,
            self.file_inventory,
        )
        self.upload_thread.progress.connect(self.on_upload_progress)
        self.upload_thread.finished.connect(self.on_upload_finished)
        self.upload_thread.start()

    def cancel_upload(self):
        """Cooperatively cancel the running Box upload."""
        if self.upload_thread and self.upload_thread.isRunning():
            self.upload_thread.cancel()
            self.cancel_upload_btn.setText("Cancelling…")
            self.cancel_upload_btn.setEnabled(False)
            self.upload_status_label.setText("Cancellation requested — waiting for in-flight files to finish…")
    
    def upload_to_box_manual(self):
        """Manual Box upload trigger"""
        if not self.current_deployment_folder:
            QMessageBox.information(self, "No Data", "No deployment data to upload.")
            return
        
        self.upload_to_box()
    
    def on_upload_progress(self, current, total, filename):
        """Update upload progress"""
        percent = int((current / total) * 100)
        self.upload_progress_bar.setValue(percent)
        self.upload_status_label.setText(f"Uploading: {filename} ({current}/{total})")
    
    def on_upload_finished(self, success, message):
        """Handle upload completion"""
        # Re-enable buttons, hide the cancel button
        self.open_btn.setEnabled(True)
        self.upload_now_btn.setEnabled(True)
        self.new_btn.setEnabled(True)
        self.cancel_upload_btn.hide()
        
        if success:
            self.upload_progress_bar.setValue(100)
            self.upload_status_label.setText(f"✓ {message}")
            self.upload_status_label.setStyleSheet("color: green; font-weight: bold;")
            QMessageBox.information(self, "Upload Complete", message)
            self.box_upload_complete = True
            # Update provenance in both metadata CSVs
            self._write_upload_provenance()
        else:
            self.upload_status_label.setText(f"✗ {message}")
            self.upload_status_label.setStyleSheet("color: red; font-weight: bold;")
            QMessageBox.warning(self, "Upload Failed", message)

    def _write_upload_provenance(self):
        """Set is_uploaded_to_box=True + uploader + timestamp in both metadata CSVs,
        then re-upload the updated CSVs to Box."""
        if not self.current_deployment_folder:
            return
        observer = self.metadata.get('observer', '')
        upload_dt = datetime.now(timezone.utc).isoformat()
        updated_paths = []

        for csv_path, fields in [
            (self.current_deployment_folder / "image_file_metadata.csv", IMAGE_FIELDS),
            (self.current_deployment_folder / "audio_file_metadata.csv", AUDIO_FIELDS),
        ]:
            if not csv_path.exists():
                continue
            try:
                with open(csv_path, newline='', encoding='utf-8') as f:
                    rows = list(csv.DictReader(f))
                for row in rows:
                    row['is_uploaded_to_box'] = True
                    row['box_uploader'] = observer
                    row['box_upload_datetime'] = upload_dt
                with open(csv_path, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
                    writer.writeheader()
                    writer.writerows(rows)
                self.log(f"Updated provenance locally: {csv_path.name}")
                updated_paths.append(csv_path)
            except Exception as e:
                self.log(f"Warning: could not update provenance in {csv_path.name}: {e}")

        # Re-upload the provenance-stamped CSVs to the same Box folder
        deploy_folder_id = self.upload_thread.deploy_folder_id if self.upload_thread else None
        if updated_paths and deploy_folder_id:
            self.provenance_thread = ProvenanceUploadThread(updated_paths, deploy_folder_id)
            self.provenance_thread.finished.connect(self._on_provenance_upload_finished)
            self.provenance_thread.start()
        elif updated_paths:
            self.log("Warning: deploy_folder_id not available — provenance CSVs updated locally only")
    
    def _on_provenance_upload_finished(self, success, message):
        """Handle completion of provenance CSV re-upload to Box."""
        if success:
            self.log(f"✓ {message}")
        else:
            self.log(f"Warning: {message}")

    def _run_device_qc_checks(self, device_entries: list, device_label: str):
        """Run QC checks after a device completes. Logs each check (pass + fail) to qc_report.json."""
        if not self.current_deployment_folder:
            return
        deploy_start = self.metadata.get('deployment_start', '')
        deploy_end = self.metadata.get('deployment_end', '')

        check_results = [
            ("sequence_gap", check_sequence_integrity(device_entries, device_label)),
            ("temporal_plausibility", validate_datetimes(device_entries, deploy_start, deploy_end)),
        ]

        any_warnings = False
        for check_name, warnings_list in check_results:
            if warnings_list:
                any_warnings = True
                for msg in warnings_list:
                    self.log(f"  QC [{device_label}] {msg}")
                    append_qc_report(self.current_deployment_folder, check_name, device_label, "warning", msg)
            else:
                append_qc_report(self.current_deployment_folder, check_name, device_label, "pass", "OK")

        if not any_warnings:
            self.log(f"  QC [{device_label}] All checks passed.")

    def run_orphan_scan(self):
        """Scan staging folder for files missing from disk or not in inventory."""
        if not self.current_deployment_folder:
            QMessageBox.information(self, "No Session", "Load a session first.")
            return
        result = scan_orphans(self.current_deployment_folder, self.file_inventory)
        missing = result["missing_from_disk"]
        orphaned = result["orphaned_on_disk"]
        if not missing and not orphaned:
            append_qc_report(self.current_deployment_folder, "orphan_scan", "", "pass",
                             "Inventory and disk match — no orphans or missing files")
            QMessageBox.information(self, "Orphan Scan", "All clear — every inventoried file exists on disk and no unexpected files were found.")
            return
        append_qc_report(self.current_deployment_folder, "orphan_scan", "", "warning",
                         f"{len(missing)} inventoried file(s) missing from disk; {len(orphaned)} disk file(s) not in inventory")
        lines = []
        if missing:
            lines.append(f"FILES IN INVENTORY BUT MISSING FROM DISK ({len(missing)}):")
            for m in missing:
                lines.append(f"  [{m['device']}] {m['filename']}")
        if orphaned:
            if lines:
                lines.append("")
            lines.append(f"FILES ON DISK NOT IN INVENTORY ({len(orphaned)}):")
            for o in orphaned:
                lines.append(f"  [{o['device']}] {o['filename']}")
        msg = QMessageBox(self)
        msg.setWindowTitle("Orphan Scan Results")
        msg.setIcon(QMessageBox.Warning)
        msg.setText(f"Orphan scan found issues ({len(missing)} missing from disk, {len(orphaned)} unexpected on disk).")
        msg.setDetailedText("\n".join(lines))
        msg.exec()
        self.log(f"Orphan scan: {len(missing)} missing from disk, {len(orphaned)} unexpected on disk.")
        if missing or orphaned:
            self.log("\n".join(f"  {l}" for l in lines if l))

    def run_fixity_check(self):
        """Re-hash all raw_data/ files and compare against session inventory hashes."""
        if not self.current_deployment_folder:
            QMessageBox.information(self, "No Session", "Load a session first.")
            return
        if not self.file_inventory:
            QMessageBox.information(self, "No Inventory", "No file inventory in session — run SD card copy first.")
            return
        if self.fixity_thread and self.fixity_thread.isRunning():
            QMessageBox.information(self, "In Progress", "Fixity check is already running.")
            return

        self.fixity_btn.setEnabled(False)
        self.fixity_btn.setText("Checking…")
        self.log("Starting staging drive fixity check…")

        self.fixity_thread = FixityCheckThread(self.current_deployment_folder, self.file_inventory, self.metadata)
        self.fixity_thread.progress.connect(self._on_fixity_progress)
        self.fixity_thread.finished.connect(self._on_fixity_finished)
        self.fixity_thread.start()

    def _on_fixity_progress(self, checked: int, total: int, filename: str):
        if total > 0 and filename:
            self.log(f"  Fixity: {checked}/{total} — {filename}")

    def _on_fixity_finished(self, ok: bool, summary: str, issues: list):
        self.fixity_btn.setEnabled(True)
        self.fixity_btn.setText("Verify File Hashes")
        self.fixity_check_run = True
        self.log(summary)

        # Aggregate fixity_check entry covering the whole run
        severity = "pass" if ok else "error"
        append_qc_report(self.current_deployment_folder, "file_hash_verification_run", "", severity, summary)

        if ok:
            QMessageBox.information(self, "File Hashes Verified", summary)
            return

        lines = []
        mismatches = [i for i in issues if i["type"] == "mismatch"]
        missing = [i for i in issues if i["type"] == "missing"]
        if mismatches:
            lines.append(f"HASH MISMATCHES ({len(mismatches)}) — local bytes differ from Box bytes:")
            for m in mismatches:
                lines.append(f"  ✗ {m['filename']}")
        if missing:
            if lines:
                lines.append("")
            lines.append(f"FILES LOCAL BUT MISSING FROM BOX ({len(missing)}):")
            for m in missing:
                lines.append(f"  ✗ {m['filename']}")

        msg = QMessageBox(self)
        msg.setWindowTitle("Verify File Hashes — Issues Found")
        msg.setIcon(QMessageBox.Critical)
        msg.setText(summary)
        msg.setDetailedText("\n".join(lines))
        msg.exec()

        for issue in issues:
            append_qc_report(
                self.current_deployment_folder,
                "file_hash_" + issue["type"],
                "",
                "error",
                issue["filename"],
            )

    def run_box_verify(self):
        """Compare Box deployment folder against local inventory."""
        if not self.current_deployment_folder:
            QMessageBox.information(self, "No Session", "Load a session first.")
            return
        if not self.file_inventory:
            QMessageBox.information(self, "No Inventory", "No file inventory in session.")
            return
        if not self.box_authenticated:
            QMessageBox.information(self, "Not Authenticated", "Authenticate with Box first.")
            return
        self.box_verify_btn.setEnabled(False)
        self.box_verify_btn.setText("Verifying…")
        self.log("Starting Box upload verification…")
        self.box_verify_thread = BoxVerifyThread(
            self.current_deployment_folder, self.file_inventory, self.metadata
        )
        self.box_verify_thread.finished.connect(self._on_box_verify_finished)
        self.box_verify_thread.start()

    def _on_box_verify_finished(self, ok: bool, summary: str, issues: list):
        self.box_verify_btn.setEnabled(self.box_authenticated)
        self.box_verify_btn.setText("Verify Box Upload")
        self.log(summary)

        # Aggregate result
        severity = "pass" if ok and not issues else ("warning" if ok else "error")
        append_qc_report(self.current_deployment_folder, "box_verify", "", severity, summary)

        if not issues:
            QMessageBox.information(self, "Box Verify Passed", summary)
            return

        missing = [i for i in issues if i["type"] == "missing_from_box"]
        extra = [i for i in issues if i["type"] == "extra_on_box"]
        lines = []
        if missing:
            lines.append(f"FILES IN INVENTORY BUT MISSING FROM BOX ({len(missing)}):")
            for m in missing:
                lines.append(f"  ✗ {m['filename']}")
        if extra:
            if lines:
                lines.append("")
            lines.append(f"FILES ON BOX BUT NOT IN LOCAL INVENTORY ({len(extra)}):")
            for e in extra:
                lines.append(f"  ⚠ {e['filename']}")

        msg = QMessageBox(self)
        msg.setWindowTitle("Box Verify — Issues Found" if missing else "Box Verify — Extras on Box")
        msg.setIcon(QMessageBox.Critical if missing else QMessageBox.Warning)
        msg.setText(summary)
        msg.setDetailedText("\n".join(lines))
        msg.exec()

    def open_different_deployment(self):
        """Pop the deployment selection dialog mid-session.
        If a session is active, run the pre-departure checklist first, then write
        a session summary, then load the new selection."""
        if self.current_deployment_folder:
            if not self.show_pre_departure_checklist():
                return
            try:
                generate_session_summary(
                    self.current_deployment_folder,
                    self.metadata,
                    self.file_inventory,
                    self.devices,
                )
            except Exception:
                pass

        sessions = self.find_all_sessions()
        if not sessions:
            QMessageBox.information(
                self, "No Deployments Found",
                f"No deployments with session.json found under:\n{self.staging_root}"
            )
            return

        # Reset transient state before loading another deployment
        self.fixity_check_run = False
        self.box_upload_complete = False
        self.offer_resume_session(sessions)

    def open_staging_folder(self):
        """Open staging folder in file explorer"""
        import subprocess
        import platform
        
        if platform.system() == 'Darwin':
            subprocess.run(['open', str(self.current_deployment_folder)])
        elif platform.system() == 'Windows':
            subprocess.run(['explorer', str(self.current_deployment_folder)])
        else:
            subprocess.run(['xdg-open', str(self.current_deployment_folder)])
    
    def _build_checklist_items(self) -> list[dict]:
        """
        Evaluate pre-departure checklist. Returns list of
        {"label": str, "ok": bool, "note": str} dicts.
        """
        items = []

        # 1. All devices complete
        all_done = True
        pending_count = 0
        for i in range(self.device_tree.topLevelItemCount()):
            if self.device_tree.topLevelItem(i).text(2) == "Pending":
                all_done = False
                pending_count += 1
        items.append({
            "label": "All devices complete",
            "ok": all_done,
            "note": f"{pending_count} device(s) still pending" if not all_done else "",
        })

        # 2. Device manifests written
        raw_data_dir = self.current_deployment_folder / "raw_data"
        manifest_ok = True
        missing_manifests = []
        if raw_data_dir.is_dir():
            for device_dir in [d for d in raw_data_dir.iterdir() if d.is_dir()]:
                expected_manifest = device_dir / f"{device_dir.name}_manifest.json"
                if not expected_manifest.exists():
                    manifest_ok = False
                    missing_manifests.append(device_dir.name)
        items.append({
            "label": "Device manifests written",
            "ok": manifest_ok,
            "note": f"Missing: {', '.join(missing_manifests)}" if missing_manifests else "",
        })

        # 3. Fixity check run this session
        items.append({
            "label": "Staging drive fixity check run",
            "ok": self.fixity_check_run,
            "note": "Click 'Verify File Hashes' before departing" if not self.fixity_check_run else "",
        })

        # 4. Box upload verified
        verification_file = qc_path_for(self.current_deployment_folder, "box_upload_verification.json")
        upload_manifest = qc_path_for(self.current_deployment_folder, "box_upload_manifest.json")
        box_ok = self.box_upload_complete or verification_file.exists() or upload_manifest.exists()
        items.append({
            "label": "Box upload completed",
            "ok": box_ok,
            "note": "No upload recorded for this session" if not box_ok else "",
        })

        # 5. No QC errors in qc_report.json
        qc_ok = True
        qc_error_count = 0
        qc_path = qc_path_for(self.current_deployment_folder, "qc_report.json")
        if qc_path.exists():
            try:
                with open(qc_path) as f:
                    qc_data = json.load(f)
                current = qc_data.get("current_state", [])
                errors = [c for c in current if c.get("severity") == "error"]
                qc_error_count = len(errors)
                qc_ok = qc_error_count == 0
            except Exception:
                pass
        items.append({
            "label": "No QC errors recorded",
            "ok": qc_ok,
            "note": f"{qc_error_count} error(s) in qc_report.json" if not qc_ok else "",
        })

        return items

    def show_pre_departure_checklist(self) -> bool:
        """
        Show the pre-departure checklist dialog. Returns True if the user
        acknowledges and wants to proceed, False to cancel.
        Only shown if a session is active.
        """
        if not self.current_deployment_folder:
            return True  # no active session — let action proceed unimpeded

        items = self._build_checklist_items()
        all_ok = all(it["ok"] for it in items)

        dlg = QDialog(self)
        dlg.setWindowTitle("Pre-Departure Checklist")
        dlg.setMinimumWidth(500)
        layout = QVBoxLayout(dlg)

        layout.addWidget(QLabel(
            "<b>Review the following before closing or resetting the session:</b>"
        ))

        list_widget = QListWidget()
        for it in items:
            icon = "✓" if it["ok"] else "⚠"
            text = f"{icon}  {it['label']}"
            if it["note"]:
                text += f"\n     {it['note']}"
            entry = QListWidgetItem(text)
            if not it["ok"]:
                from PySide6.QtGui import QColor
                entry.setForeground(QColor("#cc4400"))
            list_widget.addItem(entry)
        list_widget.setSelectionMode(QListWidget.NoSelection)
        layout.addWidget(list_widget)

        from PySide6.QtWidgets import QCheckBox
        if not all_ok:
            ack_cb = QCheckBox("I acknowledge the issues above and want to proceed")
            layout.addWidget(ack_cb)
        else:
            layout.addWidget(QLabel("All checks passed."))

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Proceed")
        if not all_ok:
            buttons.button(QDialogButtonBox.Ok).setEnabled(False)
            ack_cb.toggled.connect(buttons.button(QDialogButtonBox.Ok).setEnabled)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        accepted = dlg.exec() == QDialog.Accepted
        if accepted:
            failed = [it["label"] for it in items if not it["ok"]]
            if failed:
                append_qc_report(self.current_deployment_folder, "pre_departure", "", "warning",
                                 f"Acknowledged with {len(failed)} unmet item(s): {', '.join(failed)}")
            else:
                append_qc_report(self.current_deployment_folder, "pre_departure", "", "pass",
                                 "All pre-departure items satisfied")
        return accepted

    def closeEvent(self, event):
        """Override close to show pre-departure checklist and write summary when a session is active."""
        if self.current_deployment_folder:
            if not self.show_pre_departure_checklist():
                event.ignore()
                return
            try:
                summary_path = generate_session_summary(
                    self.current_deployment_folder,
                    self.metadata,
                    self.file_inventory,
                    self.devices,
                )
                self.log(f"Session summary written to: {summary_path.name}")
            except Exception:
                pass
        event.accept()

    def start_new_deployment(self):
        """Reset for new deployment"""
        if self.current_deployment_folder and not self.show_pre_departure_checklist():
            return
        reply = QMessageBox.question(
            self, "Start New Deployment",
            "This will reset the wizard. Are you sure?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            # Write session summary before clearing state
            if self.current_deployment_folder:
                try:
                    generate_session_summary(
                        self.current_deployment_folder,
                        self.metadata,
                        self.file_inventory,
                        self.devices,
                    )
                except Exception:
                    pass
            # Clean up session file before clearing state
            if self.current_deployment_folder:
                session_file = self.current_deployment_folder / "session.json"
                session_file.unlink(missing_ok=True)

            self.metadata = {}
            self.devices = []
            self.file_inventory = []
            self.seen_file_hashes = set()
            self.current_deployment_folder = None
            self.fixity_check_run = False
            self.box_upload_complete = False
            
            # Reset UI
            self.reserve_combo.setCurrentIndex(-1)
            self.site_code_edit.clear()
            self.deploy_start_date.setDate(QDate.currentDate())
            self.deploy_end_date.setDate(QDate.currentDate())
            self.observer_combo.setCurrentIndex(-1)
            self.clear_all_devices()
            self.device_tree.clear()
            self.log_text.clear()
            self.summary_text.clear()
            self.upload_group.hide()
            
            self.tabs.setCurrentIndex(0)


def main():
    if not EXIF_AVAILABLE:
        print("Warning: PIL/piexif not available. EXIF extraction will be disabled.")
        print("Install with: pip install pillow piexif")
    
    if not BOX_AVAILABLE:
        print("Warning: box-sdk-gen not available. Box upload will be disabled.")
        print("Install with: pip install box-sdk-gen")
        print("Then run box_auth_setup.py to authenticate")
    
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    
    window = FieldDataWizard()
    window.show()
    
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
