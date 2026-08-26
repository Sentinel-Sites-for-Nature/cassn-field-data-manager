# Deployment folders within deployment events — Box architecture plan

## Purpose

Make an individual device deployment the durable data unit inside its current
deployment event. A deployment event remains the physical and operational
parent for the 2026 season. It is lightweight in the narrower sense that a
future season may stop using that directory layer prospectively; historical
event-organized data does not need to be migrated to match a later design.

This document is a design plan, not authorization to reorganize existing Box
data. The current event-centric tree remains authoritative until a separately
tested cutover is approved.

## Recommended ownership model

- A **deployment event** is the field-service round, app selection, and present
  Box parent.
- A **deployment** owns one device interval's media, file metadata, QC evidence,
  and downstream platform metadata.
- A **SoundHub submission batch** temporarily groups many deployments for one
  S3 transfer. Its two cumulative manifests are local batch artifacts, not
  deployment or event records.

The transition rule is not to bake the event concept into deployment IDs or
downstream platform IDs. A later prospective layout may drop the event folder
without requiring historical folders to move.

## Target Box tree for the current workflow

```text
field_data/
└── <year>/
    └── <reserve>/
        └── <deployment_event_id>/
            ├── <deployment_id>/
            │   ├── media/
            │   ├── metadata/
            │   │   ├── image_file_metadata.csv   # camera, if applicable
            │   │   └── audio_file_metadata.csv   # ARU, if applicable
            │   ├── qc/
            │   ├── wildlife_insights/
            │   │   ├── deployment.csv
            │   │   └── occurrences.csv           # when produced
            │   └── soundhub/
            │       ├── deployment.csv             # exactly one deployment row
            │       ├── recording.csv              # only this deployment's recordings
            │       └── submissions/
            │           └── <timestamp>_submission.md
            └── <small event-level records or indexes, if needed>/
```

The event folder retains its current meaning as one field-service round. Media
and platform products move into child deployment folders so different device
intervals are not mixed at the event root. `metadata/` can hold both image and
audio file schemas; `media/` holds immutable ingested files and necessary
device sidecars such as `CONFIG.TXT`.

## SoundHub staging and Box backup

SoundHub's S3 format still requires two cumulative manifests at the local batch
root:

```text
UCNature-SSN/deployment.csv
UCNature-SSN/recording.csv
```

They describe the complete waiting batch and remain local until transfer.
Copying them into every Box folder would create duplicate batch snapshots that
become stale as more deployments are staged.

The local `.cassn_fragments/<deployment_id>/` files are the right backup unit.
After every successful GUI stage, the app should:

1. Write or replace the local per-deployment fragment.
2. Rebuild and validate the cumulative local manifests.
3. Upload/version that deployment's two fragment CSVs under
   `<deployment_event_id>/<deployment_id>/soundhub/`.
4. Download or checksum the Box versions to confirm an exact match.
5. Report local staging and Box metadata backup as separate statuses.

Only the small CSVs require immediate backup. The derived FLAC transfer copies
may remain local because Box already holds the source WAVs. A Box backup failure
must preserve completed FLACs and be safely retryable without retranscoding.
Before live S3 upload, preflight should require each pending deployment's Box
fragment to match its local fragment.

After verified submission, place the report in each affected deployment's
`soundhub/submissions/` folder. Exact filename-level provenance remains in
`audio_file_metadata.csv`.

## What Strathearn demonstrated

The 2026-08-25 GUI run created 24 valid FLACs across two deployments and
excluded two 488-byte header-only failures. It also exposed three independent
gaps:

- staging copied historical blanks instead of refreshing authoritative lookup
  and config values;
- the cumulative deployment manifest could drift from its two fragments while
  upload preflight still passed;
- the Box event-level `soundhub/recording.csv` remained an older 26-row copy
  because local staging had no Box-backup step.

The first two are now fixed in the staging/upload-ready contract. The third is
the reason for deployment-level, stage-time Box fragment backups.

## Transition plan

### Phase 1 — make staging truthfully upload-ready

- Enrich fields owned by current lookups/config at the staging boundary.
- Require complete deployment metadata and recording timestamps.
- Require cumulative manifests to exactly equal all deployment fragments.
- Run the same validator at stage completion and upload preflight.
- Restage Strathearn idempotently; preserve its existing FLACs.

### Phase 2 — add deployment-level SoundHub metadata backups

- Implement the stage-time Box copy and verification described above.
- Use `<deployment_event_id>/<deployment_id>/soundhub/`.
- Retain existing event-root SoundHub CSVs only as transition artifacts.
- Add tests for partial Box failure, retry, exclusions, and multi-event batches.

### Phase 3 — route new ingests into deployment children

- Route each selected card into its exact deployment folder within the event.
- Generate file metadata and downstream files in that deployment folder.
- Update Box upload, verification, resume state, Wildlife Insights splitting,
  and SoundHub provenance to resolve deployment children.

### Phase 4 — decide the post-2026 layout prospectively

After the survey/app redesign, decide whether future seasons still benefit from
the event layer. If not, drop it for future data only. Existing event-organized
seasons may remain exactly as filed.

## Invariants and acceptance tests

- A deployment has one stable Box path within its filed event.
- Deployment IDs and downstream metadata remain valid if a future season stops
  organizing new files by event.
- Per-deployment SoundHub CSVs contain only that deployment's rows.
- Cumulative SoundHub manifests exist once per local batch and exactly match
  the union of local fragments.
- Header-only failures remain in file/QC metadata but never in SoundHub output.
- Re-staging replaces rows and Box versions without duplication or re-encoding.
- A Box backup failure cannot silently present a deployment as fully backed up.
- No S3 upload starts unless local validation and Box metadata verification pass.

## Decisions to settle before Phase 3

- Whether `media/` uses direct files or `media/images/` and `media/audio/`.
- Whether per-deployment file metadata stays split by image/audio schema.
- Whether the redesigned post-2026 workflow retains the event directory layer.
