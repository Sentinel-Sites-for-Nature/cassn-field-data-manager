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


class LookupSchemaError(ValueError):
    """A required lookup exists but does not implement the active schema."""


@dataclass(frozen=True)
class Site:
    """Canonical site identity loaded from ``sites.csv``."""

    site_name: str
    site_short_name: str
    site_code: str


def load_sites(csv_path: Path) -> list[Site]:
    """Return canonical :class:`Site` records from sites.csv.

    On any failure, warns and returns an empty list — the app then syncs
    lookup tables from Box on startup.
    """
    if not csv_path.exists():
        print(
            "Warning: Could not load site lookup data — "
            "lookup tables will be synced from Box on startup. "
            f"(missing: {csv_path})"
        )
        return []

    sites: list[Site] = []
    try:
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                site_name = row["site_name"].strip()
                site_short_name = row["site_short_name"].strip()
                site_code = row["site_code"].strip()
                if site_name and site_short_name and site_code:
                    sites.append(Site(site_name, site_short_name, site_code))
        if not sites:
            raise LookupSchemaError(f"No canonical site rows found in {csv_path}")
    except KeyError as exc:
        raise LookupSchemaError(
            f"{csv_path.name} uses the wrong schema; required columns are: "
            "site_name, site_short_name, site_code"
        ) from exc
    except LookupSchemaError:
        raise
    except Exception as e:
        print(
            "Warning: Could not load site lookup data — "
            "lookup tables will be synced from Box on startup. "
            f"({e})"
        )
        sites = []
    return sites


