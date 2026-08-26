"""
The file inventory: naming, per-file records, session state, and summaries.

The wizard walks each SD card and, for every kept file, records one dict in its
``file_inventory`` list. That list is the spine of the whole app — every CSV
and QC check reads from it. This module owns the parts of that
process that are pure given their inputs, so they can be tested without a GUI or
a physical card:

* :func:`build_deployment_filename` — the prospective naming rule that keeps
  every media prefix identical to its selected deployment ID.
* :func:`build_renamed_filename` — the retained historical naming helper used
  by older records and maintenance code.
* :func:`build_inventory_record` — assembles one inventory dict from the four
  metadata sources (EXIF, Reconyx/ExifTool, CONFIG.TXT, WAV comment) plus plot
  and SoundHub lookups, applying the exact source-precedence the original used.
* :func:`refresh_legacy_device_manifest` — refreshes an existing, retired
  per-device sidecar for compatibility with historical deployments.
* :func:`generate_session_summary` — the human-readable deployment digest.
* :func:`write_session` / :func:`find_all_sessions` — crash-safe session.json
  persistence and the staging-wide scan used to resume or reopen deployments.

The stateful parts — walking the card, incrementing the per-device sequence and
event counters, hashing, copying, and emitting QC findings — stay in the wizard,
which threads its loop-carried counters through :func:`build_deployment_filename`
and :func:`build_inventory_record`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path, PurePosixPath

from cassn.core.audio_metadata import hz_to_khz
from cassn.core.classification import classify_file
from cassn.core.quality_control import append_qc_report, qc_path_for
from cassn.lookups import normalize_deployment_event_metadata


# ---------------------------------------------------------------------------
# Staged storage paths
# ---------------------------------------------------------------------------

def _canonical_storage_relpath(value) -> str:
    """Normalize and validate a deployment-relative inventory path."""
    raw = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or ".." in path.parts
        or not path.parts
        or path.parts[0] != "raw_data"
    ):
        raise ValueError(f"Invalid storage_relpath: {value!r}")
    return path.as_posix()


def default_storage_relpath(device_label, filename) -> str:
    """Return the historical flat path for one inventory identity."""
    device_label = str(device_label or "").strip()
    filename = str(filename or "").strip()
    if not device_label or not filename:
        return ""
    return _canonical_storage_relpath(
        PurePosixPath("raw_data", device_label, filename).as_posix()
    )


def inventory_storage_relpath(entry: dict) -> str:
    """Return one entry's physical path with legacy flat fallback.

    Sessions written before split-aware storage do not contain
    ``storage_relpath``. They continue to resolve as
    ``raw_data/<device_label>/<new_filename>``.
    """
    device_label = str(entry.get("device_label", "") or "").strip()
    filename = str(entry.get("new_filename", "") or "").strip()
    stored = entry.get("storage_relpath", "")
    if not stored:
        return default_storage_relpath(device_label, filename)

    relative_path = _canonical_storage_relpath(stored)
    parts = PurePosixPath(relative_path).parts
    if filename and parts[-1] != filename:
        raise ValueError(
            f"storage_relpath filename {parts[-1]!r} does not match {filename!r}"
        )
    if device_label and (len(parts) < 3 or parts[1] != device_label):
        stored_device = parts[1] if len(parts) > 1 else ""
        raise ValueError(
            f"storage_relpath device {stored_device!r} does not match {device_label!r}"
        )
    return relative_path


def set_inventory_storage_relpath(entry: dict, relative_path) -> str:
    """Validate, store, and return a new physical path for an inventory entry."""
    candidate = dict(entry)
    candidate["storage_relpath"] = str(relative_path)
    normalized = inventory_storage_relpath(candidate)
    entry["storage_relpath"] = normalized
    return normalized


def index_inventory_by_storage_relpath(file_inventory) -> dict[str, dict]:
    """Index complete inventory entries by canonical deployment-relative path."""
    indexed: dict[str, dict] = {}
    for entry in file_inventory:
        relative_path = inventory_storage_relpath(entry)
        if not relative_path:
            continue
        if relative_path in indexed:
            raise ValueError(f"Duplicate inventory storage path: {relative_path}")
        indexed[relative_path] = entry
    return indexed


def deduplicate_exact_storage_entries(file_inventory) -> list[dict]:
    """Remove only provably identical duplicate storage records in place.

    Historical interrupted/resumed sessions can contain the same CONFIG record
    twice.  Colliding records are safe to collapse only when they identify the
    same source-relative file and carry the same non-empty SHA-256.  Any other
    collision remains a hard error so genuinely conflicting data is never
    discarded silently.  Returns the removed records for audit/logging.
    """
    indexed: dict[str, dict] = {}
    kept: list[dict] = []
    removed: list[dict] = []
    for entry in file_inventory:
        relative_path = inventory_storage_relpath(entry)
        prior = indexed.get(relative_path)
        if prior is None:
            indexed[relative_path] = entry
            kept.append(entry)
            continue

        same_source = (
            str(prior.get("source_relpath", ""))
            == str(entry.get("source_relpath", ""))
            and bool(entry.get("source_relpath"))
        )
        prior_sha256 = str(prior.get("file_hash_sha256", ""))
        same_hash = bool(prior_sha256) and prior_sha256 == str(
            entry.get("file_hash_sha256", "")
        )
        same_sha1 = (
            not prior.get("file_hash_sha1")
            or not entry.get("file_hash_sha1")
            or prior.get("file_hash_sha1") == entry.get("file_hash_sha1")
        )
        if same_source and same_hash and same_sha1:
            removed.append(entry)
            continue
        raise ValueError(f"Conflicting duplicate inventory storage path: {relative_path}")

    if removed:
        file_inventory[:] = kept
    return removed


def inventory_by_source_relpath(file_inventory, device_label) -> dict[str, dict]:
    """Index one device's inventory by card-relative source identity."""
    indexed: dict[str, dict] = {}
    for entry in file_inventory:
        if entry.get("device_label") != device_label:
            continue
        source_relpath = str(entry.get("source_relpath", "") or "")
        if not source_relpath:
            continue
        prior = indexed.get(source_relpath)
        if prior is not None and prior is not entry:
            raise ValueError(
                f"Duplicate inventory source path for {device_label}: {source_relpath}"
            )
        indexed[source_relpath] = entry
    return indexed


