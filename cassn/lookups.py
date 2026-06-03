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
from dataclasses import dataclass, field
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
        with open(csv_path, "r", encoding="utf-8") as f:
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
        for encoding in ("utf-8", "utf-8-sig", "latin-1"):
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
                        if plot_number >= 1 and plot_name:
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
    with open(json_path, encoding="utf-8") as f:
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
    with open(csv_path, encoding="utf-8") as f:
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
    with open(csv_path, "r", encoding="utf-8") as f:
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
        with open(json_path, "r", encoding="utf-8") as f:
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
        with open(json_path, "r", encoding="utf-8") as f:
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
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
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


# ---------------------------------------------------------------------------
# Container — replaces module-level globals
# ---------------------------------------------------------------------------

# Canonical filenames inside the lookup-tables directory.
SITES_CSV = "sites.csv"
PLOTS_CSV = "plots.csv"
ARUS_CSV = "ARUs.csv"
CAMERAS_CSV = "cameras.csv"
SOUNDHUB_JSON = "soundhub_config.json"
WI_CONFIG_JSON = "wi_config.json"
PROGRAM_CONFIG_JSON = "program_config.json"


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
    arus: dict = field(default_factory=dict)
    cameras: dict = field(default_factory=dict)
    wi_config: dict = field(default_factory=dict)
    program_config: dict = field(default_factory=dict)
    plot_coords: dict = field(default_factory=dict)

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
        self.arus = load_arus(data_dir / ARUS_CSV)
        self.cameras = load_cameras(data_dir / CAMERAS_CSV)
        self.wi_config = load_wi_config(data_dir / WI_CONFIG_JSON)
        self.program_config = load_program_config(data_dir / PROGRAM_CONFIG_JSON)
        self.plot_coords = load_plot_coords(data_dir / PLOTS_CSV)

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
