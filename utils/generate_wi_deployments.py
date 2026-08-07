#!/usr/bin/env python3
"""
Generate Wildlife Insights deployment CSVs for Box deployments.

For each deployment in Box:
  1. Downloads deployment_event_record.json (small JSON file only)
  2. Generates wildlife_insights_ML_deployments.csv and/or wildlife_insights_SA_deployments.csv
  3. Uploads the generated CSVs back to the same Box deployment folder

On first run, also generates a local_data/cameras.csv skeleton from local_data/plots.csv
with sensor_height and sensor_orientation pre-filled — fill in camera_id and feature_type.

Usage:
    python3 utils/generate_wi_deployments.py [--local PATH] [--force]

Options:
    --local PATH    Process a single local deployment folder instead of Box
    --force         Regenerate WI CSVs even if they already exist in Box
"""

import argparse
import csv
import io
import json
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from box_sdk_gen import BoxClient, BoxOAuth, OAuthConfig

TOKEN_FILE = Path.home() / ".cassn_config" / "box_tokens.json"
CONFIG_FILE = Path.home() / ".cassn_config" / "config.json"
LOCAL_DATA_DIR = Path(__file__).resolve().parent.parent / "local_data"

CAMERA_DEVICE_TYPES = {"ML", "SA"}

WI_COLUMNS = [
    "project_id", "deployment_id", "subproject_name", "subproject_design",
    "placename", "longitude", "latitude", "start_date", "end_date",
    "event_name", "event_description", "event_type", "bait_type", "bait_description",
    "feature_type", "feature_type_methodology", "camera_id", "quiet_period",
    "camera_functioning", "sensor_height", "height_other", "sensor_orientation",
    "orientation_other", "recorded_by", "plot_treatment", "plot_treatment_description",
    "detection_distance",
]

WI_COORDINATE_QUANTUM = Decimal("0.00000001")


def format_wi_coordinate(value: object) -> str:
    """Format a coordinate to the eight decimal places accepted by WI."""
    if value is None:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    try:
        coordinate = Decimal(raw)
        if not coordinate.is_finite():
            return raw
        rounded = coordinate.quantize(WI_COORDINATE_QUANTUM, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return raw
    if rounded == 0:
        rounded = abs(rounded)
    return format(rounded, ".8f")


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")


# ---------------------------------------------------------------------------
# Box auth
# ---------------------------------------------------------------------------

class _SimpleTokenStorage:
    def __init__(self, path: Path):
        self._path = path

    def store(self, token) -> None:
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump({"access_token": token.access_token, "refresh_token": token.refresh_token}, f, indent=2)

    def get(self):
        try:
            from box_sdk_gen import AccessToken
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return AccessToken(access_token=data["access_token"], refresh_token=data.get("refresh_token"))
        except Exception:
            return None

    def clear(self) -> None:
        if self._path.exists():
            self._path.unlink()


def load_box_client() -> tuple[BoxClient, str]:
    """Returns (client, root_folder_id)."""
    for path in (TOKEN_FILE, CONFIG_FILE):
        if not path.exists():
            raise FileNotFoundError(f"Required file not found: {path}")
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)
    box_cfg = config.get("box", config)
    oauth = OAuthConfig(
        client_id=box_cfg["client_id"],
        client_secret=box_cfg["client_secret"],
        token_storage=_SimpleTokenStorage(TOKEN_FILE),
    )
    return BoxClient(BoxOAuth(oauth)), box_cfg["target_folder_id"]


def list_folder_items(client: BoxClient, folder_id: str) -> list:
    items, marker = [], None
    while True:
        resp = client.folders.get_folder_items(
            folder_id, fields=["id", "name", "type"], usemarker=True, marker=marker, limit=1000
        )
        items.extend(resp.entries)
        marker = getattr(resp, "next_marker", None)
        if not marker:
            break
    return items


def download_json(client: BoxClient, file_id: str) -> dict:
    buf = io.BytesIO()
    client.downloads.download_file_to_output_stream(file_id, buf)
    return json.loads(buf.getvalue())


def get_or_create_subfolder(client: BoxClient, parent_folder_id: str, name: str) -> str:
    """Return the folder ID of a named subfolder, creating it if it doesn't exist."""
    items = list_folder_items(client, parent_folder_id)
    for item in items:
        if getattr(item, "type", None) == "folder" and getattr(item, "name", None) == name:
            return item.id
    folder = client.folders.create_folder(name, {"id": parent_folder_id})
    return folder.id