def next_plain_file_sequence(file_inventory, device_label) -> int:
    """Return one past the greatest existing ``_NNNNN`` plain-media suffix.

    Counting records is unsafe after an intentional duplicate drop because a
    gap can make ``count + 1`` reuse an existing destination filename.
    """
    import re

    greatest = 0
    for entry in file_inventory:
        if entry.get("device_label") != device_label:
            continue
        stem = Path(str(entry.get("new_filename", ""))).stem
        match = re.search(r"_(\d{5})$", stem)
        if match:
            greatest = max(greatest, int(match.group(1)))
    return greatest + 1


# ---------------------------------------------------------------------------
# Deterministic card walk
# ---------------------------------------------------------------------------

def sorted_walk(source_dir):
    """Yield ``(root, dirs, files)`` like :func:`os.walk`, but with both
    directories and files visited in sorted (alphabetical) order.

    Plain ``os.walk`` returns entries in filesystem-dependent order. For
    Reconyx cameras that spread a deployment's bursts across several
    ``NNNRECNX`` folders, that makes the per-device event numbering depend on
    disk order — i.e. non-reproducible. Sorting both lists makes the traversal
    deterministic; sorting ``dirs`` in place also steers ``os.walk``'s descent.
    """
    for root, dirs, files in os.walk(source_dir):
        dirs.sort()
        files.sort()
        yield root, dirs, files


