# SoundHub GUI workflow status

The shared cumulative staging and verified-submission workflow was implemented
locally on 2026-08-25. It is exercised with mocked S3 and temporary Box
fixtures; no additional live SoundHub upload is required before the next real
batch.

The backlog CLI already has the reference behavior in
`cassn/soundhub/provenance.py` and `utils/prep_soundhub.py`: exact filename-level
Box matching, pending-only media selection, immediate S3 verification, verified
provenance stamping, and event-local submission reports.

## Implemented locally

1. **Use one shared submission service.** The GUI and CLI both call
   `plan_soundhub_submission()` and `execute_soundhub_submission()`; the old
   current-event-only GUI stamping loop has been removed.
2. **Upload only the planned pending batch.** The staging manifests remain
   cumulative, but media already marked submitted on Box must not be sent again
   after SoundHub drains its landing prefix.
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
   operator to keep Box Drive running until metadata and receipts finish
   syncing. Automated server polling remains optional future polish.
8. **Keep year selection automatic.** Infer the Box year from the staged
   deployment IDs and reject mixed-year batches. Retain an advanced path
   override only for nonstandard storage locations.
9. **Clean up operator-facing terminal output.** Replace implementation terms
   such as `Box event files` with plain descriptions such as `Box metadata CSVs`
   and `deployment events represented`. Make the preflight, upload,
   verification, Box update, and report stages visually distinct, and end with
   an unambiguous success or recovery summary.

## Acceptance evidence

- Header-only BD failure rows remain excluded from staging.
- A cumulative staging tree containing earlier submitted events selects only
  pending media and writes receipts only to events in the new batch.
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

## Next real batch

Use the GUI repeatedly to add deployment events to staging. When the desired
batch is ready, review the GUI preflight carefully before approving the one live
transfer. That normal batch is the appropriate end-to-end confirmation; do not
perform a redundant test upload.
