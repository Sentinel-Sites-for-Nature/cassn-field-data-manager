# CA-SSN Field Data Manager

A Python desktop application for downloading, uploading, and managing wildlife image and audio data. Built for the California Sentinel Sites for Nature (CASSN) program — standardized biodiversity data collected with camera traps and acoustic recorders across California reserves and partner organizations.

![Version](https://img.shields.io/badge/version-4.0-blue)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## Features

- **Guided Workflow**: Step-by-step interface for SD card download and cloud storage upload across multi-plot, multi-device deployments
- **Standardized File Naming**: Files renamed to a consistent convention (`ORG_SITE_plotN_DEVTYPE_YYYYMMDD_SEQNO.ext`) for all devices. Camera images additionally encode trigger event and burst position (`EVENTNO_POS`) so photos from the same trigger are grouped.
- **Per-file Metadata Generation**: Two CSVs written per deployment: `image_file_metadata.csv` (camera trap files) and `audio_file_metadata.csv` (AudioMoth recordings and config files). See the Metadata Schema section below for full field lists.
- **Deployment Records**: Deployment configuration and file manifest saved as JSON for each session.
- **Data Provenance**: Each metadata row records app version, processing timestamp, and Box upload status (uploader + datetime). Provenance CSVs are automatically re-uploaded to Box after the upload completes.
- **Data Integrity**: SHA-256 and SHA-1 checksums recorded for each file. SHA-256 is the primary archival checksum; SHA-1 supports fast comparison with Box-reported hashes.
- **AudioMoth Parsing**: Recording schedule, gain, filter cutoff, and sample rate extracted from CONFIG.TXT; per-file battery voltage, temperature, and gain parsed from WAV comment headers.
- **Reconyx MakerNote Parsing**: Sequence position, trigger type (Motion/Time-lapse), sequence total, ambient temperature, moon phase, battery voltage, and battery type extracted directly from Reconyx HYPERFIRE HP4K EXIF MakerNote via ExifTool.
- **Device Identification**: Physical device IDs recorded per file. Camera serial numbers sourced from `cameras.csv`, AudioMoth device IDs parsed from CONFIG.TXT.
- **Timestamps**: `recorded_datetime` stored as ISO 8601 with UTC offset (e.g. `2025-12-04T15:48:05-08:00`), sourced from EXIF for cameras and AudioMoth filename for audio; DST-aware via `zoneinfo`
- **Cloud Storage**: Automatic upload to Box with progress tracking and OAuth token refresh
- **Multi-Format File Support**: Images (JPG, PNG, TIF, RAW), audio (WAV, MP3, FLAC)
- **Box-Synced Lookup Tables**: Site, plot, camera, and ARU metadata are synced from your organization's Box `app_config` folder on launch and cached locally, so every install runs against the same authoritative tables.
- **Wildlife Insights Export**: Generates deployment CSVs formatted for upload to Wildlife Insights from `image_file_metadata.csv`, using camera metadata from `cameras.csv` and `wi_config.json`.
- **SoundHub-Ready Audio Metadata**: `audio_file_metadata.csv` fields map directly to SoundHub deployment template columns — gain, filter cutoff (kHz), recording schedule, ARU hardware setup — so no field renaming is needed at submission time.
- **Session Persistence**: Interrupted downloads resume automatically. Previously copied files are skipped and sequence/event numbering continues correctly.

## Installation

### Prerequisites

- Python 3.9 or higher
- pip package manager

### Install Dependencies

```bash
pip install -r requirements.txt
```

Reconyx MakerNote extraction also requires the **ExifTool** command-line program
(`PyExifTool` is only a Python wrapper around it). Install the binary separately:

```bash
# macOS
brew install exiftool
# Debian/Ubuntu
sudo apt install libimage-exiftool-perl
```

If ExifTool isn't available the app still runs — it simply skips the Reconyx
extras (temperature, moon phase, battery) and the ExifTool sequence fallback.

### Download

Clone this repository:

```bash
git clone https://github.com/Sentinel-Sites-for-Nature/cassn-field-data-manager.git
cd cassn-field-data-manager
```

### Configure Box Credentials

Box credentials live in `~/.cassn_config/`, a hidden folder in your home
directory, outside the repo so they are never accidentally committed to version
control. The app also caches synced lookup tables and Box tokens here.

Create the folder and config file:

```bash
mkdir -p ~/.cassn_config
cp config.json.example ~/.cassn_config/config.json
```

Edit `~/.cassn_config/config.json` and add your Box application credentials and
folder IDs:

```json
{
  "box": {
    "client_id": "YOUR_BOX_CLIENT_ID",
    "client_secret": "YOUR_BOX_CLIENT_SECRET",
    "field_data_folder_id": "YOUR_CASSN_FIELD_DATA_FOLDER_ID",
    "app_config_folder_id": "YOUR_CASSN_APP_CONFIG_FOLDER_ID"
  }
}
```

- **`field_data_folder_id`** — the Box folder uploads are written to.
- **`app_config_folder_id`** — the Box folder the app syncs lookup tables *from*
  on launch (sites, plots, cameras, ARUs, and the JSON config files).

Lock down the folder permissions so only you can read it:

```bash
chmod 700 ~/.cassn_config
chmod 600 ~/.cassn_config/config.json
```

**To get Box credentials:**
1. Go to https://app.box.com/developers/console
2. Create a new app (Custom App → OAuth 2.0)
3. Copy the Client ID and Client Secret
4. Navigate to each Box folder and copy its ID from the URL (e.g. `https://app.box.com/folder/123456789` → folder ID is `123456789`)

> For a managed program, the organization admin typically provides a ready-made
> `config.json` (client credentials + the two folder IDs) over a secure channel.
> Treat it like a password — never commit it or place it in a shared cloud folder.

## Usage

### 1. Box Authentication (First Time Setup)

After configuring `~/.cassn_config/config.json`, authenticate with Box
using the utility script:

```bash
python utils/box_auth_setup.py
```

Follow the prompts to:
- Open the Box authorization URL in your browser
- Grant access to the application
- Paste the full redirect URL back into the terminal

This creates or refreshes `~/.cassn_config/box_tokens.json`, which enables
automatic cloud uploads and lookup-table sync. No manual copy step is required.
For detailed Box utility documentation, see [`utils/README.md`](utils/README.md).
Box tokens expire after ~60 days of inactivity — re-run the command above to
refresh them.

### 2. Run the Application

```bash
python -m cassn
```

On launch the app syncs the latest lookup tables from your Box `app_config`
folder into `~/.cassn_config/lookup_tables/`. The first launch requires an
internet connection; afterward the cached tables are reused if Box is
unreachable (with a confirmation prompt showing when they were last synced).

### 3. Workflow

For a sequential workflow diagram, see [`docs/workflow.md`](docs/workflow.md).

#### Step 1: Deployment Metadata
- Select your organization (driven by the synced `program_config.json`)
- Choose the reserve/site from dropdown (auto-complete enabled)
- Enter deployment start and end dates
- Select who is downloading the data
- Check which devices (ML, SA, BD, BT) for each plot
- Configure local staging location (default: `~/Desktop/CASSN_field_data_staging`)
- Enable/disable automatic Box upload

#### Step 2: Collect SD Card Data
- Insert SD card for each device
- Select the device from the list
- Click "Select SD Card & Copy Files" to copy, rename, and hash all files to local staging
- Repeat for all devices

#### Step 3: Review & Finalize
- Review deployment summary
- View file counts and sizes by device
- Files automatically upload to Box (if enabled)
- Re-run Box verification checks on demand from the "Re-run QC Checks" group
- Open staging folder to verify
- Start new deployment or exit

### Screenshots

#### Deployment Metadata Entry
Enter deployment information, select devices, and configure storage location.

![Deployment Metadata Entry](screenshots/01-metadata-entry.png)

#### SD Card Data Collection
Copy files from SD cards with automatic renaming and metadata extraction.

![SD Card Data Collection](screenshots/02-data-collection.png)

#### Review & Upload to Box
View deployment summary and upload to Box cloud storage.

![Review & Upload](screenshots/03-review-upload.png)

## Output Structure

The application creates an organized folder structure in your staging location:

```
ORG_SITE_YYYYMMDD/
├── deployment_event_record.json        # Deployment event record (devices, file count, dates)
├── image_file_metadata.csv             # Per-file metadata for all camera trap images
├── audio_file_metadata.csv             # Per-file metadata for all AudioMoth recordings
├── qc/                                 # QC / audit sidecars (travel to Box with the data)
│   ├── qc_report.json                  # Full audit trail of every QC check
│   ├── box_upload_manifest.json        # Pre-upload manifest (reconciliation source)
│   ├── box_upload_verification.json    # Post-upload Box reconciliation report
│   ├── deployment_summary.txt          # Human-readable rollup of the deployment
│   └── lookup_snapshot/                # Lookup/config files snapshotted at metadata generation time
├── WI_metadata/                        # Wildlife Insights deployment CSVs
│   ├── wildlife_insights_ML_deployments.csv
│   └── wildlife_insights_SA_deployments.csv
└── raw_data/
    ├── plot1_ML/                       # Plot 1, Medium-Large camera
    │   ├── UC_Bodega_plot1_ML_20260303_00001_1.jpg   # Trigger event 1, photo 1
    │   ├── UC_Bodega_plot1_ML_20260303_00001_2.jpg   # Trigger event 1, photo 2
    │   ├── UC_Bodega_plot1_ML_20260303_00002_1.jpg   # Trigger event 2, photo 1
    │   └── ...
    ├── plot1_BD/                       # Plot 1, Bird recorder
    │   ├── UC_Bodega_plot1_BD_20260303_00001.wav
    │   └── ...
    └── ...
```

### Wildlife Insights Deployment CSV

At the end of each session, the app automatically generates deployment CSVs formatted for upload to Wildlife Insights from `image_file_metadata.csv`, saved to `WI_metadata/` within the deployment folder. One CSV is produced per camera device type (ML, SA). Requires `cameras.csv` and `wi_config.json` in the synced lookup tables.

In Wildlife Insights deployment CSVs, latitude and longitude are written with exactly eight digits after the decimal point. Shorter values are padded with trailing zeroes and longer values are rounded to eight places, satisfying Wildlife Insights' requirement of four to eight decimal places without modifying the source lookup values.

## Metadata Schema

### `image_file_metadata.csv`

One row per camera trap file (images and associated files). Fields map directly to Wildlife Insights deployment and image columns.

| Field | Description |
|---|---|
| `filename` | Standardized filename assigned by the app |
| `original_filename` | Original filename from SD card |
| `deployment_event_id` | Deployment event identifier (`ORG_SITE_YYYYMMDDend`) |
| `deployment_id` | Per-device deployment ID (`ORG_SITE_plotN_DEVTYPE_YYYYMMDDend`) |
| `organization`, `site`, `site_full_name`, `site_code` | Site identifiers |
| `start_date`, `end_date` | Deployment start and end dates |
| `recorded_by` | Observer who downloaded the data |
| `subproject`, `subproject_design`, `placename`, `event_name`, `event_description` | WI deployment descriptors |
| `plot_number`, `device_type`, `camera_id`, `file_type` | Per-device identity |
| `file_size_bytes`, `file_hash_sha256`, `file_hash_sha1` | File properties and integrity hashes. SHA-256 is the primary archival checksum; SHA-1 supports comparison with Box-reported file hashes. |
| `recorded_datetime` | ISO 8601 datetime with UTC offset; sourced from EXIF |
| `latitude`, `longitude` | Plot coordinates from `plots.csv` |
| `camera_make`, `camera_model` | Camera manufacturer and model from EXIF |
| `sequence_trigger_type`, `sequence_event_num`, `sequence_position`, `sequence_total` | Reconyx sequence data from MakerNote |
| `temperature_c`, `moon_phase`, `battery_voltage`, `battery_voltage_avg`, `battery_type` | Reconyx MakerNote extras (via ExifTool) |
| `project_id`, `bait_type`, `bait_description`, `event_type`, `quiet_period`, `camera_functioning` | Wildlife Insights fields from `wi_config.json` |
| `feature_type`, `feature_type_methodology`, `sensor_height`, `height_other`, `sensor_orientation`, `orientation_other` | WI deployment setup from `cameras.csv` |
| `plot_treatment`, `plot_treatment_description`, `detection_distance` | WI plot fields from `cameras.csv` |
| `app_version`, `processing_datetime` | Processing provenance |
| `is_uploaded_to_box`, `box_uploader`, `box_upload_datetime` | Box upload provenance |
| `is_uploaded_to_pelican`, `pelican_uploader`, `pelican_upload_datetime` | Pelican transfer provenance |
| `is_submitted_to_wi`, `wi_submitter`, `wi_submission_datetime` | WI submission provenance |
| `notes` | Free text |

### `audio_file_metadata.csv`

One row per AudioMoth file (WAV recordings and CONFIG.TXT files). Fields map directly to SoundHub deployment template columns.

| Field | Description |
|---|---|
| `filename`, `original_filename` | Standardized and original filenames |
| `deployment_event_id`, `deployment_id` | Deployment identifiers |
| `organization`, `site`, `site_full_name`, `site_code` | Site identifiers |
| `deployment_start_date`, `deployment_end_date`, `recorded_by` | Deployment context |
| `subproject`, `subproject_design`, `placename`, `event_name`, `event_description` | SoundHub deployment descriptors |
| `plot_number`, `device_type`, `device_id`, `file_type` | Per-device identity |
| `file_size_bytes`, `file_hash_sha256`, `file_hash_sha1` | File properties and integrity hashes. SHA-256 is the primary archival checksum; SHA-1 supports comparison with Box-reported file hashes. |
| `recorded_datetime` | ISO 8601 datetime with UTC offset; sourced from AudioMoth filename |
| `latitude`, `longitude` | Plot coordinates from `plots.csv` |
| `ARU_make`, `ARU_model` | Hardcoded `AudioMoth`; model from CONFIG.TXT firmware string |
| `sample_rate_hz` | From WAV header or CONFIG.TXT |
| `gain` | Recording gain from WAV comment or CONFIG.TXT |
| `filter_type_khz` | High-pass filter cutoff in kHz (blank for BD) |
| `battery_voltage`, `temperature_c` | From AudioMoth WAV comment |
| `date_installed`, `deployment_start_time`, `deployment_end_time` | From CONFIG.TXT recording schedule |
| `frequency`, `duration` | Recording schedule from CONFIG.TXT |
| `filter_type_duration`, `filter_type_amplitude` | Trigger filter settings from CONFIG.TXT |
| `feature_type`, `feature_type_details`, `ARU_container`, `ARU_microphone`, `mounted_on`, `sensor_height_meters`, `ARU_status` | SoundHub physical setup from `ARUs.csv` and `soundhub_config.json` |
| `app_version`, `processing_datetime` | Processing provenance |
| `is_uploaded_to_box`, `box_uploader`, `box_upload_datetime` | Box upload provenance |
| `is_uploaded_to_pelican`, `pelican_uploader`, `pelican_upload_datetime` | Pelican transfer provenance |
| `is_submitted_to_soundhub`, `soundhub_submitter`, `soundhub_submission_datetime` | SoundHub submission provenance |
| `is_submitted_to_nabat`, `nabat_submitter`, `nabat_submission_datetime` | NABat submission provenance |
| `notes` | Free text |

## Device Types

- **ML**: Medium-Large Animal Camera
- **SA**: Small Animal Camera
- **BD**: Acoustic Recorder (Birds)
- **BT**: Acoustic Recorder (Bats)

## Data Quality Checks

The app runs a layered set of QC checks at every stage of data movement —
during SD card copy, at device completion, before/after Box upload, and on
demand from buttons in the GUI. Every check writes a pass/warning/error
entry to `qc/qc_report.json` in the deployment folder, which travels to Box
with the data and serves as the audit trail.

### Reading `qc_report.json`

The file has two top-level sections:

```json
{
  "generated": "<ISO timestamp>",
  "current_state": [
    // One entry per (check, device) pair — latest result wins.
    // Sorted: errors first, then warnings, then passes.
    // Per-file detail entries (file_hash_mismatch, file_hash_missing) are
    // automatically dropped here once a later passing file_hash_verification_run
    // supersedes them.
  ],
  "history": {
    "session_checks": [...],   // all session-level entries, chronological
    "devices": {
      "p1_ML": [...],          // per-device entries, chronological
      "p1_BD": [...]
    }
  }
}
```

**Read `current_state` first** to see where things stand right now. Errors
appear at the top of the array; everything below is warnings then passes.
The `history` section is the full append-only log if you want to dig into
when something happened or what was tried.

Every entry has the same shape:

```json
{
  "check": "file_hash_verification_run",
  "description": "On-demand: re-hashes every file in raw_data/...",
  "severity": "pass",            // "pass" | "warning" | "error"
  "message": "Fixity check complete: 60 files checked. 0 hash mismatch(es), 0 file(s) missing from disk.",
  "timestamp": "2026-05-07T06:50:03"
}
```

The `description` is a plain-English explanation of what the check is testing,
so the file is self-documenting — you don't need to come back to this README
to interpret it.

### All checks at a glance

| Check (qc_report key) | Description | When it runs | Where it's reported |
|---|---|---|---|
| `hash_verification` | SHA-256 and SHA-1 are computed at the source, then again at the destination after copy. Mismatch aborts the file and removes the destination. | Automatic, every SD card copy (per file) | Log panel + critical popup on mismatch; one aggregate entry per device in `qc_report.json` |
| `file_size_floor` | Flags copied media files below device-aware floors: images under 500 KB, bird AudioMoth WAVs (`BD`) under 1 GB, and bat AudioMoth WAVs (`BT`) under 500 KB. Config `.txt` files are excluded. | Automatic, every SD card copy (per file) | Log panel + warning summary at device completion; entry per device in `qc_report.json` |
| `duplicate_detection` | Session-wide set of SHA-256 hashes. New file whose hash matches one already in inventory is deleted and skipped. | Automatic, every SD card copy (per file) | Log panel + per-duplicate entry plus a per-device aggregate in `qc_report.json` |
| `expected_file_count` | Source SD card is auto-counted (filtering hidden/system files) and silently compared against the **total inventoried files for the device**. Resume-safe — partial first-attempts plus completing-attempts compare correctly against source. | Automatic, after each SD card copy | Log panel + warning popup on mismatch; per-device entry in `qc_report.json` |
| `sequence_gap` | Per camera device: validates app-assigned RECONYX event grouping. Each event should have `sequence_total` frames, observed positions should be sequential and start at 1, timestamps within an event should increase with adjacent frames no more than 2 seconds apart, and event numbers should be contiguous. | Automatic at device completion | Log panel + per-device entry in `qc_report.json` |
| `temporal_plausibility` | Per device: flags files recorded before deployment start, files dated after the collection (deployment end) date, and clock-reset clusters (≥3 files at the same second). | Automatic at device completion | Log panel + per-device entry in `qc_report.json` |
| `coordinate_validation` | Plot coordinates (from `plots.csv`) must be non-null and fall within the California study-area bounding box. Catches unset (0,0) coordinates and values baked into the lookup table that land outside the expected region. | Automatic before CSV generation | Log panel + session-level entry in `qc_report.json` |
| `lookup_snapshot` | Copies the lookup/config tables in use into `qc/lookup_snapshot/` (with a manifest) so regenerated metadata can be tied to the exact site, plot, camera, ARU, SoundHub, and WI configuration used. | Automatic at metadata generation | `qc/lookup_snapshot/` + session-level entry in `qc_report.json` |
| (pre-upload manifest) | Before Box upload starts, writes `qc/box_upload_manifest.json` listing every uploadable file with its SHA-256 and SHA-1 when available. Used by the post-upload check below. | Automatic, immediately before Box upload | Sidecar file in deployment folder |
| `box_upload` | After upload, recursively lists the Box deployment folder and reconciles against the pre-upload manifest. Each file gets up to 3 retries (single-file failures don't abort the whole batch). | Automatic, after every Box upload | Upload progress panel + session-level entry in `qc_report.json` |
| `file_hash_verification_run` | Compares each expected Box raw-data file's stored local SHA-1 against Box's server-side SHA-1. For older sessions without stored SHA-1, the app computes SHA-1 from the local file. Lookup uses the complete deployment-relative path, so both flat (`raw_data/device/file`) and WI-split (`raw_data/device/device_N/file`) layouts compare correctly without filename collisions. | Automatic after successful Box upload; manual button on Tab 3 ("Verify Box ↔ Local Hashes") | Post-upload verification progress panel + detail popup + log panel + session-level entry in `qc_report.json`. Individual mismatches recorded as `file_hash_mismatch` entries. |
| `box_verify` | Calls the Box API, recursively lists the deployment folder, and reconciles complete deployment-relative paths against local inventory. Flat legacy sessions and nested WI-split sessions are both supported. Known deployment-metadata files (`*_metadata.csv`, `deployment_event_record.json`, `qc_report.json`, `wildlife_insights_*.csv`, `*_manifest.json`) are auto-whitelisted as expected extras. | Automatic after successful Box upload; manual button on Tab 3 ("Verify Box Upload") | Post-upload verification progress panel + detail popup + log panel + session-level entry in `qc_report.json` |
| `session_health` | At app launch, every `session.json` in the staging root is parsed. Truncated or malformed files are surfaced in the Open Deployment dialog as "⚠ CORRUPTED" rather than being silently hidden. | Automatic at app launch and "Open Different Deployment…" | Resume dialog entry + per-deployment `qc_report.json` |
| `pre_departure` | Aggregate readiness check before closing or switching deployments: all devices complete, file-hash verification run, Box upload done, and no current QC errors. Reads `current_state` of `qc_report.json` so resolved errors don't trigger false alarms. | Automatic on window close, "Start New Deployment", and "Open Different Deployment…" | Modal dialog with ✓/⚠ items + session-level entry in `qc_report.json` |
| (session summary) | Plain-text rollup at session close: dates, device counts, per-device file totals, plot coordinates, and QC pass/warning/error counts. | Automatic at session close | `qc/deployment_summary.txt` in deployment folder |

### Per-device check coverage

Not every check applies to every file type:

| Check | Image (ML, SA) | Audio (BD, BT) | Config (`.txt`) |
|---|---|---|---|
| `hash_verification` | ✓ | ✓ | ✓ |
| `file_size_floor` | >500 KB | BD >1 GB; BT >500 KB | — |
| `duplicate_detection` | ✓ | ✓ | ✓ |
| `expected_file_count` | ✓ | ✓ | counted |
| `sequence_gap` (RECONYX bursts) | ✓ | — | — |
| `temporal_plausibility` | ✓ | ✓ | — |

Session-level checks (`coordinate_validation`, `lookup_snapshot`, `box_upload`,
`box_verify`, `file_hash_verification_run`, `session_health`, `pre_departure`)
cover all files regardless of type.

### Sidecar files written to the deployment folder

Beyond `qc_report.json`, the QC system creates these files in the deployment
folder (all upload to Box with the rest of the data, except as noted):

| File | Written by | Uploaded to Box? | Purpose |
|---|---|---|---|
| `qc/qc_report.json` | every QC entry | yes | full audit trail of every check run |
| `qc/box_upload_manifest.json` | pre-upload manifest step | yes | reconciliation source for post-upload check |
| `qc/box_upload_verification.json` | Box verify step | yes | reconciliation report from the Box-verify check |
| `qc/deployment_summary.txt` | session-close summary | yes | human-readable rollup of the deployment |
| `qc/lookup_snapshot/` | metadata generation | yes | copy of the lookup/config tables used, with a manifest |

Historical deployments may contain a local-only
`raw_data/<device>/<device>_manifest.json`. New ingestions no longer create this
redundant sidecar; existing files are preserved for compatibility.

### Resilience features

A few cross-cutting behaviors that aren't single QC checks but support them:

- **Resume mid-deployment.** Every 10 files, `session.json` is rewritten with the latest inventory. If the SD card disconnects mid-copy or the app crashes, "Open Deployment" finds the in-progress session, restores state, skips already-copied files (matched by original filename + hash), cleans up partial writes, and continues.
- **Box upload retry.** Each file gets up to 3 upload attempts before being recorded as a per-file failure. Single-file failures no longer abort the whole batch — re-running the upload skips already-uploaded files and retries only the failures.
- **Filename collision safety.** File reconciliation and fixity checks key by the complete deployment-relative storage path, not filename alone. This distinguishes both devices and numbered WI split folders. CONFIG sidecars include `plotN` in their filename (`UC_QuailRidge_plot1_BD_<DATE>_CONFIG_01.txt`) so each plot's config is uniquely identifiable.
- **Migration of legacy reports.** When a `qc_report.json` from before the `current_state`/`history` schema is opened, it's migrated automatically — old entries become `history`, and `current_state` is computed fresh.

## Configuration

### Lookup Tables

The app runs against a set of lookup tables and config files that define your
sites, plots, cameras, ARUs, and export defaults:

- `sites.csv` — site/reserve names and codes
- `plots.csv` — plot names, numbers, and coordinates
- `cameras.csv` — camera serial numbers and Wildlife Insights metadata per plot
- `ARUs.csv` — per-plot ARU physical setup (mount, sensor height, status)
- `wi_config.json` — Wildlife Insights project IDs and upload defaults
- `soundhub_config.json` — ARU hardware defaults (make, model, microphone, containers)
- `program_config.json` — organization label(s) and observer names for the dropdowns

**How the app gets them:** on launch the app downloads these files from your
Box `app_config` folder (`app_config_folder_id` in `config.json`) into
`~/.cassn_config/lookup_tables/` and reloads them. This keeps every install on
the same authoritative tables — to update them, edit the files in the Box
`app_config` folder and relaunch. If Box is unreachable, the cached copies are
reused after a confirmation prompt; if no cache exists yet, the app stops and
asks you to connect to the internet.

### Example / template files

The repo includes tracked example versions in `example_lookups/` to document
the expected schema and to seed your Box `app_config` folder:

- `example_lookups/sites.csv`
- `example_lookups/plots.csv`
- `example_lookups/cameras.csv`
- `example_lookups/wi_config.json`
- `example_lookups/soundhub_config.json`
- `example_lookups/ARUs.csv`

Field notes:

- **`cameras.csv`**: Camera serial numbers and Wildlife Insights metadata per plot. Columns include `camera_id` (physical serial number), `feature_type` (e.g. `Road dirt`, `Trail game`), `sensor_height`, `sensor_orientation`, `plot_treatment`, `plot_treatment_description`, and `detection_distance`.
- **`wi_config.json`**: Wildlife Insights project IDs and upload defaults. Edit `project_id_ML` and `project_id_SA` to match your project IDs in Wildlife Insights.
- **`soundhub_config.json`**: Static ARU hardware defaults that apply to all deployments — `ARU_make`, `ARU_model`, `ARU_microphone`, container types, sample rates, and schedule. Values are copied into `audio_file_metadata.csv` at processing time.
- **`ARUs.csv`**: One row per `(site_code, plot_number, device_type)` recording physical ARU setup — `mounted_on`, `sensor_height_meters`, `ARU_status`. Add a row for each ARU before or after processing.

> The standalone CLI tools in `utils/` (e.g. `generate_wi_deployments.py`) read
> their lookup tables from a repo-local `local_data/` folder instead of the
> Box-synced cache. See [`utils/README.md`](utils/README.md) for details.

## Development

### Project Structure

```
cassn-field-data-manager/
├── cassn/                            # Application package — run with `python -m cassn`
│   ├── __main__.py                   # Entry point: wires config, lookups, and the GUI together
│   ├── config.py                     # Paths, thresholds, schema field lists, QC descriptions
│   ├── lookups.py                    # Lookup-table loaders + LookupTables container
│   ├── box/                          # Box auth, client, and upload/verify threads
│   ├── core/                         # Classification, metadata extraction, inventory, QC
│   ├── export/                       # Wildlife Insights / metadata CSV writers
│   └── gui/                          # PySide6 wizard UI
├── utils/                            # Standalone CLI tools (see utils/README.md)
│   ├── box_auth_setup.py             # Box OAuth authentication utility
│   ├── generate_wi_deployments.py    # Wildlife Insights deployment CSV generator
│   ├── generate_occurrences.py       # Wildlife Insights occurrences CSV generator
│   ├── convert_to_flac.py            # WAV → FLAC batch converter
│   └── verify_flac_conversion.py     # FLAC conversion integrity check
├── example_lookups/                  # Tracked example lookup tables (schema reference / seed)
│   ├── sites.csv
│   ├── plots.csv
│   ├── cameras.csv
│   ├── wi_config.json
│   ├── soundhub_config.json
│   └── ARUs.csv
├── assets/                           # Logos / icon used by the UI
├── screenshots/                      # Application screenshots for this README
├── docs/                             # Supplementary docs (workflow diagram)
├── config.json.example               # Box config template → ~/.cassn_config/config.json
├── requirements.txt                  # Python dependencies
├── .gitignore
└── README.md                         # This file
```

Operational data lives outside the repo: credentials, Box tokens, and the
synced lookup-table cache are kept in `~/.cassn_config/`, and deployments are
staged under `~/Desktop/CASSN_field_data_staging/` by default.