def count_expected_files(source_dir) -> int:
    """Count the files a card scan will keep: everything ``classify_file`` does
    not call ``"other"``, skipping dotfiles and resource forks.

    Kept identical to the wizard's copy loop so the post-copy "expected vs
    inventory" check only differs on genuine drops (hash mismatch, duplicate).
    The previous per-device extension list diverged from ``classify_file``
    (e.g. it counted only ``.jpg``/``.jpeg`` for cameras, and only
    ``CONFIG.TXT`` among ``.txt`` files), so the expected count and the kept
    set could disagree on files that were perfectly fine.
    """
    return sum(
        1
        for f in Path(source_dir).rglob("*")
        if f.is_file()
        and not f.name.startswith(".")
        and not f.name.startswith("_")
        and classify_file(f.name) != "other"
    )


def already_copied_relpaths(file_inventory, device_label) -> set:
    """Source-relative paths already inventoried for a device, for resume.

    Matching on the card-relative path (not the bare filename) avoids skipping
    a file that merely shares a basename with one in another folder — e.g.
    ``100RECNX/RCNX0001.JPG`` vs ``101RECNX/RCNX0001.JPG`` — and is stable even
    if the card remounts at a different location. Entries without a
    ``source_relpath`` (pre-fix sessions) are ignored.
    """
    return {
        entry["source_relpath"]
        for entry in file_inventory
        if entry.get("device_label") == device_label and entry.get("source_relpath")
    }


def reconcile_device_dir(device_dir, file_inventory, device_label) -> list[str]:
    """Delete staged files not backed by an inventory record, before a resume.

    ``session.json`` is only flushed on a wall-clock interval, so a crash or
    quit can leave files physically copied into ``device_dir`` that never made
    it into the persisted ``file_inventory``. On the next resume those files
    are unrecognized: the copy loop re-copies their source under fresh event
    numbers and strands the first copies as orphans (duplicate content, and
    gaps in the app-assigned event sequence).

    Reconciling makes the inventory authoritative: any file in ``device_dir``
    whose name is not a ``new_filename`` recorded for ``device_label`` is an
    orphan and is removed. The un-recorded source files are then re-copied
    cleanly by the caller, so numbering stays contiguous. Retired legacy device
    manifest sidecars and dotfiles are always exempt.

    Returns the list of removed filenames (empty when nothing was orphaned).
    """
    device_dir = Path(device_dir)
    if not device_dir.is_dir():
        return []

    expected = {
        entry["new_filename"]
        for entry in file_inventory
        if entry.get("device_label") == device_label and entry.get("new_filename")
    }
    manifest_name = f"{device_label}_manifest.json"

    removed: list[str] = []
    for path in sorted(device_dir.iterdir()):
        if not path.is_file():
            continue
        name = path.name
        if name.startswith(".") or name == manifest_name or name in expected:
            continue
        try:
            path.unlink()
            removed.append(name)
        except OSError:
            # A file we can't delete is left in place rather than aborting the
            # resume; the post-copy count check will still surface a mismatch.
            pass
    return removed


# ---------------------------------------------------------------------------
# Naming convention
# ---------------------------------------------------------------------------

def build_renamed_filename(
    org: str,
    site_short_name: str,
    plot_num,
    dev_code: str,
    date_str: str,
    seq_str: str,
    file_ext: str,
) -> str:
    """Build a renamed file following the deployment naming convention.

    ``{org}_{site_short_name}_plot{plot_num}_{dev_code}_{date_str}_{seq_str}{file_ext}``

    The caller supplies ``date_str`` and ``seq_str`` so the same convention
    covers all three cases the wizard produces:

    * config sidecar — ``date_str`` is the deployment *start* date, ``seq_str``
      is ``"CONFIG_01"``;
    * sequence-aware image — ``seq_str`` is ``f"{event_num:05d}_{position}"``;
    * plain media — ``seq_str`` is a zero-padded running counter ``f"{n:05d}"``.
    """
    return f"{org}_{site_short_name}_plot{plot_num}_{dev_code}_{date_str}_{seq_str}{file_ext}"


