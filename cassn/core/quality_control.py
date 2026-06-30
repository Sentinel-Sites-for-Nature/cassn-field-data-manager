"""
Quality-control checks and the append-only QC report.

This module owns everything that lands in a deployment's ``qc/`` subfolder:

* **Sidecar paths** — :func:`qc_path_for` resolves a name inside ``qc/`` and
  :func:`migrate_qc_sidecars` lifts pre-``qc/`` reports out of the deployment
  root (one-shot, idempotent).
* **The report** — :func:`append_qc_report` keeps an append-only ``history``
  and recomputes a ``current_state`` view on every write
  (:func:`_compute_qc_current_state`). The latest entry per ``(check, device)``
  wins; stale per-file fixity findings are dropped once a newer
  ``file_hash_verification_run`` supersedes them.
* **The checks** — :func:`check_sequence_integrity` (Reconyx bursts),
  :func:`validate_datetimes` (temporal plausibility) and
  :func:`validate_coordinates` (study-area bounds).
* **Audit scans** — :func:`snapshot_lookup_tables` freezes the active lookup
  tables alongside the metadata they produced.

Every function is pure given its inputs except the two that touch the
filesystem (:func:`append_qc_report`, :func:`snapshot_lookup_tables`) and the
path helpers; none of them import Qt or Box.
"""

from __future__ import annotations

import json
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cassn.config import (
    LOCAL_DATA_DIR,
    QC_CHECK_DESCRIPTIONS,
    QC_SIDECAR_FILES,
    QC_SUBFOLDER,
)
from cassn.core.classification import file_size_floor_for, format_size_floor
from cassn.core.hashing import sha256


# ---------------------------------------------------------------------------
# Copy-time predicates
#
# Pure decision rules applied per file during the SD-card copy. The GUI owns the
# logging, QC-report writes, and dialogs; these functions own the rule itself so
# it can be read and tested without Qt.
# ---------------------------------------------------------------------------

def verify_copy_hash(source_hash: str, dest_hash: str) -> bool:
    """True if the post-copy file is byte-identical to the source.

    Compares SHA-256 only — a matching SHA-256 already proves byte-identity.
    SHA-1 is still computed/stored elsewhere for the Box↔disk fixity check.
    """
    return source_hash == dest_hash


def check_file_size_floor(size_bytes: int, file_type: str, dev_code: str) -> tuple[int, str] | None:
    """Return ``(floor_bytes, floor_label)`` if below the device-aware floor, else None."""
    floor = file_size_floor_for(file_type, dev_code)
    if floor is not None and size_bytes < floor:
        return floor, format_size_floor(floor)
    return None


def is_duplicate_hash(file_hash: str, seen_hashes: set) -> bool:
    """True if this SHA-256 was already inventoried in the current session."""
    return file_hash in seen_hashes


def is_duplicate_media(file_hash: str, file_type: str, seen_hashes: set) -> bool:
    """True if a file should be skipped as a session-wide duplicate.

    Only media (image/audio) participate in duplicate detection. CONFIG/text
    files are exempt: two AudioMoths left on identical default settings produce
    byte-identical CONFIG.TXT files, and each device's settings record must be
    kept rather than silently dropped.
    """
    if file_type == "config":
        return False
    return is_duplicate_hash(file_hash, seen_hashes)


def build_box_verification_record(
    *,
    hash_ok: bool,
    hash_summary: str,
    hash_issues: list,
    box_summary: str,
    box_issues: list,
    verified_at: str | None = None,
) -> dict:
    """Assemble the post-upload Box verification artifact written to
    ``qc/box_upload_verification.json``.

    Records the result of the two checks the app already runs after upload —
    the Box file-list reconciliation and the Box<->local SHA-1 comparison — as
    a persisted, machine-readable artifact. The file was previously referenced
    by the deployment-readiness check but never actually written.
    """
    box_missing = [i for i in box_issues if i.get("type") == "missing_from_box"]
    box_extra = [i for i in box_issues if i.get("type") == "extra_on_box"]
    verified = hash_ok and not box_missing and not box_extra and not hash_issues
    return {
        "verified_at": verified_at or datetime.now().isoformat(timespec="seconds"),
        "verified": verified,
        "box_file_list": {
            "summary": box_summary,
            "missing_from_box": [i.get("filename", "") for i in box_missing],
            "extra_on_box": [i.get("filename", "") for i in box_extra],
        },
        "hash_verification": {
            "ok": hash_ok,
            "summary": hash_summary,
            "issues": [
                {"type": i.get("type", ""), "filename": i.get("filename", "")}
                for i in hash_issues
            ],
        },
    }


