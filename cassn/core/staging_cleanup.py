"""Conservative cleanup of Box-verified local deployment-event staging.

The workflow is intentionally metadata-only: it compares SHA-1 values captured
at ingest with the SHA-1 values Box computes from stored bytes.  It never
downloads Box media and never re-hashes local media.  A deployment is eligible
only after the normal application workflow has completed and written a passing
``qc/box_upload_verification.json`` artifact.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from cassn.box.client import BoxStorage
from cassn.box.verification import collect_box_file_hashes, is_orphan_on_box
from cassn.config import CONFIG_JSON
from cassn.core.classification import sanitize_box_folder_name
from cassn.core.hashing import sha1 as compute_file_sha1
from cassn.core.inventory import (
    SESSION_SCHEMA_VERSION,
    index_inventory_by_storage_relpath,
)


DEFAULT_STAGING_ROOT = Path.home() / "Desktop" / "CASSN_field_data_staging"
_SHA1_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_BOX_FIELDS = ["id", "type", "name", "sha1"]
_FINISHED_DEVICE_STATUSES = {"Complete", "Skipped"}
_LOCAL_ONLY_AFTER_UPLOAD = {
    # The verification record is necessarily written after verification. Older
    # upload runs also created the manifest after snapshotting uploadable files,
    # so historical events may legitimately retain both QC proofs only locally.
    "qc/box_upload_manifest.json",
    "qc/box_upload_verification.json",
}
_EXACT_SMALL_SIDECARS = {
    "image_file_metadata.csv",
    "audio_file_metadata.csv",
    "deployment_event_record.json",
    "qc/box_upload_manifest.json",
}


class StagingCleanupError(RuntimeError):
    """Raised when an already-planned cleanup can no longer be applied safely."""


@dataclass
class StagingCleanupPlan:
    staging_root: Path
    deployment_folder: Path
    event_name: str
    reasons: list[str] = field(default_factory=list)
    box_folder_id: str = ""
    box_path: str = ""
    file_count: int = 0
    local_bytes: int = 0
    event_signature: str = ""

    @property
    def clearable(self) -> bool:
        return not self.reasons and bool(self.box_folder_id and self.event_signature)


@dataclass(frozen=True)
class StagingCleanupResult:
    deployment_folder: Path
    file_count: int
    bytes_cleared: int


def configured_staging_root(config_path: Path = CONFIG_JSON) -> Path:
    """Return the staging root selected in the app, or the app default."""
    config_path = Path(config_path).expanduser()
    try:
        with config_path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        configured = str(data.get("staging_root") or "").strip()
        if configured:
            return Path(configured).expanduser()
    except (OSError, ValueError, TypeError):
        pass
    return DEFAULT_STAGING_ROOT


def discover_staged_deployments(staging_root: Path) -> list[Path]:
    """Return direct child directories that carry an application session."""
    root = Path(staging_root).expanduser()
    if not root.is_dir():
        return []
    deployments: list[Path] = []
    for child in sorted(root.iterdir(), key=lambda path: path.name.lower()):
        if child.is_dir() and (child / "session.json").is_file():
            deployments.append(child)
    return deployments


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _path_examples(paths: list[str], *, limit: int = 5) -> str:
    examples = ", ".join(paths[:limit])
    if len(paths) > limit:
        examples += f", ... +{len(paths) - limit} more"
    return examples


def _read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} does not contain a JSON object")
    return value


def _event_signature(deployment_folder: Path) -> tuple[str, list[str]]:
    """Hash path/size/mtime metadata to detect any change before deletion."""
    digest = hashlib.sha256()
    errors: list[str] = []
    for path in sorted(deployment_folder.rglob("*")):
        relative = path.relative_to(deployment_folder).as_posix()
        if relative == ".cassn_cleanup.lock":
            continue
        if path.is_symlink():
            errors.append(f"staging contains a symlink: {relative}")
            continue
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError as exc:
            errors.append(f"could not inspect {relative}: {exc}")
            continue
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest(), errors


def _verification_artifact(deployment_folder: Path) -> Path:
    preferred = deployment_folder / "qc" / "box_upload_verification.json"
    if preferred.is_file():
        return preferred
    return deployment_folder / "box_upload_verification.json"


def _provenance_errors(deployment_folder: Path) -> list[str]:
    errors: list[str] = []
    found_rows = 0
    for name in ("image_file_metadata.csv", "audio_file_metadata.csv"):
        path = deployment_folder / name
        if not path.is_file():
            continue
        try:
            with path.open(newline="", encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))
        except (OSError, UnicodeError, csv.Error) as exc:
            errors.append(f"could not read {name}: {exc}")
            continue
        found_rows += len(rows)
        unstamped = sum(
            1 for row in rows if not _truthy(row.get("is_uploaded_to_box"))
        )
        if unstamped:
            errors.append(
                f"{name} has {unstamped} row(s) not stamped as uploaded to Box"
            )
    if found_rows == 0:
        errors.append("no image or audio metadata rows were found")
    return errors


def _resolve_box_deployment(
    storage: BoxStorage,
    field_data_folder_id: str,
    *,
    year: str,
    site_name: str,
    event_name: str,
) -> tuple[str, str]:
    reserve_name = sanitize_box_folder_name(site_name)
    box_path = f"{year}/{reserve_name}/{event_name}"
    year_id = storage.find_child_folder(
        field_data_folder_id, year, fields=_BOX_FIELDS
    )
    if not year_id:
        raise StagingCleanupError(f"Box year folder not found: {year}")
    reserve_id = storage.find_child_folder(
        year_id, reserve_name, fields=_BOX_FIELDS
    )
    if not reserve_id:
        raise StagingCleanupError(f"Box site folder not found: {year}/{reserve_name}")
    event_id = storage.find_child_folder(
        reserve_id, event_name, fields=_BOX_FIELDS
    )
    if not event_id:
        raise StagingCleanupError(f"Box deployment folder not found: {box_path}")
    return str(event_id), box_path


def inspect_deployment_for_cleanup(
    deployment_folder: Path,
    staging_root: Path,
    storage: BoxStorage,
    field_data_folder_id: str,
) -> StagingCleanupPlan:
    """Build a live, fail-closed cleanup plan for one staged deployment."""
    root = Path(staging_root).expanduser().resolve()
    deployment = Path(deployment_folder).expanduser()
    plan = StagingCleanupPlan(root, deployment, deployment.name)

    if Path(staging_root).expanduser().is_symlink():
        plan.reasons.append(f"staging root is a symlink: {staging_root}")
        return plan
    if deployment.is_symlink():
        plan.reasons.append(f"deployment folder is a symlink: {deployment}")
        return plan
    try:
        deployment = deployment.resolve(strict=True)
    except OSError as exc:
        plan.reasons.append(f"deployment folder is unavailable: {exc}")
        return plan
    plan.deployment_folder = deployment
    if deployment == root or deployment.parent != root:
        plan.reasons.append("deployment is not a direct child of the staging root")
        return plan

    session_path = deployment / "session.json"
    try:
        session = _read_json(session_path)
    except (OSError, ValueError) as exc:
        plan.reasons.append(f"invalid session.json: {exc}")
        return plan
    if session.get("schema_version") != SESSION_SCHEMA_VERSION:
        plan.reasons.append("session.json has an unsupported schema version")

    recorded_folder = str(session.get("deployment_folder") or "").strip()
    if not recorded_folder:
        plan.reasons.append("session.json does not record its deployment folder")
    else:
        try:
            if Path(recorded_folder).expanduser().resolve() != deployment:
                plan.reasons.append(
                    "session.json points to a different deployment folder"
                )
        except OSError:
            plan.reasons.append("session.json contains an invalid deployment folder")

    metadata = session.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        plan.reasons.append("session.json metadata is missing")
    event_id = str(metadata.get("deployment_event_id") or "").strip()
    if not event_id or event_id != deployment.name:
        plan.reasons.append("deployment folder name does not match deployment_event_id")

    devices = session.get("devices")
    statuses = session.get("device_statuses")
    if not isinstance(devices, list) or not devices:
        plan.reasons.append("session.json has no selected devices")
    if not isinstance(statuses, dict):
        statuses = {}
    unfinished: list[str] = []
    for device in devices if isinstance(devices, list) else []:
        label = str(device[3]) if isinstance(device, (list, tuple)) and len(device) > 3 else ""
        status = statuses.get(label, {}) if label else {}
        value = status.get("status") if isinstance(status, dict) else ""
        if not label or value not in _FINISHED_DEVICE_STATUSES:
            unfinished.append(label or "<unknown device>")
    if unfinished:
        plan.reasons.append(
            "device collection is not complete: " + ", ".join(unfinished[:10])
        )

    verification_path = _verification_artifact(deployment)
    try:
        verification = _read_json(verification_path)
        if verification.get("verified") is not True:
            plan.reasons.append("latest Box upload verification did not pass")
    except (OSError, ValueError) as exc:
        plan.reasons.append(f"passing Box verification artifact is unavailable: {exc}")

    plan.reasons.extend(_provenance_errors(deployment))

    inventory = session.get("file_inventory")
    if not isinstance(inventory, list) or not inventory:
        plan.reasons.append("session.json has no file inventory")
        inventory = []
    try:
        inventory_by_path = index_inventory_by_storage_relpath(inventory)
    except ValueError as exc:
        plan.reasons.append(f"invalid file inventory: {exc}")
        inventory_by_path = {}

    local_uploadable: set[str] = set()
    local_raw: set[str] = set()
    for path in sorted(deployment.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(deployment).as_posix()
        if not BoxStorage.should_upload_file(path):
            continue
        local_uploadable.add(relative)
        if relative.startswith("raw_data/"):
            local_raw.add(relative)

    expected_raw = set(inventory_by_path)
    missing_local = sorted(expected_raw - local_raw)
    untracked_local = sorted(local_raw - expected_raw)
    if missing_local:
        plan.reasons.append(
            f"{len(missing_local)} inventoried raw file(s) are missing locally: "
            f"{_path_examples(missing_local)}"
        )
    if untracked_local:
        plan.reasons.append(
            f"{len(untracked_local)} local raw file(s) are absent from session inventory: "
            f"{_path_examples(untracked_local)}"
        )

    stored_hashes: dict[str, str] = {}
    for relative, entry in inventory_by_path.items():
        digest = str(entry.get("file_hash_sha1") or "").strip().lower()
        if not _SHA1_RE.fullmatch(digest):
            plan.reasons.append(f"stored SHA-1 is missing or invalid: {relative}")
            continue
        stored_hashes[relative] = digest
        local_path = deployment.joinpath(*relative.split("/"))
        if not local_path.is_file():
            continue
        try:
            size = local_path.stat().st_size
            recorded_size = int(entry.get("file_size_bytes"))
        except (OSError, TypeError, ValueError):
            plan.reasons.append(f"stored file size is missing or invalid: {relative}")
            continue
        if size != recorded_size:
            plan.reasons.append(f"local file size changed after ingest: {relative}")

    plan.file_count = len(expected_raw)
    plan.local_bytes = sum(
        deployment.joinpath(*relative.split("/")).stat().st_size
        for relative in expected_raw
        if deployment.joinpath(*relative.split("/")).is_file()
    )
    plan.event_signature, signature_errors = _event_signature(deployment)
    plan.reasons.extend(signature_errors)

    # Do not spend Box API calls on an event that already fails local safety
    # checks.  Fix the local evidence first, then rerun the dry run.
    if plan.reasons:
        return plan

    site_name = str(metadata.get("site_name") or "").strip()
    end_date = str(metadata.get("deployment_event_end_date") or "").strip()
    year = end_date[:4]
    if not site_name:
        plan.reasons.append("site_name is missing from session metadata")
        return plan
    if not re.fullmatch(r"\d{4}", year):
        plan.reasons.append("deployment_event_end_date does not identify a year")
        return plan
    if not field_data_folder_id:
        plan.reasons.append("Box field-data folder ID is not configured")
        return plan

    try:
        plan.box_folder_id, plan.box_path = _resolve_box_deployment(
            storage,
            str(field_data_folder_id),
            year=year,
            site_name=site_name,
            event_name=deployment.name,
        )
        box_hashes = collect_box_file_hashes(storage, plan.box_folder_id)
    except Exception as exc:
        plan.reasons.append(str(exc))
        return plan

    expected_on_box = local_uploadable - _LOCAL_ONLY_AFTER_UPLOAD
    missing_box = sorted(expected_on_box - set(box_hashes))
    if missing_box:
        plan.reasons.append(
            f"{len(missing_box)} local uploadable file(s) are missing from Box: "
            f"{_path_examples(missing_box)}"
        )

    unexpected_box_raw = sorted(
        path
        for path in (set(box_hashes) - expected_raw)
        if path.startswith("raw_data/") and is_orphan_on_box(path)
    )
    if unexpected_box_raw:
        plan.reasons.append(
            f"{len(unexpected_box_raw)} unexpected raw file(s) are present on Box: "
            f"{_path_examples(unexpected_box_raw)}"
        )

    for relative, expected_hash in stored_hashes.items():
        box_hash = box_hashes.get(relative)
        if box_hash is None:
            continue
        if not box_hash:
            plan.reasons.append(f"Box SHA-1 is unavailable: {relative}")
        elif box_hash != expected_hash:
            plan.reasons.append(f"Box SHA-1 does not match ingest SHA-1: {relative}")

    for relative in sorted(_EXACT_SMALL_SIDECARS & expected_on_box):
        box_hash = box_hashes.get(relative)
        if box_hash is None:
            continue
        if not box_hash:
            plan.reasons.append(f"Box SHA-1 is unavailable: {relative}")
            continue
        local_hash = compute_file_sha1(deployment.joinpath(*relative.split("/")))
        if box_hash != local_hash:
            plan.reasons.append(f"Box copy is not current: {relative}")

    return plan


def clear_verified_deployment(plan: StagingCleanupPlan) -> StagingCleanupResult:
    """Permanently remove one still-unchanged, preflighted deployment folder."""
    if not plan.clearable:
        raise StagingCleanupError("deployment did not pass cleanup preflight")

    root = plan.staging_root.resolve()
    deployment = plan.deployment_folder
    if deployment.is_symlink():
        raise StagingCleanupError("deployment became a symlink after preflight")
    try:
        resolved = deployment.resolve(strict=True)
    except OSError as exc:
        raise StagingCleanupError(f"deployment is no longer available: {exc}") from exc
    if resolved == root or resolved.parent != root:
        raise StagingCleanupError("refusing to clear outside the staging root")

    current_signature, errors = _event_signature(resolved)
    if errors or current_signature != plan.event_signature:
        raise StagingCleanupError(
            "deployment changed after cleanup preflight; rerun the command"
        )

    lock_path = resolved / ".cassn_cleanup.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise StagingCleanupError("another cleanup is already using this deployment") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(f"pid={os.getpid()}\n")

    try:
        shutil.rmtree(resolved)
    except Exception as exc:
        if lock_path.exists():
            lock_path.unlink(missing_ok=True)
        raise StagingCleanupError(f"could not clear {resolved}: {exc}") from exc

    return StagingCleanupResult(
        deployment_folder=resolved,
        file_count=plan.file_count,
        bytes_cleared=plan.local_bytes,
    )