def build_deployment_filename(
    deployment_id: str,
    seq_str: str,
    file_ext: str,
) -> str:
    """Build a prospective media filename from an exact deployment ID.

    Historical files keep the names written by their original app version. New
    ingest uses this helper so a `-seqNN` deployment discriminator is preserved in
    every media and CONFIG filename.
    """
    deployment_id = str(deployment_id or "").strip()
    if not deployment_id:
        raise ValueError("deployment_id is required for a new media filename")
    return f"{deployment_id}_{seq_str}{file_ext}"


# ---------------------------------------------------------------------------
# Per-file inventory record
# ---------------------------------------------------------------------------

def build_inventory_record(
    *,
    original_filename: str,
    new_filename: str,
    plot_num,
    plot_label: str,
    dev_code: str,
    device_label: str,
    device_id: str,
    deployment_id: str,
    file_type: str,
    file_size_bytes: int,
    file_hash_sha256: str,
    file_hash_sha1: str,
    recorded_datetime: str,
    source_path: str,
    source_relpath: str = "",
    plot_metadata: dict,
    exif_data: dict,
    config_data: dict,
    wav_data: dict,
    reconyx_data: dict,
    trigger_type,
    seq_pos,
    seq_total,
    event_num,
    date_installed: str,
    soundhub_config: dict,
) -> dict:
    """Assemble one ``file_inventory`` record from all metadata sources.

    Source precedence is preserved exactly from the original:

    * device-physical fields (sample rate, gain, filters, battery, temperature)
      prefer the per-file WAV comment over the device-wide CONFIG.TXT;
    * model/make fields prefer CONFIG.TXT;
    * schedule fields (start/end time, frequency, duration) prefer CONFIG.TXT
      and fall back to the device-keyed SoundHub config defaults;
    * image-only Reconyx extras (moon phase, battery, temperature) come from
      ExifTool; audio rows leave them blank and vice-versa.

    Sequence fields are recorded only when the source had Reconyx sequence data
    (``seq_pos is not None``); otherwise they are blank, matching the original.
    """
    return {
        'original_filename': original_filename,
        'new_filename': new_filename,
        # Physical path relative to the deployment folder. This begins flat and
        # is updated when an oversized camera folder is split for WI upload.
        'storage_relpath': default_storage_relpath(device_label, new_filename),
        'plot_number': plot_num,
        'plot_label': plot_label,
        'device_type': dev_code,
        'device_label': device_label,
        'device_id': device_id,
        'deployment_id': deployment_id,
        # Image identity comes from the selected curated device placement;
        # audio rows leave this blank and use the AudioMoth device_id instead.
        'camera_id': device_id if file_type == 'image' else '',
        # The camera's hardware serial read from EXIF (image rows only); QC
        # cross-checks that camera_id is a substring of this.
        'camera_serial_exif': reconyx_data.get('camera_serial_exif', ''),
        'file_type': file_type,
        'file_size_bytes': file_size_bytes,
        'file_hash_sha256': file_hash_sha256,
        'file_hash_sha1': file_hash_sha1,
        'recorded_datetime': recorded_datetime,
        'latitude': plot_metadata.get('plot_latitude') or '',
        'longitude': plot_metadata.get('plot_longitude') or '',
        'elevation_m': plot_metadata.get('elevation_m') or '',
        'camera_make': exif_data.get('Make', ''),
        'camera_model': exif_data.get('Model', ''),
        'sequence_trigger_type': trigger_type or '',
        'sequence_event_num': event_num if seq_pos is not None else '',
        'sequence_position': seq_pos if seq_pos is not None else '',
        'sequence_total': seq_total if seq_total is not None else '',
        'source_path': source_path,
        'source_relpath': source_relpath,
        # AudioMoth fields (audio rows only; blank for images)
        # Identity: GUANO (wav_data) is authoritative, then CONFIG.TXT, then the
        # soundhub_config.json default applied downstream in metadata_csv.
        'ARU_make':    wav_data.get('ARU_make', '') or config_data.get('ARU_make', ''),
        'ARU_model':   wav_data.get('ARU_model', '') or config_data.get('ARU_model', ''),
        'sample_rate_hz': wav_data.get('sample_rate_hz', '') or config_data.get('sample_rate_hz', ''),
        'gain':        wav_data.get('gain_setting', '') or config_data.get('gain_setting', ''),
        'filter_type_khz': hz_to_khz(
            wav_data.get('high_pass_filter_hz', '')
            or wav_data.get('low_pass_filter_hz', '')
            or config_data.get('high_pass_filter_hz', '')
            or config_data.get('low_pass_filter_hz', '')
        ),
        'battery_voltage': wav_data.get('battery_voltage', ''),
        'temperature_c': wav_data.get('temperature_c', '') or reconyx_data.get('temperature_c', ''),
        'date_installed': date_installed,
        # Actual recording date range from CONFIG.TXT (audio only); blank when the
        # config omits it, so metadata_csv can fall back to file timestamps.
        'deployment_start_date': config_data.get('deployment_start_date', ''),
        'deployment_end_date':   config_data.get('deployment_end_date', ''),
        'deployment_start_time': config_data.get('deployment_start_time', '') or soundhub_config.get(f'deployment_start_time_{dev_code}', ''),
        'deployment_end_time':   config_data.get('deployment_end_time', '')   or soundhub_config.get(f'deployment_end_time_{dev_code}', ''),
        'frequency':   config_data.get('frequency', '') or soundhub_config.get('frequency', ''),
        'duration':    config_data.get('duration', '')   or soundhub_config.get(f'duration_{dev_code}', ''),
        # Actual file length (seconds) and why the recording ended — both from the
        # WAV header/ICMT (audio rows only). Distinct from the scheduled 'duration'.
        'recording_duration_sec': wav_data.get('recording_duration_sec', ''),
        'recording_stop_reason':  wav_data.get('recording_stop_reason', ''),
        'filter_type_duration':  wav_data.get('filter_type_duration', '')  or config_data.get('filter_type_duration', ''),
        'filter_type_amplitude': wav_data.get('filter_type_amplitude', '') or config_data.get('filter_type_amplitude', ''),
        # Reconyx extras (image rows only; blank for audio)
        'moon_phase':          reconyx_data.get('moon_phase', ''),
        'battery_voltage_avg': reconyx_data.get('battery_voltage_avg', ''),
        'battery_type':        reconyx_data.get('battery_type', ''),
    }


