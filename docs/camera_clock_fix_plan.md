# Camera clock-offset correction — plan

*Status: pending. Trigger: Sedgwick (UC_Sedgwick_20260610) cameras whose internal
clock was set one year early (recorded 2025 instead of 2026). Execute AFTER all
downloads for the site are finished. p1_ML is already corrected (EXIF only).*

## Scope (to confirm — step 1)

Suspected affected (recorded 2025, should be 2026): **p1_ML, p2_ML, p3_ML, p4_ML,
and p4_SA**. All audio (BD/BT) is unaffected — AudioMoth datetimes come from the
GUANO/filename, not a camera clock. Other SA cameras (p1–p3) believed correct (2026).

**Verification method** (don't read every photo): after all plots are staged, for
each camera folder `raw_data/p{1..4}_{ML,SA}`, sample ~20 evenly-spaced images'
`DateTimeOriginal` year. Any folder showing 2025 → affected; confirm uniformity with
a denser sample (every ~200th). Check **all** camera folders, not just the suspected
ones, so an unexpected bad clock isn't missed. Output: a confirmed affected list.

## Why this is more than an EXIF edit

The app's metadata CSVs and fixity manifests are built from the in-memory/saved
`file_inventory`, which stores each file's `recorded_datetime` AND its SHA-256/SHA-1
**as captured at original ingest** (2025 dates, hashes of the 2025 bytes).
Rewriting EXIF changes the file bytes, so after a fix the inventory is stale on
**both** counts. "Regenerate metadata" rebuilds CSVs from that stale inventory, so it
does NOT pick up corrected dates or hashes. Both must be recomputed and written back.

## The fix, end to end (per affected device, on the staged G-DRIVE copies)

1. **Shift EXIF +1 year** — the proven p1_ML method (updates EXIF *and* the Reconyx
   MakerNote, idempotent via the year guard):
   ```
   exiftool "-AllDates+=1:0:0 0:0:0" -if '$DateTimeOriginal=~/^2025/' \
            -overwrite_original -ext jpg <device_folder>
   ```
   `-AllDates` shifts DateTimeOriginal/CreateDate/ModifyDate; because the Reconyx
   MakerNote tag is also named "DateTimeOriginal", it gets shifted too. The
   `-if …2025` guard makes re-runs safe (only 2025 files move). Run on APFS staging
   while the drive is uncontended (downloads done, Box sync paused, Spotlight off).
   (Stale leftover: Reconyx `DayOfWeek` label — nothing reads it; ignore.)
2. **Recompute hashes** — for each shifted file, recompute SHA-256 + SHA-1
   (`cassn.core.hashing.sha256_sha1`).
3. **Re-derive `recorded_datetime`** from the corrected EXIF
   (`cassn.core.image_metadata.parse_camera_recorded_datetime`).
4. **Patch `file_inventory`** in the deployment's `session.json` for those files
   (update `file_hash_sha256`, `file_hash_sha1`, `recorded_datetime`).
5. **Rewrite the device manifest(s)** (`write_device_manifest`) and **regenerate the
   metadata CSVs / WI exports** (`write_metadata_outputs` +
   `generate_wi_deployments_from_image_csv`) from the patched inventory.

Steps 2–5 don't exist as a one-click action today, so this needs a small reusable
helper — propose `utils/fix_camera_clock.py` (CLI, mirroring `utils/generate_wi_deployments.py`).
It performs 1–5 and prints a report (files shifted, hashes updated, date range
before/after).

**Flexible offset (not hardcoded to +1 year).** The offset is CLI input so the tool
handles any clock error — wrong year, wrong month, or a few-hours timezone/DST slip:

```
python utils/fix_camera_clock.py \
    --deployment <deployment folder> \
    --devices p2_ML p3_ML p4_ML p4_SA \
    --years 1 [--months 0 --days 0 --hours 0 --minutes 0 --seconds 0] \
    [--subtract]        # default ADD (clock behind/slow); --subtract if AHEAD/fast
    [--only-year 2025]  # optional scope/idempotency guard
    [--dry-run]         # preview only, write nothing
```

Implementation: assemble exiftool's `Y:M:D h:m:s` shift and choose the operator —
`-AllDates+=<shift>` (add) or `-AllDates-=<shift>` (subtract). Examples:
`--years 1` → `+=1:0:0 0:0:0`; `--hours 3 --subtract` → `-=0:0:0 3:0:0`;
`--days 2 --hours 5` → `+=0:0:2 5:0:0`. exiftool handles month/year boundaries
correctly. Single-direction offsets (the normal case) are one pass; a rare mixed
offset is two runs.

- **`--only-year` / `--only-before`**: optional guard so only the wrong files are
  shifted (makes re-runs safe / supports partial recovery). Omit for a clean one-shot
  run. Replaces the case-specific `-if '$DateTimeOriginal=~/^2025/'`.
- **`--dry-run`**: report the before→after date range per device without writing —
  confirm the offset is right before touching files.

Reusable for any future clock error on any device.

## Answers to the specific questions

- **Q2 — is the p1_ML method the best model?** Yes. The `-AllDates += 1 year` shift
  with the `-if year==2025` guard, run on the staged APFS copies, is the right
  approach — it fixes EXIF and the MakerNote in one pass and is safe to re-run. Just
  wrap it in steps 2–5 so the inventory/hashes/CSVs follow.
- **Q3 — update the metadata CSV?** Yes, but it requires patching `file_inventory`
  first (step 4) — regenerating from the stale inventory would keep the 2025 dates.
  (Note: the camera data is in `image_file_metadata.csv`; the audio CSV is unaffected.)
- **Q4 — does QC compliance require recalculating hashes?** **Yes, mandatory.**
  Rewriting EXIF changes the bytes → new SHA-256/SHA-1. The manifest and inventory
  hashes must be recomputed, or fixity/QC and the Box hash check will (correctly)
  flag every corrected file.
- **Q5 — fix staged data before Box upload, then keep using the app?** Yes —
  **provided the re-inventory (steps 2–5) is done before uploading.** The app's
  automatic post-upload hash verification compares the inventory's SHA-1 against
  Box's SHA-1 of the uploaded bytes. If you shift EXIF but skip the re-hash, every
  corrected file uploads fine but then **fails verification** (old inventory hash ≠
  new Box hash). With steps 2–5 done first, the inventory matches the corrected
  files and the normal Box upload + verification works unchanged.

## Required order of operations

downloads finished → confirm affected list (step 1) → for each affected device:
EXIF shift (1) → re-hash (2) → re-date (3) → patch inventory (4) → regenerate
manifests + CSVs (5) → **then** Box upload via the app as normal.

## Verification

- Re-sample affected folders → all `DateTimeOriginal` now 2026.
- Spot-check `image_file_metadata.csv`: `recorded_datetime` for corrected files reads
  2026; `file_hash_sha256/sha1` match freshly computed hashes of the files on disk.
- Run the app's Box upload; confirm the automatic hash verification passes (no
  mismatches) and the temporal-plausibility QC warning is gone.
