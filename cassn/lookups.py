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
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
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


DEPLOYMENT_EVENT_REQUIRED_FIELDS = frozenset({
    "deployment_event_id",
    "site_short_name",
    "site_name",
    "deployment_event_start_date",
    "deployment_event_end_date",
})

DEVICE_DEPLOYMENT_REQUIRED_FIELDS = frozenset({
    "deployment_id",
    "deployment_event_id",
    "deployment_sequence",
    "site_short_name",
    "plot_number",
    "device_type",
    "device_id",
    "asset_tag",
    "deployment_start_date",
    "deployment_end_date",
    "identifier_policy",
    "feature_type",
    "mounted_on",
    "sensor_height_meters",
})
RUNTIME_DEVICE_TYPES = frozenset({"ML", "SA", "BD", "BT"})
AUDIO_DEVICE_TYPES = frozenset({"BD", "BT"})
AUDIOMOTH_SERIAL_PATTERN = re.compile(r"[0-9A-F]{16}")
ASSET_TAG_PATTERN = re.compile(r"[0-9]{4}")


def normalize_deployment_event_metadata(metadata: dict | None) -> dict:
    """Return event metadata using the current, explicit event-date keys.

    Historical ``session.json`` and ``deployment_event_record.json`` files used
    the ambiguous keys ``deployment_start`` and ``deployment_end``.  Those
    artifacts are immutable, so readers normalize them at ingress while every
    current runtime and writer uses only ``deployment_event_start_date`` and
    ``deployment_event_end_date``.
    """
    normalized = dict(metadata or {})
    aliases = (
        ("deployment_event_start_date", "deployment_start"),
        ("deployment_event_end_date", "deployment_end"),
    )
    for current, legacy in aliases:
        if not normalized.get(current) and normalized.get(legacy):
            normalized[current] = normalized[legacy]
        normalized.pop(legacy, None)
    return normalized


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


def load_deployment_events(csv_path: Path) -> list[dict]:
    """Load the canonical deployment-event identities and date intervals."""
    rows = _load_required_csv(csv_path, DEPLOYMENT_EVENT_REQUIRED_FIELDS)
    seen_ids: set[str] = set()
    for row_number, row in enumerate(rows, start=2):
        event_id = row["deployment_event_id"]
        if not event_id or not row["site_short_name"] or not row["site_name"]:
            raise LookupSchemaError(
                f"{csv_path.name} row {row_number} has a blank event or site identity"
            )
        if event_id in seen_ids:
            raise LookupSchemaError(
                f"{csv_path.name} contains duplicate deployment_event_id: {event_id}"
            )
        seen_ids.add(event_id)

        try:
            start = date.fromisoformat(row["deployment_event_start_date"])
            end = date.fromisoformat(row["deployment_event_end_date"])
        except (TypeError, ValueError) as exc:
            raise LookupSchemaError(
                f"{csv_path.name} row {row_number} has an invalid event date; "
                "expected YYYY-MM-DD"
            ) from exc
        if end < start:
            raise LookupSchemaError(
                f"{csv_path.name} row {row_number} has an end date before its start date"
            )
        if not event_id.endswith(end.strftime("%Y%m%d")):
            raise LookupSchemaError(
                f"{csv_path.name} row {row_number} deployment_event_id does not "
                "end with its deployment_event_end_date"
            )
    return rows


