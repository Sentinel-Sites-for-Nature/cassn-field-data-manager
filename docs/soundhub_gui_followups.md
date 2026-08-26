# SoundHub GUI workflow status

The shared cumulative staging and verified-submission workflow was implemented,
tested, committed as `449f628`, and pushed to the feature branch on 2026-08-25.
It is exercised with mocked S3 and temporary Box fixtures. The Strathearn GUI
run below provides real staging evidence without requiring a redundant live
SoundHub upload.

The backlog CLI already has the reference behavior in
`cassn/soundhub/provenance.py` and `utils/prep_soundhub.py`: exact filename-level
Box matching, pending-only media selection, immediate S3 verification, verified
provenance stamping, and event-local submission reports.

## Implemented locally

1. **Use one shared submission service.** The GUI and CLI both call
   `plan_soundhub_submission()` and `execute_soundhub_submission()`; the old
   current-event-only GUI stamping loop has been removed.
2. **Upload only one active batch.** The staging manifests are cumulative while
   deployment events are being assembled, but a fully submitted batch must be
   cleared before another begins. Any submitted/pending mixture blocks upload.
3. **Show the preflight scope before confirmation.** Display Box year root,
   event count, deployment count, recording count, object count, total bytes,
   and already-submitted count. Any unmatched, duplicate, mixed-state, or blank
   timestamp row blocks the upload button.
4. **Stamp only files actually verified.** Update
   `is_submitted_to_soundhub`, `soundhub_submitter`, and
   `soundhub_submission_datetime` only after the exact planned S3 object set
   verifies successfully. Header-only or otherwise excluded recordings remain
   unsubmitted.
5. **Write the batch report with the data.** Copy the generated Markdown report
   into the `soundhub/` folder of every affected Box deployment event.
6. **Make failure states explicit.** Cancellation or failed S3 verification
   must leave Box provenance unchanged and preserve a safe retry path. If S3
   verifies but a Box write fails, show that distinct recovery state rather
   than reporting the whole workflow as complete.
7. **Confirm Box synchronization.** The completion message explicitly tells the
   operator to keep Box Drive running until metadata and submission reports finish
   syncing. Automated server polling remains optional future polish.
8. **Keep year selection automatic.** Infer the Box year from the staged
   deployment IDs and reject mixed-year batches. Retain an advanced path
   override only for nonstandard storage locations.
9. **Clean up operator-facing terminal output.** Replace implementation terms
   such as `Box event files` with plain descriptions such as `Box metadata CSVs`
   and `deployment events represented`. Make the preflight, upload,
   verification, Box update, and report stages visually distinct, and end with
   an unambiguous success or recovery summary.
10. **Treat staging as one submission batch.** The GUI adds deployment events
    while a batch is active, but refuses to append to a fully submitted batch.
    Completed-batch cleanup remains a dry-run-first CLI maintenance action,
    because it is outside the SD-card walkthrough. Cleanup verifies exact Box
    provenance and the shared event-local report, then removes only the derived
    local project mirror and fragment rebuild inputs after explicit `--apply`.

## Acceptance evidence

- Header-only BD failure rows remain excluded from staging.
- A staging tree containing both previously submitted and pending recordings is
  blocked before upload, preventing old manifests from entering a new batch.
- An interrupted upload resumed against partially present S3 objects.
- Verification failure leaves every Box provenance cell unchanged.
- Successful mocked verification stamps only the exact planned rows and writes
  the same report to every affected event.
- A Box failure after successful S3 verification produces a distinct recovery
  state that says not to re-upload.
- A 2027 event resolves to `field_data/2027` without operator input.
- A mixed 2026/2027 batch is blocked before any S3 write.
- CLI output describes the same scope consistently without requiring knowledge
  of the internal provenance implementation.

## Strathearn real-stage evidence — 2026-08-25

- GUI staging completed for two deployment IDs: 13 plot-1 recordings and 11
  plot-2 recordings.
- All 24 FLAC files exist and expose readable headers whose measured durations
  match the `recording.csv` intervals.
- Project manifests contain two unique deployment rows and 24 unique recording
  rows; upload preflight selects 26 S3 objects totaling 23.01 GB and performs
  no writes.
- Two 488-byte plot-2 WAV failures were correctly excluded. The missing
  filename sequence numbers are therefore expected and auditable in
  `audio_file_metadata.csv`.
- The run exposed blank `subproject_design` and `mounted_on` because staging
  projected an older `audio_file_metadata.csv` without refreshing fields whose
  authority is the current lookup/config. Staging now enriches those fields at
  the boundary and validates them before reporting success.
- The run exposed a cumulative `deployment.csv` that differed from its
  per-deployment fragments in date/time text formatting. Stage completion and
  upload preflight now both require exact cumulative/fragment parity and ISO
  deployment date/time formats.
- The existing Box event-level `soundhub/recording.csv` is stale at 26 rows and
  still contains the two header-only failures with blank end timestamps. GUI
  staging refreshed the local copy but did not synchronize it to Box.

The stage-time Box backup gap and the recommended deployment-centric storage
model are specified in `docs/deployment_centric_box_architecture.md`.

## Next real batch

Keep the verified Strathearn batch staged and add future deployments as they
become ready. Before the eventual live transfer, confirm a current
per-deployment Box backup and review GUI preflight carefully. That normal batch
is the appropriate end-to-end confirmation; do not perform a redundant test
upload.
