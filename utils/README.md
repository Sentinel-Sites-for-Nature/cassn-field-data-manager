# CA-SSN Field Data Manager — Utilities

Helper scripts for maintenance and data recovery tasks.

---

## `box_auth_setup.py`

Runs the Box OAuth setup flow and writes reusable Box tokens to
`~/.cassn_credentials/box_tokens.json`.

### When to use

Use this script the first time you connect `cassn_field_data_manager.py` to Box,
or any time your saved Box tokens stop working and need to be refreshed.

### Requirements
```bash
pip install box-sdk-gen
```

You also need `~/.cassn_credentials/config.json` with your Box app
`client_id` and `client_secret`.

To verify `box-sdk-gen` is installed:

```bash
python3 -c "import box_sdk_gen; print('box-sdk-gen ok')"
```

### Setup

1. Confirm your Box credentials file exists:
   ```bash
   ls ~/.cassn_credentials/config.json
   ```
2. If `~/.cassn_credentials/config.json` does not exist, create it from the example file:
   ```bash
   mkdir -p ~/.cassn_credentials
   cp config.json.example ~/.cassn_credentials/config.json
   ```
3. Edit `~/.cassn_credentials/config.json` and add your Box app `client_id` and `client_secret`.

### Run
```bash
python3 utils/box_auth_setup.py
```

During the OAuth flow, `box_auth_setup.py` will:

1. Open your browser to the Box authorization page.
2. Ask you to log in and grant access.
3. Ask you to paste the full redirect URL back into the terminal.
4. Exchange that authorization code for tokens.
5. Save `box_tokens.json` to `~/.cassn_credentials/`.

### Output

On success, the script writes:

```text
~/.cassn_credentials/box_tokens.json
```

That token file is then used by:

- `cassn_field_data_manager.py`
- `recover_file_metadata.py`

### Notes

- If the script finds an existing token file, it tests that connection first
- If the existing token is invalid, it falls back to a fresh OAuth flow
- Refreshed tokens are written back to `~/.cassn_credentials/box_tokens.json` automatically
- The browser may redirect to a page that does not load; that is expected
- Paste the entire redirect URL, not just the authorization code

---

## `recover_file_metadata.py`

Recovers a deployment by downloading the full Box folder to the staging drive,
then regenerating:

- `file_metadata.csv`
- `deployment_event_record.json`
- `recovery_report.json`

### When to use

Use this script when a deployment was successfully uploaded to Box but the
local metadata artifacts were missing, incomplete, or need to be rebuilt.

### Requirements
```bash
pip install Pillow box-sdk-gen
```

Pillow is required. The script fails immediately if EXIF support is unavailable.

### Setup

1. Confirm Box credentials exist in `~/.cassn_credentials/`:
   - `config.json`
   - `box_tokens.json`
   ```bash
   ls ~/.cassn_credentials/config.json ~/.cassn_credentials/box_tokens.json
   ```
2. Open [`utils/recover_file_metadata.py`](/Users/johnimperato/GitHub/cassn-field-data-manager/utils/recover_file_metadata.py) and confirm the hard-coded recovery root matches your machine:
   - `RECOVERY_ROOT = Path("/Volumes/G-DRIVE ArmorATD/cassn-field-data-staging")`
   - Change this path if you want recovered deployments written somewhere else
   ```bash
   rg -n 'RECOVERY_ROOT' utils/recover_file_metadata.py
   ```
3. Confirm that recovery path exists and is writable on your machine.
   ```bash
   ls "/Volumes/G-DRIVE ArmorATD/cassn-field-data-staging"
   ```
4. Find the Box deployment folder ID from the Box URL:
   - `https://app.box.com/folder/123456789012`
   - Use the top-level deployment folder ID, not the `raw_data` subfolder ID
5. Confirm local plot metadata exists for label recovery:
   ```bash
   ls local_data/plots.csv
   ```

### Run
```bash
python3 utils/recover_file_metadata.py BOX_FOLDER_ID
```