def upload_to_folder(client: BoxClient, folder_id: str, filename: str, content: bytes) -> None:
    """Upload file to Box folder, overwriting if it already exists."""
    items = list_folder_items(client, folder_id)
    existing = {
        getattr(i, "name"): getattr(i, "id")
        for i in items if getattr(i, "type", None) == "file"
    }
    buf = io.BytesIO(content)
    if filename in existing:
        client.uploads.upload_file_version(
            existing[filename],
            attributes={"name": filename},
            file=buf,
        )
    else:
        client.uploads.upload_file(
            attributes={"name": filename, "parent": {"id": folder_id}},
            file=buf,
        )


# ---------------------------------------------------------------------------
# Local data loading
# ---------------------------------------------------------------------------

def load_cameras() -> dict:
    """Returns dict keyed by (site_code, plot_number_int, device_type) → row."""
    path = LOCAL_DATA_DIR / "cameras.csv"
    if not path.exists():
        log(f"Warning: cameras.csv not found — camera_id, feature_type, sensor_height, sensor_orientation will be blank")
        return {}
    result = {}
    with open(path, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                key = (row["site_code"].strip(), int(row["plot_number"]), row["device_type"].strip())
                result[key] = row
            except (KeyError, ValueError):
                continue
    return result


def load_plot_coords() -> dict:
    """Returns dict keyed by (site_code, plot_number_int) → {latitude, longitude}."""
    path = LOCAL_DATA_DIR / "plots.csv"
    if not path.exists():
        return {}
    result = {}
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            with open(path, "r", encoding=encoding) as f:
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


def load_wi_config() -> dict:
    path = LOCAL_DATA_DIR / "wi_config.json"
    if not path.exists():
        raise FileNotFoundError(
            f"wi_config.json not found at {path}.\n"
            "Copy example_lookups/wi_config.json to local_data/ and fill in your project IDs."
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# cameras.csv skeleton generation
# ---------------------------------------------------------------------------

def generate_cameras_skeleton() -> None:
    """Write local_data/cameras.csv from plots.csv if it doesn't already exist."""
    cameras_path = LOCAL_DATA_DIR / "cameras.csv"
    if cameras_path.exists():
        return

    plots_path = LOCAL_DATA_DIR / "plots.csv"
    if not plots_path.exists():
        log(f"Warning: plots.csv not found — skipping cameras.csv skeleton generation")
        return

    rows = []
    with open(plots_path, "r", encoding="utf-8") as f:
        for plot_row in csv.DictReader(f):
            site = plot_row["site_code"].strip()
            plot_num = plot_row["plot_number"].strip()
            for dev_type, orientation in (("ML", "Parallel"), ("SA", "Pointed Downward")):
                rows.append({
                    "site_code": site,
                    "plot_number": plot_num,
                    "device_type": dev_type,
                    "camera_id": "",
                    "feature_type": "",
                    "sensor_height": "Knee height",
                    "sensor_orientation": orientation,
                })

    CAMERA_FIELDS = ["site_code", "plot_number", "device_type", "camera_id", "feature_type", "sensor_height", "sensor_orientation"]
    with open(cameras_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CAMERA_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    log(f"Generated cameras.csv skeleton with {len(rows)} rows → {cameras_path}")
    log("  Fill in camera_id and feature_type columns, then re-run.")


# ---------------------------------------------------------------------------
# WI row building
# ---------------------------------------------------------------------------

def _event_name(start_date: str, end_date: str) -> str:
    """e.g. 2025NOV-2026JAN"""
    try:
        s = datetime.strptime(start_date, "%Y-%m-%d")
        e = datetime.strptime(end_date, "%Y-%m-%d")
        return f"{s.strftime('%Y%b').upper()}-{e.strftime('%Y%b').upper()}"
    except Exception:
        return ""


def build_wi_rows(
    deployment_info: dict,
    devices: list[dict],
    cameras: dict,
    plot_coords: dict,
    wi_config: dict,
) -> dict[str, list[dict]]:
    """Returns {dev_type: [row_dict, ...]} for ML and SA devices only."""
    site = deployment_info.get("site", "")
    org = deployment_info.get("organization", "")
    start = deployment_info.get("deployment_start", "")
    end = deployment_info.get("deployment_end", "")
    observer = deployment_info.get("observer", "")

    # Subproject groups deployments as Site_Year (year from the end date).
    # Mirrors cassn.export.wildlife_insights.subproject_for; kept inline because
    # this backfill script runs standalone without importing the package.
    subproject_name = f"{site}_{end[:4]}" if site and len(end) >= 4 else ""
    event_name = _event_name(start, end)

    rows_by_type: dict[str, list[dict]] = {}

    for device in devices:
        dev_type = device.get("device_type", "")
        if dev_type not in CAMERA_DEVICE_TYPES:
            continue

        plot_num = device.get("plot_number")
        try:
            plot_num_int = int(plot_num)
        except (TypeError, ValueError):
            plot_num_int = None

        cam = cameras.get((site, plot_num_int, dev_type), {}) if plot_num_int else {}
        coords = plot_coords.get((site, plot_num_int), {}) if plot_num_int else {}

        camera_id = cam.get("camera_id", "")
        if not camera_id:
            log(f"  Warning: camera_id missing for {site} plot {plot_num} {dev_type}")

        row = {
            "project_id": wi_config.get(f"project_id_{dev_type}", ""),
            "deployment_id": f"{org}_{site}_plot{plot_num}_{dev_type}_{end.replace('-', '')}",
            "subproject_name": subproject_name,
            "subproject_design": "",
            "placename": f"{site}_plot{plot_num}",
            "longitude": format_wi_coordinate(coords.get("longitude", "")),
            "latitude": format_wi_coordinate(coords.get("latitude", "")),
            "start_date": f"{start} 00:00:00" if start else "",
            "end_date": f"{end} 23:59:59" if end else "",
            "event_name": event_name,
            "event_description": "",
            "event_type": wi_config.get("event_type", "Temporal"),
            "bait_type": wi_config.get(f"bait_type_{dev_type}", ""),
            "bait_description": wi_config.get(f"bait_description_{dev_type}", ""),
            "feature_type": cam.get("feature_type", ""),
            "feature_type_methodology": "",
            "camera_id": camera_id,
            "quiet_period": wi_config.get("quiet_period", 0),
            "camera_functioning": wi_config.get("camera_functioning_default", "Camera Functioning"),
            "sensor_height": cam.get("sensor_height", "Knee height"),
            "height_other": "",
            "sensor_orientation": cam.get("sensor_orientation", "Parallel" if dev_type == "ML" else "Pointed Downward"),
            "orientation_other": "",
            "recorded_by": observer,
            "plot_treatment": "",
            "plot_treatment_description": "",
            "detection_distance": "",
        }
        rows_by_type.setdefault(dev_type, []).append(row)

    return rows_by_type


def rows_to_csv_bytes(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=WI_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------
# Deployment processing
# ---------------------------------------------------------------------------

def process_deployment(
    deployment_metadata: dict,
    manifest: dict | None,
    cameras: dict,
    plot_coords: dict,
    wi_config: dict,
    *,
    client: BoxClient | None = None,
    box_folder_id: str | None = None,
    local_output_dir: Path | None = None,
    existing_filenames: set[str] | None = None,
    force: bool = False,
) -> int:
    """Build and write/upload WI CSVs for one deployment. Returns number of files written."""
    deployment_info = {
        "organization": deployment_metadata.get("organization", ""),
        "reserve_name": deployment_metadata.get("reserve_name", ""),
        "site": deployment_metadata.get("site", ""),
        "deployment_start": deployment_metadata.get("deployment_start", ""),
        "deployment_end": deployment_metadata.get("deployment_end", ""),
        "observer": deployment_metadata.get("observer", ""),
    }

    devices = (manifest or {}).get("devices", [])
    rows_by_type = build_wi_rows(deployment_info, devices, cameras, plot_coords, wi_config)

    # Resolve output subfolder
    wi_box_folder_id = None
    wi_local_dir = None
    wi_existing: set[str] = set()

    if client and box_folder_id:
        wi_box_folder_id = get_or_create_subfolder(client, box_folder_id, "WI_metadata")
        wi_items = list_folder_items(client, wi_box_folder_id)
        wi_existing = {getattr(i, "name") for i in wi_items if getattr(i, "type", None) == "file"}
    elif local_output_dir:
        wi_local_dir = local_output_dir / "WI_metadata"
        wi_local_dir.mkdir(exist_ok=True)
        wi_existing = {p.name for p in wi_local_dir.iterdir() if p.is_file()}

    written = 0
    for dev_type in sorted(rows_by_type):
        rows = rows_by_type[dev_type]
        filename = f"wildlife_insights_{dev_type}_deployments.csv"

        if filename in wi_existing and not force:
            log(f"  Skipping {filename} (already exists in WI_metadata; use --force to overwrite)")
            continue

        csv_bytes = rows_to_csv_bytes(rows)

        if wi_box_folder_id:
            upload_to_folder(client, wi_box_folder_id, filename, csv_bytes)
            log(f"  Uploaded WI_metadata/{filename} ({len(rows)} rows)")
        elif wi_local_dir:
            (wi_local_dir / filename).write_bytes(csv_bytes)
            log(f"  Wrote WI_metadata/{filename} ({len(rows)} rows)")

        written += 1

    return written


# ---------------------------------------------------------------------------
# Box traversal
# ---------------------------------------------------------------------------

def find_deployment_folders(client: BoxClient, root_folder_id: str) -> list[tuple[str, str]]:
    """Traverse root → year → reserve → deployment. Returns [(folder_id, folder_name)]."""
    deployments = []
    for year_item in list_folder_items(client, root_folder_id):
        if getattr(year_item, "type", None) != "folder":
            continue
        for reserve_item in list_folder_items(client, year_item.id):
            if getattr(reserve_item, "type", None) != "folder":
                continue
            for dep_item in list_folder_items(client, reserve_item.id):
                if getattr(dep_item, "type", None) != "folder":
                    continue
                deployments.append((dep_item.id, dep_item.name))
    return deployments


def fetch_deployment_jsons(client: BoxClient, folder_id: str) -> tuple[dict | None, dict | None, set[str]]:
    """Download deployment_event_record.json from a Box folder."""
    items = list_folder_items(client, folder_id)
    file_map = {
        getattr(i, "name"): getattr(i, "id")
        for i in items if getattr(i, "type", None) == "file"
    }
    existing_names = set(file_map.keys())

    deployment_metadata, manifest = None, None

    if "deployment_event_record.json" in file_map:
        try:
            record = download_json(client, file_map["deployment_event_record.json"])
            deployment_metadata = record.get("deployment_info")
            manifest = record
        except Exception as e:
            log(f"  Warning: could not read deployment_event_record.json: {e}")

    return deployment_metadata, manifest, existing_names


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate Wildlife Insights deployment CSVs from Box or a local folder."
    )
    parser.add_argument("--local", metavar="PATH", help="Process a local deployment folder instead of Box")
    parser.add_argument("--force", action="store_true", help="Regenerate CSVs even if they already exist")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    generate_cameras_skeleton()

    try:
        wi_config = load_wi_config()
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    cameras = load_cameras()
    plot_coords = load_plot_coords()

    # --- Local mode ---
    if args.local:
        local_path = Path(args.local).expanduser().resolve()
        if not local_path.is_dir():
            print(f"ERROR: Not a directory: {local_path}", file=sys.stderr)
            return 1

        meta_path = local_path / "deployment_event_record.json"
        if not meta_path.exists():
            print(f"ERROR: deployment_event_record.json not found in {local_path}", file=sys.stderr)
            return 1

        with open(meta_path, "r", encoding="utf-8") as f:
            record = json.load(f)

        deployment_metadata = record.get("deployment_info")
        manifest = record

        existing = {p.name for p in local_path.iterdir() if p.is_file()}
        log(f"Processing local: {local_path.name}")
        process_deployment(
            deployment_metadata, manifest, cameras, plot_coords, wi_config,
            local_output_dir=local_path,
            existing_filenames=existing,
            force=args.force,
        )
        return 0

    # --- Box mode ---
    log("Authenticating with Box")
    try:
        client, root_folder_id = load_box_client()
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    user = client.users.get_user_me()
    log(f"Connected as {user.name} ({user.login})")

    log("Traversing Box folder hierarchy")
    deployments = find_deployment_folders(client, root_folder_id)
    log(f"Found {len(deployments)} deployment folder(s)")

    total_written = 0
    for folder_id, folder_name in deployments:
        log(f"\n{folder_name}")
        deployment_metadata, manifest, existing_names = fetch_deployment_jsons(client, folder_id)

        if not deployment_metadata:
            log("  Skipping: deployment_event_record.json not found or unreadable")
            continue

        written = process_deployment(
            deployment_metadata, manifest, cameras, plot_coords, wi_config,
            client=client,
            box_folder_id=folder_id,
            existing_filenames=existing_names,
            force=args.force,
        )
        total_written += written

    log(f"\nDone. {total_written} CSV file(s) uploaded to Box.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
