# CA-SSN Field Data Manager

A Python desktop application for downloading, uploading, and managing wildlife image and audio data. Built for the California Sentinel Sites for Nature (CA-SSN) program — standardized biodiversity data collected with camera traps and acoustic recorders across California reserves and partner organizations.

![Version](https://img.shields.io/badge/version-4.0-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)

## Features

- **Guided Workflow**: Step-by-step interface for SD card download and cloud storage upload across multi-plot, multi-device deployments
- **Parallel Card Ingestion**: Copy one to four device cards concurrently. Each card has independent progress, speed/ETA, warnings, cancellation, and a rolling slot that becomes reusable as soon as that card is safely checkpointed.
- **Standardized File Naming**: Files renamed to a consistent convention (`ORG_SITE_plotN_DEVTYPE_YYYYMMDD_SEQNO.ext`) for all devices. Camera images additionally encode trigger event and burst position (`EVENTNO_POS`) so photos from the same trigger are grouped.
- **Per-file Metadata Generation**: Two CSVs written per deployment: `image_file_metadata.csv` (camera trap files) and `audio_file_metadata.csv` (AudioMoth recordings and config files). See the Metadata Schema section below for full field lists.
- **Deployment Records**: Deployment configuration and file manifest saved as JSON for each session.
- **Data Provenance**: Each metadata row records app version, processing timestamp, and Box upload status (uploader + datetime). Provenance CSVs are automatically re-uploaded to Box after the upload completes. Local metadata regeneration preserves upload/submission history and notes when deployment ID, filename, and SHA-256 match. If a record with history cannot be matched safely, regeneration stops before replacing either CSV and identifies the conflicting file; resolve that correction explicitly before retrying.
- **Data Integrity**: SHA-256 and SHA-1 checksums recorded for each file. SHA-256 is the primary archival checksum; SHA-1 supports fast comparison with Box-reported hashes.
- **AudioMoth Parsing**: Recording schedule, gain, filter cutoff, and sample rate extracted from CONFIG.TXT; per-file battery voltage, temperature, and gain parsed from WAV comment headers.
- **Reconyx MakerNote Parsing**: Sequence position, trigger type (Motion/Time-lapse), sequence total, ambient temperature, moon phase, battery voltage, and battery type extracted directly from Reconyx HYPERFIRE HP4K EXIF MakerNote via ExifTool.
- **Device Identification**: Event identity and dates come from `deployment_events.csv`; each card is bound to one exact monitoring interval in `deployments.csv`. AudioMoth IDs read from the card are used when present and the deployment row supplies the fallback when CONFIG.TXT is absent.
- **Timestamps**: `recorded_datetime` stored as ISO 8601 with UTC offset (e.g. `2025-12-04T15:48:05-08:00`), sourced from EXIF for cameras and GUANO for AudioMoth WAVs; audio without GUANO timestamps falls back to the original filename interpreted as local Pacific time
- **Cloud Storage**: Upload to Box with progress tracking and OAuth token refresh. Transfers are started by hand from the grouped action menus on the final tab — nothing uploads on its own.
- **Multi-Format File Support**: Images (JPG, PNG, TIF, RAW), audio (WAV, MP3, FLAC)
- **Lookup Authority**: Manually curated `deployment_events.csv` and `deployments.csv` are distributed with the complete Box `app_config` snapshot. Every app installation validates the snapshot before replacing its offline cache.
- **Wildlife Insights Export**: Generates deployment CSVs formatted for upload to Wildlife Insights from `image_file_metadata.csv`, using event-scoped camera metadata from `deployments.csv` and defaults from `wi_config.json`.
- **Wildlife Insights Image Batching**: At camera ingest completion, folders above 15,000 images are silently organized into verified, burst-preserving numbered parts, ready for direct local upload to WI. Interrupted preparation is resumable.
- **SoundHub-Ready Audio Metadata**: Projects `audio_file_metadata.csv` into SoundHub deployment and recording CSVs with the required field names and units, including gain, filter cutoff (kHz), recording schedule, and ARU hardware setup.
- **Wildlife SoundHub Submission**: Bird audio is transcoded to lossless FLAC into a local tree mirroring SoundHub's S3 bucket, with `deployment.csv` and `recording.csv` projected from `audio_file_metadata.csv`, then uploaded and verified. Box keeps the original WAVs untouched. See the Wildlife SoundHub Preparation section.
- **Session Persistence**: Interrupted downloads resume automatically. Concurrent workers checkpoint through one session writer; previously copied files are skipped and sequence/event numbering continues correctly.
- **Immutable NDP Source Versioning**: OSDF object paths are never reused. Media
  corrections are versioned per logical file, while deployment inventories and
  manifests advance together and are published only after verified transfer.
  See [CASSN NDP source versioning](NDP_SOURCE_VERSIONING.md).

## Installation

These steps set up CA-SSN Field Data Manager from this repository. On macOS,
the Dock launcher runs this checkout using its `.venv` Python environment.
Keep both in a permanent location: the launcher depends on them and uses the
current code each time it starts. Earlier standalone macOS builds bundled their
own application copy; these instructions describe the current repository and
Dock launcher setup.

### Prerequisites

- Git
- Python 3.10 or higher, with `pip` and the `venv` module
- macOS for the Dock launcher and SD-card mount/eject helpers

### Download

Clone the repository in the location where you intend to keep it:

```bash
git clone https://github.com/Sentinel-Sites-for-Nature/cassn-field-data-manager.git
cd cassn-field-data-manager
```

Run the remaining commands from this repository root.

### Create the Python Environment and Install Dependencies

```bash
python3 --version                     # Must be Python 3.10 or higher
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

If `python3` names an older installation, use your Python 3.10-or-higher
executable to create `.venv` instead. The app launcher always uses
`.venv/bin/python`. The commands below name that interpreter explicitly, so
activating the environment is optional.

### External Tools for Specific Workflows

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

Preparing bird audio for Wildlife SoundHub requires the **FLAC** encoder. It is
only needed for that step; without it the rest of the app is unaffected.

```bash
# macOS
brew install flac
# Debian/Ubuntu
sudo apt install flac
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
- **`app_config_folder_id`** — the canonical Box folder containing sites,
  plots, JSON defaults, `deployment_events.csv`, and `deployments.csv`.

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
.venv/bin/python utils/box_auth_setup.py
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

From the repository root:

```bash
.venv/bin/python -m cassn
```

#### Install the macOS Dock Launcher

After setting up `.venv` and authenticating with Box, install the clickable
launcher:

```bash
.venv/bin/python utils/install_macos_app.py
```

The installer creates **CA-SSN Field Data Manager.app** in `~/Applications`
and command symlinks in `~/.local/bin` for `cassn-app`, `cassn-clear-staging`,
`cassn-mount-sd`, and `cassn-eject-sd`. Open the `.app` in Finder, then
right-click its Dock icon and choose **Options → Keep in Dock**. Adding `--open`
to the installer command also starts the app after installation.

The `.app` calls `cassn-app`, which changes to this repository and runs
`.venv/bin/python -m cassn`. It uses the project environment even when Conda or
another Python installation is active. Finder/Dock launches write diagnostics to
`~/Library/Logs/CA-SSN Field Data Manager/launcher.log`.

To run the installed commands by name in Terminal, add `~/.local/bin` to your
`PATH`. For the default macOS zsh shell, add this line to `~/.zprofile` and open
a new Terminal window; running the line also updates the current Terminal:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

With `PATH` configured, launch from any directory:

```bash
cassn-app
```

If you move the repository later, recreate `.venv` at the new location and rerun
the installer there to refresh the command links. To refresh an existing launcher
in another location, supply the same directory with `--install-dir`, for example
`--install-dir /Applications`; that directory must be writable by your user.

On launch the app first downloads the complete Box `app_config` snapshot to
temporary files. It validates `deployment_events.csv` and `deployments.csv`
together, validates their canonical event, site, plot, sequence, and interval
relationships, and only then
replaces the local cache. A fresh authenticated installation therefore
bootstraps entirely from Box. If Box is unavailable, the app continues only
when the complete local cache is valid and shows an offline-cache warning. If
neither source is valid, startup stops with instructions to reconnect or repair
the curated lookup files. Legacy camera/ARU files are never loaded.

### Validate curated lookup data

Before distributing a manually edited lookup snapshot, run the read-only
validator from the repository root:

```bash
.venv/bin/python utils/validate_curated_lookups.py /path/to/lookup_directory
```

The directory must contain the complete runtime snapshot, including
`sites.csv`, `plots.csv`, `deployment_events.csv`, `deployments.csv`, and the
required JSON configuration files. The validator
checks schemas, canonical event IDs and dates, site/plot/sequence relationships,
and deployment interval ordering. It never
copies, installs, or publishes files. Distribution to Box remains a deliberate
manual operation.

### 3. Workflow

#### Parallel deployment windows

Use **File → New Window** (**⌘N** on macOS, **Ctrl+N** on Windows) to work on a
separate deployment. Each window has its own deployment selection, inventory,
card jobs and progress. **Deployment → Open Deployment in New Window…** opens
the saved-session picker in another window. A deployment event and source card
can each be used by only one window at a time.

Card ingest can continue in one window while another uploads. Box and SoundHub
operations run one at a time across windows, through verification and provenance
updates. Closing an idle window leaves other windows and their jobs running;
an active window must finish or cancel its jobs before it can close. Relaunching
the app brings its existing window forward; use **File → New Window** to add
another deployment. An older running version without window reactivation must
be brought forward through the Python icon in the Dock or Mission Control.

#### Step 1: Deployments and devices
- Select your organization (driven by the synced `program_config.json`)
- Choose the formal site name from the dropdown; the stable short name and acronym fill automatically
- Choose a completed deployment event; its read-only dates and deployed devices load automatically
- Review the separate read-only summary of devices still deployed in the field
- Select who is downloading the data
- Check which devices (ML, SA, BD, BT) for each plot
- Configure local staging location (default: `~/Desktop/CASSN_field_data_staging`)

#### Step 2: Card ingest
- Insert SD card for each device
- Select the device from the list
- Click "Select card & copy" to copy, rename, and hash all files to local staging
- Repeat for all devices

#### Step 3: Review & upload
- Review deployment summary
- View file counts and sizes by device
- At ingest completion, oversized camera folders are already prepared in Wildlife Insights batches of at most 15,000 images
- Start transfers by hand from the grouped action menus at the bottom of the tab.
  Nothing uploads on its own:
  - **Uploads** — Box upload, and the two-step SoundHub path (prepare, then upload)
  - **QC Checks** — Box verification, Box↔local hashes, camera timezone checks, SoundHub verification
  - **Deployment** — open either staging folder, switch deployment, start a new one
- Start new deployment or exit

### Screenshots

#### Deployments and devices
Choose a site and curated deployment event to populate deployment metadata, select devices, and configure the staging location.

![Deployments and devices with event metadata populated from lookup tables](screenshots/01-metadata-entry.png)

#### Card ingest
Monitor device status and individual card jobs while files are copied, renamed, and verified; adjust simultaneous cards and expand activity details as needed.

![Card ingest showing completed and active card jobs, concurrency controls, and activity details](screenshots/02-data-collection.png)

#### Review & upload
Review the collection summary and staged-file tree, start transfers and verification from the grouped menus, and monitor Box upload progress.

![Review and upload showing the collection summary, staged deployment tree, action menus, and Box upload progress](screenshots/03-review-upload.png)

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
    ├── p1_ML/                          # Plot 1, Medium-Large camera
    │   ├── UC_Bodega_plot1_ML_20260303_00001_1.jpg   # Flat when ≤15,000 images
    │   └── ...
    ├── p2_ML/                          # Oversized camera folder (>15,000 images)
    │   ├── p2_ML_1/                    # Numbered WI batch, ≤15,000 images
    │   │   └── ...
    │   └── p2_ML_2/                    # Trigger bursts are never split between batches
    │       └── ...
    ├── p1_BD/                          # Plot 1, Bird recorder
    │   ├── UC_Bodega_plot1_BD_20260303_00001.wav
    │   └── ...
    └── ...
```

### Wildlife Insights Deployment CSV

At the end of each session, the app automatically generates deployment CSVs formatted for upload to Wildlife Insights from `image_file_metadata.csv`, saved to `WI_metadata/` within the deployment folder. One CSV is produced per camera device type (ML, SA). Requires event-scoped camera fields in `deployments.csv` and defaults in `wi_config.json`.

In Wildlife Insights deployment CSVs, latitude and longitude are written with exactly eight digits after the decimal point. Shorter values are padded with trailing zeroes and longer values are rounded to eight places, satisfying Wildlife Insights' requirement of four to eight decimal places without modifying the source lookup values.

After each ML or SA card finishes copying and hash verification, the app silently
prepares that device's local image folders before marking ingestion complete.
Folders with at most 15,000 images stay flat. Larger folders are moved into
`<device>_1`, `<device>_2`, and subsequent numbered subfolders, with each trigger
burst kept together and every part held at or below 15,000 images. These folders
can be uploaded directly from local disk to Wildlife Insights without a Box upload.

The resulting structure is verified and session inventory paths are saved before
completion. Cancellation or an error leaves ingestion incomplete and safe to retry;
after a crash, moved image locations are recovered before copying resumes. Other
devices can continue ingesting concurrently. Existing Box-upload history prevents
automatic reorganization. The first Box upload retains a preparation check for
older unsplit sessions without upload history.

Box preparation and upload wait for all card jobs to finish. Card ingestion is
disabled throughout the Box upload cycle, including provenance updates and
post-upload verification, so the local inventory and folder layout stay stable.
Manual Box and hash checks also wait for card jobs and prevent switching,
resetting, or closing the deployment until their result is handled. Background
callbacks are checked against their worker, operation, and deployment; stale
callbacks cannot update a different deployment's results.

## Wildlife SoundHub Preparation

Bird (BD) audio is submitted to [Wildlife SoundHub](https://wildlifesoundhub.org/),
which runs BirdNET over it and provides the validation tooling. Bat (BT) audio is
**not** part of this path — it is destined for NABat.

The operator starts the SoundHub workflow after ingest:

- **Box keeps the WAVs**, uncompressed and unmodified, as the archival original.
- **SoundHub gets a FLAC transcode** plus two metadata CSVs, staged locally in a
  tree that mirrors the S3 bucket exactly, then pushed.

FLAC is lossless — decoding returns bit-identical audio — so the WAVs are kept for
a different reason: `flac --keep-foreign-metadata` carries the AudioMoth RIFF
comment across (it lands in an APPLICATION block tagged `riff`) but **does not
preserve the GUANO chunk**. Until that is solved, the WAV on Box is the only copy
holding the full original metadata.

### Bucket layout

```text
s3://casoundhub/upload/UCNature-SSN/deployment.csv
s3://casoundhub/upload/UCNature-SSN/recording.csv
s3://casoundhub/upload/UCNature-SSN/<deployment_id>/<filename>.flac
```

Both CSVs sit at the **project** root and combine deployments in the **current
pending submission batch**. After a completed batch is cleared, the next batch
starts fresh. Local staging mirrors the S3 layout without path rewriting.

Two properties of the SoundHub account shape everything here:

| Property | Consequence |
|---|---|
| The IAM role has `PutObject` and `ListBucket` but **no `DeleteObject`** | A key written to the wrong place can only be removed by SoundHub staff by hand. Deployment ids are read from `audio_file_metadata.csv` and validated, never re-derived from folder names. |
| SoundHub **drains** the `upload/` landing zone once it ingests a submission | An empty prefix is not evidence of a failed upload. Verify immediately after uploading; the durable record lives in the `is_submitted_to_soundhub` / `soundhub_submitter` / `soundhub_submission_datetime` columns, never in a later bucket listing. |

### The two CSVs

Both project measured recording facts from `audio_file_metadata.csv`; export
does not reread the audio. Staging fills legacy blank placement/protocol fields
from current curated lookups and defaults, and recomputes ARU status from stop
reasons without modifying the source CSV.

**`deployment.csv`** — one row per SoundHub deployment, in the column order of the
Deployment Template sheet of `templates/SoundHub_Metadata_Template.xlsx`. It uses
the source rows with the enrichment above, with `ARU_status` exported as
`aru_status` and `metal_pole` normalized to `pole`. `feature_type` and `aru_status`
columns are always included; absent features use the template value `None`.
Dates and times carry **no** UTC offset.

Deployment `aru_status` is `low voltage` if any source recording for that recorder
reports it, including header-only recordings excluded from the upload; otherwise
it is `functioning`, a default that does not certify complete recording coverage.

Note that one CA-SSN deployment *event* (`UC_StrathearnRanch_20260714`) contains
several SoundHub *deployments* — one per plot's recorder
(`UC_StrathearnRanch_plot1_BD_20260714`) — each with its own S3 folder and its own
row here.

**`recording.csv`** — one row per FLAC, carrying the per-file timestamps SoundHub
cannot otherwise obtain. SoundHub's standard ingest reads recording times out of
the original AudioMoth filename (`20251222_161300.WAV`), but CA-SSN renames files
earlier in the pipeline to a convention whose date component is the deployment
*retrieval* date — identical for every file in a deployment. The sidecar CSV
resolves this.

| Column | Source |
|---|---|
| `path` | `<deployment_id>/<staged filename>.flac`, relative to the project root |
| `sample_rate` | Measured `sample_rate_hz`, in Hz; missing values block staging |
| `deployment_id` | Straight from `audio_file_metadata.csv` |
| `start` | Stored `recorded_datetime` — GUANO timestamp preferred, otherwise the original AudioMoth filename interpreted as local Pacific time |
| `end` | `start` plus `recording_duration_sec` |

Legacy staging fragments must be regenerated from their source audio metadata
before uploading with this schema. Rebuilding refuses old headers instead of
silently dropping fields.

Unlike the deployment dates, `start` and `end` **do** carry a UTC offset:
`2026-05-11 00:00:00-07:00`.

### Preparing and uploading from the app

Both steps are on the **Review & Finalize** tab and are operator-initiated.

1. **Uploads → Add Bird Audio to SoundHub Staging…** transcodes the open
   deployment event's BD WAVs to FLAC (level 5, `--verify`,
   `--keep-foreign-metadata`) and adds or refreshes its rows in the cumulative
   local staging batch. Existing staged events remain, the two project CSVs are
   rebuilt, and nothing is uploaded. Source WAVs are never touched. Progress is
   per-file and the job is cancellable; completed files are kept, so re-running
   resumes. A copy of both CSVs is also written to the event's `soundhub/`
   subfolder so the metadata travels to Box alongside the raw data.
2. **Uploads → Upload Bird Data to SoundHub** preflights the whole waiting
   batch, shows its exact scope for confirmation, uploads only recordings not
   already recorded as submitted on Box, verifies them immediately, then stamps
   the exact Box rows and writes the submission report.

**QC Checks → Check SoundHub Landing Zone** is a diagnostic for the currently
pending batch. Successful uploads are verified automatically; a later empty
landing zone is expected after SoundHub ingests the batch.

### Preparing previously-downloaded deployments

`utils/prep_soundhub.py` covers the backlog — deployments ingested before this step
existed. It shares the app's code, so a deployment prepared here is identical to one
prepared in the GUI.

```bash
# One deployment
python utils/prep_soundhub.py stage --deployment "/path/to/UC_QuailRidge_20260108"

# Every deployment under a season folder
python utils/prep_soundhub.py stage --root "/path/to/2026"

# Review what is staged and where it will land
python utils/prep_soundhub.py status

# Validate exact staged-to-Box provenance coverage without writing anything
python utils/prep_soundhub.py upload --preflight-only

# Push, verify, update Box provenance, and write the submission report
python utils/prep_soundhub.py upload
```

Staging is idempotent: an existing FLAC is left alone and a re-staged deployment
replaces its own rows in the project CSVs rather than duplicating them. Uploads
skip objects already present at the same size, so an interrupted transfer is
finished by simply running the command again. Both GUI and backlog uploads map
each staged FLAC to its exact Box `audio_file_metadata.csv` row before S3 is touched.
After immediate S3 verification it stamps `is_submitted_to_soundhub`,
`soundhub_submitter`, and `soundhub_submission_datetime` on those rows only and
writes the same Markdown submission report into the `soundhub/` folder of every
affected Box deployment event. Completed and pending recordings are never
allowed in the same submission batch.

Staging is cumulative **within one submission batch**, not across completed
submissions. Once every staged recording is recorded as submitted, the GUI
refuses to add another deployment to those completed manifests. After SoundHub
acceptance is confirmed, use the dry-run-first maintenance command to clear only
the derived local FLAC staging copy and begin a fresh batch:

```bash
python utils/prep_soundhub.py clear-completed
python utils/prep_soundhub.py clear-completed --apply
```

Cleanup removes the local project mirror and its `.cassn_fragments` rebuild
inputs. It never changes Box, S3, source WAVs, or event-local submission reports.

The submitter defaults to `Imperato, John`. The Box year folder is inferred from
the staged deployment IDs and the standard Box Drive location—2027 deployments
automatically resolve to `data/2027`; CLI overrides remain available for
a different submitter or Box location. Mixed-year staging is rejected so each
submission has one unambiguous Box source root.

### Configuration

AWS credentials are **not** read by the app — boto3 resolves them from the standard
chain (`~/.aws/credentials`, environment, instance role). Confirm the right identity
before a first push:

```bash
aws sts get-caller-identity
```

The rest is optional, under a `soundhub` block in `~/.cassn_config/config.json`.
Omitted keys fall back to the constants in `cassn/config.py`:

```json
{
  "soundhub": {
    "staging_root": "/Users/YOU/cassn/soundhub/s3_upload_staging",
    "bucket": "casoundhub",
    "upload_prefix": "upload",
    "project_short_name": "UCNature-SSN",
    "region": "us-east-2",
    "aws_profile": null
  }
}
```

Project-level metadata (objectives, taxonomy, sensor layout, admin contact) is
entered once in the SoundHub web interface and is **not** submitted as a file.

## Metadata Schema

### `image_file_metadata.csv`

One row per camera image, including archival/QC fields and inputs to the Wildlife Insights export.

| Field | Description |
|---|---|
| `filename` | Standardized filename assigned by the app |
| `original_filename` | Original filename from SD card |
| `deployment_event_id` | Closed deployment-event identifier (`ORG_SITE_YYYYMMDDend`); blank while the event is open. Legacy `OPEN_...` and `INFERRED_...` placeholders are not valid identifiers. |
| `deployment_id` | Per-device deployment ID (`UC_SITE_plotN_DEVTYPE_YYYYMMDDend`). It is assigned only after retrieval/closure; open placements deliberately leave this field blank. |
| `organization`, `site_name`, `site_short_name`, `site_code` | Formal site name, stable relational/deployment-ID token, and acronym |
| `start_date`, `end_date` | Curated camera placement dates, expanded to `00:00:00` and `23:59:59` respectively |
| `recorded_by` | Observer who downloaded the data |
| `subproject`, `subproject_design`, `placename`, `event_name`, `event_description` | WI deployment descriptors |
| `plot_number`, `device_type`, `camera_id`, `file_type` | Plot/device/file identity; `camera_id` is the selected curated placement's physical device ID |
| `camera_serial_exif` | Hardware serial read from camera EXIF via ExifTool; blank when unavailable; retained separately from curated `camera_id` |
| `file_size_bytes`, `file_hash_sha256`, `file_hash_sha1` | File properties and integrity hashes. SHA-256 is the primary archival checksum; SHA-1 supports comparison with Box-reported file hashes. |
| `recorded_datetime` | New images: original camera wall clock interpreted in America/Los_Angeles, with date-specific UTC offset; blank for invalid, nonexistent, or ambiguous capture times. Existing session rows are preserved. |
| `recorded_datetime_utc`, `camera_effective_offset` | Resolved UTC capture time (`Z`) and the effective Pacific offset; blank when unresolved |
| `camera_datetime_original`, `camera_datetime_source`, `camera_offset_time_original` | Unmodified selected EXIF clock string, its tag (`DateTimeOriginal`, or `DateTime` fallback), and reported offset |
| `camera_timestamp_source`, `camera_timestamp_status`, `camera_timestamp_severity` | `pacific_local_v1` resolution policy/version, QC reason codes, and pass/warning/error. Offset disagreements, missing/malformed offsets, and DateTime fallback are warnings; matching offsets do not establish clock accuracy. |
| `latitude`, `longitude`, `elevation_m` | Plot coordinates and elevation (metres) from `plots.csv` |
| `camera_make`, `camera_model` | Camera manufacturer and model from EXIF |
| `sequence_trigger_type`, `sequence_position`, `sequence_total` | Camera trigger type and burst position/total from Reconyx metadata |
| `sequence_event_num` | App-assigned trigger-event number within the device's inventory |
| `temperature_c`, `moon_phase`, `battery_voltage`, `battery_voltage_avg`, `battery_type` | Reconyx MakerNote extras (via ExifTool) |
| `project_id`, `bait_type`, `bait_description`, `event_type`, `quiet_period`, `camera_functioning` | Wildlife Insights fields from `wi_config.json` |
| `feature_type` | Camera placement feature from curated `deployments.csv` |
| `sensor_height`, `sensor_orientation` | Camera protocol constants from `wi_config.json` |
| `feature_type_methodology`, `height_other`, `orientation_other`, `plot_treatment`, `plot_treatment_description`, `detection_distance` | Reserved WI columns; currently blank |
| `app_version`, `processing_datetime` | App version and processing timestamp (UTC, ISO 8601) |
| `is_uploaded_to_box`, `box_uploader`, `box_upload_datetime` | Box upload provenance |
| `is_uploaded_to_osdf`, `osdf_uploader`, `osdf_upload_datetime` | Verified OSDF publication provenance; independent of the transfer client |
| `is_submitted_to_wi`, `wi_submitter`, `wi_submission_datetime` | WI submission provenance |
| `notes` | Free text |

### `audio_file_metadata.csv`

One row per AudioMoth recording or CONFIG.TXT sidecar. SoundHub exports BD recording rows using the mappings above; CONFIG rows have no capture timestamp or recording outcome.

| Field | Description |
|---|---|
| `filename`, `original_filename` | Standardized and original filenames |
| `deployment_event_id`, `deployment_id` | Deployment identifiers |
| `organization`, `site_name`, `site_short_name`, `site_code` | Formal site name, stable relational/deployment-ID token, and acronym |
| `deployment_start_date`, `deployment_end_date` | CONFIG.TXT first/last recording dates; otherwise the device's earliest/latest recorded-file dates; blank if neither is available |
| `recorded_by` | Observer who downloaded the data |
| `subproject`, `subproject_design`, `placename`, `event_name`, `event_description` | SoundHub deployment descriptors |
| `plot_number`, `device_type`, `device_id`, `file_type` | Per-device identity; `device_id` is the physical recorder serial, preferring device-reported identity over the curated placement fallback |
| `file_size_bytes`, `file_hash_sha256`, `file_hash_sha1` | File properties and integrity hashes. SHA-256 is the primary archival checksum; SHA-1 supports comparison with Box-reported file hashes. |
| `recorded_datetime` | ISO 8601 datetime; GUANO timestamp with the device's configured UTC offset preferred, otherwise original filename interpreted as local Pacific time; blank when unavailable |
| `latitude`, `longitude`, `elevation_m` | Plot coordinates and elevation (metres) from `plots.csv` |
| `ARU_make`, `ARU_model`, `ARU_firmware` | Distinct manufacturer, hardware model, and firmware fields from device metadata; missing make/model may use `soundhub_config.json` defaults |
| `sample_rate_hz` | WAV header sample rate in Hz, falling back to CONFIG.TXT and then the device-type protocol default |
| `gain` | Recording gain from WAV comment or CONFIG.TXT |
| `filter_type_khz` | Available high-/low-pass cutoff from the WAV comment, otherwise CONFIG.TXT, converted to kHz; blank if absent |
| `battery_voltage`, `temperature_c` | Battery voltage (V) and temperature (°C), preferring GUANO over the WAV comment |
| `date_installed` | Curated recorder placement start date |
| `deployment_start_time`, `deployment_end_time` | CONFIG.TXT schedule bounds, falling back to device-type protocol defaults |
| `frequency`, `duration` | CONFIG.TXT schedule frequency (`daily`) and total scheduled recording time (`HH:MM`), with protocol defaults when missing; `duration` is not a file's measured length |
| `recording_duration_sec` | Actual WAV duration from RIFF headers, rounded to whole seconds; blank when unavailable or on CONFIG rows; used to calculate SoundHub recording end time |
| `recording_stop_reason` | Device-reported stop/cancel reason parsed from the WAV comment; blank when no reason is parsed |
| `filter_type_duration` | Minimum trigger duration in seconds, from the WAV comment before CONFIG.TXT |
| `filter_type_amplitude` | Bare numeric threshold, from the WAV comment before CONFIG.TXT; firmware determines whether its scale is percent or a raw 16-bit value |
| `mounted_on`, `sensor_height_meters` | Survey123 ARU placement from device-level `deployments.csv` |
| `feature_type`, `ARU_container`, `ARU_microphone` | Protocol/hardware values from `soundhub_config.json`; absent feature defaults to literal `None` |
| `feature_type_details` | Reserved; currently blank |
| `ARU_status` | `low voltage` only when the normalized stop reason is exactly `low voltage`; otherwise `functioning`, including a missing reason. This default does not certify recording completeness. Blank for CONFIG rows |
| `app_version`, `processing_datetime` | App version and processing timestamp (UTC, ISO 8601) |
| `is_uploaded_to_box`, `box_uploader`, `box_upload_datetime` | Box upload provenance |
| `is_uploaded_to_osdf`, `osdf_uploader`, `osdf_upload_datetime` | Verified OSDF publication provenance; independent of the transfer client |
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
| `camera_timezone_consistency` | Camera clocks are expected to show current local Pacific time. Compares original EXIF capture time and offset against `America/Los_Angeles`. New processing resolves local civil time and flags reported-offset disagreements as warnings. Invalid, nonexistent, or ambiguous capture times remain unresolved errors. Historical mismatches remain errors until separately corrected. | Automatic at camera device completion; manual **QC Checks → Check Camera Timezones**; re-evaluated for readiness | One per-device result with counts and example filenames in the card panel, log, and `qc_report.json`. Original EXIF clock and offset, resolved UTC, and QC status are retained in new session rows and image CSVs. Exports and session resume do not recalculate old rows. Images are never corrected by this check; verified copying and Box backup remain available. |
| `coordinate_validation` | Plot coordinates and elevation (from `plots.csv`) must be non-null and fall within the California study-area bounding box and elevation range (-100 m to 4,500 m). Catches unset (0,0) coordinates, a blank or non-numeric `elevation_m`, and values baked into the lookup table that land outside the expected region. | Automatic before CSV generation | Log panel + session-level entry in `qc_report.json` |
| `lookup_snapshot` | Copies the lookup/config tables in use into `qc/lookup_snapshot/` (with a manifest) so regenerated metadata can be tied to the exact site, plot, camera, ARU, SoundHub, and WI configuration used. | Automatic at metadata generation | `qc/lookup_snapshot/` + session-level entry in `qc_report.json` |
| `wi_image_split` | Enforces the Wildlife Insights 15,000-image folder limit. Oversized ML/SA folders are split without cutting trigger bursts, structurally verified, and synchronized back to session inventory. | Silent at camera ingest completion; first-Box-upload fallback for older sessions | Per-device entry in `qc_report.json` during ingest; existing preparation panel and session-level QC entry for the upload fallback |
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

Session-level checks (`coordinate_validation`, `lookup_snapshot`,
`wi_image_split`, `box_upload`, `box_verify`, `file_hash_verification_run`,
`session_health`, `pre_departure`) cover all files regardless of type.

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

- **Resume mid-deployment.** During copying, inventory checkpoints are saved to local `session.json` about every 30 seconds and at card completion. If the SD card disconnects or the app crashes, "Open Deployment" restores the saved session and resumes by source-relative file path. Already-inventoried files are skipped when their staged copies exist and match the recorded size when available; missing or size-inconsistent copies are restored. This resume check does not re-hash every existing copy.
- **Resume WI preparation.** Each image move is atomic. If preparation is cancelled, completed moves remain in place and their exact nested paths are saved in `session.json`; retrying the card ingest finishes the remaining moves. After a crash, actual image locations are recovered before copying resumes. Older sessions can also resume preparation through the first-upload fallback. Ingest completion and Box upload require successful preparation and saved inventory paths.
- **Box upload retry.** Each file gets up to 3 upload attempts before being recorded as a per-file failure. Single-file failures no longer abort the whole batch — re-running the upload skips already-uploaded files and retries only the failures.
- **Filename collision safety.** File reconciliation and fixity checks key by the complete deployment-relative storage path, not filename alone. This distinguishes both devices and numbered WI split folders. CONFIG sidecars include `plotN` in their filename (`UC_QuailRidge_plot1_BD_<DATE>_CONFIG_01.txt`) so each plot's config is uniquely identifiable.
- **Migration of legacy reports.** When a `qc_report.json` from before the `current_state`/`history` schema is opened, it's migrated automatically — old entries become `history`, and `current_state` is computed fresh.

## Configuration

### Lookup Tables

The app runs against authoritative site/plot references, manually curated
deployment intervals, and export defaults:

- `sites.csv` — `site_name,site_short_name,site_code`, where the values are the formal name, stable relational/deployment-ID token, and acronym
- `plots.csv` — plot names, numbers, coordinates, and hand-entered `elevation_m`, joined to sites by `site_short_name`
- `deployment_events.csv` — canonical event ID, site, `deployment_event_start_date`, and `deployment_event_end_date`; this is the sole runtime authority for event naming and dates
- `deployments.csv` — one curated row per monitoring interval, joined to events by `deployment_event_id` and to sites by `site_short_name`; `device_id` holds the physical camera or AudioMoth serial, while ARU `asset_tag` preserves the four-digit field label
- `wi_config.json` — Wildlife Insights project IDs and upload defaults
- `soundhub_config.json` — ARU hardware defaults (make, model, microphone, containers)
- `program_config.json` — organization label(s) and observer names for the dropdowns

**How the app gets them:** Maintainers curate `deployment_events.csv` and
`deployments.csv`, validate the complete directory with
`utils/validate_curated_lookups.py`, and deliberately place the accepted files
in Box `app_config`. Box is the distributed current snapshot for all
installations; `~/.cassn_config/lookup_tables/` is only a validated offline
cache. Startup synchronizes Box before constructing `LookupTables`.
`devices.csv`, `cameras.csv`, `ARUs.csv`, and Survey123 exports are not runtime
fallbacks.

> Standalone maintenance tools may use explicitly supplied local inputs. See
> [`utils/README.md`](utils/README.md) for each tool's lookup behavior.

## Development

Create `.venv` using the [installation steps](#installation), then install the
application and development dependencies into that same environment. Run the
test suite with its Python interpreter and warnings treated as errors:

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q -W error
```

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
│   ├── ndp/                          # NDP source manifest, staging, and Pelican transfer
│   ├── soundhub/                     # SoundHub FLAC staging, CSV export, S3 upload
│   └── gui/                          # PySide6 wizard UI
├── utils/                            # Standalone CLI tools (see utils/README.md)
│   ├── box_auth_setup.py             # Box OAuth authentication utility
│   ├── clear_box_verified_staging.py # Box-verified local staging cleanup
│   ├── install_macos_app.py          # Dock-ready macOS launcher installer
│   └── prep_soundhub.py              # SoundHub FLAC staging + S3 upload
├── assets/                           # Application and macOS launcher icons
├── schemas/                          # Published machine-readable data contracts
├── screenshots/                      # Application screenshots for this README
├── templates/                        # SoundHub reference workbook
├── tests/                            # Regression tests and compatibility fixtures
├── NDP_SOURCE_VERSIONING.md          # Append-only OSDF source versioning rules
├── config.json.example               # Box config template → ~/.cassn_config/config.json
├── requirements.txt                  # Python dependencies
├── requirements-dev.txt              # Application dependencies plus test tooling
├── .gitignore
└── README.md                         # This file
```

Operational data lives outside the repo: credentials, Box tokens, and the
synced lookup-table cache are kept in `~/.cassn_config/`, and deployments are
staged under `~/Desktop/CASSN_field_data_staging/` by default.