Replace `BOX_FOLDER_ID` with the numeric deployment folder ID from Box. 

The script recovers exactly one deployment folder per run.

Progress is printed to the terminal as files are downloaded and processed.

Example terminal output during a live recovery run:

![Recovery progress](../screenshots/04-recover-file-metadata-progress.png)

### Output

The script creates a local recovery folder under:

```text
/Volumes/G-DRIVE ArmorATD/cassn-field-data-staging/<deployment-folder-name>/
```

That recovery folder contains:

- the downloaded Box deployment contents
- `file_metadata.csv`
- `deployment_event_record.json`
- `recovery_report.json`

If the deployment folder already exists locally, the script fails and does not overwrite it.

### Notes

- Authenticates using `~/.cassn_credentials/box_tokens.json` — no
  separate authentication step needed
- Downloads the entire deployment and preserves the Box folder structure
- Uses Box modified time for the recovered `timestamp` field
- Sets unrecoverable fields such as `original_filename` and `source_path` to `NA`
- Writes recovered outputs locally only; it does not upload `file_metadata.csv`
  or `deployment_event_record.json` back to Box
- Writes `recovery_report.json` even when the run completes with failures
- Uses `local_data/plots.csv` for plot-label lookup; `example_lookups/` files are templates only

---

## `generate_wi_deployments.py`

Generates Wildlife Insights deployment CSVs from CASSN deployment folders.

You can use it two ways:
- scan Box for deployment folders and generate WI CSVs in bulk, then upload them back to Box
- or process one local deployment folder for testing

For each deployment event, the script writes two CSVs into a `WI_metadata/`
subfolder:

- `wildlife_insights_ML_deployments.csv` — one row per ML (parallel) camera plot
- `wildlife_insights_SA_deployments.csv` — one row per SA (downward) camera plot

### When to use

- Run this after a deployment has already been uploaded to Box and you need the
  WI deployment CSVs.
- Use the local mode if you want to test one deployment folder before doing a
  broader backfill.
- If WI CSVs already exist in a deployment folder, the script skips that folder
  unless you use `--force`.

### Requirements

```bash
pip install box-sdk-gen
```

No extra dependencies beyond `box-sdk-gen`.

### Setup

This script depends on two local files in `local_data/`. These are gitignored on
purpose because they contain project IDs and camera serial numbers you do not
want committed.

#### 1. `local_data/cameras.csv`

Maps each site + plot + camera type to the camera used there, plus the few WI
fields this script needs. Create it from the example:

```bash
cp example_lookups/cameras.csv local_data/cameras.csv
```

#### 2. `local_data/wi_config.json`

Stores WI project IDs and a few deployment-level defaults. Copy the template and
fill in your values:

```bash
cp example_lookups/wi_config.json local_data/wi_config.json
```