# ---------------------------------------------------------------------------
# Retired legacy device manifest
# ---------------------------------------------------------------------------

def refresh_legacy_device_manifest(
    device_label: str,
    device_dir: Path,
    entries: list,
) -> bool:
    """Refresh an existing retired per-device manifest; never create one.

    New ingestions use the session inventory and Box upload manifest instead.
    This compatibility helper keeps a historical manifest internally consistent
    when a maintenance utility changes file bytes. Returns ``True`` only when an
    existing manifest was successfully rewritten.
    """
    manifest_path = Path(device_dir) / f"{device_label}_manifest.json"
    if not manifest_path.exists():
        return False

    manifest = {
        "device_label": device_label,
        "generated": datetime.now().isoformat(),
        "file_count": len(entries),
        "files": [
            {
                "filename": e.get("new_filename", ""),
                "size_bytes": e.get("file_size_bytes", 0),
                "sha256": e.get("file_hash_sha256", ""),
                "sha1": e.get("file_hash_sha1", ""),
            }
            for e in entries
        ],
    }
    try:
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)
        return True
    except Exception as exc:
        print(f"Warning: could not refresh legacy manifest for {device_label}: {exc}")
        return False


# ---------------------------------------------------------------------------
# Deployment summary
# ---------------------------------------------------------------------------