def check_expected_count(expected: int | None, actual: int) -> bool:
    """True if counts match (or ``expected`` is unknown)."""
    return expected is None or expected == actual


# ---------------------------------------------------------------------------
# Sidecar paths
# ---------------------------------------------------------------------------

def qc_path_for(deployment_folder: Path, filename: str) -> Path:
    """Path to a QC sidecar file inside the ``qc/`` subfolder of a deployment.

    Creates the subfolder if missing.
    """
    qc_dir = deployment_folder / QC_SUBFOLDER
    qc_dir.mkdir(parents=True, exist_ok=True)
    return qc_dir / filename


def migrate_qc_sidecars(deployment_folder: Path) -> None:
    """Move pre-existing QC sidecars from the deployment root into ``qc/``.

    Safe to call repeatedly; a no-op once files are already in ``qc/``.
    """
    if not deployment_folder.is_dir():
        return
    qc_dir = deployment_folder / QC_SUBFOLDER
    for name in QC_SIDECAR_FILES:
        old = deployment_folder / name
        new = qc_dir / name
        if old.exists() and not new.exists():
            try:
                qc_dir.mkdir(parents=True, exist_ok=True)
                old.rename(new)
            except Exception:
                pass  # best-effort; don't block on migration failure


# ---------------------------------------------------------------------------
# The QC report (append-only history + computed current state)
# ---------------------------------------------------------------------------

def _compute_qc_current_state(history_session: list, history_devices: dict) -> list:
    """Collapse the append-only history into a current-state view.

    Rules:
      1. For each unique ``(check, device)`` pair, keep the entry with the
         latest timestamp.
      2. Per-file detail entries (``file_hash_mismatch``, ``file_hash_missing``)
         are dropped if a ``file_hash_verification_run`` exists with a newer
         timestamp — that run supersedes them.

    Returns entries sorted error < warning < pass, then by check then device.
    """
    # Walk history, tagging each entry with its scope (device or "")
    all_entries: list[dict] = []
    for e in history_session:
        e2 = dict(e)
        e2["_device"] = ""
        all_entries.append(e2)
    for dev, items in (history_devices or {}).items():
        for e in items:
            e2 = dict(e)
            e2["_device"] = dev
            all_entries.append(e2)

    # Most recent file_hash_verification_run timestamp (for filtering stale per-file entries)
    runs = [e for e in all_entries if e.get("check") == "file_hash_verification_run"]
    latest_run_ts = max((e["timestamp"] for e in runs), default="")

    # Group by (check, device); keep latest per group
    by_key: dict[tuple, dict] = {}
    for e in all_entries:
        key = (e["check"], e["_device"])
        prev = by_key.get(key)
        if prev is None or e["timestamp"] > prev["timestamp"]:
            by_key[key] = e

    current: list[dict] = []
    for (check, _), e in by_key.items():
        # Drop stale per-file fixity entries
        if check in ("file_hash_mismatch", "file_hash_missing") and latest_run_ts:
            if e["timestamp"] < latest_run_ts:
                continue
        current.append(e)

    severity_rank = {"error": 0, "warning": 1, "pass": 2}
    current.sort(key=lambda e: (
        severity_rank.get(e.get("severity"), 9),
        e.get("check", ""),
        e.get("_device", ""),
    ))
    return current