def load_device_deployments(csv_path: Path) -> list[dict]:
    """Load curated deployment intervals and validate prospective IDs."""
    rows = _load_required_csv(csv_path, DEVICE_DEPLOYMENT_REQUIRED_FIELDS)
    for row_number, row in enumerate(rows, start=2):
        try:
            plot_number = int(row["plot_number"])
            start = date.fromisoformat(row["deployment_start_date"])
        except (TypeError, ValueError) as exc:
            raise LookupSchemaError(
                f"{csv_path.name} row {row_number} has an invalid plot or start date"
            ) from exc
        if plot_number < 1:
            raise LookupSchemaError(
                f"{csv_path.name} row {row_number} has an invalid plot number"
            )
        if not row["site_short_name"] or not row["device_type"]:
            raise LookupSchemaError(
                f"{csv_path.name} row {row_number} has a blank site or device type"
            )
        if row["device_type"] not in RUNTIME_DEVICE_TYPES:
            raise LookupSchemaError(
                f"{csv_path.name} row {row_number} has unsupported device_type: "
                f"{row['device_type']}"
            )

        device_id = row.get("device_id", "")
        asset_tag = row.get("asset_tag", "")
        if row["device_type"] in AUDIO_DEVICE_TYPES:
            if device_id and not AUDIOMOTH_SERIAL_PATTERN.fullmatch(device_id):
                raise LookupSchemaError(
                    f"{csv_path.name} row {row_number} has a noncanonical AudioMoth "
                    "device_id; expected a 16-character uppercase hexadecimal serial"
                )
            if asset_tag and not ASSET_TAG_PATTERN.fullmatch(asset_tag):
                raise LookupSchemaError(
                    f"{csv_path.name} row {row_number} has a noncanonical ARU "
                    "asset_tag; expected four digits"
                )
        elif asset_tag:
            raise LookupSchemaError(
                f"{csv_path.name} row {row_number} assigns asset_tag to a camera row"
            )

        try:
            sequence = int(row["deployment_sequence"])
        except (TypeError, ValueError) as exc:
            raise LookupSchemaError(
                f"{csv_path.name} row {row_number} has an invalid deployment_sequence"
            ) from exc
        if sequence < 0 or str(sequence) != row["deployment_sequence"]:
            raise LookupSchemaError(
                f"{csv_path.name} row {row_number} deployment_sequence must be a "
                "canonical non-negative integer"
            )

        if row["deployment_end_date"]:
            try:
                end = date.fromisoformat(row["deployment_end_date"])
            except ValueError as exc:
                raise LookupSchemaError(
                    f"{csv_path.name} row {row_number} has an invalid end date"
                ) from exc
            if end < start:
                raise LookupSchemaError(
                    f"{csv_path.name} row {row_number} has an end date before its start date"
                )
            if not row["deployment_id"]:
                raise LookupSchemaError(
                    f"{csv_path.name} row {row_number} is closed without a deployment_id"
                )
        else:
            if row["deployment_id"] or row["deployment_event_id"]:
                raise LookupSchemaError(
                    f"{csv_path.name} row {row_number} assigns an identifier to an open deployment"
                )
    return rows


def deployment_id_matches_contract(
    row: dict,
    *,
    sequence_suffix_required: bool = False,
) -> bool:
    """Whether a closed prospective row follows the canonical ID formula."""
    if not row.get("deployment_id") or not row.get("deployment_end_date"):
        return False
    try:
        plot_number = int(row["plot_number"])
        sequence = int(row["deployment_sequence"])
        end_token = date.fromisoformat(row["deployment_end_date"]).strftime("%Y%m%d")
    except (KeyError, TypeError, ValueError):
        return False
    suffix = f"-seq{sequence:02d}" if sequence_suffix_required else ""
    expected_tail = (
        f"_{row['site_short_name']}_plot{plot_number}_{row['device_type']}_"
        f"{end_token}{suffix}"
    )
    deployment_id = row["deployment_id"]
    return deployment_id.endswith(expected_tail) and bool(
        deployment_id[: -len(expected_tail)]
    )


def deployment_storage_label(row: dict) -> str:
    """Return the stable raw-data directory label for one deployment row."""
    base = f"p{int(row['plot_number'])}_{row['device_type']}"
    sequence = int(row.get("deployment_sequence") or 0)
    return base if sequence == 0 else f"{base}_seq{sequence:02d}"


