"""Validated Box-to-cache synchronization for application lookup snapshots."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from cassn.box.auth import BoxConfig, get_box_client
from cassn.box.client import BoxStorage
from cassn.lookups import (
    BOX_MANAGED_FILENAMES,
    DEPLOYMENTS_CSV,
    DEVICES_CSV,
    PLOTS_CSV,
    SITES_CSV,
    SOUNDHUB_JSON,
    WI_CONFIG_JSON,
    LookupSchemaError,
    LookupTables,
    load_device_deployments,
    load_devices,
)


DEVICE_PAIR_FILENAMES = (DEVICES_CSV, DEPLOYMENTS_CSV)
REQUIRED_RUNTIME_FILENAMES = frozenset({
    SITES_CSV,
    PLOTS_CSV,
    DEVICES_CSV,
    DEPLOYMENTS_CSV,
    SOUNDHUB_JSON,
    WI_CONFIG_JSON,
})
LAST_SYNC_FILENAME = ".last_synced"


class LookupBootstrapError(RuntimeError):
    """No valid Box snapshot or offline cache is available."""


@dataclass(frozen=True)
class LookupValidation:
    devices: int
    deployments: int
    hashes: dict[str, str]


@dataclass(frozen=True)
class LookupBootstrapResult:
    lookups: LookupTables
    source: str
    warning: str = ""
    synced_files: tuple[str, ...] = ()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_device_lookup_pair(
    devices_path: Path,
    deployments_path: Path,
) -> LookupValidation:
    """Validate the two curated files as one relational snapshot."""
    devices = load_devices(devices_path)
    deployments = load_device_deployments(deployments_path)

    device_ids = [row["device_record_id"] for row in devices]
    if any(not value for value in device_ids):
        raise LookupSchemaError("devices.csv contains a blank device_record_id")
    if len(device_ids) != len(set(device_ids)):
        raise LookupSchemaError("devices.csv contains duplicate device_record_id values")

    closed_without_ids = [
        row
        for row in deployments
        if row.get("deployment_end_date")
        and (not row["deployment_id"] or not row["deployment_event_id"])
    ]
    if closed_without_ids:
        raise LookupSchemaError(
            f"deployments.csv has {len(closed_without_ids)} closed placement(s) "
            "without deployment_id or deployment_event_id"
        )
    named_open = [
        row for row in deployments if not row.get("deployment_end_date") and row["deployment_id"]
    ]
    if named_open:
        raise LookupSchemaError("deployments.csv assigns deployment_id to an open placement")

    named_open_events = [
        row
        for row in deployments
        if not row.get("deployment_end_date") and row.get("deployment_event_id", "")
    ]
    if named_open_events:
        raise LookupSchemaError(
            "deployments.csv assigns deployment_event_id to an open placement"
        )
    placeholder_events = [
        row.get("deployment_event_id", "")
        for row in deployments
        if row.get("deployment_event_id", "").startswith(("INFERRED_", "OPEN_"))
    ]
    if placeholder_events:
        raise LookupSchemaError(
            "deployments.csv contains legacy inferred/open deployment event IDs"
        )

    deployment_ids = [row["deployment_id"] for row in deployments if row["deployment_id"]]
    if len(deployment_ids) != len(set(deployment_ids)):
        raise LookupSchemaError("deployments.csv contains duplicate deployment_id values")

    reversed_intervals = [
        row
        for row in deployments
        if row.get("deployment_end_date")
        and row["deployment_end_date"] < row["deployment_start_date"]
    ]
    if reversed_intervals:
        raise LookupSchemaError(
            f"deployments.csv has {len(reversed_intervals)} placement(s) whose end "
            "date precedes the start date"
        )

    event_sites: dict[str, set[str]] = {}
    event_slots: set[tuple[str, str, int, str]] = set()
    duplicate_slots: set[tuple[str, str, int, str]] = set()
    for row in deployments:
        event_id = row.get("deployment_event_id", "")
        if not event_id:
            continue
        event_sites.setdefault(event_id, set()).add(row["site_short_name"])
        slot = (
            event_id,
            row["site_short_name"],
            int(row["plot_number"]),
            row["device_type"],
        )
        if slot in event_slots:
            duplicate_slots.add(slot)
        event_slots.add(slot)
    multi_site_events = [event for event, sites in event_sites.items() if len(sites) > 1]
    if multi_site_events:
        examples = ", ".join(sorted(multi_site_events)[:5])
        raise LookupSchemaError(
            "deployments.csv assigns deployment_event_id values to multiple sites; "
            f"examples: {examples}"
        )
    if duplicate_slots:
        examples = ", ".join(
            f"{event_id}/{site}/plot{plot}/{device_type}"
            for event_id, site, plot, device_type in sorted(duplicate_slots)[:5]
        )
        raise LookupSchemaError(
            f"deployments.csv has {len(duplicate_slots)} duplicate plot/device "
            f"slot(s) within a deployment event; examples: {examples}"
        )

    known_devices = set(device_ids)
    unknown = {
        row.get("device_record_id", "")
        for row in deployments
        if row.get("device_record_id", "") not in known_devices
    }
    if unknown:
        raise LookupSchemaError(
            f"deployments.csv references {len(unknown)} unknown device record(s)"
        )

    return LookupValidation(
        devices=len(devices),
        deployments=len(deployments),
        hashes={
            DEVICES_CSV: _sha256(devices_path),
            DEPLOYMENTS_CSV: _sha256(deployments_path),
        },
    )


def validate_lookup_directory(lookup_dir: Path) -> tuple[LookupTables, LookupValidation]:
    """Strictly validate a complete runtime lookup directory."""
    missing = sorted(
        name for name in REQUIRED_RUNTIME_FILENAMES if not (lookup_dir / name).is_file()
    )
    if missing:
        raise LookupSchemaError("Missing required lookup files: " + ", ".join(missing))

    pair = validate_device_lookup_pair(
        lookup_dir / DEVICES_CSV,
        lookup_dir / DEPLOYMENTS_CSV,
    )
    lookups = LookupTables.load(lookup_dir)
    if not lookups.sites:
        raise LookupSchemaError("sites.csv has no canonical site rows")
    if not lookups.plot_names:
        raise LookupSchemaError("plots.csv has no canonical plot rows")

    valid_sites = {site.site_short_name for site in lookups.sites}
    unknown_sites = {
        row["site_short_name"]
        for row in lookups.device_deployments
        if row["site_short_name"] not in valid_sites
    }
    if unknown_sites:
        raise LookupSchemaError(
            f"deployments.csv references {len(unknown_sites)} unknown site_short_name value(s)"
        )
    # A plot remains authoritative even when its optional display name is
    # blank; plot_metadata contains every valid site/plot row.
    valid_plots = {
        (site_short_name, str(plot_number))
        for site_short_name, plot_number in lookups.plot_metadata
    }
    unknown_plots = {
        (row["site_short_name"], row["plot_number"])
        for row in lookups.device_deployments
        if (row["site_short_name"], row["plot_number"]) not in valid_plots
    }
    if unknown_plots:
        raise LookupSchemaError(
            f"deployments.csv references {len(unknown_plots)} unknown plot(s)"
        )
    return lookups, pair


def _download_file(client, file_id: str, destination: Path) -> None:
    content = client.downloads.download_file(file_id)
    with destination.open("wb") as handle:
        for chunk in content:
            handle.write(chunk)
        handle.flush()
        os.fsync(handle.fileno())


def _replace_cache_from_staging(staging_dir: Path, cache_dir: Path, names: list[str]) -> None:
    """Replace downloaded files with rollback if any replacement fails."""
    backup_dir = staging_dir / "previous"
    backup_dir.mkdir()
    previous: dict[str, bool] = {}
    for name in names:
        destination = cache_dir / name
        previous[name] = destination.is_file()
        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            raise LookupBootstrapError(f"Unsafe lookup cache target: {destination}")
        if destination.is_file():
            shutil.copy2(destination, backup_dir / name)

    replaced: list[str] = []
    try:
        for name in names:
            os.replace(staging_dir / name, cache_dir / name)
            replaced.append(name)
    except Exception:
        for name in replaced:
            destination = cache_dir / name
            backup = backup_dir / name
            if previous[name]:
                os.replace(backup, destination)
            else:
                destination.unlink(missing_ok=True)
        raise


def sync_box_lookup_cache(client, folder_id: str, cache_dir: Path) -> tuple[LookupTables, tuple[str, ...]]:
    """Download, validate, and transactionally install the Box configuration."""
    cache_dir = cache_dir.resolve()
    cache_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=".box-lookups-", dir=cache_dir.parent))
    try:
        storage = BoxStorage(client)
        box_files = {
            item.name: item.id
            for item in storage.iter_folder_items(folder_id)
            if item.type == "file" and item.name in BOX_MANAGED_FILENAMES
        }
        missing = sorted(REQUIRED_RUNTIME_FILENAMES - set(box_files))
        if missing:
            raise LookupBootstrapError(
                "Box app_config is missing required files: " + ", ".join(missing)
            )

        for name, file_id in box_files.items():
            _download_file(client, file_id, staging_dir / name)

        validate_lookup_directory(staging_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        names = sorted(box_files)
        _replace_cache_from_staging(staging_dir, cache_dir, names)
        (cache_dir / LAST_SYNC_FILENAME).write_text(
            datetime.now(timezone.utc).isoformat(), encoding="utf-8"
        )
        lookups, _pair = validate_lookup_directory(cache_dir)
        return lookups, tuple(names)
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def bootstrap_lookup_tables(
    box_config: BoxConfig,
    cache_dir: Path,
    *,
    client_factory: Callable[[BoxConfig], object | None] = get_box_client,
) -> LookupBootstrapResult:
    """Prefer Box, fall back only to a fully valid offline cache."""
    box_error = ""
    if not box_config.app_config_folder_id:
        box_error = "Box app_config_folder_id is not configured"
    else:
        try:
            client = client_factory(box_config)
            if client is None:
                raise LookupBootstrapError("Box authentication is unavailable")
            lookups, synced = sync_box_lookup_cache(
                client, box_config.app_config_folder_id, cache_dir
            )
            return LookupBootstrapResult(
                lookups=lookups,
                source="box",
                synced_files=synced,
            )
        except Exception as exc:
            box_error = str(exc)

    try:
        lookups, _pair = validate_lookup_directory(cache_dir)
    except Exception as cache_exc:
        raise LookupBootstrapError(
            "Could not load a valid lookup snapshot from Box or the local cache. "
            "Connect to Box or repair the curated lookup files, then relaunch. "
            f"Box: {box_error}. Cache: {cache_exc}."
        ) from cache_exc

    return LookupBootstrapResult(
        lookups=lookups,
        source="offline-cache",
        warning=(
            "Box lookup sync was unavailable; using the last validated local cache. "
            f"Reason: {box_error}"
        ),
    )
