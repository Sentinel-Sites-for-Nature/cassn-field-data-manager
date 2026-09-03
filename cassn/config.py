"""
App-wide constants, directory paths, and schema definitions.

Everything fixed at startup (paths, thresholds, schema lists, QC text) lives
here. Nothing in this module reads ``config.json`` or the lookup CSVs — that
side-effecting work moved into :mod:`cassn.box.auth` and :mod:`cassn.lookups`
and is invoked explicitly from the entry point. Importing this module only
ever touches the filesystem to ensure the config directories exist, which is
idempotent and safe.
"""

from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Paths
#
# Credentials and lookup tables always live under ~/.cassn_config/. Bundled
# assets (logos) live next to the package, under BUNDLE_DIR.
# ---------------------------------------------------------------------------

CONFIG_DIR = Path.home() / ".cassn_config"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

BUNDLE_DIR = Path(__file__).resolve().parent.parent

LOCAL_DATA_DIR = CONFIG_DIR / "lookup_tables"
LOCAL_DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_JSON = CONFIG_DIR / "config.json"
BOX_TOKEN_FILE = CONFIG_DIR / "box_tokens.json"

# ---------------------------------------------------------------------------
# App identity
# ---------------------------------------------------------------------------

VERSION = "4.0"
APP_TITLE = "CA-SSN Field Data Manager"

# Camera EXIF and AudioMoth filenames both encode local Pacific time.
PACIFIC_TZ = ZoneInfo("America/Los_Angeles")

# ---------------------------------------------------------------------------
# Upload / QC thresholds
# ---------------------------------------------------------------------------

CHUNKED_UPLOAD_THRESHOLD = 20 * 1024 * 1024   # 20 MB — use chunked upload above this size
IMAGE_SIZE_FLOOR_BYTES = 500 * 1024           # 500 KB — unusually small for field camera JPGs
BIRD_AUDIO_SIZE_FLOOR_BYTES = 1_000_000_000   # 1 GB — scheduled bird recordings should be large
BAT_AUDIO_SIZE_FLOOR_BYTES = 500 * 1024       # 500 KB — conservative floor for triggered bat recordings

# ---------------------------------------------------------------------------
# File classification
# ---------------------------------------------------------------------------

IMAGE_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".tif", ".tiff",
    ".bmp", ".gif", ".cr2", ".nef", ".arw", ".dng",
})
AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".flac", ".m4a", ".aac", ".wma", ".ogg"})
CONFIG_EXTENSIONS = frozenset({".txt"})

# ---------------------------------------------------------------------------
# Device types
# ---------------------------------------------------------------------------

DEVICE_TYPES: dict[str, str] = {
    "ML": "Medium-Large Animal Camera",
    "SA": "Small Animal Camera",
    "BD": "Acoustic Recorder Birds",
    "BT": "Acoustic Recorder Bats",
}
CAMERA_DEVICE_TYPES = frozenset({"ML", "SA"})
AUDIO_DEVICE_TYPES = frozenset({"BD", "BT"})

# ---------------------------------------------------------------------------
# People / organizations
# ---------------------------------------------------------------------------

ORGANIZATIONS = ["UC"]
DEFAULT_DOWNLOADER = "Imperato, John"
DOWNLOADERS = [
    "Imperato, John",
    "Bloom, Ryan",
    "Kaplan-Zenk, Samara",
    "Other",
]

# ---------------------------------------------------------------------------
# QC sidecar files
#
# Subfolder under each deployment that holds QC/audit sidecar files. Keeps the
# deployment root clean.
# ---------------------------------------------------------------------------

QC_SUBFOLDER = "qc"
QC_SIDECAR_FILES = (
    "qc_report.json",
    "box_upload_manifest.json",
    "box_upload_verification.json",
    "deployment_summary.txt",
)

# ---------------------------------------------------------------------------
# Metadata CSV field lists (column order is significant — preserved verbatim)
# ---------------------------------------------------------------------------