To find your WI project ID: log in to [app.wildlifeinsights.org](https://app.wildlifeinsights.org),
open the project, and copy the number from the URL:
`wildlifeinsights.org/manage/projects/XXXXX`

### Run — Box mode (all deployments)

```bash
python3 utils/generate_wi_deployments.py
```

Traverses the Box deployment folders, downloads the deployment JSON files it
needs, generates the WI CSVs, and uploads them back to a `WI_metadata/`
subfolder in Box.

### Run — local mode (single deployment)

```bash
python3 utils/generate_wi_deployments.py --local PATH
```

Processes one local deployment folder. Writes WI CSVs to a `WI_metadata/` subfolder
inside that folder. Useful for testing before running against Box.

Example:

```bash
python3 utils/generate_wi_deployments.py --local '/Volumes/G-DRIVE ArmorATD/2026/UC_QuailRidge_20260108'
```

### Output

For each deployment event, the script creates or updates:

```text
<deployment-folder>/
└── WI_metadata/
    ├── wildlife_insights_ML_deployments.csv
    └── wildlife_insights_SA_deployments.csv
```

### Notes

- Authenticates using `~/.cassn_credentials/box_tokens.json` — no separate authentication step needed
- Only downloads `deployment_event_record.json` from Box; media files are never downloaded
- Audio device types (`BD`, `BT`) are ignored
- Missing `wi_config.json` causes the script to fail before writing output
- Missing `cameras.csv` causes warnings and blank camera-specific fields
- Existing WI CSVs are skipped unless you use `--force`

---

## `generate_occurrences.py`

Joins pre-processing file metadata (`file_metadata.csv`) with post-processing Wildlife Insights results (`images.csv`) to produce a deployment event occurrence record CSV. Each row is an animal detection with a unique file identifier, timestamp, and spatial coordinates. Blanks, humans, vehicles, and unidentified results are excluded.

### Run
```bash
python3 utils/generate_occurrences.py <file_metadata.csv> <images.csv> <output_dir>
```

### Output

```text
<output_dir>/
└── UC_SITE_YYYYMMDD_occurrences.csv
```

---

## `fix_camera_clock.py`

Corrects a camera **clock offset** on already-staged images, end to end. A camera
deployed with a wrong internal clock writes wrong timestamps into every photo's
EXIF; this rewrites the dates and keeps the deployment's QC/fixity records
consistent so the normal Box upload + hash verification still pass.

Per device, it:

1. Shifts the EXIF dates with `exiftool` (`-AllDates`, which also moves the Reconyx
   MakerNote `DateTimeOriginal`).
2. Recomputes SHA-256/SHA-1 and file size (the bytes changed, so the old hashes
   are stale).
3. Re-reads `recorded_datetime` from the corrected EXIF.
4. Patches the deployment's `session.json` file inventory.
5. Rewrites the device manifest(s) and regenerates the metadata CSVs.

The offset is fully general — any combination of
years/months/days/hours/minutes/seconds, added or subtracted — so it handles a
wrong year, a wrong month, or a few-hours timezone/DST slip.

### When to use

Use it when a camera's clock was set wrong and the photos are already staged
locally (e.g. cameras that recorded 2025 because the clock was a year early).
Audio (`BD`/`BT`) is never affected — AudioMoth datetimes come from the
GUANO/filename, not a camera clock.

> **Close the CASSN app before running.** Its autosave would otherwise overwrite
> the corrected `session.json`.

### Requirements

`exiftool` on your PATH, plus the project's normal Python environment (the script
imports the `cassn` package).

```bash
exiftool -ver        # confirm exiftool is installed
```

### Run

Always preview with `--dry-run` first — it samples the current years and reports
what would change without writing anything:

```bash
python utils/fix_camera_clock.py \
  --deployment "/Volumes/G-DRIVE ArmorATD/.../UC_Sedgwick_20260610" \
  --devices p1_ML p2_ML p3_ML p4_ML p4_SA \
  --years 1 --only-year 2025 --dry-run
```

Drop `--dry-run` to apply. Each device folder named under `--devices` lives in
`raw_data/`.

### Speeding it up on a slow/HDD staging drive — `--scratch`

The per-file EXIF rewrite + re-hash is slow on a spinning HDD's random I/O. Pass
`--scratch <fast_dir>` (e.g. an internal SSD) and the tool, per device, copies the
device folder to that scratch dir (one sequential read the HDD handles well), does
the heavy work there, and copies the corrected files back only if they changed —
then patches the inventory/manifests/CSVs exactly as the in-place run does. The
data ends up back on the staging drive, so the app uploads from there unchanged.

`--scratch` takes any fast local path with room for **one device folder at a
time**; omit it to work in place.

```bash
python utils/fix_camera_clock.py \
  --deployment "/Volumes/G-DRIVE ArmorATD/.../UC_Sedgwick_20260610" \
  --devices p1_ML p2_ML p3_ML p4_ML p4_SA \
  --years 1 --only-year 2025 --scratch /tmp/cassn_scratch
```

### Options

