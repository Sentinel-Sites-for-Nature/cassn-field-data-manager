"""Conservative cleanup of Box-verified local deployment-event staging.

The workflow is intentionally metadata-only: it compares SHA-1 values captured
at ingest with the SHA-1 values Box computes from stored bytes.  It never
downloads Box media and never re-hashes local media.  A deployment is eligible
only after the normal application workflow has completed and written a passing
``qc/box_upload_verification.json`` artifact. Explicit force bypasses data
verification while retaining saved ingest/publication holds, local deletion
boundaries and change detection. Unreadable hold/publication evidence blocks cleanup.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import plistlib
import stat
import sys
from contextlib import nullcontext
from datetime import datetime, timezone
from uuid import uuid4
from dataclasses import dataclass, field
from pathlib import Path

from cassn.core.ingest_safety import require_no_ingest_holds
from cassn.box.client import BoxStorage
from cassn.core.wi_requests import require_wi_complete, staging_write_lock
from cassn.box.upload_state import require_uploads_complete
from cassn.box.publication import STATE_PATH, batch_remote_path, require_publication_complete
from cassn.box.verification import collect_box_file_hashes, is_orphan_on_box, recorded_archive_paths
from cassn.box.metadata_merge import CSV_NAMES, merge_csv, merge_event, read_csv, read_object
from cassn.box.metadata_store import BoxFileStore
from cassn.config import CONFIG_JSON, DEFAULT_STAGING_ROOT, LOCK_DIR, VERSION
from cassn.profile import PROFILE
from cassn.core.event_lock import ResourceBusy, ResourceLease, deployment_keys
from cassn.core.cleanup_evidence import platform_inventory, soundhub_clearance
from cassn.core.wi_requests import STATE_FILE, camera_clearance
from cassn.box.upload_state import atomic_json
from cassn.soundhub import receipt_location
from cassn.soundhub import upload as soundhub_upload
from cassn.soundhub.upload import load_soundhub_config
from cassn.core.classification import sanitize_box_folder_name
from cassn.core.hashing import sha1 as compute_file_sha1
from cassn.core.inventory import (
    SESSION_SCHEMA_VERSION,
    index_inventory_by_storage_relpath,
    legacy_device_manifest,
    session_summary_text,
)



_SHA1_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_BOX_FIELDS = ["id", "type", "name", "sha1"]
_FINISHED_DEVICE_STATUSES = {"Complete", "Skipped"}
_LOCAL_ONLY_AFTER_UPLOAD = {
    # The verification record is necessarily written after verification. Older
    # upload runs also created the manifest after snapshotting uploadable files,
    # so historical events may legitimately retain both QC proofs only locally.
    "qc/box_upload_manifest.json",
    "qc/box_upload_verification.json",
    "qc/file_hash_verification.json",
    "qc/box_file_list_verification.json",
}


def _report_reason(value):
    if re.search(r"(?i)\b(?:https?://|s3://|bearer\s|authorization\b|access_token\b|refresh_token\b|client_secret\b)", value):
        return "Service evidence is unavailable. Check the connection and run Check again."
    return value


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
    force: bool = False

    profile: str = PROFILE
    config_path: Path = CONFIG_JSON
    field_data_folder_id: str = ""
    storage: BoxStorage | None = field(default=None, repr=False)
    checks: dict = field(default_factory=lambda: {name: {"status": "incomplete"} for name in
                                                 ("Local staging", "Box", "Wildlife Insights", "SoundHub")})
    warnings: list[str] = field(default_factory=list)
    staged_files_signature: str = ""
    staged_flacs: list[Path] = field(default_factory=list, repr=False)
    checked_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def report(self):
        plist = Path(sys.executable).parent.parent / "Info.plist"
        build = None
        if getattr(sys, "frozen", False) and plist.is_file():
            build = plistlib.loads(plist.read_bytes()).get("CFBundleVersion")
        checks = {name: dict(result) for name, result in self.checks.items()}
        for result in checks.values():
            if "reason" in result:
                result["reason"] = _report_reason(result["reason"])
        return dict(version=VERSION, build=build or "source checkout", event=self.event_name, folder=str(self.deployment_folder), profile=self.profile,
                    checked_at=self.checked_at, box_folder_id=self.box_folder_id, files=self.file_count,
                    bytes=self.local_bytes, checks=checks,
                    reasons=[_report_reason(r) for r in self.reasons], warnings=[_report_reason(w) for w in self.warnings],
                    clearable=self.clearable, force=self.force)

    def summary_lines(self):
        lines = []
        for name, result in self.checks.items():
            lines.append(f"{name}: {result['status']}")
            for deployment, counts in result.get("deployments", {}).items():
                numbers = ", ".join(f"{key} {value}" for key, value in counts.items() if isinstance(value, int))
                lines.append(f"  {deployment}: {numbers}")
        return lines

    @property
    def clearable(self) -> bool:
        return not self.reasons and bool(self.event_signature and (self.force or self.box_folder_id))


@dataclass(frozen=True)
class StagingCleanupResult:
    deployment_folder: Path
    file_count: int
    bytes_cleared: int
    report_path: Path


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


def _identity(st):
    return st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns


def _tree_snapshot(folder):
    """Record directory identities as well as files, and propagate traversal errors."""
    def fail(exc):
        raise exc
    tree = {".": _identity(folder.stat(follow_symlinks=False))}
    for base, directories, files in os.walk(folder, onerror=fail, followlinks=False):
        for name in [*directories, *files]:
            path = Path(base) / name
            info = path.lstat()
            rel = str(path.relative_to(folder))
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ValueError(f"Staging contains a symlink or unsupported file: {rel}")
            if info.st_dev != tree["."][0]:
                raise ValueError(f"Staging contains a separate mounted volume: {rel}")
            tree[rel] = _identity(info)
    return tree


def _signature(tree):
    digest = hashlib.sha256()
    for name in sorted(tree):
        digest.update(os.fsencode(name))
        digest.update(repr(tree[name]).encode("ascii"))
        digest.update(bytes(1))
    return digest.hexdigest()


def _event_signature(folder):
    try:
        return _signature(_tree_snapshot(folder)), []
    except (OSError, ValueError) as exc:
        return "", [str(exc)]


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
    storage: BoxStorage | None,
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


def _soundhub_submission_updates_only(local_bytes: bytes, box_bytes: bytes) -> bool:
    """Accept only forward submission bookkeeping changes on identical CSV rows."""
    import io
    from datetime import datetime

    fields = {"is_submitted_to_soundhub", "soundhub_submitter", "soundhub_submission_datetime"}
    try:
        local = csv.DictReader(io.StringIO(local_bytes.decode("utf-8-sig")))
        remote = csv.DictReader(io.StringIO(box_bytes.decode("utf-8-sig")))
        headers = local.fieldnames
        if (not headers or headers != remote.fieldnames
                or len(headers) != len(set(headers))
                or not (fields | {"filename", "deployment_id"}) <= set(headers)):
            return False
        def indexed(reader):
            rows = {}
            for row in reader:
                key = (row["deployment_id"], row["filename"])
                if not all(key) or key in rows or None in row or any(v is None for v in row.values()):
                    raise ValueError("invalid or duplicate CSV row")
                rows[key] = row
            return rows
        before, after = indexed(local), indexed(remote)
        if not before or before.keys() != after.keys():
            return False
        for key, old in before.items():
            new = after[key]
            if old == new:
                continue
            if any(old[f] != new[f] for f in set(headers) - fields):
                return False
            if (old["is_submitted_to_soundhub"].strip().lower() not in {"", "false", "0"}
                    or old["soundhub_submitter"].strip()
                    or old["soundhub_submission_datetime"].strip()
                    or new["is_submitted_to_soundhub"].strip().lower() not in {"true", "1"}
                    or not new["soundhub_submitter"].strip()):
                return False
            stamp = datetime.fromisoformat(new["soundhub_submission_datetime"].replace("Z", "+00:00"))
            if stamp.utcoffset() is None:
                return False
        return True
    except (UnicodeError, ValueError, KeyError, TypeError, csv.Error):
        return False


def inspect_deployment_for_cleanup(
    deployment_folder: Path,
    staging_root: Path,
    storage: BoxStorage,
    field_data_folder_id: str,
    *,
    force: bool = False,
    profile: str = PROFILE,
    config_path: Path = CONFIG_JSON,
    progress=lambda stage: None,
) -> StagingCleanupPlan:
    """Verify an event, or explicitly bypass data verification with force."""
    root = Path(staging_root).expanduser().resolve()
    deployment = Path(deployment_folder).expanduser()
    plan = StagingCleanupPlan(root, deployment, deployment.name, force=force, storage=storage,
                              profile=profile, config_path=config_path, field_data_folder_id=field_data_folder_id)
    try:
        _inspect_event(plan, Path(staging_root).expanduser(), progress)
    except InterruptedError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError, csv.Error) as exc:
        plan.reasons.append(f"Cannot read the event evidence: {exc}")
    if plan.reasons and not plan.event_signature:
        plan.checks["Local staging"]["status"] = "blocked"
    return plan


def _inspect_event(plan, staging_root, progress):
    root, deployment = plan.staging_root, plan.deployment_folder
    profile, config_path = plan.profile, plan.config_path
    storage, field_data_folder_id, force = plan.storage, plan.field_data_folder_id, plan.force
    progress("Checking local staging")

    if staging_root.is_symlink():
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

    config_path = Path(config_path)
    if config_path.exists():
        try:
            config_data = _read_json(config_path)
            if not isinstance(config_data.get("soundhub", {}), dict):
                raise ValueError("invalid SoundHub settings")
        except (ValueError, OSError) as exc:
            plan.reasons.append(f"Cannot read the profile settings: {exc}")
            return plan
    settings = load_soundhub_config(config_path)
    for protected in (Path(settings["staging_root"]).expanduser().resolve(),
                      Path(soundhub_upload.SOUNDHUB_STAGING_DEFAULT).resolve(),
                      receipt_location.receipt_root("uc").resolve(), receipt_location.receipt_root("csu").resolve(),
                      (receipt_location.RECEIPT_BASE / "transfers").resolve(),
                      receipt_location.LEGACY_DRIVE_RECEIPTS.resolve(), Path(config_path).parent.resolve()):
        if deployment.is_relative_to(protected) or protected.is_relative_to(deployment):
            plan.reasons.append("The event overlaps a configuration, receipt, or SoundHub staging folder.")
            return plan

    if not deployment.is_dir():
        plan.reasons.append("deployment target is not a directory")
        return plan
    if (deployment / ".cassn_cleanup.lock").exists():
        plan.reasons.append("An earlier removal is incomplete. Inspect the event and cleanup report.")
        return plan
    try:
        require_wi_complete(deployment)
        require_no_ingest_holds(deployment)
        require_publication_complete(deployment)
        require_uploads_complete(deployment)
    except ValueError as exc:
        plan.reasons.append(str(exc))
        return plan
    if force:
        plan.event_signature, errors = _event_signature(deployment)
        plan.reasons.extend(errors)
        for path in deployment.rglob("*"):
            if not path.is_symlink() and path.is_file():
                plan.file_count += 1
                plan.local_bytes += path.stat().st_size
        return plan

    session_path = deployment / "session.json"
    try:
        session = _read_json(session_path)
    except (OSError, ValueError) as exc:
        plan.reasons.append(f"invalid session.json: {exc}")
        return plan
    if session.get("schema_version") != SESSION_SCHEMA_VERSION:
        plan.reasons.append("session.json has an unsupported schema version")

    marker = session.get("box_project")
    if marker is not None and marker != {"profile": profile, "data_folder": field_data_folder_id}:
        plan.reasons.append("The session belongs to a different Box project.")
    if marker is None and profile == "csu":
        plan.reasons.append("This unmarked session cannot establish the CSU Box project.")
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
        if "evidence_version" in verification and verification["evidence_version"] != 1:
            plan.reasons.append("unsupported Box verification evidence version")
        if verification.get("evidence_version") == 1:
            from types import SimpleNamespace
            from cassn.core.verification_evidence import readiness
            # Cleanup receives the selected data root, not a GUI profile. Read
            # the recorded profile only to reconstruct the non-secret binding.
            destination = verification.get("destination", {})
            if not isinstance(destination, dict):
                raise ValueError("invalid verification destination")
            config = SimpleNamespace(field_data_folder_id=str(field_data_folder_id),
                                     profile=profile)
            _, current = readiness(deployment, session.get("file_inventory"), metadata, config)
            if not current:
                plan.reasons.append("Box verification evidence is stale or incomplete")
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
    if len(inventory_by_path) != len(inventory):
        plan.reasons.append("Inventory contains missing or repeated paths.")
    try:
        attempt_files = _publication_files(deployment)
    except (ValueError, OSError, TypeError, AttributeError) as exc:
        plan.reasons.append(f"Cannot identify the local publication records: {exc}")
        return plan
    plan.local_bytes = 0
    for path in sorted(deployment.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(deployment).as_posix()
        plan.local_bytes += path.stat().st_size
        if not BoxStorage.should_upload_file(path):
            try:
                owned = _owned_local_file(path, deployment, relative, attempt_files, inventory)
            except (OSError, ValueError, TypeError):
                owned = False
            if not owned:
                plan.reasons.append(f"Move {relative} outside the event folder, then run Check again.")
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

    plan.file_count = len(expected_raw & local_raw)
    plan.event_signature, signature_errors = _event_signature(deployment)
    plan.reasons.extend(signature_errors)

    # Do not spend Box API calls on an event that already fails local safety
    # checks.  Fix the local evidence first, then rerun the dry run.
    if plan.reasons:
        plan.checks["Local staging"]["status"] = "blocked"
        return plan

    plan.checks["Local staging"] = {"status": "passed", "originals": plan.file_count}
    progress("Checking Wildlife Insights requests")
    birds = None
    try:
        cameras, birds = platform_inventory(deployment, session)
        coverage = camera_clearance(deployment, cameras)
        plan.checks["Wildlife Insights"] = {"status": "passed" if coverage else "not applicable",
                                            "deployments": coverage}
    except (ValueError, KeyError, TypeError, OSError, csv.Error) as exc:
        plan.reasons.append(str(exc))
        plan.checks["Wildlife Insights"] = {"status": "blocked", "reason": str(exc)}
    progress("Checking Box files and metadata")
    before_box = len(plan.reasons)
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

    if storage is None:
        plan.reasons.append("Connect to Box and run Check again.")
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
        plan.reasons.append(_report_reason(str(exc)))
        return plan

    publication_state = deployment / STATE_PATH
    batch_id = ""
    if publication_state.exists():
        state = read_object(publication_state.read_bytes())
        batch_id = str(state.get("batch_attempt_id") or "")
        if not re.fullmatch(r"[0-9]{8}T[0-9]{6}_[0-9a-f]{32}", batch_id):
            plan.reasons.append("publication record has no valid batch archive identity")
            return plan
    def remote_path(relative):
        return batch_remote_path(Path(relative), batch_id).as_posix() if batch_id else relative

    expected_on_box = (local_uploadable | expected_raw) - _LOCAL_ONLY_AFTER_UPLOAD
    missing_box = sorted(remote_path(p) for p in expected_on_box if remote_path(p) not in box_hashes)
    if missing_box:
        plan.reasons.append(
            f"{len(missing_box)} required file(s) are missing from Box: "
            f"{_path_examples(missing_box)}"
        )

    archive_paths = set()
    if batch_id:
        try:
            archive_paths, archive_issues = recorded_archive_paths(storage, plan.box_folder_id, box_hashes)
            if archive_issues:
                plan.reasons.append(f"archive metadata has {len(archive_issues)} unverified row(s)")
        except Exception as exc:
            plan.reasons.append(f"cannot verify accumulated metadata: {exc}")
    unexpected_box_raw = sorted(
        path
        for path in (set(box_hashes) - expected_raw - archive_paths)
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

    # The QC report accumulates after upload. The application owns this exact file.
    # User documents and ordinary generated files must match current Box bytes.
    compare = expected_on_box - expected_raw - {"qc/qc_report.json"}
    for relative in sorted(compare):
        box_hash = box_hashes.get(remote_path(relative))
        if box_hash is None:
            continue
        if not box_hash:
            plan.reasons.append(f"Box SHA-1 is unavailable: {relative}")
            continue
        local_hash = compute_file_sha1(deployment.joinpath(*relative.split("/")))
        if box_hash != local_hash:
            if relative == "qc/deployment_summary.txt":
                summary = (deployment / relative).read_text(encoding="utf-8")
                stamp = re.search(r"^Generated:        (\d{4}-\d\d-\d\d \d\d:\d\d)$", summary, re.MULTILINE)
                if stamp and summary == session_summary_text(deployment, metadata, inventory, devices, stamp[1]):
                    continue
            if batch_id and relative in (*CSV_NAMES, "deployment_event_record.json"):
                try:
                    file_id = storage.folder_file_map(plan.box_folder_id)[relative]
                    store = BoxFileStore(storage.client)
                    ref = store.snapshot(file_id)
                    box_bytes = store.read(ref)
                    local_bytes = deployment.joinpath(relative).read_bytes()
                    if relative in CSV_NAMES:
                        remote_fields, remote_rows = read_csv(box_bytes)
                        if read_csv(merge_csv(box_bytes, local_bytes)) != (remote_fields, remote_rows):
                            raise ValueError("Box metadata does not cover this batch")
                        rows_by_key = {(r["deployment_id"], r["filename"]): r for r in remote_rows}
                        for row in read_csv(local_bytes)[1]:
                            if not _truthy(rows_by_key[(row["deployment_id"], row["filename"])].get("is_uploaded_to_box")):
                                raise ValueError("Box row is not stamped uploaded")
                    else:
                        record = read_object(box_bytes)
                        if record.get("file_count") != len(archive_paths):
                            raise ValueError("event count does not match verified archive metadata")
                        if read_object(merge_event(box_bytes, local_bytes, file_count=record["file_count"],
                                                   deployment_lookup=deployment / "qc/lookup_snapshot/deployments.csv")) != record:
                            raise ValueError("Box event does not cover this batch")
                    store.assert_unchanged(ref)
                    continue
                except Exception as exc:
                    plan.reasons.append(f"cannot verify accumulated {relative}: {exc}")
                continue
            if relative == "audio_file_metadata.csv":
                try:
                    file_id = storage.folder_file_map(plan.box_folder_id)[relative]
                    from cassn.soundhub.box_metadata import BoxMetadataStore
                    store = BoxMetadataStore(storage.client, field_data_folder_id)
                    box_bytes = store.read(store.snapshot(file_id))
                    if _soundhub_submission_updates_only(
                        deployment.joinpath(relative).read_bytes(), box_bytes
                    ):
                        continue
                except Exception as exc:
                    plan.reasons.append(f"could not compare Box audio metadata: {exc}")
            plan.reasons.append(f"local and Box metadata differ: {relative}")

    plan.checks["Box"] = {"status": "passed" if before_box == len(plan.reasons) else "blocked",
                          "folder_id": plan.box_folder_id, "originals": len(stored_hashes)}
    progress("Checking SoundHub recordings")
    if birds is not None:
        details = plan.checks["SoundHub"]
        if not birds or profile == "csu":
            details.update(status="not applicable", reason="No eligible UC bird recordings.")
        else:
            def read_box_audio():
                ids = [i.id for i in storage.iter_folder_items(plan.box_folder_id)
                       if i.type == "file" and i.name == "audio_file_metadata.csv"]
                if len(ids) != 1:
                    raise ValueError("Cannot identify the Box audio metadata file.")
                store = BoxFileStore(storage.client)
                ref = store.snapshot(ids[0])
                rows = read_csv(store.read(ref))[1]
                store.assert_unchanged(ref)
                return rows
            try:
                plan.staged_flacs, warnings = soundhub_clearance(
                    deployment, settings, profile, birds, read_box_audio, details)
                plan.staged_files_signature = _signature({str(p): _identity(p.lstat()) for p in plan.staged_flacs})
                plan.warnings.extend(warnings)
                details["status"] = "passed"
            except Exception as exc:
                plan.reasons.append(f"SoundHub: {exc}")
                details.update(status="incomplete", reason=str(exc))
    return plan


def _publication_files(folder):
    from cassn.box.metadata_merge import SHARED_NAMES, WI_INPUT_NAMES, WI_STAGED_NAMES
    owned = set()
    for journal in (folder / "qc/box_publications").glob("*/attempt.json"):
        attempt = journal.parent.name
        if not re.fullmatch(r"[0-9]{8}T[0-9]{6}_[0-9a-f]{32}", attempt):
            continue
        state = _read_json(journal)
        if state.get("attempt_id") != attempt:
            continue
        owned.add(journal)
        delivery = journal.parent / "receipt_delivery.json"
        if delivery.is_file():
            record = _read_json(delivery)
            if record.get("batch_attempt_id") == attempt and re.fullmatch(r"[0-9a-f]{64}", record.get("sha256", "")):
                owned.add(delivery)
        for name, record in state.get("files", {}).items():
            if (name not in (*SHARED_NAMES, *WI_INPUT_NAMES.values(), *WI_STAGED_NAMES.values(),
                             "qc/box_upload_manifest.json", "qc/box_upload_verification.json", "qc/deployment_summary.txt")
                    and not (name.startswith("qc/lookup_snapshot/") and len(Path(name).parts) == 3
                             and (folder / name).is_file())):
                continue
            base = journal.parent / name.replace("/", "__")
            source = Path(str(base) + ".source")
            if source.is_file() and compute_file_sha1(source) == (record.get("source") or {}).get("sha1"):
                owned.add(source)
            local = Path(str(base) + ".local")
            if name in SHARED_NAMES and local.is_file():
                if name in CSV_NAMES:
                    read_csv(local.read_bytes())
                else:
                    read_object(local.read_bytes())
                owned.add(local)
            candidate = Path(str(base) + ".candidate")
            if candidate.is_file() and compute_file_sha1(candidate) == (record.get("saved") or {}).get("sha1"):
                owned.add(candidate)
            for index, update in enumerate(record.get("wi_updates", []), 1):
                path = Path(str(base) + f".wi-{index}.source")
                if path.is_file() and compute_file_sha1(path) == update.get("current", {}).get("sha1"):
                    owned.add(path)
    return owned


def _owned_local_file(path, folder, relative, publication_files, inventory):
    if relative in {"session.json", STATE_FILE, STATE_PATH} or path in publication_files:
        return True
    parts = Path(relative).parts
    if len(parts) == 3 and parts[0] == "raw_data" and parts[2] == f"{parts[1]}_manifest.json":
        record = _read_json(path)
        generated = record.get("generated")
        if not isinstance(generated, str):
            return False
        datetime.fromisoformat(generated)
        rows = [row for row in inventory if row.get("device_label") == parts[1]]
        return bool(rows) and record == legacy_device_manifest(parts[1], rows, generated)
    if path.name == ".DS_Store":
        with path.open("rb") as stream:
            return stream.read(8) == bytes.fromhex("0000000142756431")
    if path.name.startswith("._") and path.with_name(path.name[2:] or ".missing").is_file():
        with path.open("rb") as stream:
            return stream.read(8) == bytes.fromhex("0005160700020000")
    if relative in {"qc/box_upload_sessions/metrics.json", "qc/box_upload_sessions/diagnostics/provenance.json"}:
        record = _read_json(path)
        return isinstance(record.get("stage_seconds"), dict) and "acknowledged_MB_per_second" in record
    if path.parent == folder / "qc/box_upload_sessions" and re.fullmatch(r"[0-9a-f]{64}", path.stem):
        if path.suffix == ".lock":
            return path.stat().st_size == 0
        if path.suffix == ".json":
            data = _read_json(path)
            return data.get("version") == 1 and data.get("state") == "complete" and bool(data.get("file_id"))
    return False


def clear_verified_deployment(plan: StagingCleanupPlan, *, event_lease=None, progress=lambda stage: None) -> StagingCleanupResult:
    """Repeat all checks under exclusive ownership and persist the outcome outside the event."""
    if not plan.clearable:
        raise StagingCleanupError("deployment did not pass cleanup preflight")
    folder = plan.deployment_folder
    keys = deployment_keys(folder, plan.event_name, plan.field_data_folder_id)
    if event_lease is not None and not event_lease.owns(keys):
        raise StagingCleanupError("This window no longer owns the selected event.")
    try:
        ownership = nullcontext(event_lease) if event_lease else ResourceLease(LOCK_DIR, keys)
        directory = event_lease.directory if event_lease else LOCK_DIR
        with ownership, staging_write_lock(folder), ResourceLease(directory, ["gui-settings", "cloud-operations",
                "soundhub-batch:" + str(Path(load_soundhub_config(plan.config_path)["staging_root"]).expanduser().resolve())]):
            if folder.is_symlink() or folder.resolve().parent != plan.staging_root or plan.staging_root.is_symlink():
                raise StagingCleanupError("Deployment path changed after cleanup preflight.")
            fresh = inspect_deployment_for_cleanup(folder, plan.staging_root, plan.storage,
                        plan.field_data_folder_id, profile=plan.profile, config_path=plan.config_path,
                        force=plan.force, progress=progress)
            if (fresh.event_signature != plan.event_signature or fresh.box_folder_id != plan.box_folder_id
                    or fresh.staged_files_signature != plan.staged_files_signature or fresh.checks != plan.checks):
                raise StagingCleanupError("Deployment changed after cleanup preflight. " + " ".join(fresh.reasons))
            if not fresh.clearable:
                raise StagingCleanupError(" ".join(fresh.reasons))
            progress("Clearing the verified event")
            return _remove_unchanged_deployment(fresh, folder)
    except (ResourceBusy, OSError, ValueError) as exc:
        raise StagingCleanupError(f"Cannot clear {folder.name}: {exc}") from exc


def save_cleanup_report(plan, outcome="checked"):
    report = Path(plan.config_path).parent / "cleanup-reports" / (uuid4().hex + ".json")
    if report.resolve().is_relative_to(plan.deployment_folder.resolve()):
        raise StagingCleanupError("The report folder overlaps the selected event.")
    atomic_json(report, dict(plan.report(), outcome=outcome))
    return report


def _remove_unchanged_deployment(plan, folder):
    # Capture an exact removal list. Files added later must survive and prevent rmdir.
    tree = _tree_snapshot(folder)
    if _signature(tree) != plan.event_signature:
        raise StagingCleanupError("Deployment changed after cleanup preflight.")
    report = save_cleanup_report(plan, "removal started")
    evidence = plan.report()
    try:
        _remove_snapshot(folder, tree)
        if folder.exists() or (plan.staged_flacs and plan.staged_files_signature != _signature(
                {str(path): _identity(path.lstat()) for path in plan.staged_flacs})):
            raise StagingCleanupError("Could not confirm the event and independent SoundHub files after removal.")
        evidence["outcome"] = "cleared"
    except Exception as exc:
        evidence.update(outcome="removal incomplete", folder_exists=folder.exists(), error=_report_reason(str(exc)))
        raise StagingCleanupError(f"Inspect {folder} and {report}: {exc}") from exc
    finally:
        atomic_json(report, evidence)
    return StagingCleanupResult(folder, plan.file_count, plan.local_bytes, report)

def _remove_snapshot(folder, tree):
    """Unlink only checked entries, relative to pinned directories on the checked volume."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_fd = os.open(folder.parent, directory_flags)
    try:
        event_fd = os.open(folder.name, directory_flags, dir_fd=parent_fd)
        try:
            if _identity(os.fstat(event_fd)) != tree["."]:
                raise StagingCleanupError("Event directory changed.")
            marker = ".cassn_cleanup.lock"
            with os.fdopen(os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                                   dir_fd=event_fd), "w") as stream:
                stream.write(f"pid={os.getpid()}\n")
            children = {}
            for name, identity in tree.items():
                if name != ".":
                    path = Path(name)
                    children.setdefault(str(path.parent), []).append((path, identity))

            def remove_children(fd, base):
                for path, identity in children.get(base, []):
                    if str(path) == "session.json":
                        continue
                    st = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
                    if _identity(st) != identity:
                        raise StagingCleanupError(f"File changed during cleanup: {path}")
                    if stat.S_ISDIR(st.st_mode):
                        child_fd = os.open(path.name, directory_flags, dir_fd=fd)
                        try:
                            if _identity(os.fstat(child_fd)) != identity:
                                raise StagingCleanupError(f"Folder changed during cleanup: {path}")
                            remove_children(child_fd, str(path))
                        finally:
                            os.close(child_fd)
                        os.rmdir(path.name, dir_fd=fd)
                    else:
                        os.unlink(path.name, dir_fd=fd)

            remove_children(event_fd, ".")
            has_session = "session.json" in tree
            expected = {marker, "session.json"} if has_session else {marker}
            if set(os.listdir(event_fd)) != expected:
                raise StagingCleanupError("New files appeared during removal.")
            session_bytes = None
            if has_session:
                with os.fdopen(os.open("session.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=event_fd), "rb") as saved:
                    if _identity(os.fstat(saved.fileno())) != tree["session.json"]:
                        raise StagingCleanupError("Session changed during removal.")
                    session_bytes = saved.read()
            try:
                os.unlink(marker, dir_fd=event_fd)
                if has_session:
                    os.unlink("session.json", dir_fd=event_fd)
                if not os.path.samestat(os.fstat(event_fd), os.stat(folder.name, dir_fd=parent_fd, follow_symlinks=False)):
                    raise StagingCleanupError("Selected event was moved during removal.")
                os.rmdir(folder.name, dir_fd=parent_fd)
            except Exception:
                if marker not in os.listdir(event_fd):
                    os.close(os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=event_fd))
                if session_bytes is not None and "session.json" not in os.listdir(event_fd):
                    with os.fdopen(os.open("session.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                                           dir_fd=event_fd), "wb") as restored:
                        restored.write(session_bytes)
                raise
        finally:
            os.close(event_fd)
    finally:
        os.close(parent_fd)
