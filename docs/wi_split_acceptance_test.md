# Wildlife Insights split: real-data acceptance test

The automated suite covers deterministic planning, burst preservation, file
moves, structural verification, cancellation/resume, inventory-path updates,
flat and nested Box reconciliation, and prior-upload detection. It does not
contact the production Box account. Use this checklist once with a real or
representative deployment before treating the feature as operationally
accepted.

## Full app and Box test

Use a fresh, backed-up deployment with:

- a working Box connection;
- at least one ML or SA camera containing more than 15,000 images;
- no existing `qc/box_upload_manifest.json`, Box verification artifact, or
  `is_uploaded_to_box=True` provenance for this deployment;
- enough free local space and time for the complete upload and verification.

Then:

1. Ingest the SD cards normally and reach **Review & Finalize**.
2. Start Box upload, either automatically or with **Upload to Box Now**.
3. Confirm the progress panel first says it is planning/preparing Wildlife
   Insights folders. No extra confirmation dialog should appear.
4. After preparation, inspect the oversized camera folder locally:
   - it contains `<device>_1`, `<device>_2`, and as many later parts as needed;
   - every part contains at most 15,000 images;
   - no image remains loose in the parent device folder;
   - frames from one trigger burst are not divided between two parts.
5. Let Box upload and both automatic post-upload verification stages finish.
6. Confirm the same nested folders and files appear under the Box deployment.
7. Confirm all of the following local audit evidence:
   - `session.json` image records use nested `storage_relpath` values;
   - `qc/qc_report.json` contains a passing `wi_image_split` entry;
   - `qc/box_upload_manifest.json` contains the complete nested relative paths;
   - `qc/box_upload_verification.json` reports successful file-list and hash
     verification.

The test passes when preparation, upload, Box file-list reconciliation, and
Box-to-local SHA-1 verification all complete without missing files, hash
mismatches, or unexpected Box files.

## Cancellation and resume test

This is optional but recommended on a backed-up validation deployment:

1. Cancel while **Preparing WI folders** is visible.
2. Confirm Box upload does not begin and the app reports that preparation can
   be resumed. Some already-moved images may remain in numbered parts; this is
   expected.
3. Click **Upload to Box Now** again.
4. Confirm preparation moves only the remaining images, verifies the complete
   structure, and then starts Box upload.

Same-volume moves may finish too quickly to cancel on fast storage. That is not
a failure of the normal acceptance test.

## Small-limit engine rehearsal

The GUI intentionally fixes the production policy at 15,000. To exercise the
split/undo engine with only a few copied test images, use the CLI on a throwaway
deployment copy:

```bash
# Preview a six-image limit without changing files:
python utils/split_for_wi.py --root "/path/to/throwaway/deployment" --limit 6

# Apply, inspect, then restore the flat layout:
python utils/split_for_wi.py --root "/path/to/throwaway/deployment" --limit 6 --apply --yes
python utils/split_for_wi.py --root "/path/to/throwaway/deployment" --undo --yes
```

This rehearsal validates the splitting engine only. It does not exercise GUI
gating, session inventory updates, or Box upload and verification.
