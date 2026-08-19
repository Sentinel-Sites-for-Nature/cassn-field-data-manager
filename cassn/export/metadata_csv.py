"""
The deployment metadata CSVs: ``image_file_metadata.csv`` and ``audio_file_metadata.csv``.

:func:`build_metadata_rows` is the pure heart of this module — it turns the
``file_inventory`` plus the lookup tables into the two row sets, applying the
same source lookups and SoundHub fallbacks the original did. The two
CSV column orders live in :mod:`cassn.config` (``IMAGE_FIELDS`` / ``AUDIO_FIELDS``)
and ``csv.DictWriter(extrasaction="ignore")`` drops any extra inventory keys, so
the image and audio rows can safely share the common ``base`` block.

:func:`write_metadata_outputs` is the orchestrator the GUI calls: it builds the
rows, writes both CSVs (logging an incremental row-count diff), writes
``deployment_event_record.json``, and snapshots the lookup tables into ``qc/``.
All user-facing logging goes through an injected ``log`` callback, so nothing
here imports Qt.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from cassn.config import AUDIO_FIELDS, IMAGE_FIELDS, LOCAL_DATA_DIR, VERSION
from cassn.core.audio_metadata import normalize_gain
from cassn.core.quality_control import append_qc_report, snapshot_lookup_tables
from cassn.export.wildlife_insights import deployment_event_name, subproject_for


def build_metadata_rows(metadata: dict, file_inventory: list, lookups) -> tuple[list[dict], list[dict]]:
    """Build ``(image_rows, audio_rows)`` from the file inventory and lookups.

    ``lookups`` is a :class:`cassn.lookups.LookupTables`; this reads its
    canonical ``sites``, event-scoped ``arus`` / ``cameras`` views
    derived from device-level deployments, ``soundhub_config`` (ARU defaults),
    and ``wi_config`` (WI project defaults).
    """
    org = metadata.get('organization', '')
    site_name = metadata.get('site_name', '')
    site_short_name = metadata.get('site_short_name', '')
    site_code = metadata.get('site_code', '')
    end = metadata.get('deployment_end', '')
    observer = metadata.get('observer', '')
    deployment_event_id = metadata.get("deployment_event_id", "")
    if not deployment_event_id:
        raise ValueError("No Survey123 deployment_event_id is active")
    subproject = subproject_for(site_short_name, end)
    now_iso = datetime.now(timezone.utc).isoformat()

    soundhub_config = lookups.soundhub_config
    wi_config = lookups.wi_config

    image_rows: list[dict] = []
    audio_rows: list[dict] = []

    # Per-device fallback recording range: earliest/latest actual recorded-file
    # date for each device, used when a CONFIG.TXT omits its First/Last recording
    # date. recorded_datetime is ISO 8601, so its date prefix sorts correctly.
    dev_date_range: dict[str, tuple[str, str]] = {}
    for entry in file_inventory:
        if entry.get('file_type') != 'audio':
            continue
        rec = (entry.get('recorded_datetime') or '')[:10]
        if not rec:
            continue
        label = entry.get('device_label', '')
        lo, hi = dev_date_range.get(label, (rec, rec))
        dev_date_range[label] = (min(lo, rec), max(hi, rec))

    for entry in file_inventory:
        plot_num = entry.get('plot_number', '')
        dev_type = entry.get('device_type', '')
        file_type = entry.get('file_type', '')

        try:
            plot_num_int = int(plot_num)
        except (TypeError, ValueError):
            plot_num_int = None

        placename = f"{site_short_name}_plot{plot_num}"

        # Event-scoped placement lookup from the new device-level
        # deployments.csv. The compatibility views are populated only after a
        # Survey123 round has been selected.
        placement_key = (site_short_name, plot_num_int, dev_type)
        if dev_type in {"ML", "SA"}:
            placement_row = lookups.cameras.get(placement_key, {})
            aru_row = {}
        else:
            placement_row = lookups.arus.get(placement_key, {})
            aru_row = placement_row
        deployment_id = placement_row.get("deployment_id", "")
        if not deployment_id:
            raise ValueError(
                f"No Survey123 deployment row for {site_short_name} "
                f"plot {plot_num} {dev_type}"
            )
        placement_start = placement_row.get("deployment_start_date", "")
        placement_end = placement_row.get("deployment_end_date", "")
        if not placement_start or not placement_end:
            raise ValueError(
                f"Incomplete Survey123 placement interval for "
                f"{site_short_name} plot {plot_num} {dev_type}"
            )

        # SoundHub config lookup (keyed by device type suffix)
        aru_container = soundhub_config.get(f'ARU_container_{dev_type}', '')
        aru_microphone = soundhub_config.get('ARU_microphone', '')
        sh_feature_type = soundhub_config.get('feature_type', '')
        sh_mounted_on = soundhub_config.get('mounted_on', '')

        base = {
            'filename':           entry.get('new_filename', ''),
            'original_filename':  entry.get('original_filename', ''),
            'deployment_event_id': deployment_event_id,
            'deployment_id':      deployment_id,
            'organization':       org,
            'site_name':          site_name,
            'site_short_name':    site_short_name,
            'site_code':          site_code,
            'subproject':         subproject,
            'subproject_design':  '',
            'placename':          placename,
            'event_name':         deployment_event_name(placement_start, placement_end),
            'event_description':  '',
            'plot_number':        plot_num,
            'device_type':        dev_type,
            'file_type':          file_type,
            'file_size_bytes':    entry.get('file_size_bytes', ''),
            'file_hash_sha256':   entry.get('file_hash_sha256', ''),
            'file_hash_sha1':     entry.get('file_hash_sha1', ''),
            'recorded_datetime':  entry.get('recorded_datetime', ''),
            'latitude':           entry.get('latitude', ''),
            'longitude':          entry.get('longitude', ''),
            'elevation_m':        entry.get('elevation_m', ''),
            'app_version':        VERSION,
            'processing_datetime': now_iso,
            'is_uploaded_to_box': False,
            'box_uploader':       '',
            'box_upload_datetime': '',
            'is_uploaded_to_pelican': False,
            'pelican_uploader':   '',
            'pelican_upload_datetime': '',
            'notes':              '',
        }

        if file_type == 'image':
            cam_key = (
                (site_short_name, plot_num_int, dev_type)
                if plot_num_int else None
            )
            cam = lookups.cameras.get(cam_key, {}) if cam_key else {}
            row = {**base,
                'start_date':    f"{placement_start} 00:00:00",
                'end_date':      f"{placement_end} 23:59:59",
                'recorded_by':   observer,
                'camera_id':     entry.get('camera_id', ''),
                'camera_serial_exif': entry.get('camera_serial_exif', ''),
                'camera_make':   entry.get('camera_make', ''),
                'camera_model':  entry.get('camera_model', ''),
                'sequence_trigger_type': entry.get('sequence_trigger_type', ''),
                'sequence_event_num':    entry.get('sequence_event_num', ''),
                'sequence_position':     entry.get('sequence_position', ''),
                'sequence_total':        entry.get('sequence_total', ''),
                'temperature_c':         entry.get('temperature_c', ''),
                'moon_phase':            entry.get('moon_phase', ''),
                'battery_voltage':       entry.get('battery_voltage', ''),
                'battery_voltage_avg':   entry.get('battery_voltage_avg', ''),
                'battery_type':          entry.get('battery_type', ''),
                # WI fields
                'project_id':            wi_config.get(f'project_id_{dev_type}', ''),
                'bait_type':             wi_config.get(f'bait_type_{dev_type}', ''),
                'bait_description':      wi_config.get(f'bait_description_{dev_type}', ''),
                'event_type':            wi_config.get('event_type', 'Temporal'),
                'quiet_period':          wi_config.get('quiet_period', 0),
                'camera_functioning':    wi_config.get('camera_functioning_default', 'Camera Functioning'),
                'feature_type':          cam.get('feature_type', ''),
                'feature_type_methodology': '',
                'sensor_height':         cam.get('sensor_height', ''),
                'height_other':          '',
                'sensor_orientation':    cam.get('sensor_orientation', ''),
                'orientation_other':     '',
                'plot_treatment':        cam.get('plot_treatment', ''),
                'plot_treatment_description': cam.get('plot_treatment_description', ''),
                'detection_distance':    cam.get('detection_distance', ''),
                'is_submitted_to_wi':    False,
                'wi_submitter':          '',
                'wi_submission_datetime': '',
            }
            image_rows.append(row)

        else:  # audio or config
            # Actual recording range: CONFIG.TXT dates first, else the device's
            # earliest/latest recorded-file date. ``date_installed`` below uses
            # this device placement's Survey123 start date.
            dev_min, dev_max = dev_date_range.get(entry.get('device_label', ''), ('', ''))
            row = {**base,
                'deployment_start_date': entry.get('deployment_start_date') or dev_min,
                'deployment_end_date':   entry.get('deployment_end_date') or dev_max,
                'recorded_by':           observer,
                'device_id':             entry.get('device_id', ''),
                'ARU_make':              entry.get('ARU_make', '') or soundhub_config.get('ARU_make', ''),
                'ARU_model':             entry.get('ARU_model', '') or soundhub_config.get('ARU_model', ''),
                'sample_rate_hz':        entry.get('sample_rate_hz', '') or soundhub_config.get(f'sample_rate_hz_{dev_type}', ''),
                # Normalized again here, not only at extraction: gain is written
                # into session.json per file as the card is copied, so a
                # deployment part-copied under an older build carries the
                # device's raw lower-case spelling in records this run did not
                # produce. Re-normalizing at projection makes the CSV uniform
                # without rewriting the inventory.
                'gain':                  normalize_gain(entry.get('gain', '')),
                'filter_type_khz':       entry.get('filter_type_khz', ''),
                'battery_voltage':       entry.get('battery_voltage', ''),
                'temperature_c':         entry.get('temperature_c', ''),
                'date_installed':        placement_start,
                'deployment_start_time': entry.get('deployment_start_time', '') or soundhub_config.get(f'deployment_start_time_{dev_type}', ''),
                'deployment_end_time':   entry.get('deployment_end_time', '')   or soundhub_config.get(f'deployment_end_time_{dev_type}', ''),
                'frequency':             entry.get('frequency', '') or soundhub_config.get('frequency', ''),
                'duration':              entry.get('duration', '')   or soundhub_config.get(f'duration_{dev_type}', ''),
                'recording_duration_sec': entry.get('recording_duration_sec', ''),
                'recording_stop_reason':  entry.get('recording_stop_reason', ''),
                'filter_type_duration':  entry.get('filter_type_duration', ''),
                'filter_type_amplitude': entry.get('filter_type_amplitude', ''),
                'feature_type':          sh_feature_type,
                'feature_type_details':  '',
                'ARU_container':         aru_container,
                'ARU_microphone':        aru_microphone,
                'mounted_on':            sh_mounted_on,
                'sensor_height_meters':  aru_row.get('sensor_height_meters', ''),
                'ARU_status':            aru_row.get('ARU_status', ''),
                'is_submitted_to_soundhub': False,
                'soundhub_submitter':    '',
                'soundhub_submission_datetime': '',
                'is_submitted_to_nabat': False,
                'nabat_submitter':       '',
                'nabat_submission_datetime': '',
            }
            audio_rows.append(row)

    return image_rows, audio_rows


def build_deployment_event_record(metadata: dict, devices: list, file_count: int) -> dict:
    """Build the ``deployment_event_record.json`` payload (deployment info + device list)."""
    return {
        'deployment_info': metadata,
        'devices': [
            {'plot_number': pn, 'plot_label': pl, 'device_type': dc, 'device_label': dl}
            for pn, pl, dc, dl in devices
        ],
        'file_count': file_count,
        'generated': datetime.now().isoformat(),
        'version': VERSION,
    }


def _count_csv_rows(path: Path) -> int:
    """Row count of an existing CSV, or ``-1`` if absent/unreadable (a sentinel)."""
    if not path.exists():
        return -1
    try:
        with open(path, newline='', encoding='utf-8') as f:
            return sum(1 for _ in csv.DictReader(f))
    except Exception:
        return -1


def _format_csv_write_message(name: str, prev_count: int, new_count: int) -> str:
    """Human-readable 'Generated/Updated' line describing a CSV write."""
    if prev_count < 0:
        return f"Generated: {name} ({new_count} rows)"
    if new_count == prev_count:
        return f"Updated: {name} (no change, {new_count} rows)"
    delta = new_count - prev_count
    sign = "+" if delta > 0 else ""
    return f"Updated: {name} ({sign}{delta} rows, {new_count} total)"


def _write_rows(path: Path, fieldnames: list[str], rows: list[dict], *, log: Callable[[str], None]) -> None:
    """Write one CSV and log an incremental row-count diff against the prior file."""
    prev = _count_csv_rows(path)
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    log(_format_csv_write_message(path.name, prev, len(rows)))


def write_metadata_outputs(
    deployment_folder: Path,
    metadata: dict,
    file_inventory: list,
    devices: list,
    lookups,
    *,
    log: Callable[[str], None] = lambda _m: None,
) -> None:
    """Build and write both metadata CSVs, the event record, and the lookup snapshot.

    Safe to call incrementally after each device completes. ``log`` receives the
    same progress lines the original printed to its activity log.
    """
    image_rows, audio_rows = build_metadata_rows(metadata, file_inventory, lookups)

    _write_rows(deployment_folder / "image_file_metadata.csv", IMAGE_FIELDS, image_rows, log=log)
    _write_rows(deployment_folder / "audio_file_metadata.csv", AUDIO_FIELDS, audio_rows, log=log)

    # deployment_event_record.json
    record = build_deployment_event_record(metadata, devices, len(file_inventory))
    manifest_path = deployment_folder / "deployment_event_record.json"
    existed = manifest_path.exists()
    with open(manifest_path, 'w') as f:
        json.dump(record, f, indent=2)
    log(f"{'Updated' if existed else 'Generated'}: deployment_event_record.json")

    # Freeze the active lookup tables alongside the metadata they produced.
    try:
        lookup_manifest = snapshot_lookup_tables(deployment_folder)
        if lookup_manifest:
            ok_count = sum(1 for item in lookup_manifest if "error" not in item)
            err_count = len(lookup_manifest) - ok_count
            severity = "warning" if err_count else "pass"
            append_qc_report(
                deployment_folder, "lookup_snapshot", "", severity,
                f"Snapshotted {ok_count} lookup/config file(s) to qc/lookup_snapshot/"
                + (f"; {err_count} failed" if err_count else ""),
            )
            log(f"Updated: qc/lookup_snapshot/ ({ok_count} file(s))")
        else:
            append_qc_report(
                deployment_folder, "lookup_snapshot", "", "warning",
                f"No lookup/config files found in {LOCAL_DATA_DIR}",
            )
            log("Warning: no lookup/config files available to snapshot")
    except Exception as e:
        append_qc_report(
            deployment_folder, "lookup_snapshot", "", "warning",
            f"Could not snapshot lookup/config files: {e}",
        )
        log(f"Warning: could not snapshot lookup/config files: {e}")