def build_deployment_rounds(
    event_rows: list[dict],
    placement_rows: list[dict],
) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """Build selectable events from the canonical event table.

    ``deployment_events.csv`` supplies every closed event's ID, site, and dates.
    ``deployments.csv`` only supplies the device rows joined by event ID. All
    open placements at a site form one read-only current field set, even when
    individual devices were installed or redeployed on different dates.
    """
    canonical_events = {row["deployment_event_id"]: row for row in event_rows}
    closed_rows_by_event: dict[str, list[dict]] = defaultdict(list)
    open_rows_by_site: dict[str, list[dict]] = defaultdict(list)
    for row in placement_rows:
        event_id = row["deployment_event_id"]
        if event_id:
            event = canonical_events.get(event_id)
            if event is None:
                raise LookupSchemaError(
                    f"deployments.csv references unknown deployment_event_id: {event_id}"
                )
            if row["site_short_name"] != event["site_short_name"]:
                raise LookupSchemaError(
                    "deployments.csv site_short_name disagrees with deployment_events.csv "
                    f"for {event_id}"
                )
            closed_rows_by_event[event_id].append(row)
        elif not row["deployment_end_date"]:
            open_rows_by_site[row["site_short_name"]].append(row)

    events_by_site: dict[str, list[dict]] = defaultdict(list)
    rows_by_round: dict[str, list[dict]] = {}
    for event_row in event_rows:
        event_id = event_row["deployment_event_id"]
        site_short_name = event_row["site_short_name"]
        round_rows = closed_rows_by_event[event_id]
        if not round_rows:
            continue
        round_id = f"{site_short_name}:closed:{event_id}"
        events_by_site[site_short_name].append({
            "deployment_round_id": round_id,
            "deployment_event_id": event_id,
            "deployment_event_start_date": event_row["deployment_event_start_date"],
            "deployment_event_end_date": event_row["deployment_event_end_date"],
            "deployment_count": len(round_rows),
            "device_count": len(round_rows),  # historical session/UI compatibility
        })
        rows_by_round[round_id] = round_rows

    for site_short_name, round_rows in open_rows_by_site.items():
        start_dates = sorted({row["deployment_start_date"] for row in round_rows})
        earliest_start = start_dates[0]
        latest_start = start_dates[-1]
        round_id = f"{site_short_name}:open"
        events_by_site[site_short_name].append({
            "deployment_round_id": round_id,
            "deployment_event_id": "",
            "deployment_event_start_date": earliest_start,
            "deployment_event_end_date": "",
            "latest_open_deployment_start_date": latest_start,
            "deployment_count": len(round_rows),
            "device_count": len(round_rows),  # historical session/UI compatibility
        })
        rows_by_round[round_id] = round_rows

    for site_short_name, site_events in events_by_site.items():
        # A download is normally for returned cards, so put the newest closed
        # round first and list currently deployed/open rounds afterward.
        events_by_site[site_short_name] = sorted(
            site_events,
            key=lambda event: (
                bool(event["deployment_event_end_date"]),
                event["deployment_event_end_date"]
                or event["deployment_event_start_date"],
            ),
            reverse=True,
        )

    return dict(events_by_site), rows_by_round


# ---------------------------------------------------------------------------
# Container — replaces module-level globals
# ---------------------------------------------------------------------------

# Canonical filenames inside the lookup-tables directory.
SITES_CSV = "sites.csv"
PLOTS_CSV = "plots.csv"
DEPLOYMENTS_CSV = "deployments.csv"
DEPLOYMENT_EVENTS_CSV = "deployment_events.csv"
SOUNDHUB_JSON = "soundhub_config.json"
WI_CONFIG_JSON = "wi_config.json"
PROGRAM_CONFIG_JSON = "program_config.json"

# Box is the distribution point for the complete validated, curated runtime
# snapshot. Every app installation bootstraps and refreshes its offline cache
# from that snapshot.
BOX_MANAGED_FILENAMES = frozenset({
    SITES_CSV,
    PLOTS_CSV,
    DEPLOYMENTS_CSV,
    DEPLOYMENT_EVENTS_CSV,
    SOUNDHUB_JSON,
    WI_CONFIG_JSON,
    PROGRAM_CONFIG_JSON,
    "motus.csv",
})