def append_qc_report(deployment_folder: Path, check: str, device: str, severity: str, message: str) -> None:
    """Append one finding to ``qc_report.json`` in the deployment folder.

    Structure::

        {
          "generated": "...",
          "current_state": [...],      # latest entry per (check, device)
          "history": {
            "session_checks": [...],   # chronological session-level checks
            "devices": { "p1_ML": [...], ... }
          }
        }

    History is append-only; ``current_state`` is rebuilt on every call.
    """
    report_path = qc_path_for(deployment_folder, "qc_report.json")
    if report_path.exists():
        try:
            with open(report_path) as f:
                report = json.load(f)
        except Exception:
            report = {}
    else:
        report = {}

    # Migrate legacy flat structure (session_checks/devices at top level) to nested history
    if "history" not in report:
        report["history"] = {
            "session_checks": report.pop("session_checks", []),
            "devices": report.pop("devices", {}),
        }
    history = report["history"]
    history.setdefault("session_checks", [])
    history.setdefault("devices", {})

    entry = {
        "check": check,
        "description": QC_CHECK_DESCRIPTIONS.get(check, ""),
        "severity": severity,
        "message": message,
        "timestamp": datetime.now().isoformat(),
    }

    if device:
        history["devices"].setdefault(device, []).append(entry)
    else:
        history["session_checks"].append(entry)

    report["generated"] = datetime.now().isoformat()
    report["current_state"] = _compute_qc_current_state(
        history["session_checks"], history["devices"]
    )

    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_sequence_integrity(entries: list, device_label: str) -> list[str]:
    """Check Reconyx sequence/event grouping integrity for one device's entries.

    Returns a list of warning strings (empty = all clear). Verifies, per
    app-assigned burst/event:

      (a) actual frame count vs ``sequence_total``;
      (b) positions are sequential, start at 1, and don't duplicate;
      (c) timestamps within an event are tightly clustered (≤2s between frames);
      (d) ``sequence_event_num`` is contiguous across the device.
    """
    warnings = []
    image_entries = [e for e in entries if e.get('file_type') == 'image' and e.get('sequence_event_num') not in ('', None)]
    if not image_entries:
        return warnings

    bursts = defaultdict(list)
    for e in image_entries:
        try:
            bursts[int(e['sequence_event_num'])].append(e)
        except (ValueError, TypeError):
            pass

    if not bursts:
        return warnings

    # (a-c) check each named burst/event: count, positions, and timestamp coherence.
    for event_num, files in sorted(bursts.items()):
        totals = {e.get('sequence_total') for e in files if e.get('sequence_total') not in ('', None)}
        expected = None
        if totals:
            try:
                expected = int(next(iter(totals)))
                actual = len(files)
                if actual != expected:
                    warnings.append(
                        f"Burst #{event_num}: expected {expected} frames, found {actual} "
                        f"(missing {expected - actual})"
                    )
            except (ValueError, TypeError):
                expected = None

        positioned = []
        for e in files:
            try:
                positioned.append((int(e.get('sequence_position')), e))
            except (ValueError, TypeError):
                pass

        if positioned:
            positions = sorted(pos for pos, _ in positioned)
            if len(set(positions)) != len(positions):
                warnings.append(
                    f"Burst #{event_num}: duplicate sequence positions {positions}"
                )
            observed_sequence = list(range(min(positions), max(positions) + 1))
            if positions != observed_sequence:
                warnings.append(
                    f"Burst #{event_num}: non-sequential observed positions {positions}; "
                    f"observed positions should not skip within an app-assigned event"
                )
            if min(positions) != 1:
                warnings.append(
                    f"Burst #{event_num}: first observed sequence position is {min(positions)}; "
                    "expected position 1 to start the event"
                )

        timed = []
        for pos, e in sorted(positioned):
            dt_str = e.get('recorded_datetime', '')
            if not dt_str:
                continue
            try:
                dt = datetime.fromisoformat(str(dt_str))
                if dt.tzinfo is not None:
                    dt = dt.replace(tzinfo=None)
                timed.append((pos, dt, e.get('new_filename', '')))
            except Exception:
                pass

        if len(timed) >= 2:
            for (pos1, dt1, _), (pos2, dt2, _) in zip(timed, timed[1:]):
                gap_seconds = (dt2 - dt1).total_seconds()
                if gap_seconds <= 0:
                    warnings.append(
                        f"Burst #{event_num}: timestamps are not increasing between "
                        f"positions {pos1} and {pos2}"
                    )
                elif gap_seconds > 2:
                    warnings.append(
                        f"Burst #{event_num}: positions {pos1} and {pos2} are "
                        f"{gap_seconds:g}s apart; expected 1s, tolerating up to 2s"
                    )

            span_seconds = (timed[-1][1] - timed[0][1]).total_seconds()
            span_limit = expected if expected is not None else max(1, len(timed) - 1)
            if span_seconds > span_limit:
                warnings.append(
                    f"Burst #{event_num}: timestamp span is {span_seconds:g}s across "
                    f"{len(timed)} frame(s); expected about {max(0, len(timed) - 1)}s "
                    f"and tolerating up to {span_limit}s"
                )

    # (d) check for gaps in event_num sequence
    event_nums = sorted(bursts.keys())
    for i in range(1, len(event_nums)):
        if event_nums[i] != event_nums[i - 1] + 1:
            gap_start = event_nums[i - 1] + 1
            gap_end = event_nums[i] - 1
            warnings.append(
                f"Gap in event sequence: events {gap_start}–{gap_end} missing "
                f"(jumped from {event_nums[i-1]} to {event_nums[i]})"
            )

    return warnings


