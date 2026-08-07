# Wildlife Insights image splitting: implementation checkpoint

## Agreed workflow

Camera-image folders are prepared for Wildlife Insights after local ingestion
and quality control, but before the first Box upload:

1. Ingest files locally into the normal flat device folder.
2. Generate metadata and run quality control.
3. Split camera folders containing more than 15,000 images.
4. Structurally verify the resulting layout.
5. Begin Box upload only after verification succeeds.

Splitting is automatic, visible in the application, and does not require a
confirmation prompt. A failed or cancelled split blocks Box upload.

## Folder contract

Devices containing at most 15,000 images remain flat. Oversized camera devices
use numbered folders nested beneath the original device folder:

```text
raw_data/
  p1_ML/
    p1_ML_1/
    p1_ML_2/
```

Once split, the parent device folder contains no loose images. Trigger bursts
remain intact, every part contains at most 15,000 images, and a single malformed
burst larger than the limit is a blocking error.

## Safety and recovery

- Splitting is resumable rather than transactional: individual same-volume
  moves are atomic, while a cancellation may leave a valid partial layout.
- A resumed run derives the same plan from loose and already-placed images and
  continues without moving correctly placed files again.
- Duplicate filenames, destination collisions, missing sources, or structural
  verification failures stop the operation without overwriting files.
- The application does not automatically roll back completed moves.
- The existing `utils/split_for_wi.py --undo` command remains available for
  exceptional recovery; no GUI undo control will be added.

## Scope decisions

- Automatic splitting applies to new deployments and resumed deployments that
  have not completed their first Box upload.
- Historical or already-uploaded deployments are never reorganized
  automatically. The existing CLI remains the only retroactive tool.
- Per-device `*_manifest.json` files will be retired from the active workflow;
  existing legacy files will be preserved. `qc/box_upload_manifest.json` and
  `deployment_event_record.json` remain in use.
- The limit is an internal Wildlife Insights policy constant in the GUI. The
  CLI keeps `--limit` for testing and future policy changes.

## Implementation sequence

1. **Completed:** Finalize and harden the reusable core splitting engine.
2. **Completed:** Retire per-device manifest generation and readiness checks.
3. Make inventory paths and Box verification support flat and split layouts.
4. Integrate automatic, cancellable splitting into the GUI between QC and Box
   upload.
5. Complete end-to-end tests and update user-facing workflow documentation.

Each item is reviewed and committed independently on `main`. Unrelated AWS POC,
audit-output, and temporary files are excluded from this work.
