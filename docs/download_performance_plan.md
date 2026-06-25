# Download (SD-card ingest) performance — plan

*Status: future work. Captured June 2026 after observing slow downloads during a
40k-image Reconyx deployment.*

## Problem

SD-card ingest (`process_sd_card_files` in `cassn/gui/wizard.py`) runs as a single
sequential loop, so it pegs **one CPU core** while the rest sit idle. Observed during
a real download: `python` at 98% of one core, on a fanless M3 Air writing to an
external **HDD** (G-DRIVE, ~70 MB/s sequential). It is **CPU-bound** (per-file
hashing + EXIF), compounded by redundant full-file reads on slow media and by macOS
Spotlight indexing the freshly written images.

## Findings (current behavior)

- **Single-threaded**: one `os.walk` loop, one file at a time (`wizard.py:1357–1359`).
- **Per-file read amplification** — the source file is read ~3× and the dest once:
  - `sha256_sha1(source)` — full read + hash (`hashing.py:hash_file`, both digests one pass)
  - `shutil.copy2(source, dest)` — full source read + dest write
  - `sha256_sha1(dest)` — full dest read-back to verify the copy (the `verify_copy_hash` step)
  - Pillow EXIF + the persistent exiftool (`ReconyxExtractor`) — additional source reads
- **Order-dependent Reconyx sequencing**: burst/event numbers are assigned while
  walking *sorted* files in order via the stateful `current_event_num`
  (`wizard.py:1404–1407`). Any reordering breaks event numbering.
- **Shared mutable state**: `self.file_inventory`, the dedup set `self.seen_file_hashes`,
  and per-device counters are mutated inline.
- **exiftool** runs as a single persistent `-stay_open` process (one at a time).

## Phase 0 — operational (no code; do these first / every time)

- **Exclude the staging drive from Spotlight** so macOS doesn't re-read every new
  image to index it: System Settings → Spotlight → Search Privacy → add the staging
  drive, or `sudo mdutil -i off "/Volumes/<staging drive>"`.
- **Pause Box Drive sync** during big downloads (it competes for disk + CPU).
- Avoid other transfers to/from the staging drive mid-download (e.g. S3/`aws s3 cp`).
- Prefer an **SSD destination** and a quality **UHS-II reader** where possible — the
  HDD + cheap-reader combination is the hard floor.

## Phase 1 — copy+hash fusion (low risk, no threading) ⭐ do first

Eliminate one of the two full source reads by hashing *while* copying:

- Replace `sha256_sha1(source)` + `shutil.copy2` with a single streaming pass:
  read each source block once, **write it to the dest and feed it to the source
  hashers in the same loop** (mirror `hashing.hash_file`'s block loop). Then keep the
  existing `sha256_sha1(dest)` read-back to verify.
- Net: source goes from ~2 full reads to 1 (~33% fewer full-file reads per image),
  identical digests, no ordering/threading risk.
- Touch points: the copy block in `wizard.py` (~1391–1418) and a small helper in
  `cassn/core/hashing.py` (e.g. `copy_and_hash(src, dst) -> (sha256, sha1)`).

## Phase 2 — parallelize the CPU work (bigger, higher value)

Only worth it if Phase 0+1 aren't enough. Parallelism helps because `hashlib.update()`
releases the GIL, so threaded hashing uses multiple cores, and idle cores are real
headroom. The existing `BoxUploadThread` (`cassn/box/threads.py`, `ThreadPoolExecutor`,
`_state_lock`) is the template.

Design that preserves correctness:
1. **Sequential pass** over sorted files to assign Reconyx burst/event numbers and the
   renamed filenames (keep the order-dependent logic single-threaded and cheap).
2. **Worker pool** for the heavy per-file work — copy + dual hash + verify + EXIF —
   returning a record per file. Guard `file_inventory`, the dedup set, and counters
   with a lock (as the upload thread does).
3. **exiftool pool**: give each worker its own `ExifToolHelper` (or a small pool of
   `-stay_open` processes) instead of one shared instance.
4. **I/O caveat**: on an HDD, parallel *disk* I/O thrashes seeks — the gain is
   overlapping CPU (hash/EXIF) with I/O waits, not parallel reads. Cap workers low
   (e.g. 2–4) and benchmark; more is not better on a spinning disk. On an SSD
   destination, scale higher.

## Verification

- **Correctness is paramount** (data-integrity path). After any change, process a
  known deployment and confirm, byte-for-byte vs. a baseline run:
  - identical `file_hash_sha256` / `file_hash_sha1` per file,
  - identical Reconyx `sequence_event_num` / `sequence_position` / `sequence_total`,
  - identical file count, dedup behavior, and CSV output.
- **Benchmark** wall-clock on the same card+drive before/after each phase; watch
  `python` CPU% (should exceed one core in Phase 2) and total time.
- Test the **retry path** (transient hash mismatch still recovers; persistent still
  errors) survives the refactor.

## Recommendation

Phase 0 immediately (free, big effect via Spotlight exclusion). Phase 1 next (small,
safe, ~⅓ fewer full reads). Phase 2 only if ingest remains a recurring bottleneck —
it's a real refactor of the integrity-critical copy path, so gate it behind the
byte-for-byte correctness checks above.