def validate_datetimes(entries: list, deployment_start: str, collection_date: str) -> list[str]:
    """Temporal plausibility checks for one device's entries.

    Returns a list of warning strings (empty = all clear). Flags pre-deployment
    dates, dates after the collection date, and clock-reset clusters (≥3 files
    sharing a timestamp to the second).

    EXIF datetimes are timezone-aware (e.g. ``-08:00``); deployment bounds are
    naive, so aware datetimes are stripped to naive before comparison to avoid
    mixing the two.
    """
    warnings = []
    datetimes = []  # list of NAIVE datetimes for safe comparison
    for e in entries:
        dt_str = e.get('recorded_datetime', '')
        if not dt_str:
            continue
        try:
            dt = datetime.fromisoformat(str(dt_str))
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)
            datetimes.append(dt)
        except Exception:
            pass

    if not datetimes:
        return warnings

    try:
        deploy_start = datetime.strptime(deployment_start, "%Y-%m-%d")
    except Exception:
        deploy_start = None
    try:
        # Files dated on the collection date itself are fine; anything the next
        # calendar day or later is flagged.
        collect_end = datetime.strptime(collection_date, "%Y-%m-%d") + timedelta(days=1)
    except Exception:
        collect_end = None

    pre_deploy = [dt for dt in datetimes if deploy_start and dt < deploy_start]
    if pre_deploy:
        warnings.append(f"{len(pre_deploy)} file(s) recorded before deployment start ({deployment_start}); earliest: {min(pre_deploy).isoformat()}")

    post_collect = [dt for dt in datetimes if collect_end and dt >= collect_end]
    if post_collect:
        warnings.append(f"{len(post_collect)} file(s) recorded after collection date ({collection_date}); latest: {max(post_collect).isoformat()}")

    # Clock-reset cluster: ≥3 files with same timestamp to the second
    ts_counts = Counter(dt.replace(microsecond=0) for dt in datetimes)
    resets = [(ts, cnt) for ts, cnt in ts_counts.items() if cnt >= 3]
    if resets:
        worst = max(resets, key=lambda x: x[1])
        warnings.append(f"Possible clock reset: {worst[1]} files share timestamp {worst[0].isoformat()} — camera clock may have reset to factory default")

    return warnings


def check_recording_stop_reasons(entries: list, device_label: str) -> list[str]:
    """Flag audio recordings that ended for a concerning reason.

    AudioMoth records why each recording stopped. "File size limit" is normal for
    long high-sample-rate (bat) recordings and is ignored; **low voltage** (a
    battery death) and a **switch position change** (the device switch was toggled,
    often unexpectedly) are flagged. Returns one warning per distinct concerning
    reason with a count. Self-filters to audio entries, so it is a no-op for
    camera devices. Empty list = nothing concerning.
    """
    concerning = ("low voltage", "low battery", "switch position")
    counts: Counter = Counter()
    for e in entries:
        if e.get('file_type') != 'audio':
            continue
        reason = (e.get('recording_stop_reason') or '').strip()
        if reason and any(term in reason.lower() for term in concerning):
            counts[reason] += 1
    return [f"{n} recording(s) stopped due to: {reason}" for reason, n in counts.items()]