# Exact legacy filenames removed from the runtime contract. A successful Box
# refresh prunes these from the local cache so stale copies cannot be mistaken
# for active inputs.
RETIRED_LOOKUP_FILENAMES = frozenset({"devices.csv", "cameras.csv", "ARUs.csv"})


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
    deployment_events: list[dict] = field(default_factory=list)
    device_deployments: list[dict] = field(default_factory=list)
    active_deployment_rows: list[dict] = field(default_factory=list)
    deployments_by_id: dict[str, dict] = field(default_factory=dict)
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
        self.deployment_events = load_deployment_events(data_dir / DEPLOYMENT_EVENTS_CSV)
        self.device_deployments = load_device_deployments(data_dir / DEPLOYMENTS_CSV)
        self.deployments, self._deployment_rows_by_round = build_deployment_rounds(
            self.deployment_events,
            self.device_deployments,
        )
        self.active_deployment_round_id = ""
        self.active_deployment_rows = []
        self.deployments_by_id = {
            row["deployment_id"]: row
            for row in self.device_deployments
            if row.get("deployment_id")
        }
        self.arus = {}
        self.cameras = {}
        self.wi_config = load_wi_config(data_dir / WI_CONFIG_JSON)
        self.program_config = load_program_config(data_dir / PROGRAM_CONFIG_JSON)
        self.plot_coords = load_plot_coords(data_dir / PLOTS_CSV)

    def activate_deployment_round(self, round_id: str) -> None:
        """Expose camera/ARU compatibility views for one selected field round."""
        if round_id not in self._deployment_rows_by_round:
            raise LookupSchemaError(f"Unknown deployment round: {round_id}")

        self.active_deployment_rows = list(self._deployment_rows_by_round[round_id])
        chosen: dict[tuple, dict] = {}
        for row in self.active_deployment_rows:
            key = (
                row["site_short_name"],
                int(row["plot_number"]),
                row["device_type"],
            )
            # Compatibility views are only for older slot-keyed code. Prefer
            # the earliest deployment in a slot; ingest and metadata use the
            # exact deployment ID and never rely on this lossy view.
            previous = chosen.get(key)
            if previous is None or int(row.get("deployment_sequence") or 0) < int(
                previous.get("deployment_sequence") or 0
            ):
                chosen[key] = row

        cameras: dict[tuple, dict] = {}
        arus: dict[tuple, dict] = {}
        for key, row in chosen.items():
            if row["device_type"] in {"ML", "SA"}:
                cameras[key] = {
                    **row,
                    "camera_id": row.get("device_id", ""),
                }
            elif row["device_type"] in {"BD", "BT"}:
                arus[key] = row

        self.active_deployment_round_id = round_id
        self.cameras = cameras
        self.arus = arus

    def clear_active_deployment_round(self) -> None:
        """Clear event-scoped device views when no valid round is selected."""
        self.active_deployment_round_id = ""
        self.active_deployment_rows = []
        self.cameras = {}
        self.arus = {}

    def deployment_for_id(self, deployment_id: str) -> dict:
        """Return one exact deployment row, or an empty mapping when absent."""
        return self.deployments_by_id.get(deployment_id, {})

    def active_rows_for_slot(self, site: str, plot_number: int, device_type: str) -> list[dict]:
        """Return sequence-ordered active rows for a plot/device slot."""
        return sorted(
            (
                row
                for row in self.active_deployment_rows
                if row["site_short_name"] == site
                and int(row["plot_number"]) == int(plot_number)
                and row["device_type"] == device_type
            ),
            key=lambda row: int(row.get("deployment_sequence") or 0),
        )

    def active_deployment_for_label(self, device_label: str) -> dict:
        """Resolve a raw-data label to its exact active deployment row."""
        matches = [
            row
            for row in self.active_deployment_rows
            if deployment_storage_label(row) == device_label
        ]
        if len(matches) > 1:
            raise LookupSchemaError(
                f"Active event maps {device_label} to multiple deployment rows"
            )
        return matches[0] if matches else {}

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
            if event.get("deployment_event_end_date")
        ]

    def current_rounds(self, site_short_name: str) -> list[dict]:
        """Open placements shown as read-only field inventory in the GUI."""
        return [
            event
            for event in self.deployments.get(site_short_name, [])
            if not event.get("deployment_event_end_date")
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