def generate_session_summary(
    deployment_folder: Path,
    metadata: dict,
    file_inventory: list,
    devices: list,
) -> Path:
    """Write ``deployment_summary.txt`` (a human-readable digest) and return its path."""
    metadata = normalize_deployment_event_metadata(metadata)
    lines = []
    lines.append("=" * 60)
    lines.append("CASSN DEPLOYMENT SUMMARY")
    lines.append("=" * 60)
    lines.append(f"Generated:        {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"Site name:        {metadata.get('site_name', '?')}")
    lines.append(f"Site short name:  {metadata.get('site_short_name', '?')}")
    lines.append(f"Site code:        {metadata.get('site_code', '?')}")
    lines.append(f"Organization:     {metadata.get('organization', '?')}")
    lines.append(f"Observer:         {metadata.get('observer', '?')}")
    lines.append(
        "Deployment event start: "
        f"{metadata.get('deployment_event_start_date', '?')}"
    )
    lines.append(
        "Deployment event end:   "
        f"{metadata.get('deployment_event_end_date', '?')}"
    )
    lines.append("")

    # Device summary
    device_counts: dict[str, int] = {}
    for entry in file_inventory:
        dl = entry.get("device_label", "unknown")
        device_counts[dl] = device_counts.get(dl, 0) + 1
    total_files = len(file_inventory)

    lines.append(f"Total files inventoried: {total_files}")
    lines.append(f"Devices configured:      {len(devices)}")
    lines.append("")
    lines.append("Per-device file counts:")
    for label, count in sorted(device_counts.items()):
        lines.append(f"  {label}: {count} files")
    lines.append("")

    # Coordinate list (unique plot → first seen lat/lon, plus elevation when
    # plots.csv carries one — the column is optional, so it is appended only
    # when present rather than printed as a bare trailing comma.)
    plot_coords: dict[str, tuple] = {}
    for entry in file_inventory:
        p = entry.get("plot_number", "")
        lat = entry.get("latitude", "")
        lon = entry.get("longitude", "")
        if p and lat and lon and p not in plot_coords:
            plot_coords[p] = (lat, lon, entry.get("elevation_m", ""))
    if plot_coords:
        lines.append("Plot coordinates (first seen):")
        for plot, (lat, lon, elev) in sorted(plot_coords.items()):
            elev_suffix = f", {elev} m" if str(elev).strip() else ""
            lines.append(f"  Plot {plot}: {lat}, {lon}{elev_suffix}")
        lines.append("")

    # QC summary
    qc_path = qc_path_for(deployment_folder, "qc_report.json")
    if qc_path.exists():
        try:
            with open(qc_path) as f:
                qc_data = json.load(f)
            current = qc_data.get("current_state", [])
            errors = [c for c in current if c.get("severity") == "error"]
            warnings_list = [c for c in current if c.get("severity") == "warning"]
            lines.append(f"QC report: {len(errors)} error(s), {len(warnings_list)} warning(s) — current state")
            if errors:
                lines.append("  Errors:")
                for c in errors[:10]:
                    lines.append(f"    [{c.get('_device', '')}] {c.get('message', '')}")
            if warnings_list:
                lines.append("  Warnings:")
                for c in warnings_list[:10]:
                    lines.append(f"    [{c.get('_device', '')}] {c.get('message', '')}")
        except Exception:
            lines.append("QC report: could not read qc_report.json")
    else:
        lines.append("QC report: none")
    lines.append("")
    lines.append("=" * 60)

    summary_path = qc_path_for(deployment_folder, "deployment_summary.txt")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return summary_path


# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------

SESSION_SCHEMA_VERSION = 1


def write_session(deployment_folder: Path, session: dict) -> str:
    """Atomically persist a session dict to ``session.json`` in the deployment.

    Writes to a ``.json.tmp`` sibling first and ``replace()``s it into place,
    which is atomic on POSIX — an interrupted save never leaves a half-written
    ``session.json``. Returns an empty string on success or a user-displayable
    error on failure; callers must stop collection when recovery state cannot be
    persisted.

    Serialized compactly (no indent, no separator padding). This file is a
    machine-only crash-recovery record with one record per copied file, so its
    whitespace is pure overhead: dropping it cuts ~20% of the bytes written and
    speeds serialization several-fold on large deployments. ``json.load`` ignores
    the formatting, so resume round-trips the identical data.
    """
    try:
        session_path = deployment_folder / "session.json"
        tmp_path = session_path.with_suffix('.json.tmp')
        with open(tmp_path, "w") as f:
            json.dump(session, f, separators=(",", ":"), default=str)
        tmp_path.replace(session_path)
        return ""
    except Exception as exc:
        return f"Could not save session recovery file: {exc}"


def find_all_sessions(staging_root: Path) -> list:
    """Scan ``staging_root`` for all ``session.json`` files.

    Returns a list of ``{path, data, status, saved_at, error_msg}`` dicts, where
    ``status`` is ``"ok"`` or ``"corrupted"``, sorted newest first (by
    ``saved_at`` for parseable files, by file mtime for corrupted ones).
    Corrupted sessions are also recorded in that deployment's
    ``qc_report.json`` so the audit trail captures them.
    """
    sessions: list[dict] = []
    if not staging_root.exists():
        return sessions
    try:
        subdirs = [d for d in staging_root.iterdir() if d.is_dir()]
    except Exception:
        return sessions

    for subdir in subdirs:
        session_file = subdir / "session.json"
        if not session_file.exists():
            continue
        try:
            with open(session_file, "r") as f:
                data = json.load(f)
            if data.get("schema_version") != SESSION_SCHEMA_VERSION:
                continue
            folder = Path(data.get("deployment_folder", ""))
            if not folder.exists():
                continue
            saved_at = datetime.fromisoformat(data.get("saved_at", "2000-01-01"))
            sessions.append({"path": session_file, "data": data, "status": "ok",
                             "saved_at": saved_at, "error_msg": ""})
        except json.JSONDecodeError as e:
            mtime = datetime.fromtimestamp(session_file.stat().st_mtime)
            sessions.append({"path": session_file, "data": None, "status": "corrupted",
                             "saved_at": mtime, "error_msg": str(e)})
            # Record the corruption in that deployment's qc_report.json so the audit
            # trail captures it even if the user never opens the deployment again.
            try:
                append_qc_report(session_file.parent, "session_health", "", "error",
                                 f"session.json could not be parsed: {e}")
            except Exception:
                pass
        except Exception:
            continue

    sessions.sort(key=lambda x: x["saved_at"], reverse=True)
    return sessions


def find_all_sessions_multi(staging_roots) -> list:
    """Scan several staging roots for ``session.json`` files, merged newest-first.

    Delegates per-root scanning to :func:`find_all_sessions`, so parsing and
    corruption handling are identical. Sessions that resolve to the same file
    (e.g. when two configured roots overlap or one is a symlink of another) are
    de-duplicated, keeping the first occurrence. A root that does not exist —
    such as an unplugged external drive — contributes nothing rather than
    raising, so a disconnected location is silently skipped.
    """
    merged: list[dict] = []
    seen: set = set()
    for root in staging_roots:
        for sess in find_all_sessions(Path(root)):
            try:
                key = sess["path"].resolve()
            except Exception:
                key = sess["path"]
            if key in seen:
                continue
            seen.add(key)
            merged.append(sess)
    merged.sort(key=lambda x: x["saved_at"], reverse=True)
    return merged
