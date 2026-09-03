# Sensor identity and firmware: metadata follow-ups

Written 2026-09-01 while designing `cassn/ndp/` (see the CASSN OSDF directory
structure handoff, decisions D3 and D18). None of this blocks the NDP manifest
work. It is recorded here so an agent can pick it up cleanly afterwards.

Two related defects and one schema cleanup. They share a cause: sensor identity
is spread across differently-named columns, and firmware is parsed but never
persisted.

## 1. `sensor_firmware` is never populated — empty in 100% of rows

**Evidence.** In `CASSN_occurrence_pipeline_runs/2026-08-19_QRR_full_event`,
`sensor_firmware` is empty in all 6,886 occurrence rows.

**Chain of causes:**

1. `cassn/core/audio_metadata.py` **does** parse AudioMoth firmware. It reads
   the GUANO chunk (firmware >= 1.10.0) and `CONFIG.TXT`, and normalizes
   `"AudioMoth-Firmware-Basic (1.11.0)"` to
   `"AudioMoth-Firmware-Basic 1.11.0"` (see the comments at
   `audio_metadata.py:114` and `:377`).
2. **The value is then dropped.** `cassn/config.py` has no firmware column in
   either `IMAGE_FIELDS` or `AUDIO_FIELDS` — the string "firmware" does not
   appear in that file at all. So it never reaches
   `audio_file_metadata.csv`.
3. `cassnoccurrences` declares `sensor_firmware` in `R/schema.R:34` and
   explicitly sets it to `NA_character_` for Motus (`R/motus.R:278`). Nothing
   anywhere fills it, because no input carries it.

**Fix:** add a firmware column to `AUDIO_FIELDS` (and decide whether cameras
have an equivalent worth capturing), write the already-parsed value into it, and
have `cassnoccurrences` read it in `R/enrich.R` alongside `sensor_make` and
`sensor_model`.

**Note on scope:** existing `audio_file_metadata.csv` files on Box lack the
column, so this needs a backfill for historical events. `audio_metadata.py` can
re-derive firmware from the WAVs, and `utils/backfill_audio_durations.py` is a
working precedent for this shape of backfill.

## 2. Firmware is being written into `sensor_model` for NABat rows

**Evidence.** From the same run, the distinct sensor triples are:

| platform | `sensor_make` | `sensor_model` |
|---|---|---|
| wildlife_insights | `RECONYX` | `HYPERFIRE HP4K` |
| motus | `Cellular Tracking Technologies` | `SENSORSTATION` |
| nabat | `Open Acoustic Devices` | `AudioMoth-Firmware-Basic 1.12.1` |

The NABat row's make is right, but its **model holds a firmware string**.

**Cause.** `R/nabat.R:83` assigns `sensor_model` from NABat's
`"Detector Model"` export column, and NABat's own data has the firmware string
in that field. Compare `R/enrich.R:242-243`, which fills make/model from CASSN's
own metadata for rows it can match — those rows come out correct.

**There are three distinct facts being squeezed into two columns:**

| Concept | Correct value for CASSN ARUs |
|---|---|
| make | `Open Acoustic Devices` |
| hardware model | `AudioMoth 1.2.0` |
| firmware | `AudioMoth-Firmware-Basic 1.12.1` |

**Fix:** prefer CASSN metadata over the platform export for sensor identity, and
route the firmware string to `sensor_firmware` rather than `sensor_model`.
Verify the intended constants with John before hard-coding — the make and
hardware model above are his statement of what all CASSN ARUs should be, not a
value read from the data.

## 2b. Traced 2026-09-02: what is actually wrong, and the agreed fix

The 2026-09-01 entries above were written from code reading. A trace against the
real corpus and one real WAV settles it. **Two separate bugs, only one still
live.**

### The device reports all three fields correctly

Read from `UC_QuailRidge_plot1_BT_202601_00010.wav` on Box, GUANO chunk:

```
Make:Open Acoustic Devices
Model:AudioMoth
Serial:243B1F026488A78B
Firmware Version:AudioMoth-Firmware-Basic (1.11.0)
```

Three separate, correctly-labeled fields. Nothing is missing from the device.

### `ARU_make` — already fixed, legacy data only

The stored value correlates exactly with `app_version` across all 25 audio
metadata files:

| app_version | stored `ARU_make` |
|---|---|
| 4.0 (current) | `Open Acoustic Devices` — correct |
| 3.0 | `AudioMoth` — wrong |
| v2.1-migrated | `AudioMoth` — wrong |

`UC_QuailRidge_20260618` carries both (93 rows from 4.0, 70 from 3.0) because it
is the merged event. **No code fix needed**; 1,549 legacy rows need backfill.

