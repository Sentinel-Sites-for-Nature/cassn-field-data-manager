"""
The file inventory: naming, per-file records, device manifests, session state.

The wizard walks each SD card and, for every kept file, records one dict in its
``file_inventory`` list. That list is the spine of the whole app — every CSV,
manifest, and QC check reads from it. This module owns the parts of that
process that are pure given their inputs, so they can be tested without a GUI or
a physical card:

* :func:`build_renamed_filename` — the single source of truth for the
  ``{org}_{site}_plot{n}_{devcode}_{YYYYMMDD}_{seq}{ext}`` naming convention.
* :func:`build_inventory_record` — assembles one inventory dict from the four
  metadata sources (EXIF, Reconyx/ExifTool, CONFIG.TXT, WAV comment) plus plot
  and SoundHub lookups, applying the exact source-precedence the original used.
* :func:`write_device_manifest` — the tamper-evident fixity sidecar written
  before a card is ejected.
* :func:`generate_session_summary` — the human-readable deployment digest.
* :func:`write_session` / :func:`find_all_sessions` — crash-safe session.json
  persistence and the staging-wide scan used to resume or reopen deployments.

The stateful parts — walking the card, incrementing the per-device sequence and
event counters, hashing, copying, and emitting QC findings — stay in the wizard,
which threads its loop-carried counters through :func:`build_renamed_filename`
and :func:`build_inventory_record`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from cassn.core.audio_metadata import hz_to_khz
from cassn.core.classification import classify_file
from cassn.core.quality_control import append_qc_report, qc_path_for


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


# ---------------------------------------------------------------------------
# Naming convention
# ---------------------------------------------------------------------------

def build_renamed_filename(
    org: str,
    site: str,
    plot_num,
    dev_code: str,
    date_str: str,
    seq_str: str,
    file_ext: str,
) -> str:
    """Build a renamed file following the deployment naming convention.

    ``{org}_{site}_plot{plot_num}_{dev_code}_{date_str}_{seq_str}{file_ext}``

    The caller supplies ``date_str`` and ``seq_str`` so the same convention
    covers all three cases the wizard produces:

    * config sidecar — ``date_str`` is the deployment *start* date, ``seq_str``
      is ``"CONFIG_01"``;
    * sequence-aware image — ``seq_str`` is ``f"{event_num:05d}_{position}"``;
    * plain media — ``seq_str`` is a zero-padded running counter ``f"{n:05d}"``.
    """
    return f"{org}_{site}_plot{plot_num}_{dev_code}_{date_str}_{seq_str}{file_ext}"


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
        'plot_number': plot_num,
        'plot_label': plot_label,
        'device_type': dev_code,
        'device_label': device_label,
        'device_id': device_id,
        # Image identity comes from cameras.csv (the camera_id); audio rows
        # leave this blank and use device_id (the AudioMoth serial) instead.
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
# Device manifest
# ---------------------------------------------------------------------------

def write_device_manifest(device_label: str, device_dir: Path, entries: list) -> None:
    """Write a fixity manifest sidecar for a completed device.

    Records the file count plus ``{filename, size_bytes, sha256, sha1}`` for
    every file. Written before the SD card is ejected — the last chance for a
    tamper-evident record.
    """
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
    manifest_path = device_dir / f"{device_label}_manifest.json"
    try:
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)
    except Exception as exc:
        print(f"Warning: could not write manifest for {device_label}: {exc}")


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
    lines = []
    lines.append("=" * 60)
    lines.append("CASSN DEPLOYMENT SUMMARY")
    lines.append("=" * 60)
    lines.append(f"Generated:        {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"Reserve:          {metadata.get('reserve_name', '?')}")
    lines.append(f"Organization:     {metadata.get('organization', '?')}")
    lines.append(f"Observer:         {metadata.get('observer', '?')}")
    lines.append(f"Deployment event start: {metadata.get('deployment_start', '?')}")
    lines.append(f"Deployment event end:   {metadata.get('deployment_end', '?')}")
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

    # Coordinate list (unique plot → first seen lat/lon)
    plot_coords: dict[str, tuple] = {}
    for entry in file_inventory:
        p = entry.get("plot_number", "")
        lat = entry.get("latitude", "")
        lon = entry.get("longitude", "")
        if p and lat and lon and p not in plot_coords:
            plot_coords[p] = (lat, lon)
    if plot_coords:
        lines.append("Plot coordinates (first seen):")
        for plot, (lat, lon) in sorted(plot_coords.items()):
            lines.append(f"  Plot {plot}: {lat}, {lon}")
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


def write_session(deployment_folder: Path, session: dict) -> None:
    """Atomically persist a session dict to ``session.json`` in the deployment.

    Writes to a ``.json.tmp`` sibling first and ``replace()``s it into place,
    which is atomic on POSIX — an interrupted save never leaves a half-written
    ``session.json``. Swallows all errors: a failed save must never crash the app.
    """
    try:
        session_path = deployment_folder / "session.json"
        tmp_path = session_path.with_suffix('.json.tmp')
        with open(tmp_path, "w") as f:
            json.dump(session, f, indent=2, default=str)
        tmp_path.replace(session_path)
    except Exception:
        pass


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
