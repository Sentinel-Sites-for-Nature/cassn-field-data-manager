#!/usr/bin/env python3

import sys
import csv
import os
import re
import struct
import subprocess
import wave
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
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
    QCompleter, QFrame, QSizePolicy, QProgressBar
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
    """Load plot names from local_data/plots.csv."""
    plot_names = {}
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
                            plot_names[site_code] = [None, None, None, None]

                        if 1 <= plot_number <= 4 and plot_name:
                            plot_names[site_code][plot_number - 1] = plot_name
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
    """Load ARUs.csv → dict keyed (deployment_event_id, site_code, plot_number_int, device_type)."""
    result = {}
    if not path.exists():
        print(f"  WARNING: ARUs.csv not found at {path}")
        return result
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                key = (
                    row["deployment_event_id"].strip(),
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
    
    def __init__(self, deployment_folder, metadata):
        super().__init__()
        self.deployment_folder = deployment_folder
        self.metadata = metadata
        self.client = None
        self.deploy_folder_id = None  # set during run(); used for provenance re-upload
        self._box_folder_files: dict[str, set[str]] = {}  # folder_id → set of existing filenames
    
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
            skipped = 0

            for file_path in uploadable_files:
                rel_path = file_path.relative_to(self.deployment_folder)
                was_skipped = self.upload_file_with_path(
                    file_path, deploy_folder.id, rel_path
                )
                if was_skipped:
                    skipped += 1
                else:
                    uploaded += 1
                self.progress.emit(uploaded + skipped, total_files, file_path.name)

            if skipped > 0:
                self.finished.emit(
                    True,
                    f"Upload complete: {uploaded} uploaded, {skipped} already on Box (skipped)"
                )
            else:
                self.finished.emit(True, f"Successfully uploaded {uploaded} files to Box")
            
        except Exception as e:
            self.finished.emit(False, f"Upload error: {str(e)}")

    @staticmethod
    def should_upload_file(file_path):
        """Skip macOS/Finder junk files and internal app files from Box uploads."""
        name = file_path.name
        SKIP_NAMES = {'session.json'}
        return not (name.startswith('.') or name.startswith('._') or name in SKIP_NAMES)
    
    def find_or_create_folder(self, client, parent_id, folder_name):
        """Find existing folder or create new one, using paginated listing."""
        from box_sdk_gen import CreateFolderParent
        from box_sdk_gen.box.errors import BoxAPIError

        def _search_pages():
            offset = 0
            while True:
                page = client.folders.get_folder_items(parent_id, limit=1000, offset=offset)
                entries = page.entries or []
                for item in entries:
                    if item.type == 'folder' and item.name == folder_name:
                        return item
                if len(entries) < 1000:
                    return None
                offset += 1000

        existing = _search_pages()
        if existing:
            return existing

        try:
            return client.folders.create_folder(folder_name, CreateFolderParent(id=parent_id))
        except BoxAPIError as e:
            if e.response_info.status_code == 409 and e.response_info.code == 'item_name_in_use':
                # Created between our listing and now — fetch it
                found = _search_pages()
                if found:
                    return found
                raise Exception(f"Folder '{folder_name}' conflict but not found in listing")
            raise Exception(f"Error creating folder '{folder_name}': {e}")
    
    def upload_file_with_path(self, local_path, parent_folder_id, relative_path):
        """Upload file maintaining directory structure. Returns True if skipped (already on Box)."""
        from box_sdk_gen.box.errors import BoxAPIError

        current_folder_id = parent_folder_id
        if len(relative_path.parts) > 1:
            for folder_name in relative_path.parts[:-1]:
                folder = self.find_or_create_folder(
                    self.client, current_folder_id, folder_name
                )
                current_folder_id = folder.id

        # Build a paginated name cache for this folder on first access
        file_name = relative_path.name
        if current_folder_id not in self._box_folder_files:
            existing = set()
            offset = 0
            while True:
                page = self.client.folders.get_folder_items(
                    current_folder_id, limit=1000, offset=offset
                )
                entries = page.entries or []
                for item in entries:
                    if item.type == 'file':
                        existing.add(item.name)
                if len(entries) < 1000:
                    break
                offset += 1000
            self._box_folder_files[current_folder_id] = existing

        if file_name in self._box_folder_files[current_folder_id]:
            return True  # already on Box

        file_size = local_path.stat().st_size
        try:
            with open(local_path, 'rb') as file_stream:
                if file_size >= CHUNKED_UPLOAD_THRESHOLD:
                    self.client.chunked_uploads.upload_big_file(
                        file=file_stream,
                        file_name=file_name,
                        file_size=file_size,
                        parent_folder_id=current_folder_id,
                    )
                else:
                    self.client.uploads.upload_file(
                        attributes={'name': file_name, 'parent': {'id': current_folder_id}},
                        file=file_stream,
                    )
            self._box_folder_files[current_folder_id].add(file_name)
            return False
        except BoxAPIError as e:
            if e.response_info.status_code == 409 and e.response_info.code == 'item_name_in_use':
                # File already in Box (cache miss) — update cache and treat as uploaded
                self._box_folder_files[current_folder_id].add(file_name)
                return True
            raise


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
        self.upload_thread = None
        self.provenance_thread = None
        
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
        session_path, session_data = self.find_resumable_session()
        if session_path and session_data:
            self.offer_resume_session(session_path, session_data)

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
            tmp_path = session_path.with_suffix('.json.tmp')
            with open(tmp_path, "w") as f:
                json.dump(session, f, indent=2, default=str)
            tmp_path.replace(session_path)  # atomic on POSIX — never leaves a half-written file
        except Exception:
            pass  # never crash the app on a failed save

    def find_resumable_session(self) -> tuple:
        """Scan staging_root for valid session.json files. Returns (path, data) of most recent."""
        try:
            if not self.staging_root.exists():
                return None, None

            candidates = []
            for subdir in self.staging_root.iterdir():
                if not subdir.is_dir():
                    continue
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
                    saved_at = datetime.fromisoformat(data["saved_at"])
                    candidates.append((saved_at, session_file, data))
                except Exception:
                    continue

            if not candidates:
                return None, None

            candidates.sort(key=lambda x: x[0], reverse=True)
            _, best_path, best_data = candidates[0]
            return best_path, best_data

        except Exception:
            return None, None

    def offer_resume_session(self, session_path, session_data):
        """Prompt the user to resume a saved session."""
        meta = session_data.get("metadata", {})
        statuses = session_data.get("device_statuses", {})
        total = len(statuses)
        completed = sum(1 for v in statuses.values() if v.get("status") in ("Complete", "Skipped"))

        reply = QMessageBox.question(
            self,
            "Resume Previous Session",
            f"An in-progress deployment was found:\n\n"
            f"  Reserve:  {meta.get('reserve_name', '—')}\n"
            f"  Period:   {meta.get('deployment_start', '—')} to {meta.get('deployment_end', '—')}\n"
            f"  Progress: {completed} of {total} devices done\n\n"
            "Resume where you left off?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply == QMessageBox.Yes:
            self.restore_session(session_data)

    def restore_session(self, session_data):
        """Restore app state from a saved session dict."""
        self.metadata = session_data.get("metadata", {})
        self.devices = [tuple(d) for d in session_data.get("devices", [])]
        self.file_inventory = session_data.get("file_inventory", [])
        self.current_deployment_folder = Path(session_data["deployment_folder"])

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
        
        # Device grid
        grid_layout = QGridLayout()
        
        grid_layout.addWidget(QLabel("Plot"), 0, 0)
        col = 1
        for dev_code, dev_name in DEVICE_TYPES.items():
            label = QLabel(dev_name)
            label.setWordWrap(True)
            grid_layout.addWidget(label, 0, col)
            col += 1
        
        self.device_checkboxes = {}
        self.plot_labels = {}
        
        for plot_num in range(1, 5):
            plot_label = QLabel(f"Plot {plot_num}")
            self.plot_labels[plot_num] = plot_label
            grid_layout.addWidget(plot_label, plot_num, 0)
            
            self.device_checkboxes[plot_num] = {}
            col = 1
            for dev_code in DEVICE_TYPES.keys():
                cb = QCheckBox()
                self.device_checkboxes[plot_num][dev_code] = cb
                grid_layout.addWidget(cb, plot_num, col, Qt.AlignCenter)
                col += 1
        
        device_layout.addLayout(grid_layout)
        
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
        control_layout.addWidget(copy_btn)
        control_layout.addWidget(skip_btn)
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
        
        self.upload_group.setLayout(upload_layout)
        self.upload_group.hide()
        layout.addWidget(self.upload_group)
        
        # Action buttons
        button_layout = QHBoxLayout()
        
        self.open_btn = QPushButton("Open Staging Folder")
        self.open_btn.clicked.connect(self.open_staging_folder)
        
        self.upload_now_btn = QPushButton("Upload to Box Now")
        self.upload_now_btn.clicked.connect(self.upload_to_box_manual)
        self.upload_now_btn.setEnabled(self.box_authenticated)
        
        self.new_btn = QPushButton("Start New Deployment")
        self.new_btn.clicked.connect(self.start_new_deployment)
        
        exit_btn = QPushButton("Exit")
        exit_btn.clicked.connect(self.close)
        
        button_layout.addWidget(self.open_btn)
        button_layout.addWidget(self.upload_now_btn)
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
        """Update plot labels based on reserve"""
        plot_names = PLOT_NAMES.get(reserve_code, None)
        
        for plot_num in range(1, 5):
            if plot_names and len(plot_names) >= plot_num:
                self.plot_labels[plot_num].setText(f"Plot {plot_num}: {plot_names[plot_num - 1]}")
            else:
                self.plot_labels[plot_num].setText(f"Plot {plot_num}")
    
    def select_all_devices(self):
        """Select all device checkboxes"""
        for plot_num in range(1, 5):
            for dev_code in DEVICE_TYPES.keys():
                self.device_checkboxes[plot_num][dev_code].setChecked(True)
    
    def clear_all_devices(self):
        """Clear all device checkboxes"""
        for plot_num in range(1, 5):
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
        
        # Build device list
        plot_names = PLOT_NAMES.get(reserve_code, None)
        
        self.devices = []
        for plot_num in range(1, 5):
            if plot_names and len(plot_names) >= plot_num:
                plot_label = plot_names[plot_num - 1]
            else:
                plot_label = str(plot_num)
            
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
        """Add message to log"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.append(f"[{timestamp}] {message}")
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
            total_for_device = sum(
                1 for entry in self.file_inventory if entry['device_label'] == device_label
            )
            selected.setText(3, str(total_for_device))
            self.save_session()

            self.log(f"✓ Completed! {total_for_device} files copied.\n")
            
        except Exception as e:
            self.log(f"✗ Error: {str(e)}\n")
            QMessageBox.critical(self, "Copy Error", f"Error copying files: {str(e)}")
    
    def process_sd_card_files(self, source_dir, dest_dir, plot_num, plot_label, dev_code, device_label):
        """Process files from SD card."""
        files_copied = 0

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

        for root, dirs, files in os.walk(source_dir):
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
                    # Rename: ORG_SITE_DEVCODE_YYYYMMDD_CONFIG_01.txt
                    new_filename = f"{org}_{site}_{dev_code}_{start_date_str}_CONFIG_01{file_ext}"
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

                shutil.copy2(source_path, dest_path)
                file_hash = compute_file_hash(dest_path)

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

                self.file_inventory.append(file_info)
                files_copied += 1

                if file_type == "audio":
                    size_mb = dest_path.stat().st_size / (1024 * 1024)
                    self.log(f"  [{files_copied}] {new_filename}  ({size_mb:.1f} MB)")
                elif files_copied % 50 == 0:
                    self.log(f"  ...{files_copied} files processed")

                if files_copied % 10 == 0:
                    self.save_session()

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
        
        self.generate_metadata_files()
        self.update_review_tab()
        self.tabs.setCurrentIndex(2)
        
        # Auto-upload if enabled
        if self.upload_to_box_cb.isChecked() and self.box_authenticated:
            self.upload_to_box()
    
    def generate_metadata_files(self):
        """Generate split metadata CSVs and deployment_event_record.json."""
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

            # SoundHub physical lookup from ARUs.csv
            aru_key = (deployment_event_id, site_code, plot_num_int, dev_type)
            aru_row = ARUS.get(aru_key, {})

            # SoundHub config lookup (keyed by device type suffix)
            aru_container = SOUNDHUB_CONFIG.get(f'ARU_container_{dev_type}', '')
            aru_microphone = SOUNDHUB_CONFIG.get('ARU_microphone', '')
            sh_feature_type = SOUNDHUB_CONFIG.get('feature_type', '')
            sh_mounted_on   = SOUNDHUB_CONFIG.get('mounted_on', '')

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
                    'mounted_on':            sh_mounted_on,
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

        # Write image_file_metadata.csv
        img_path = self.current_deployment_folder / "image_file_metadata.csv"
        with open(img_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=IMAGE_FIELDS, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(image_rows)
        self.log(f"Generated: image_file_metadata.csv ({len(image_rows)} rows)")

        # Write audio_file_metadata.csv
        aud_path = self.current_deployment_folder / "audio_file_metadata.csv"
        with open(aud_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=AUDIO_FIELDS, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(audio_rows)
        self.log(f"Generated: audio_file_metadata.csv ({len(audio_rows)} rows)")

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
        with open(manifest_path, 'w') as f:
            json.dump(manifest, f, indent=2)
        self.log("Generated: deployment_event_record.json")

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
            with open(out_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=WI_COLUMNS)
                writer.writeheader()
                writer.writerows(rows)
            msg = f"Generated: WI_metadata/{filename} ({len(rows)} rows)"
            self.log(msg)
            status_lines.append(f"  - {filename} ({len(rows)} rows)")

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
        
        # Disable buttons during upload
        self.open_btn.setEnabled(False)
        self.upload_now_btn.setEnabled(False)
        self.new_btn.setEnabled(False)
        
        # Start upload thread
        self.upload_thread = BoxUploadThread(
            self.current_deployment_folder,
            self.metadata
        )
        self.upload_thread.progress.connect(self.on_upload_progress)
        self.upload_thread.finished.connect(self.on_upload_finished)
        self.upload_thread.start()
    
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
        # Re-enable buttons
        self.open_btn.setEnabled(True)
        self.upload_now_btn.setEnabled(True)
        self.new_btn.setEnabled(True)
        
        if success:
            self.upload_progress_bar.setValue(100)
            self.upload_status_label.setText(f"✓ {message}")
            self.upload_status_label.setStyleSheet("color: green; font-weight: bold;")
            QMessageBox.information(self, "Upload Complete", message)
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
    
    def start_new_deployment(self):
        """Reset for new deployment"""
        reply = QMessageBox.question(
            self, "Start New Deployment",
            "This will reset the wizard. Are you sure?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            # Clean up session file before clearing state
            if self.current_deployment_folder:
                session_file = self.current_deployment_folder / "session.json"
                session_file.unlink(missing_ok=True)

            self.metadata = {}
            self.devices = []
            self.file_inventory = []
            self.current_deployment_folder = None
            
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