| Flag | Meaning |
| --- | --- |
| `--deployment` | Deployment folder (contains `session.json` and `raw_data/`). Required. |
| `--devices` | One or more device folder names under `raw_data/`, e.g. `p2_ML p4_SA`. Required. |
| `--years/--months/--days/--hours/--minutes/--seconds` | The offset. At least one must be non-zero. |
| `--subtract` | Subtract the offset (clock was **ahead**). Default adds (clock behind). |
| `--only-year YYYY` | Only shift files whose current year matches — makes re-runs safe and supports partial recovery. |
| `--scratch DIR` | Route the heavy per-file work through this fast local dir. Omit to work in place. |
| `--dry-run` | Report what would change; write nothing. |

### Notes

- `--only-year` is the idempotency guard: with `--only-year 2025`, re-running
  won't double-shift files already corrected to 2026.
- Recomputing hashes is **mandatory**, not optional — rewriting EXIF changes the
  bytes, so without the re-hash every corrected file would (correctly) fail the
  Box hash check. The script handles this for you.
- After it finishes, the staged data, inventory, manifests, and CSVs all agree;
  run the app's Box upload as normal.

---

## `split_for_wi.py`

Splits oversized **camera** device folders into Wildlife-Insights-sized batches.
WI accepts at most **15,000 images per upload**, but a `pN_ML` / `pN_SA` folder
often holds far more. This tool scans a root, finds every camera folder over the
limit, and divides its images into `<device>_1`, `<device>_2` … subfolders —
each at or under the limit — so each subfolder is one WI upload.

Per device it:

1. Reads the folder's full image inventory (loose files **plus** anything already
   in `<device>_N` parts, so it can resume a half-finished run).
2. Plans parts of ≤ 15,000, never cutting a trigger burst (`…_00001_1/_2/_3`
   stay together, so each part lands at or just under the limit).
3. **Moves** each image into its part folder (atomic same-volume rename, no
   duplicate storage). The `<device>_manifest.json` fixity sidecar is left
   untouched in the device root.

The reusable logic lives in `cassn/core/wi_split.py`; this file is just the CLI.

### When to use

As a **terminal WI-prep step, after Box upload and QC are done** for the
deployment — splitting nests the images one level deeper than the manifest / QC
steps expect. Audio (`BD`/`BT`) is never touched. Point the WI uploader at each
`<device>_N` subfolder in turn.

### Run

```bash
# Preview only — writes nothing (default):
python utils/split_for_wi.py --root "/Volumes/G-DRIVE ArmorATD/cassn-field-data-staging"

# Perform the split on one deployment:
python utils/split_for_wi.py \
  --root ".../UC_JepsonPrairie_20260423" --apply

# Put everything back:
python utils/split_for_wi.py \
  --root ".../UC_JepsonPrairie_20260423" --undo
```

### Options

| Flag | Meaning |
| --- | --- |
| `--root` | Folder to scan: a season, a single deployment, or the staging drive. Required. |
| `--limit N` | Max images per part (default `15000`). |
| `--suffixes` | Device-folder suffixes to target (default `_ML _SA`). |
| `--apply` | Perform the split. **Default is a dry-run report.** |
| `--undo` | Reverse a previous split under `--root`. |
| `--no-keep-bursts` | Allow a part boundary to fall inside a trigger burst. |
| `--yes` | Skip the confirmation prompt on `--apply` / `--undo`. |

### Notes

- **Idempotent / resumable.** Re-running `--apply` only moves images not yet in
  their planned part, so an interrupted run (crash, quit, disk sleep) is finished
  by simply running it again; a completed split is a no-op. A big move on a
  spinning HDD can take a while — that's disk speed, not a hang.
- **Reversible.** `--undo` moves every part's images back up and removes the
  emptied part folders. A round-trip restores the folder exactly.
- **Safe against re-matching.** Part folders (`p1_ML_1`) never end in `_ML` /
  `_SA`, so re-scans can't recurse into prior output.
- On **Box**, the moves sync as moves; expect Box Drive to churn through the
  re-sync. Run it while the deployment isn't being uploaded.
