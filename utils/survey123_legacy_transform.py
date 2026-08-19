"""Transform verified Survey123 snapshots into candidate app lookup tables.

This is a deliberately non-destructive legacy adapter.  It reads a raw snapshot
plus the current lookup contract, writes a private candidate directory, and
never changes ArcGIS or the active lookup cache.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import tempfile
from collections import defaultdict
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

try:
    from cassn.lookups import canonical_deployment_ids, placement_key
except ModuleNotFoundError:  # Direct execution from outside the repository root.
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from cassn.lookups import canonical_deployment_ids, placement_key


PACIFIC = ZoneInfo("America/Los_Angeles")
SURVEY_SITE_CODE_ALIASES = {"BDCDRC": "BDC"}
GENERATED_OPEN_EVENT_PREFIXES = ("INFERRED_", "OPEN_")

DEVICE_FIELDS = (
    "device_record_id",
    "device_id",
    "device_type",
    "device_family",
    "first_seen",
    "last_seen",
    "deployment_count",
    "source_roles",
    "source_globalids",
)

DEPLOYMENT_FIELDS = (
    "deployment_id",
    "deployment_event_id",
    "event_start_date",
    "event_end_date",
    "event_match_status",
    "site_short_name",
    "plot_number",
    "device_type",
    "device_record_id",
    "device_id",
    "deployment_start_date",
    "deployment_start_datetime",
    "deployment_end_date",
    "deployment_end_datetime",
    "deployment_end_reason",
    # Existing camera lookup contract used by metadata/WI writers.
    "camera_id",
    "feature_type",
    "sensor_height",
    "sensor_orientation",
    "plot_treatment",
    "plot_treatment_description",
    "detection_distance",
    # Existing ARU lookup contract used by metadata/SoundHub writers.
    "mounted_on",
    "sensor_height_meters",
    "ARU_status",
    # Survey-specific deployment observations, kept separate from authoritative plots.
    "survey_latitude",
    "survey_longitude",
    "survey_feature",
    "survey_sensor_height",
    "survey_sensor_orientation",
    "survey_mounted_on",
    "survey_notes",
    # Retrieval/provenance fields.
    "retrieval_methods",
    "retrieval_notes",
    "source_role",
    "source_form_item_id",
    "source_globalid",
    "source_objectid",
    "source_edit_datetime",
    "retrieval_globalid",
    "retrieval_objectid",
    "plot_resolution",
    "compatibility_source",
)

ISSUE_FIELDS = (
    "severity",
    "code",
    "source_role",
    "source_globalid",
    "source_objectid",
    "message",
)


def _canonical_deployment_event_id(row: Mapping[str, Any]) -> str:
    """Return a closed-event ID and leave every open event unnamed.

    The legacy adapter used to mint ``OPEN_`` and ``INFERRED_`` placeholders.
    Those values are not authoritative Survey123 identifiers and, once copied
    into the active lookup, could be mistaken for real events by a later
    refresh. Preserve a real closed-event ID when one exists; otherwise derive
    the closed event name from the actual placement end date.
    """
    identity_date = _text(row.get("deployment_end_date"))
    if not identity_date:
        return ""
    current = _text(row.get("deployment_event_id"))
    if current and not current.startswith(GENERATED_OPEN_EVENT_PREFIXES):
        return current
    try:
        date_token = date.fromisoformat(identity_date).strftime("%Y%m%d")
    except ValueError as exc:
        raise TransformError(
            "Cannot name a closed deployment event without a valid end date"
        ) from exc
    return f"UC_{_text(row.get('site_short_name'))}_{date_token}"


class TransformError(RuntimeError):
    """Raised when a candidate transform cannot be completed safely."""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _local_datetime(epoch_ms: Any) -> datetime | None:
    if not isinstance(epoch_ms, (int, float)):
        return None
    return datetime.fromtimestamp(epoch_ms / 1000, PACIFIC)


def _local_iso(epoch_ms: Any) -> str:
    value = _local_datetime(epoch_ms)
    return value.isoformat(timespec="seconds") if value else ""


def _local_date(epoch_ms: Any) -> date | None:
    value = _local_datetime(epoch_ms)
    return value.date() if value else None


def _first_epoch(attributes: Mapping[str, Any], fields: tuple[str, ...]) -> Any:
    for field in fields:
        value = attributes.get(field)
        if isinstance(value, (int, float)):
            return value
    return None


def _normalize_plot(value: Any) -> str:
    raw = _text(value)
    if raw.lower().startswith("p"):
        raw = raw[1:]
    try:
        return str(int(raw))
    except ValueError:
        return raw


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise TransformError(f"Lookup CSV has no header: {path}")
            return list(reader.fieldnames), [dict(row) for row in reader]
    except FileNotFoundError as exc:
        raise TransformError(f"Required current lookup is missing: {path}") from exc


def _snapshot_features(snapshot_path: Path, role: str) -> list[dict[str, Any]]:
    page_dir = snapshot_path / role / "layers" / "0" / "pages"
    page_paths = sorted(page_dir.glob("page-*.json"))
    if not page_paths:
        raise TransformError(f"Snapshot has no feature pages for role: {role}")
    features: list[dict[str, Any]] = []
    for page_path in page_paths:
        payload = json.loads(page_path.read_text(encoding="utf-8"))
        page_features = payload.get("features")
        if not isinstance(page_features, list):
            raise TransformError(f"Invalid snapshot feature page: {page_path}")
        for feature in page_features:
            attributes = feature.get("attributes") if isinstance(feature, dict) else None
            if not isinstance(attributes, dict):
                raise TransformError(f"Invalid feature in snapshot page: {page_path}")
            features.append(dict(attributes))
    return features


def _write_private_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    if os.name != "nt":
        os.chmod(path, 0o600)


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o600)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _survey_item_ids(snapshot_path: Path) -> dict[str, str]:
    manifest = json.loads((snapshot_path / "manifest.json").read_text(encoding="utf-8"))
    return {
        str(source.get("role")): str(source.get("form_item_id", ""))
        for source in manifest.get("sources", [])
        if isinstance(source, dict)
    }


def _canonical_site(
    attributes: Mapping[str, Any],
    survey_value_to_short_name: Mapping[str, str],
) -> tuple[str, str]:
    survey_value = _text(attributes.get("siteID"))
    if survey_value == "other":
        survey_value = _text(attributes.get("siteID_other"))
    survey_value = SURVEY_SITE_CODE_ALIASES.get(survey_value, survey_value)
    return survey_value_to_short_name.get(survey_value, ""), survey_value


def _deployment_time(role: str, attributes: Mapping[str, Any]) -> Any:
    if role in {"ml_camera", "sa_camera"}:
        return _first_epoch(attributes, ("deploymentEndDateTime", "calcEndTime", "site_date"))
    return _first_epoch(attributes, ("deployment_endtime", "calcEndDateTime", "site_date"))


def _camera_observations(role: str, attributes: Mapping[str, Any]) -> dict[str, str]:
    if role == "ml_camera":
        return {
            "survey_latitude": _text(attributes.get("cameraLat") or attributes.get("camera_geo_y")),
            "survey_longitude": _text(attributes.get("cameraLong") or attributes.get("camera_geo_x")),
            "survey_feature": _text(attributes.get("mlCameraLocation")),
            "survey_sensor_orientation": _text(attributes.get("mlCameraDirection")),
            "survey_notes": _text(attributes.get("cameraNotes")),
        }
    return {
        "survey_latitude": _text(attributes.get("cameraLat") or attributes.get("camera_geo_y")),
        "survey_longitude": _text(attributes.get("cameraLong") or attributes.get("camera_geo_x")),
        "survey_feature": _text(attributes.get("cameraHabitat")),
        "survey_notes": _text(attributes.get("cameraNotes")),
    }


def _aru_observations(device_type: str, attributes: Mapping[str, Any]) -> dict[str, str]:
    if _text(attributes.get("single_aru")).lower() in {"yes", "true", "1"}:
        return {
            "survey_latitude": _text(attributes.get("aru_single_geo_y")),
            "survey_longitude": _text(attributes.get("aru_single_geo_x")),
            "survey_sensor_height": _text(attributes.get("aru_recorder_height")),
            "survey_sensor_orientation": _text(attributes.get("aru_recorder_orientation")),
            "survey_mounted_on": _text(attributes.get("aru_recorder_support")),
            "survey_notes": _text(attributes.get("aru_install_notes")),
        }
    prefix = "bird" if device_type == "BD" else "bat"
    return {
        "survey_latitude": _text(
            attributes.get(f"{prefix}_Y") or attributes.get("recorders_Y")
        ),
        "survey_longitude": _text(
            attributes.get(f"{prefix}_X") or attributes.get("recorders_X")
        ),
        "survey_sensor_height": _text(attributes.get(f"{prefix}_recorder_height")),
        "survey_sensor_orientation": _text(
            attributes.get(f"{prefix}_recorder_orientation")
        ),
        "survey_mounted_on": _text(
            attributes.get(f"{prefix}_recorder_support")
            or attributes.get("both_recorders_support")
        ),
        "survey_notes": _text(attributes.get(f"{prefix}_install_notes"))
        or _text(attributes.get("final_deployment_notes")),
    }


def _retrieval_notes(attributes: Mapping[str, Any]) -> str:
    fields = (
        "SARetrievalNotes",
        "MLRetrievalNotes",
        "finalRecorderRetrievalNotes",
        "damageCheck",
        "dysfunctionalRecorders",
        "wetRecorders",
    )
    return " | ".join(
        f"{field}={_text(attributes.get(field))}"
        for field in fields
        if _text(attributes.get(field))
    )


def transform_legacy_snapshot(
    snapshot_path: Path,
    lookup_dir: Path,
    output_root: Path,
) -> tuple[Path, dict[str, Any]]:
    """Generate private candidate devices/deployments without changing active lookups."""

    snapshot_path = snapshot_path.resolve()
    lookup_dir = lookup_dir.resolve()
    output_root = output_root.resolve()
    snapshot_id = snapshot_path.name
    if not (snapshot_path / "manifest.json").is_file():
        raise TransformError(f"Not a verified snapshot directory: {snapshot_path}")

    site_fields, site_rows = _read_csv(lookup_dir / "sites.csv")
    plot_fields, plot_rows = _read_csv(lookup_dir / "plots.csv")
    deployment_fields, deployment_rows = _read_csv(lookup_dir / "deployments.csv")
    required_site_fields = {"site_name", "site_short_name", "site_code"}
    required_plot_fields = {"site_short_name", "plot_number"}
    required_deployment_fields = {
        "deployment_event_id",
        "site_short_name",
        "plot_number",
        "device_type",
        "deployment_start_date",
        "deployment_end_date",
    }
    for filename, fields, required in (
        ("sites.csv", site_fields, required_site_fields),
        ("plots.csv", plot_fields, required_plot_fields),
        ("deployments.csv", deployment_fields, required_deployment_fields),
    ):
        missing = sorted(required - set(fields))
        if missing:
            raise TransformError(
                f"{filename} uses the wrong active schema; missing columns: "
                + ", ".join(missing)
            )

    camera_rows = [
        row for row in deployment_rows if _text(row.get("device_type")) in {"ML", "SA"}
    ]
    aru_rows = [
        row for row in deployment_rows if _text(row.get("device_type")) in {"BD", "BT"}
    ]
    survey_value_to_short_name: dict[str, str] = {}
    for row in site_rows:
        site_short_name = _text(row.get("site_short_name"))
        survey_value_to_short_name[_text(row.get("site_code"))] = site_short_name
        survey_value_to_short_name[site_short_name] = site_short_name
    valid_plots = {
        (_text(row.get("site_short_name")), _normalize_plot(row.get("plot_number")))
        for row in plot_rows
    }
    current_cameras: dict[tuple[str, str, str], dict[str, str]] = {}
    for row in sorted(camera_rows, key=lambda value: _text(value.get("deployment_start_date"))):
        current_cameras[
            (
                _text(row.get("site_short_name")),
                _normalize_plot(row.get("plot_number")),
                _text(row.get("device_type")),
            )
        ] = row
    current_arus_by_key: dict[tuple[str, str, str], dict[str, str]] = {}
    current_arus_by_event: dict[tuple[str, str, str, str], dict[str, str]] = {}
    for row in aru_rows:
        key = (
            _text(row.get("site_short_name")),
            _normalize_plot(row.get("plot_number")),
            _text(row.get("device_type")),
        )
        current_arus_by_key[key] = row
        current_arus_by_event[(_text(row.get("deployment_event_id")), *key)] = row

    events_by_site: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in deployment_rows:
        start_text = _text(row.get("event_start_date") or row.get("deployment_start_date"))
        if not start_text:
            continue
        start = date.fromisoformat(start_text)
        end_text = _text(row.get("event_end_date") or row.get("deployment_end_date"))
        events_by_site[_text(row.get("site_short_name"))].append(
            {
                "start": start,
                "end": date.fromisoformat(end_text) if end_text else None,
                "event_id": _text(row.get("deployment_event_id")),
            }
        )

    item_ids = _survey_item_ids(snapshot_path)
    issues: list[dict[str, Any]] = []
    issue_keys: set[tuple[str, str, str, str, str]] = set()
    normalized: list[dict[str, Any]] = []

    def add_issue(
        severity: str,
        code: str,
        role: str,
        attributes: Mapping[str, Any],
        message: str,
    ) -> None:
        key = (
            severity,
            code,
            role,
            _text(attributes.get("globalid")),
            _text(attributes.get("objectid")),
        )
        if key in issue_keys:
            return
        issue_keys.add(key)
        issues.append(
            {
                "severity": severity,
                "code": code,
                "source_role": role,
                "source_globalid": _text(attributes.get("globalid")),
                "source_objectid": _text(attributes.get("objectid")),
                "message": message,
            }
        )

    def add_deployment(
        role: str,
        attributes: Mapping[str, Any],
        device_type: str,
        device_id: str,
    ) -> None:
        site_short_name, raw_site = _canonical_site(attributes, survey_value_to_short_name)
        if not site_short_name:
            add_issue("blocking", "unknown_site", role, attributes, f"Unknown site: {raw_site}")
            return
        plot = _normalize_plot(attributes.get("plot_number"))
        plot_resolution = "survey"
        if (site_short_name, plot) not in valid_plots:
            add_issue(
                "blocking",
                "unknown_plot",
                role,
                attributes,
                f"Cannot resolve authoritative plot for {site_short_name}: {plot or '<missing>'}",
            )
            return
        start_epoch = _deployment_time(role, attributes)
        start_dt = _local_datetime(start_epoch)
        site_day = _local_date(attributes.get("site_date"))
        if not start_dt or not site_day:
            add_issue("blocking", "missing_deployment_time", role, attributes, "Missing deployment date/time")
            return

        event_candidates = [
            event
            for event in events_by_site.get(site_short_name, [])
            if event["start"] <= site_day
            and (event["end"] is None or site_day <= event["end"])
        ]
        if event_candidates:
            matched_event = max(event_candidates, key=lambda event: event["start"])
            event_id = matched_event["event_id"]
            event_status = "current_lookup"
            event_start = matched_event["start"]
            event_end = matched_event["end"]
        else:
            event_id = ""
            event_status = "unmatched"
            event_start = site_day
            event_end = None
            add_issue(
                "warning",
                "missing_current_event",
                role,
                attributes,
                f"No current deployment event contains {site_short_name} on {site_day}",
            )

        globalid = _text(attributes.get("globalid"))
        device_record_id = f"{device_type}:{device_id}" if device_id else (
            f"{device_type}:unknown:{globalid or _text(attributes.get('objectid'))}"
        )
        normalized.append(
            {
                "deployment_id": "",
                "deployment_event_id": event_id,
                "event_start_date": event_start.isoformat(),
                "event_end_date": event_end.isoformat() if event_end else "",
                "event_match_status": event_status,
                "site_short_name": site_short_name,
                "plot_number": plot,
                "device_type": device_type,
                "device_record_id": device_record_id,
                "device_id": device_id,
                "deployment_start_date": site_day.isoformat(),
                "deployment_start_datetime": start_dt.isoformat(timespec="seconds"),
                "source_role": role,
                "source_form_item_id": item_ids.get(role, ""),
                "source_globalid": globalid,
                "source_objectid": _text(attributes.get("objectid")),
                "source_edit_datetime": _local_iso(attributes.get("EditDate")),
                "plot_resolution": plot_resolution,
                "_start_dt": start_dt,
                "_attributes": dict(attributes),
            }
        )

    for role, device_type in (("ml_camera", "ML"), ("sa_camera", "SA")):
        for attributes in _snapshot_features(snapshot_path, role):
            add_deployment(role, attributes, device_type, _text(attributes.get("camera1ID")))

    for attributes in _snapshot_features(snapshot_path, "aru"):
        single = _text(attributes.get("single_aru")).lower() in {"yes", "true", "1"}
        if single:
            kind = _text(attributes.get("bird_bat")).lower()
            device_type = "BD" if "bird" in kind else "BT" if "bat" in kind else ""
            if not device_type:
                add_issue("blocking", "unknown_aru_type", "aru", attributes, "Single ARU has no bird/bat type")
                continue
            add_deployment("aru", attributes, device_type, _text(attributes.get("AM_ID_single")))
        else:
            add_deployment("aru", attributes, "BD", _text(attributes.get("AM_bird_ID")))
            add_deployment("aru", attributes, "BT", _text(attributes.get("AM_bat_ID")))

    retrievals_by_plot: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for attributes in _snapshot_features(snapshot_path, "retrieval"):
        site_short_name, raw_site = _canonical_site(attributes, survey_value_to_short_name)
        plot = _normalize_plot(attributes.get("Plot"))
        if not site_short_name or (site_short_name, plot) not in valid_plots:
            add_issue(
                "blocking",
                "invalid_retrieval_key",
                "retrieval",
                attributes,
                f"Cannot resolve retrieval site/plot: {raw_site} {plot}",
            )
            continue
        epoch = _first_epoch(attributes, ("retrievalEndDateTime", "calcEndTime", "site_date"))
        value = _local_datetime(epoch)
        if not value:
            add_issue("blocking", "missing_retrieval_time", "retrieval", attributes, "Missing retrieval time")
            continue
        retrievals_by_plot[(site_short_name, plot)].append(
            {"time": value, "attributes": attributes}
        )
    for values in retrievals_by_plot.values():
        values.sort(key=lambda item: item["time"])

    by_slot: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in normalized:
        by_slot[(row["site_short_name"], row["plot_number"], row["device_type"])].append(row)
    for rows in by_slot.values():
        rows.sort(key=lambda row: row["_start_dt"])
        for index, row in enumerate(rows):
            next_start = rows[index + 1]["_start_dt"] if index + 1 < len(rows) else None
            retrieval = next(
                (
                    item
                    for item in retrievals_by_plot.get((row["site_short_name"], row["plot_number"]), [])
                    # Retrieval forms are often submitted after the replacement
                    # device has already been installed. A retrieval on the same
                    # calendar day closes the prior placement, not the new one.
                    if item["time"].date() > row["_start_dt"].date()
                ),
                None,
            )
            retrieval_time = retrieval["time"] if retrieval else None
            if next_start and (not retrieval_time or next_start <= retrieval_time):
                end_time = next_start
                end_reason = "redeployed"
                retrieval = None
            elif retrieval_time:
                end_time = retrieval_time
                end_reason = "retrieved"
            elif row["event_end_date"]:
                end_time = datetime.combine(
                    date.fromisoformat(row["event_end_date"]), time.max, PACIFIC
                )
                end_reason = "current_event_end"
            else:
                end_time = None
                end_reason = "open"
            row["deployment_end_datetime"] = (
                end_time.isoformat(timespec="seconds") if end_time else ""
            )
            row["deployment_end_date"] = end_time.date().isoformat() if end_time else ""
            row["deployment_end_reason"] = end_reason
            row["deployment_event_id"] = _canonical_deployment_event_id(row)
            if retrieval:
                attributes = retrieval["attributes"]
                row["retrieval_methods"] = _text(attributes.get("MethodsDeployed"))
                row["retrieval_notes"] = _retrieval_notes(attributes)
                row["retrieval_globalid"] = _text(attributes.get("globalid"))
                row["retrieval_objectid"] = _text(attributes.get("objectid"))

    # Assigned only once every row has its end date and event ID: the deployment
    # ID carries the *round's* last retrieval date, and a single row cannot know
    # which round it belongs to. Open placements stay unnamed.
    resolved_ids = canonical_deployment_ids(normalized)
    for row in normalized:
        row["deployment_id"] = resolved_ids.get(placement_key(row), "")

    for row in normalized:
        key = (row["site_short_name"], row["plot_number"], row["device_type"])
        attributes = row["_attributes"]
        if row["device_type"] in {"ML", "SA"}:
            current = current_cameras.get(key, {})
            row.update(_camera_observations(row["source_role"], attributes))
            row.update(
                {
                    "camera_id": _text(current.get("camera_id")) or row["device_id"],
                    "feature_type": _text(current.get("feature_type")),
                    "sensor_height": _text(current.get("sensor_height")) or "Knee height",
                    "sensor_orientation": _text(current.get("sensor_orientation"))
                    or ("Parallel" if row["device_type"] == "ML" else "Pointed Downward"),
                    "plot_treatment": _text(current.get("plot_treatment")),
                    "plot_treatment_description": _text(
                        current.get("plot_treatment_description")
                    ),
                    "detection_distance": _text(current.get("detection_distance")),
                    "compatibility_source": (
                        "previous_deployments" if current
                        else "survey_fallback"
                    ),
                }
            )
        else:
            event_key = (row["deployment_event_id"], *key)
            current = current_arus_by_event.get(event_key) or current_arus_by_key.get(key, {})
            row.update(_aru_observations(row["device_type"], attributes))
            row.update(
                {
                    "mounted_on": _text(current.get("mounted_on")),
                    "sensor_height_meters": _text(current.get("sensor_height_meters")),
                    "ARU_status": _text(current.get("ARU_status")),
                    "compatibility_source": (
                        "previous_deployments" if current
                        else "survey_only"
                    ),
                }
            )

    device_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in normalized:
        device_groups[row["device_record_id"]].append(row)
    device_rows: list[dict[str, Any]] = []
    for device_record_id, rows in sorted(device_groups.items()):
        roles = sorted({row["source_role"] for row in rows})
        globalids = sorted({row["source_globalid"] for row in rows if row["source_globalid"]})
        starts = sorted(row["deployment_start_date"] for row in rows)
        device_type = rows[0]["device_type"]
        device_rows.append(
            {
                "device_record_id": device_record_id,
                "device_id": rows[0]["device_id"],
                "device_type": device_type,
                "device_family": "camera" if device_type in {"ML", "SA"} else "ARU",
                "first_seen": starts[0],
                "last_seen": starts[-1],
                "deployment_count": len(rows),
                "source_roles": ";".join(roles),
                "source_globalids": ";".join(globalids),
            }
        )

    clean_deployments = []
    for row in sorted(
        normalized,
        key=lambda item: (
            item["deployment_start_datetime"],
            item["site_short_name"],
            int(item["plot_number"]),
            item["device_type"],
        ),
    ):
        clean_deployments.append({field: row.get(field, "") for field in DEPLOYMENT_FIELDS})

    output_root.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(output_root, 0o700)
    final_path = output_root / snapshot_id
    if final_path.exists():
        raise TransformError(f"Candidate transform already exists: {final_path}")
    temporary_path = Path(tempfile.mkdtemp(prefix=f".{snapshot_id}-", dir=output_root))
    if os.name != "nt":
        os.chmod(temporary_path, 0o700)
    try:
        _write_private_csv(temporary_path / "devices.csv", DEVICE_FIELDS, device_rows)
        _write_private_csv(
            temporary_path / "deployments.csv", DEPLOYMENT_FIELDS, clean_deployments
        )
        _write_private_csv(temporary_path / "issues.csv", ISSUE_FIELDS, issues)
        for name in ("wi_config.json", "soundhub_config.json"):
            source = lookup_dir / name
            if not source.is_file():
                raise TransformError(f"Required current config is missing: {source}")
            shutil.copyfile(source, temporary_path / name)
            if os.name != "nt":
                os.chmod(temporary_path / name, 0o600)
        schema = {
            "schema_version": 1,
            "devices.csv": list(DEVICE_FIELDS),
            "deployments.csv": list(DEPLOYMENT_FIELDS),
            "issues.csv": list(ISSUE_FIELDS),
            "preserved_current_contract": {
                "sites.csv": site_fields,
                "plots.csv": plot_fields,
                "device_compatibility_fields": deployment_fields,
                "deployments.csv": deployment_fields,
                "wi_config.json": sorted(
                    json.loads((lookup_dir / "wi_config.json").read_text(encoding="utf-8-sig"))
                ),
                "soundhub_config.json": sorted(
                    json.loads((lookup_dir / "soundhub_config.json").read_text(encoding="utf-8-sig"))
                ),
            },
        }
        _write_private_json(temporary_path / "schema.json", schema)
        manifest = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_snapshot": str(snapshot_path),
            "source_snapshot_id": snapshot_id,
            "current_lookup_dir": str(lookup_dir),
            "authoritative_inputs": {
                name: {
                    "path": str(lookup_dir / name),
                    "sha256": _file_sha256(lookup_dir / name),
                }
                for name in ("sites.csv", "plots.csv")
            },
            "preserved_configs": {
                name: _file_sha256(lookup_dir / name)
                for name in ("wi_config.json", "soundhub_config.json")
            },
            "counts": {
                "devices": len(device_rows),
                "deployments": len(clean_deployments),
                "issues": len(issues),
                "blocking_issues": sum(issue["severity"] == "blocking" for issue in issues),
                "warnings": sum(issue["severity"] == "warning" for issue in issues),
            },
            "active_lookups_modified": False,
        }
        _write_private_json(temporary_path / "manifest.json", manifest)
        os.replace(temporary_path, final_path)
        if os.name != "nt":
            for directory in [final_path, *[p for p in final_path.rglob("*") if p.is_dir()]]:
                os.chmod(directory, 0o700)
        return final_path, manifest
    except Exception:
        shutil.rmtree(temporary_path, ignore_errors=True)
        raise
