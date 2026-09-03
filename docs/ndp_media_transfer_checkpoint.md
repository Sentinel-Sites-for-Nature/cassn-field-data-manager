# NDP media transfer implementation checkpoint

The review-independent transfer core is implemented in `cassn/ndp/` with a
dry-run-first CLI at `utils/transfer_ndp_source.py`.

It currently:

- resolves the event and device folders through the Box API;
- requires every metadata row to match exactly one Box object;
- rejects missing, extra, duplicate, unsafe, size-mismatched, and
  case-colliding files before transfer;
- requires scratch capacity for the largest deployment plus a safety margin;
- streams one deployment at a time from Box into atomic partial files and
  checks SHA-256 against `file_metadata.csv`;
- runs `pelican object sync`, then records a `stat` result for every object;
- distinguishes a returned checksum from a size-only stat result;
- retains scratch and stops by default if Pelican supplies only size, rather
  than discarding the last locally verified copy on a weak remote check;
- saves atomic local state after each durable phase so an exact rerun resumes;
- lists the remote collections involved in an explicit abandonment without
  deleting them automatically.

The following remain intentionally disabled until the manifest review settles
their contract:

- interpreting remote stat results as final `verification.status=verified`;
- stamping `is_uploaded_to_pelican`, because one set of singular columns cannot
  describe both the temporary `cassn` destination and later `ssn` destination;
- rebuilding and publishing `file_metadata.csv`, `manifest.json`, and
  `README.md` after the whole event succeeds;
- updating event-level Box metadata as a new API version. This is the current
  Box layout assumption and must be replaceable when D3 moves metadata to each
  deployment.

Before a live apply, run the disposable Pelican protocol test: interrupt and
resume `sync`, observe authentication beyond the refresh window, record the
exact `stat --checksums sha --json` response, and confirm collection mapping and
destination-extra behavior.

## First Quail Ridge preflight

The read-only 2026-09-02 preflight resolved 14 of 16 deployments: 6,971 files
and 117.79 GiB, with 22.82 GiB required scratch space. It correctly stopped the
event because Box and `image_file_metadata.csv` disagree throughout two camera
deployments:

- All 1,191 files in `UC_QuailRidge_plot2_ML_20260108` have different Box and
  metadata sizes.
- 195 files in `UC_QuailRidge_plot3_ML_20260108` have different Box and metadata
  sizes.

No media was downloaded and nothing was written to Pelican or Box.