Two earlier hypotheses were wrong and are recorded so they are not retried:
neither `parse_audiomoth_wav_comment()` (ICMT) nor the `CONFIG.TXT` parser ever
sets `ARU_make`, and the live `soundhub_config.json` on Box already says
`Open Acoustic Devices`. It was simply an older app version.

### `ARU_model` — still broken in 4.0

Every version, current included, writes the firmware string into the model
field: 1,896 of 1,899 rows written by 4.0 carry
`AudioMoth-Firmware-Basic 1.11.x`. The only three exceptions are `CONFIG.TXT`
rows, and those three are what produce the mixed-value hard error blocking
`UC_FortOrd_plot3_BD_20260610`, `UC_BigCreek_plot1_BD_20260715`, and
`UC_BigCreek_plot1_BT_20260715` from NDP staging.

The cause is deliberate, at `cassn/core/audio_metadata.py:383`, with a comment
saying the firmware string is stored as `ARU_model` "to match the CONFIG.TXT-
derived value". Consistency with the CONFIG path was chosen over correctness;
the CONFIG path has the same misrouting at line 115.

### Agreed fix (John, 2026-09-02)

**Model is `AudioMoth`, read straight from the device — no hardware revision.**
`1.2.0` was considered and dropped: GUANO reports only `AudioMoth`, so appending
a revision would assert something not read from the device. Nothing in this fix
is asserted; every value comes from the WAV. `soundhub_config.json` already says
`AudioMoth`, so its fallback agrees without a config change. **Consequence to
accept knowingly:** the hardware revision is then recorded nowhere. If it is
wanted later it needs its own field or an `ARUs.csv` column.

**Code — model only:**

1. `audio_metadata.py:383` — GUANO `Model` to `ARU_model`; GUANO
   `Firmware Version` to a new `ARU_firmware`.
2. `audio_metadata.py:115` — CONFIG `Firmware` to `ARU_firmware`, not
   `ARU_model`.
3. `config.py` — add `ARU_firmware` to `AUDIO_FIELDS`.
4. `inventory.py:439` — carry `ARU_firmware` with the same GUANO-then-CONFIG
   precedence as the other identity fields.
5. Check `cassn/soundhub/export.py`, which forwards `ARU_make`/`ARU_model` to
   SoundHub, still sends what Brian's ingest expects.

**Backfill — a column transform over 25 files, no media read.** The firmware
string is already in the CSVs, just in the wrong column, so nothing needs
re-deriving from WAVs:

```
ARU_firmware = current ARU_model      # move
ARU_model    = "AudioMoth"
ARU_make     = "Open Acoustic Devices"
```

Guard: where `ARU_model` is already plain `AudioMoth` (the 3 `CONFIG.TXT` rows),
leave `ARU_firmware` blank rather than writing `AudioMoth` as a firmware value.

Fill all three fields on **every** row including `CONFIG.TXT` rows: they are
attributes of the device, constant across the deployment, not properties of an
individual file. Uniform values keep every row in agreement, so the NDP
manifest's consistency check needs no special case, and a config-only deployment
still carries device identity.

Follow the `utils/backfill_audio_durations.py` / `backfill_box_provenance.py`
pattern: dry-run by default, `--apply` to write, atomic per-file writes that
preserve header order **and line terminators** — three of the Box metadata files
are LF where 48 are CRLF.

Sequencing: the code fix lands before or with the backfill, or the next ingest
reintroduces the problem. Box is mutable working storage, so a standalone pass
here is fine and need not wait for the larger per-deployment metadata migration.
The real constraint is that it all lands before the first OSDF publication,
after which corrections cost a new versioned path.

## 3. Rename `camera_*` / `ARU_*` to `sensor_*` — fold into the D3 migration

`IMAGE_FIELDS` uses `camera_make` / `camera_model` / `camera_id`;
`AUDIO_FIELDS` uses `ARU_make` / `ARU_model` / `device_id`. They mean the same
things. `cassnoccurrences` already normalizes both into `sensor_make` /
`sensor_model` at `R/enrich.R:242-243`, so the mapping exists — it is just
carried by every consumer instead of by the schema. With `cassn/ndp/` added,
that mapping will live in both R and Python.

The same mismatch exists for the placement window: `IMAGE_FIELDS` has
`start_date` / `end_date`, `AUDIO_FIELDS` has `deployment_start_date` /
`deployment_end_date`.

**Do this as part of the D3 change, not separately.** D3 already replaces the
event-level `image_file_metadata.csv` / `audio_file_metadata.csv` with a
per-deployment `file_metadata.csv`, which is a schema migration over files
already on Box. Renaming the sensor and date columns in that same pass means one
migration instead of two.

**Do not rename inside the manifest.** `cassn/ndp/manifest.py` nests these under
`device` and `placement`, so `device.make` and `placement.start_date` are
already unambiguous. The rename is worth doing in the CSVs, where the names are
flat and the duplication actually costs something.