def check_camera_serial(entries: list, device_label: str) -> list[str]:
    """Cross-check the cameras.csv ``camera_id`` against the EXIF camera serial.

    The lookup ``camera_id`` should be contained within the camera's full hardware
    serial read from EXIF (e.g. ``08021269`` within ``4LPXKT08021269``). Warns once
    per device when a non-empty ``camera_id`` is **not** a substring of a non-empty
    ``camera_serial_exif`` (case-insensitive) — a sign the wrong physical camera is
    deployed at this plot, or the lookup table is stale. Skips when either value is
    blank, and self-filters to image entries (no-op for audio).
    """
    for e in entries:
        if e.get('file_type') != 'image':
            continue
        cam_id = str(e.get('camera_id') or '').strip()
        serial = str(e.get('camera_serial_exif') or '').strip()
        if not cam_id or not serial:
            continue
        if cam_id.lower() not in serial.lower():
            return [
                f"camera_id '{cam_id}' (cameras.csv) is not contained in the "
                f"camera's EXIF serial '{serial}' — possible wrong camera at this plot"
            ]
        break  # consistent within a device; one verified image is enough
    return []


def validate_coordinates(file_inventory: list, bounds: dict | None = None) -> list[str]:
    """Validate ``(latitude, longitude)`` across all inventory entries.

    ``bounds`` may carry ``lat_min``/``lat_max``/``lon_min``/``lon_max``;
    defaults cover the California study area. Coordinates are filled
    programmatically from plots.csv, so the only failure modes worth checking
    are unset (0,0) values and a lookup value that lands outside the study area.
    Returns a list of warning strings (deduplicated per plot).
    """
    if bounds is None:
        bounds = {}
    lat_min = float(bounds.get('lat_min', 32.0))
    lat_max = float(bounds.get('lat_max', 42.5))
    lon_min = float(bounds.get('lon_min', -125.0))
    lon_max = float(bounds.get('lon_max', -114.0))

    warnings = []
    seen_plots: set[str] = set()

    for e in file_inventory:
        lat_raw = e.get('latitude', '')
        lon_raw = e.get('longitude', '')
        plot = str(e.get('plot_number', ''))
        if plot in seen_plots:
            continue
        if lat_raw == '' or lon_raw == '':
            warnings.append(f"Plot {plot}: coordinates are missing")
            seen_plots.add(plot)
            continue
        try:
            lat = float(lat_raw)
            lon = float(lon_raw)
        except (ValueError, TypeError):
            warnings.append(f"Plot {plot}: coordinates are not numeric ({lat_raw!r}, {lon_raw!r})")
            seen_plots.add(plot)
            continue

        if lat == 0.0 and lon == 0.0:
            warnings.append(f"Plot {plot}: coordinates are (0, 0) — likely unset")
            seen_plots.add(plot)
            continue

        if not (lat_min <= lat <= lat_max) or not (lon_min <= lon <= lon_max):
            warnings.append(f"Plot {plot}: ({lat}, {lon}) is outside expected study area bounds")
        seen_plots.add(plot)

    return warnings


# ---------------------------------------------------------------------------
# Lookup-table snapshot
# ---------------------------------------------------------------------------

def snapshot_lookup_tables(deployment_folder: Path) -> list[dict]:
    """Copy the active lookup/config files into ``qc/lookup_snapshot/``.

    Returns a manifest (one dict per file) and also writes
    ``lookup_snapshot_manifest.json`` alongside the copies, so regenerated
    metadata can be tied to the exact lookup configuration that produced it.
    """
    snapshot_dir = deployment_folder / QC_SUBFOLDER / "lookup_snapshot"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    manifest = []

    if not LOCAL_DATA_DIR.exists():
        return manifest

    for source in sorted(p for p in LOCAL_DATA_DIR.iterdir() if p.is_file()):
        if source.name == ".DS_Store":
            continue
        dest = snapshot_dir / source.name
        try:
            shutil.copy2(source, dest)
            manifest.append({
                "filename": source.name,
                "relative_path": str(dest.relative_to(deployment_folder)),
                "size_bytes": dest.stat().st_size,
                "sha256": sha256(dest),
                "snapshotted_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as exc:
            manifest.append({
                "filename": source.name,
                "relative_path": str(dest.relative_to(deployment_folder)),
                "error": str(exc),
                "snapshotted_at": datetime.now(timezone.utc).isoformat(),
            })

    manifest_path = snapshot_dir / "lookup_snapshot_manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({
            "generated": datetime.now(timezone.utc).isoformat(),
            "source_directory": str(LOCAL_DATA_DIR),
            "file_count": len(manifest),
            "files": manifest,
        }, f, indent=2)
    return manifest