def load_plot_names(csv_path: Path) -> tuple[dict, dict]:
    """Return ``(plot_names, plot_metadata)`` from plots.csv.

    * ``plot_names``: ``{site_short_name -> {plot_number_int -> plot_name_str}}``
    * ``plot_metadata``: ``{(site_short_name, plot_number_int) -> full row dict}``

    Plot count per reserve is unbounded; the UI rebuilds its grid per reserve.
    Tries multiple encodings before giving up.
    """
    plot_names: dict[str, dict[int, str]] = {}
    plot_metadata: dict[tuple, dict] = {}

    try:
        for encoding in ("utf-8-sig", "latin-1"):
            try:
                with open(csv_path, "r", encoding=encoding) as f:
                    reader = csv.DictReader(f)
                    required = {"site_short_name", "plot_number", "plot_name"}
                    missing = sorted(required - set(reader.fieldnames or []))
                    if missing:
                        raise LookupSchemaError(
                            f"{csv_path.name} uses the wrong schema; missing columns: "
                            + ", ".join(missing)
                        )
                    for row in reader:
                        site_short_name = row["site_short_name"].strip()
                        plot_number = int(row["plot_number"])
                        plot_name = row["plot_name"].strip()
                        plot_latitude = row.get("plot_latitude", "").strip()
                        plot_longitude = row.get("plot_longitude", "").strip()
                        # Hand-entered in the canonical plots.csv on Box, and
                        # deliberately optional: cached snapshots predating the
                        # column must keep loading, carrying a blank.
                        elevation_m = (row.get("elevation_m") or "").strip()
                        plot_description = row.get("plot_description", "").strip()

                        if site_short_name not in plot_names:
                            plot_names[site_short_name] = {}
                        # A plot with no name is still a real, selectable plot;
                        # callers fall back to displaying the bare number.
                        if plot_number >= 1:
                            plot_names[site_short_name][plot_number] = plot_name
                        plot_metadata[(site_short_name, plot_number)] = {
                            "plot_name": plot_name,
                            "plot_latitude": plot_latitude,
                            "plot_longitude": plot_longitude,
                            "elevation_m": elevation_m,
                            "plot_description": plot_description,
                        }
                break  # read succeeded — stop trying encodings
            except UnicodeDecodeError:
                plot_names, plot_metadata = {}, {}
                continue
    except LookupSchemaError:
        raise
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
    """Return ``{(site_short_name, plot_number_int) -> coordinates}``.

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
                        key = (
                            row["site_short_name"].strip(),
                            int(row["plot_number"]),
                        )
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


DEVICE_REQUIRED_FIELDS = frozenset({
    "device_record_id",
    "device_id",
    "device_type",
})

DEVICE_DEPLOYMENT_REQUIRED_FIELDS = frozenset({
    "deployment_id",
    "deployment_event_id",
    "site_short_name",
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


def placement_key(row: dict[str, str]) -> tuple[str, str, str, str, str]:
    """Identity of one device placement, independent of the ID assigned to it.

    Used to look a row up in the ID map without depending on ``deployment_id``,
    which is the value being computed.
    """
    return (
        str(row.get("site_short_name", "") or "").strip(),
        str(row.get("plot_number", "") or "").strip(),
        str(row.get("device_type", "") or "").strip(),
        str(row.get("deployment_start_date", "") or "").strip(),
        str(row.get("deployment_end_date", "") or "").strip(),
    )


def canonical_deployment_id(row: dict[str, str], identity_date: str) -> str:
    """Build the deployment ID for one placement from an explicit identity date.

    ``identity_date`` is the **round's** end date — the last day anything was
    retrieved at that site on that visit — not the individual device's retrieval
    date. Cameras and ARUs from one visit routinely come back across two days,
    and every other identifier the app produces (the deployment folder name in
    :func:`~cassn.lookups.build_deployment_rounds`, and every renamed file) uses
    the round's date. Deriving this one from the device's own date instead made
    22% of placements disagree with their own filenames.

    The date is passed in rather than read off the row because a single row does
    not know which round it belongs to; use :func:`canonical_deployment_ids` to
    resolve a whole table at once.
    """
    if not identity_date:
        return ""
    try:
        plot_number = str(int(row.get("plot_number", "")))
        date_token = date.fromisoformat(identity_date).strftime("%Y%m%d")
    except (TypeError, ValueError) as exc:
        raise LookupSchemaError(
            "deployments.csv needs a valid plot_number and deployment date"
        ) from exc
    return (
        f"UC_{row.get('site_short_name', '')}_plot{plot_number}_"
        f"{row.get('device_type', '')}_{date_token}"
    )


def canonical_deployment_ids(rows: list[dict[str, str]]) -> dict[tuple, str]:
    """Map every closed placement in ``rows`` to its canonical deployment ID.

    Groups the table into download rounds exactly as the wizard does, then dates
    every placement in a round by that round's last retrieval. Open placements
    are absent from the result: they have no end date and deliberately get no ID
    until the placement closes.
    """
    _events, rows_by_round = build_deployment_rounds(rows)
    resolved: dict[tuple, str] = {}
    for round_rows in rows_by_round.values():
        end_dates = [
            r.get("deployment_end_date", "")
            for r in round_rows
            if r.get("deployment_end_date", "")
        ]
        if not end_dates:
            continue
        round_end = max(end_dates)
        for row in round_rows:
            if row.get("deployment_end_date", ""):
                resolved[placement_key(row)] = canonical_deployment_id(row, round_end)
    return resolved


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
        by_site[row["site_short_name"]].append(row)

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

    for site_short_name, site_rows in by_site.items():
        closed_rows = [row for row in site_rows if row["deployment_end_date"]]
        open_rows = [row for row in site_rows if not row["deployment_end_date"]]
        round_groups = cluster_rows(closed_rows, "deployment_end_date")
        round_groups += cluster_rows(open_rows, "deployment_start_date")
        site_events: list[dict] = []
        for round_rows in round_groups:
            start = min(r["deployment_start_date"] for r in round_rows)
            end_dates = [r["deployment_end_date"] for r in round_rows if r["deployment_end_date"]]
            end = max(end_dates) if end_dates else ""
            round_id = (
                f"{site_short_name}:"
                f"{'closed-' + end if end else 'open-' + start}"
            )
            source_ids = [
                r["deployment_event_id"]
                for r in round_rows
                if r["deployment_event_id"]
                and not r["deployment_event_id"].startswith(("INFERRED_", "OPEN_"))
            ]
            source_event_id = Counter(source_ids).most_common(1)[0][0] if source_ids else ""
            metadata_event_id = (
                f"UC_{site_short_name}_{end.replace('-', '')}"
                if end
                else ""
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
        events_by_site[site_short_name] = sorted(
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

# Box is the canonical distribution point for the complete validated runtime
# snapshot. Survey123 remains upstream of the device pair, but every app
# installation bootstraps and refreshes its offline cache from Box.
BOX_MANAGED_FILENAMES = frozenset({
    SITES_CSV,
    PLOTS_CSV,
    DEVICES_CSV,
    DEPLOYMENTS_CSV,
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

    sites: list[Site] = field(default_factory=list)
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
        self.sites = load_sites(data_dir / SITES_CSV)
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
            key = (
                row["site_short_name"],
                int(row["plot_number"]),
                row["device_type"],
            )
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
            (
                row["site_short_name"],
                int(row["plot_number"]),
                row["device_type"],
            )
            for row in self._deployment_rows_by_round.get(selected, [])
        }

    def returned_rounds(self, site_short_name: str) -> list[dict]:
        """Completed rounds whose cards are available to download."""
        return [
            event
            for event in self.deployments.get(site_short_name, [])
            if event.get("deployment_end")
        ]

    def current_rounds(self, site_short_name: str) -> list[dict]:
        """Open placements shown as read-only field inventory in the GUI."""
        return [
            event
            for event in self.deployments.get(site_short_name, [])
            if not event.get("deployment_end")
        ]

    # -- canonical site views --------------------------------------------

    @property
    def site_names(self) -> list[str]:
        """Formal site display names, in file order."""
        return [site.site_name for site in self.sites]

    @property
    def site_by_name(self) -> dict[str, Site]:
        return {site.site_name: site for site in self.sites}

    @property
    def site_by_short_name(self) -> dict[str, Site]:
        return {site.site_short_name: site for site in self.sites}