IMAGE_FIELDS = [
    "filename", "original_filename", "deployment_event_id", "deployment_id",
    "organization", "site_name", "site_short_name", "site_code",
    "start_date", "end_date", "recorded_by",
    "subproject", "subproject_design", "placename", "event_name", "event_description",
    "plot_number", "device_type", "camera_id", "camera_serial_exif", "file_type",
    "file_size_bytes", "file_hash_sha256", "file_hash_sha1", "recorded_datetime",
    "latitude", "longitude", "elevation_m",
    "camera_make", "camera_model",
    "sequence_trigger_type", "sequence_event_num", "sequence_position", "sequence_total",
    "temperature_c", "moon_phase", "battery_voltage", "battery_voltage_avg", "battery_type",
    "project_id", "bait_type", "bait_description", "event_type", "quiet_period",
    "camera_functioning", "feature_type", "feature_type_methodology",
    "sensor_height", "height_other", "sensor_orientation", "orientation_other",
    "plot_treatment", "plot_treatment_description", "detection_distance",
    "app_version", "processing_datetime",
    "is_uploaded_to_box", "box_uploader", "box_upload_datetime",
    "is_uploaded_to_pelican", "pelican_uploader", "pelican_upload_datetime",
    "is_submitted_to_wi", "wi_submitter", "wi_submission_datetime",
    "notes",
]

AUDIO_FIELDS = [
    "filename", "original_filename", "deployment_event_id", "deployment_id",
    "organization", "site_name", "site_short_name", "site_code",
    "deployment_start_date", "deployment_end_date", "recorded_by",
    "subproject", "subproject_design", "placename", "event_name", "event_description",
    "plot_number", "device_type", "device_id", "file_type",
    "file_size_bytes", "file_hash_sha256", "file_hash_sha1", "recorded_datetime",
    "latitude", "longitude", "elevation_m",
    "ARU_make", "ARU_model", "ARU_firmware", "sample_rate_hz", "gain", "filter_type_khz",
    "battery_voltage", "temperature_c",
    "date_installed", "deployment_start_time", "deployment_end_time",
    "frequency", "duration", "recording_duration_sec", "recording_stop_reason",
    "filter_type_duration", "filter_type_amplitude",
    "feature_type", "feature_type_details", "ARU_container", "ARU_microphone",
    "mounted_on", "sensor_height_meters", "ARU_status",
    "app_version", "processing_datetime",
    "is_uploaded_to_box", "box_uploader", "box_upload_datetime",
    "is_uploaded_to_pelican", "pelican_uploader", "pelican_upload_datetime",
    "is_submitted_to_soundhub", "soundhub_submitter", "soundhub_submission_datetime",
    "is_submitted_to_nabat", "nabat_submitter", "nabat_submission_datetime",
    "notes",
]

# ---------------------------------------------------------------------------
# Wildlife SoundHub
#
# Bird (BD) audio only. Brian Galey's ingest reads a FLAC tree plus two
# project-level CSVs from an S3 landing zone he controls. The IAM role has
# PutObject and ListBucket but *no* DeleteObject, so a wrong key can only be
# removed by Brian by hand — get the structure right before pushing.
#
#   s3://casoundhub/upload/UCNature-SSN/deployment.csv
#   s3://casoundhub/upload/UCNature-SSN/recording.csv
#   s3://casoundhub/upload/UCNature-SSN/<deployment_id>/<filename>.flac
#
# The landing zone is *drained* once SoundHub ingests a submission, so an empty
# prefix does not mean the upload failed. Verify immediately after uploading and
# record the result in the audio CSV's soundhub provenance columns; never infer
# submission state from a later bucket listing.
# ---------------------------------------------------------------------------

SOUNDHUB_PROJECT_NAME = "UC Nature Sentinel Site Acoustics"
SOUNDHUB_PROJECT_SHORT_NAME = "UCNature-SSN"   # hyphenated: avoids S3 quoting issues
SOUNDHUB_BUCKET = "casoundhub"
SOUNDHUB_UPLOAD_PREFIX = "upload"
SOUNDHUB_REGION = "us-east-2"

# Only bird recorders go to SoundHub. Bat (BT) audio is destined for NABat and
# is deliberately excluded everywhere in this pipeline.
SOUNDHUB_DEVICE_TYPE = "BD"

SOUNDHUB_STAGING_DEFAULT = Path.home() / "cassn" / "soundhub" / "s3_upload_staging"

# Level 5 is the FLAC default. Level 8 buys ~5-10% more compression for a large
# encode-time cost; lossless either way, so the audio is identical.
FLAC_COMPRESSION_LEVEL = 5

