"""
Lookup-table loaders and their container.

Stdlib only — no Qt, no Box — so this module is importable by the CLI tools in
``utils/`` as well as the GUI. Every loader takes an explicit file path; nothing
reads from a hidden module global and nothing runs at import time. The
:class:`LookupTables` dataclass replaces the original module-level globals
(``RESERVES``, ``PLOT_NAMES``, ``PLOT_METADATA``, ``SOUNDHUB_CONFIG``,
``ARUS``) and is constructed once in the entry point, then passed where needed.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Individual loaders (each takes a full path; warn-and-degrade on failure)
# ---------------------------------------------------------------------------


def load_reserves(csv_path: Path) -> list[tuple[str, str]]:
    """Return ``[(site_code, site_name), ...]`` from sites.csv.

    On any failure, warns and returns an empty list — the app then syncs
    lookup tables from Box on startup.
    """
    reserves: list[tuple[str, str]] = []
    try:
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                site_code = row["site_code"].strip()
                site_name = row["site_name"].strip()
                if site_code and site_name:
                    reserves.append((site_code, site_name))
        if not reserves:
            raise ValueError(f"No site rows found in {csv_path}")
    except Exception as e:
        print(
            "Warning: Could not load site lookup data — "
            "lookup tables will be synced from Box on startup. "
            f"({e})"
        )
        reserves = []
    return reserves


def load_plot_names(csv_path: Path) -> tuple[dict, dict]:
    """Return ``(plot_names, plot_metadata)`` from plots.csv.

    * ``plot_names``: ``{site_code -> {plot_number_int -> plot_name_str}}``
    * ``plot_metadata``: ``{(site_code, plot_number_int) -> full row dict}``

    Plot count per reserve is unbounded; the UI rebuilds its grid per reserve.
    Tries multiple encodings before giving up.
    """
    plot_names: dict[str, dict[int, str]] = {}
    plot_metadata: dict[tuple, dict] = {}

    try:
        for encoding in ("utf-8-sig", "latin-1"):
            try:
                with open(csv_path, "r", encoding=encoding) as f:
                    for row in csv.DictReader(f):
                        site_code = row["site_code"].strip()
                        plot_number = int(row["plot_number"])
                        plot_name = row["plot_name"].strip()
                        plot_latitude = row.get("plot_latitude", "").strip()
                        plot_longitude = row.get("plot_longitude", "").strip()
                        plot_description = row.get("plot_description", "").strip()

                        if site_code not in plot_names:
                            plot_names[site_code] = {}
                        # A plot with no name is still a real, selectable plot;
                        # callers fall back to displaying the bare number.
                        if plot_number >= 1:
                            plot_names[site_code][plot_number] = plot_name
                        plot_metadata[(site_code, plot_number)] = {
                            "plot_name": plot_name,
                            "plot_latitude": plot_latitude,
                            "plot_longitude": plot_longitude,
                            "plot_description": plot_description,
                        }
                break  # read succeeded — stop trying encodings
            except UnicodeDecodeError:
                plot_names, plot_metadata = {}, {}
                continue
    except Exception as e:
        print(
            "Warning: Could not load plot lookup data — "
            "lookup tables will be synced from Box on startup. "
            f"({e})"
        )
        plot_names, plot_metadata = {}, {}

    return plot_names, plot_metadata


def load_soundhub_config(json_path: Path) -> dict:
    """Return SoundHub ARU hardware defaults from soundhub_config.json."""
    if not json_path.exists():
        print(f"  WARNING: soundhub_config.json not found at {json_path}")
        return {}
    with open(json_path, encoding="utf-8-sig") as f:
        return json.load(f)


def load_arus(csv_path: Path) -> dict:
    """Return ``{(site_code, plot_number_int, device_type) -> stripped row dict}``.

    ARUs are physical assets at a fixed location — one row per
    (site, plot, device); last row wins on duplicate keys. An older
    ``deployment_event_id`` column is accepted but ignored.
    """
    result: dict[tuple, dict] = {}
    if not csv_path.exists():
        print(f"  WARNING: ARUs.csv not found at {csv_path}")
        return result
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                key = (
                    row["site_code"].strip(),
                    int(row["plot_number"]),
                    row["device_type"].strip(),
                )
                result[key] = {k: v.strip() for k, v in row.items()}
            except (KeyError, ValueError):
                continue
    return result


def load_cameras(csv_path: Path) -> dict:
    """Return ``{(site_code, plot_number_int, device_type) -> raw row dict}``.

    Shared by the GUI's metadata CSV writer and the ``utils/`` WI generator.
    Rows are stored unmodified (values are not stripped), matching the original.
    """
    result: dict[tuple, dict] = {}
    if not csv_path.exists():
        return result
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                key = (row["site_code"].strip(), int(row["plot_number"]), row["device_type"].strip())
                result[key] = row
            except (KeyError, ValueError):
                continue
    return result


def load_wi_config(json_path: Path) -> dict:
    """Return Wildlife Insights project IDs / defaults from wi_config.json.

    Lenient: returns ``{}`` if the file is missing or unreadable. Callers that
    need to treat a missing file as a hard error (the CLI generator) check for
    existence themselves.
    """
    if not json_path.exists():
        return {}
    try:
        with open(json_path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception as e:
        print(f"Warning: could not load wi_config.json: {e}")
        return {}


def load_program_config(json_path: Path) -> dict:
    """Return per-organization program settings from program_config.json.

    Holds the organization label(s) and observer/downloader names that populate
    the two wizard dropdowns that would otherwise be hardcoded to UC. Lenient:
    returns ``{}`` if the file is missing or unreadable, so installs without the
    file fall back to the constants in :mod:`cassn.config`.
    """
    if not json_path.exists():
        return {}
    try:
        with open(json_path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception as e:
        print(f"Warning: could not load program_config.json: {e}")
        return {}


def load_plot_coords(csv_path: Path) -> dict:
    """Return ``{(site_code, plot_number_int) -> {latitude, longitude}}`` from plots.csv.

    Used by the WI generator (both GUI and CLI). Multi-encoding like
    :func:`load_plot_names`.
    """
    result: dict[tuple, dict] = {}
    if not csv_path.exists():
        return result
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            with open(csv_path, "r", encoding=encoding) as f:
                for row in csv.DictReader(f):
                    try:
                        key = (row["site_code"].strip(), int(row["plot_number"]))
                        result[key] = {
                            "latitude": row.get("plot_latitude", "").strip(),
                            "longitude": row.get("plot_longitude", "").strip(),
                        }
                    except (KeyError, ValueError):
                        continue
            return result
        except UnicodeDecodeError:
            result = {}
    return result


def load_deployments(csv_path: Path) -> dict:
    """Return ``{site_code -> [ {deployment_start, deployment_end, deployment_event_id} ]}``.

    One entry per deployment event, sorted by start date. Powers the GUI's
    deployment-event picker so dates aren't typed by hand. Missing/unreadable
    file degrades to ``{}``.
    """
    result: dict[str, list] = {}
    if not csv_path.exists():
        return result
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            with open(csv_path, "r", encoding=encoding) as f:
                for row in csv.DictReader(f):
                    site = (row.get("site_code") or "").strip()
                    if not site:
                        continue
                    result.setdefault(site, []).append({
                        "deployment_start": (row.get("deployment_start") or "").strip(),
                        "deployment_end": (row.get("deployment_end") or "").strip(),
                        "deployment_event_id": (row.get("deployment_event_id") or "").strip(),
                    })
            break
        except UnicodeDecodeError:
            result = {}
            continue
    for events in result.values():
        events.sort(key=lambda e: e["deployment_start"])
    return result


class LookupSchemaError(ValueError):
    """A required lookup exists but does not implement the active schema."""


DEVICE_REQUIRED_FIELDS = frozenset({
    "device_record_id",
    "device_id",
    "device_type",
})

DEVICE_DEPLOYMENT_REQUIRED_FIELDS = frozenset({
    "deployment_id",
    "deployment_event_id",
    "site_code",
    "plot_number",
    "device_type",
    "device_id",
    "deployment_start_date",
    "deployment_end_date",
})


def _load_required_csv(csv_path: Path, required_fields: frozenset[str]) -> list[dict]:
    """Read and validate a required new-system CSV without legacy fallback."""
    if not csv_path.exists():
        raise LookupSchemaError(f"Required lookup file is missing: {csv_path}")

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fields = set(reader.fieldnames or [])
        missing = sorted(required_fields - fields)
        if missing:
            raise LookupSchemaError(
                f"{csv_path.name} uses the wrong schema; missing columns: "
                + ", ".join(missing)
            )
        rows = [
            {key: (value or "").strip() for key, value in row.items()}
            for row in reader
        ]

    if not rows:
        raise LookupSchemaError(f"Required lookup file has no data rows: {csv_path}")
    return rows


def load_devices(csv_path: Path) -> list[dict]:
    """Load the Survey123-derived physical-device inventory."""
    return _load_required_csv(csv_path, DEVICE_REQUIRED_FIELDS)


def load_device_deployments(csv_path: Path) -> list[dict]:
    """Load Survey123-derived device-placement intervals."""
    rows = _load_required_csv(csv_path, DEVICE_DEPLOYMENT_REQUIRED_FIELDS)
    for row_number, row in enumerate(rows, start=2):
        try:
            int(row["plot_number"])
            date.fromisoformat(row["deployment_start_date"])
        except (TypeError, ValueError) as exc:
            raise LookupSchemaError(
                f"{csv_path.name} row {row_number} has an invalid plot or start date"
            ) from exc
        if row["deployment_end_date"]:
            try:
                date.fromisoformat(row["deployment_end_date"])
            except ValueError as exc:
                raise LookupSchemaError(
                    f"{csv_path.name} row {row_number} has an invalid end date"
                ) from exc
    return rows


def build_deployment_rounds(
    rows: list[dict], *, max_visit_gap_days: int = 2
) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """Build selectable SD-card download rounds from device intervals.

    Closed placements are grouped by nearby *retrieval* dates: those are the
    cards physically coming back from a reserve together, even when cameras and
    ARUs were installed on different dates. Open placements are grouped by
    nearby deployment dates so the currently deployed inventory remains
    inspectable without merging a later redeployment into an older card set.
    """
    by_site: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_site[row["site_code"]].append(row)

    events_by_site: dict[str, list[dict]] = {}
    rows_by_round: dict[str, list[dict]] = {}
    gap = timedelta(days=max_visit_gap_days)

    def cluster_rows(cluster_rows: list[dict], field_name: str) -> list[list[dict]]:
        ordered_days = sorted({date.fromisoformat(r[field_name]) for r in cluster_rows})
        day_clusters: list[list[date]] = []
        for day in ordered_days:
            if not day_clusters or day - day_clusters[-1][-1] > gap:
                day_clusters.append([day])
            else:
                day_clusters[-1].append(day)
        return [
            [r for r in cluster_rows if date.fromisoformat(r[field_name]) in days]
            for days in (set(cluster) for cluster in day_clusters)
        ]

    for site_code, site_rows in by_site.items():
        closed_rows = [row for row in site_rows if row["deployment_end_date"]]
        open_rows = [row for row in site_rows if not row["deployment_end_date"]]
        round_groups = cluster_rows(closed_rows, "deployment_end_date")
        round_groups += cluster_rows(open_rows, "deployment_start_date")
        site_events: list[dict] = []
        for round_rows in round_groups:
            start = min(r["deployment_start_date"] for r in round_rows)
            end_dates = [r["deployment_end_date"] for r in round_rows if r["deployment_end_date"]]
            end = max(end_dates) if end_dates else ""
            round_id = f"{site_code}:{'closed-' + end if end else 'open-' + start}"
            source_ids = [r["deployment_event_id"] for r in round_rows if r["deployment_event_id"]]
            source_event_id = Counter(source_ids).most_common(1)[0][0] if source_ids else ""
            metadata_event_id = (
                f"UC_{site_code}_{end.replace('-', '')}"
                if end
                else source_event_id or f"OPEN_{site_code}_{start.replace('-', '')}"
            )
            event = {
                "deployment_round_id": round_id,
                "deployment_event_id": metadata_event_id,
                "source_deployment_event_id": source_event_id,
                "deployment_start": start,
                "deployment_end": end,
                "device_count": len(round_rows),
            }
            site_events.append(event)
            rows_by_round[round_id] = round_rows

        # A download is normally for returned cards, so put the newest closed
        # round first and list currently deployed/open rounds afterward.
        events_by_site[site_code] = sorted(
            site_events,
            key=lambda event: (
                bool(event["deployment_end"]),
                event["deployment_end"] or event["deployment_start"],
            ),
            reverse=True,
        )

    return events_by_site, rows_by_round


# ---------------------------------------------------------------------------
# Container — replaces module-level globals
# ---------------------------------------------------------------------------

# Canonical filenames inside the lookup-tables directory.
SITES_CSV = "sites.csv"
PLOTS_CSV = "plots.csv"
DEVICES_CSV = "devices.csv"
DEPLOYMENTS_CSV = "deployments.csv"
SOUNDHUB_JSON = "soundhub_config.json"
WI_CONFIG_JSON = "wi_config.json"
PROGRAM_CONFIG_JSON = "program_config.json"

# Box remains authoritative for stable/manual reference data and export
# defaults. Device inventory and placement history are installed from the
# verified Survey123 transformation and must never be overwritten by an older
# Box copy during startup.
BOX_MANAGED_FILENAMES = frozenset({
    SITES_CSV,
    PLOTS_CSV,
    SOUNDHUB_JSON,
    WI_CONFIG_JSON,
    PROGRAM_CONFIG_JSON,
    "motus.csv",
})


@dataclass
class LookupTables:
    """In-memory snapshot of all lookup tables for one app run.

    Construct with :meth:`load`; refresh in place with :meth:`reload` (e.g. after
    a Box lookup-table sync).
    """

    reserves: list[tuple[str, str]] = field(default_factory=list)
    plot_names: dict = field(default_factory=dict)
    plot_metadata: dict = field(default_factory=dict)
    soundhub_config: dict = field(default_factory=dict)
    devices: list[dict] = field(default_factory=list)
    device_deployments: list[dict] = field(default_factory=list)
    arus: dict = field(default_factory=dict)
    cameras: dict = field(default_factory=dict)
    wi_config: dict = field(default_factory=dict)
    program_config: dict = field(default_factory=dict)
    plot_coords: dict = field(default_factory=dict)
    deployments: dict = field(default_factory=dict)
    active_deployment_round_id: str = ""
    _deployment_rows_by_round: dict = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, data_dir: Path) -> "LookupTables":
        tables = cls()
        tables.reload(data_dir)
        return tables

    def reload(self, data_dir: Path) -> None:
        """Re-read every lookup file from ``data_dir`` in place."""
        self.reserves = load_reserves(data_dir / SITES_CSV)
        self.plot_names, self.plot_metadata = load_plot_names(data_dir / PLOTS_CSV)
        self.soundhub_config = load_soundhub_config(data_dir / SOUNDHUB_JSON)
        self.devices = load_devices(data_dir / DEVICES_CSV)
        self.device_deployments = load_device_deployments(data_dir / DEPLOYMENTS_CSV)
        self.deployments, self._deployment_rows_by_round = build_deployment_rounds(
            self.device_deployments
        )
        self.active_deployment_round_id = ""
        self.arus = {}
        self.cameras = {}
        self.wi_config = load_wi_config(data_dir / WI_CONFIG_JSON)
        self.program_config = load_program_config(data_dir / PROGRAM_CONFIG_JSON)
        self.plot_coords = load_plot_coords(data_dir / PLOTS_CSV)

    def activate_deployment_round(self, round_id: str) -> None:
        """Expose camera/ARU compatibility views for one selected field round."""
        if round_id not in self._deployment_rows_by_round:
            raise LookupSchemaError(f"Unknown deployment round: {round_id}")

        chosen: dict[tuple, dict] = {}
        for row in sorted(
            self._deployment_rows_by_round[round_id],
            key=lambda r: (r["deployment_start_date"], r.get("deployment_start_datetime", "")),
        ):
            key = (row["site_code"], int(row["plot_number"]), row["device_type"])
            chosen[key] = row

        cameras: dict[tuple, dict] = {}
        arus: dict[tuple, dict] = {}
        for key, row in chosen.items():
            if row["device_type"] in {"ML", "SA"}:
                cameras[key] = {
                    **row,
                    "camera_id": row.get("camera_id") or row.get("device_id", ""),
                }
            elif row["device_type"] in {"BD", "BT"}:
                arus[key] = row

        self.active_deployment_round_id = round_id
        self.cameras = cameras
        self.arus = arus

    def clear_active_deployment_round(self) -> None:
        """Clear event-scoped device views when no valid round is selected."""
        self.active_deployment_round_id = ""
        self.cameras = {}
        self.arus = {}

    def available_device_keys(self, round_id: str | None = None) -> set[tuple]:
        """Return ``(site, plot, type)`` keys available in a selected round."""
        selected = round_id or self.active_deployment_round_id
        return {
            (row["site_code"], int(row["plot_number"]), row["device_type"])
            for row in self._deployment_rows_by_round.get(selected, [])
        }

    def returned_rounds(self, site_code: str) -> list[dict]:
        """Completed rounds whose cards are available to download."""
        return [
            event
            for event in self.deployments.get(site_code, [])
            if event.get("deployment_end")
        ]

    def current_rounds(self, site_code: str) -> list[dict]:
        """Open placements shown as read-only field inventory in the GUI."""
        return [
            event
            for event in self.deployments.get(site_code, [])
            if not event.get("deployment_end")
        ]

    # -- convenience views over reserves --------------------------------

    @property
    def reserve_names(self) -> list[str]:
        """Reserve display names, in file order."""
        return [name for _, name in self.reserves]

    @property
    def site_code_by_name(self) -> dict[str, str]:
        return {name: code for code, name in self.reserves}

    @property
    def site_name_by_code(self) -> dict[str, str]:
        return {code: name for code, name in self.reserves}