# Deployment Template sheet of templates/SoundHub_Metadata_Template.xlsx, in
# sheet order. Every name except project_short_name is also an AUDIO_FIELDS
# column, so the export is a projection rather than a remapping.
SOUNDHUB_DEPLOYMENT_FIELDS = [
    "project_short_name", "deployment_id", "subproject", "subproject_design",
    "placename", "longitude", "latitude", "date_installed",
    "deployment_start_date", "deployment_start_time",
    "deployment_end_date", "deployment_end_time",
    "frequency", "duration", "gain",
    "filter_type_khz", "filter_type_duration", "filter_type_amplitude",
    "ARU_make", "ARU_model", "ARU_container", "ARU_microphone",
    "feature_type", "feature_type_details", "ARU_status", "mounted_on",
    "sensor_height_meters", "recorded_by", "notes",
]

# recording.csv carries the per-file timestamps SoundHub cannot parse out of our
# renamed files. Fields follow the example Brian sent 2026-06-18 and accepted on
# 2026-06-23: start/end carry a UTC offset, unlike the deployment dates/times.
SOUNDHUB_RECORDING_FIELDS = ["filename", "deployment_id", "start", "end"]

# "2026-05-11 00:00:00-07:00" — space-separated date and time, offset retained.
# Rendered with ``datetime.isoformat(sep=SOUNDHUB_DATETIME_SEPARATOR)`` rather
# than strftime, because ``%z`` emits "-0700" without the colon. Brian's example
# rendered the offset Postgres-style ("-8"); "-07:00" parses identically in
# Postgres and in Python's fromisoformat, so it is the safer of the two.
SOUNDHUB_DATETIME_SEPARATOR = " "

# ---------------------------------------------------------------------------
# QC check descriptions (single source of truth, embedded into qc_report.json)
# ---------------------------------------------------------------------------

QC_CHECK_DESCRIPTIONS: dict[str, str] = {
    "hash_verification": (
        "Per-file hash verification: SHA-256 and SHA-1 are computed before copy, "
        "then again after copy, and both must match. Mismatches abort the copy "
        "and remove the destination file."
    ),
    "file_size_floor": (
        "Files copied below device-aware floors are flagged as possible corrupted writes: "
        "images <500 KB, bird AudioMoth WAVs <1 GB, and bat AudioMoth WAVs <500 KB."
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
        "RECONYX burst/event grouping integrity: each event_num should have exactly "
        "sequence_total frames, positions should be sequential, timestamps within each "
        "named event should be tightly clustered, and event numbers should be contiguous."
    ),
    "temporal_plausibility": (
        "Recorded timestamps should fall within the deployment window. Flags files dated "
        "before deployment start, after the collection date, and clock-reset clusters "
        "(many files at the same second)."
    ),
    "coordinate_validation": (
        "Coordinates and elevation (from plots.csv) should be non-null and fall within the "
        "California study-area bounding box and elevation range. Catches unset (0,0) "
        "coordinates, a blank or non-numeric elevation, and values baked into the lookup "
        "table that land outside the expected region."
    ),
    "wi_image_split": (
        "Before the first Box upload, camera folders above the Wildlife Insights "
        "15,000-image limit are split into verified, burst-preserving numbered parts."
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
        "On-demand end-to-end check: compare each local raw_data file's stored SHA-1 "
        "(or compute it for older sessions) against the SHA-1 Box reports for the same file. "
        "Box computes SHA-1 server-side from the stored bytes, so a match proves the file "
        "on Box matches the file captured at local ingest."
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
    "session_health": (
        "Each session.json found in staging is parsed at app launch. Truncated or malformed "
        "files are flagged so they can be repaired before being silently lost."
    ),
    "pre_departure": (
        "Aggregate readiness check before closing or switching deployments: all devices "
        "complete, fixity check run, Box upload done, no QC errors."
    ),
    "lookup_snapshot": (
        "Lookup/config snapshot: copies the currently loaded lookup tables into "
        "qc/lookup_snapshot/ so regenerated metadata can be tied to the exact "
        "site, plot, camera, ARU, SoundHub, and Wildlife Insights configuration used."
    ),
}
