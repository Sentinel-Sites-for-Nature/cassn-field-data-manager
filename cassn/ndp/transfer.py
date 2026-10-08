"""Read-only planning for Box-to-Pelican source media transfers.

The manifest staging pass has already decided which files belong to each
deployment.  This module resolves those names against Box, proves that the
flattened ``data/`` layout is unambiguous, and reports the exact transfer before
any media is downloaded or any Pelican command is run.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from cassn.lookups import LookupTables, deployment_storage_label
from cassn.ndp.manifest import METADATA_FILENAME
from cassn.ndp.staging import StagingPlan


BOX_LIST_FIELDS = ["id", "type", "name", "size", "sha1"]
STATE_FILENAME = ".ndp-transfer-state.json"
SCRATCH_MARGIN_FRACTION = 0.10
MINIMUM_SCRATCH_MARGIN_BYTES = 1024**3


class NdpTransferError(Exception):
    """A media transfer cannot be planned or continued safely."""


@dataclass(frozen=True)
class TransferFile:
    """One metadata row resolved to one immutable Box file."""

    file_id: str
    filename: str
    source_relative_path: str
    size: int
    sha256: str
    box_sha1: str = ""


@dataclass(frozen=True)
class DeploymentTransfer:
    """The complete media payload for one deployment."""

    deployment_id: str
    box_folder_id: str
    box_folder_name: str
    destination: str
    files: tuple[TransferFile, ...]

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.files)

    @property
    def data_destination(self) -> str:
        return f"{self.destination}/data"


@dataclass
class MediaTransferPlan:
    """An event transfer plan built without downloading or writing media."""

    event_dir: Path
    staging_event_root: Path
    scratch_root: Path
    destination_root: str
    deployment_event_id: str
    deployments: list[DeploymentTransfer] = field(default_factory=list)
    scratch_free_bytes: int = 0
    scratch_required_bytes: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def file_count(self) -> int:
        return sum(len(deployment.files) for deployment in self.deployments)

    @property
    def total_bytes(self) -> int:
        return sum(deployment.total_bytes for deployment in self.deployments)

    @property
    def state_path(self) -> Path:
        return self.staging_event_root / STATE_FILENAME

    @property
    def signature(self) -> str:
        """Digest of every input that determines remote object identity."""
        payload = {
            "destination_root": self.destination_root,
            "deployment_event_id": self.deployment_event_id,
            "deployments": [
                {
                    "deployment_id": deployment.deployment_id,
                    "box_folder_id": deployment.box_folder_id,
                    "files": [
                        {
                            "file_id": item.file_id,
                            "filename": item.filename,
                            "source_relative_path": item.source_relative_path,
                            "size": item.size,
                            "sha256": item.sha256,
                            "box_sha1": item.box_sha1,
                        }
                        for item in deployment.files
                    ],
                }
                for deployment in self.deployments
            ],
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class _BoxFile:
    file_id: str
    relative_path: str
    name: str
    size: int
    sha1: str


def normalize_destination_root(value: str) -> str:
    """Validate and normalize one OSDF destination collection URL."""
    raw = str(value).strip()
    parsed = urlsplit(raw)
    if parsed.scheme != "osdf" or parsed.netloc or parsed.query or parsed.fragment:
        raise NdpTransferError(
            "destination root must be an osdf:/// URL without a host, query, or fragment"
        )
    if not raw.startswith("osdf:///"):
        raise NdpTransferError("destination root must use the exact osdf:/// URL form")
    parsed_path = parsed.path.rstrip("/")
    raw_parts = parsed_path.split("/")
    if not parsed_path.startswith("/") or any(not part for part in raw_parts[1:]):
        raise NdpTransferError(
            "destination root must be one absolute, traversal-free path"
        )
    parts = [unquote(part) for part in raw_parts[1:]]
    if any(
        part in {".", ".."}
        or any(character in part for character in ("/", "\\", "\x00"))
        for part in parts
    ):
        raise NdpTransferError(
            "destination root must be one absolute, traversal-free path"
        )
    if len(parts) < 2 or parts[0] != "ndp" or parts[1] not in {"private", "public"}:
        raise NdpTransferError(
            "destination root must begin osdf:///ndp/private/ or osdf:///ndp/public/"
        )
    path = "/" + "/".join(quote(part, safe="-._~") for part in parts)
    return "osdf://" + path.rstrip("/")


def join_destination(root: str, *components: str) -> str:
    """Append literal object-name components to an OSDF URL safely."""
    normalized = normalize_destination_root(root)
    encoded = []
    for component in components:
        if not _safe_component(component):
            raise NdpTransferError(f"unsafe destination path component: {component!r}")
        encoded.append(quote(component, safe="-._~"))
    return normalized + "/" + "/".join(encoded)


def _safe_component(value: str) -> bool:
    return (
        bool(value)
        and value not in {".", ".."}
        and not any(separator in value for separator in ("/", "\\", "\x00"))
    )


def _nearest_existing_path(path: Path) -> Path:
    current = path.expanduser().resolve(strict=False)
    while not current.exists():
        parent = current.parent
        if parent == current:
            raise NdpTransferError(
                f"cannot find an existing parent for scratch root: {path}"
            )
        current = parent
    return current


def available_scratch_bytes(scratch_root: Path) -> int:
    """Return free bytes on the filesystem that will hold ``scratch_root``."""
    return shutil.disk_usage(_nearest_existing_path(Path(scratch_root))).free


def required_scratch_bytes(
    deployments: list[DeploymentTransfer], *, retain_all: bool = False
) -> int:
    """Required media bytes plus a filesystem/interruption safety margin."""
    largest = (
        sum(deployment.total_bytes for deployment in deployments)
        if retain_all
        else max((deployment.total_bytes for deployment in deployments), default=0)
    )
    margin = max(
        MINIMUM_SCRATCH_MARGIN_BYTES,
        int(largest * SCRATCH_MARGIN_FRACTION),
    )
    return largest + margin if largest else 0


def _metadata_rows(payload: bytes) -> list[dict]:
    try:
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig"), newline=""))
        if reader.fieldnames is None:
            raise ValueError("CSV has no header")
        rows = list(reader)
    except (UnicodeError, csv.Error, ValueError) as exc:
        raise NdpTransferError(
            f"could not read staged {METADATA_FILENAME}: {exc}"
        ) from exc
    if any(None in row for row in rows):
        raise NdpTransferError(f"staged {METADATA_FILENAME} has an over-wide row")
    return rows


def _is_ignored_box_file(relative_path: str) -> bool:
    name = PurePosixPath(relative_path).name
    return (
        name.startswith(".")
        or name.startswith("._")
        or name.startswith("session.json")
        or name.endswith("_manifest.json")
    )


def _box_files(storage, folder_id: str) -> list[_BoxFile]:
    found: list[_BoxFile] = []

    def _walk(current_id: str, prefix: tuple[str, ...]) -> None:
        for item in storage.iter_folder_items(current_id, fields=BOX_LIST_FIELDS):
            name = str(getattr(item, "name", "") or "")
            if not _safe_component(name):
                raise NdpTransferError(f"Box contains an unsafe item name: {name!r}")
            if item.type == "folder":
                _walk(str(item.id), (*prefix, name))
                continue
            if item.type != "file":
                continue
            relative = PurePosixPath(*prefix, name).as_posix()
            if _is_ignored_box_file(relative):
                continue
            try:
                size = int(getattr(item, "size"))
            except (TypeError, ValueError, AttributeError) as exc:
                raise NdpTransferError(
                    f"Box returned no valid size for {relative}"
                ) from exc
            found.append(
                _BoxFile(
                    str(item.id),
                    relative,
                    name,
                    size,
                    str(
                        getattr(item, "sha_1", None)
                        or getattr(item, "sha1", None)
                        or ""
                    ).lower(),
                )
            )

    _walk(folder_id, ())
    return found


def _expected_rows(deployment) -> dict[str, dict]:
    rows = _metadata_rows(deployment.files[METADATA_FILENAME])
    expected: dict[str, dict] = {}
    casefolded: dict[str, str] = {}
    for number, row in enumerate(rows, start=2):
        filename = str(row.get("filename") or "").strip()
        if not _safe_component(filename):
            raise NdpTransferError(
                f"{deployment.deployment_id}: row {number} has unsafe filename {filename!r}"
            )
        if filename in expected:
            raise NdpTransferError(
                f"{deployment.deployment_id}: duplicate filename {filename!r}"
            )
        folded = filename.casefold()
        if folded in casefolded:
            raise NdpTransferError(
                f"{deployment.deployment_id}: filenames {casefolded[folded]!r} and "
                f"{filename!r} collide on a case-insensitive scratch filesystem"
            )
        try:
            size = int(str(row.get("file_size_bytes") or ""))
        except ValueError as exc:
            raise NdpTransferError(
                f"{deployment.deployment_id}: {filename} has an invalid file_size_bytes"
            ) from exc
        digest = str(row.get("file_hash_sha256") or "").strip().lower()
        sha1 = str(row.get("file_hash_sha1") or "").strip().lower()
        if (
            size < 0
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise NdpTransferError(
                f"{deployment.deployment_id}: {filename} has invalid size or SHA-256"
            )
        if sha1 and (len(sha1) != 40 or any(c not in "0123456789abcdef" for c in sha1)):
            raise NdpTransferError(
                f"{deployment.deployment_id}: {filename} has invalid SHA-1"
            )
        expected[filename] = {"size": size, "sha256": digest, "sha1": sha1}
        casefolded[folded] = filename
    return expected


def _resolve_event_folder(storage, field_data_folder_id: str, event_dir: Path) -> str:
    if len(event_dir.parents) < 2:
        raise NdpTransferError(f"event path has no year/reserve parents: {event_dir}")
    year = event_dir.parent.parent.name
    reserve = event_dir.parent.name
    if not year.isdigit() or len(year) != 4:
        raise NdpTransferError(f"cannot derive a four-digit year from {event_dir}")
    current = str(field_data_folder_id)
    for label, name in (
        ("year", year),
        ("reserve", reserve),
        ("event", event_dir.name),
    ):
        folder_id = storage.find_child_folder(current, name, fields=BOX_LIST_FIELDS)
        if not folder_id:
            raise NdpTransferError(f"Box {label} folder not found: {name}")
        current = str(folder_id)
    return current


def plan_media_transfer(
    staging_plan: StagingPlan,
    lookups: LookupTables,
    storage,
    field_data_folder_id: str,
    scratch_root: Path,
    destination_root: str,
    *,
    scratch_free_bytes: int | None = None,
    retain_scratch: bool = False,
) -> MediaTransferPlan:
    """Resolve a validated staging plan to exact Box files and OSDF paths."""
    if not staging_plan.ok:
        raise NdpTransferError("cannot transfer an invalid NDP staging plan")
    destination_root = normalize_destination_root(destination_root)
    event_id = staging_plan.deployment_event_id
    if not _safe_component(event_id):
        raise NdpTransferError(f"unsafe deployment event id: {event_id!r}")
    plan = MediaTransferPlan(
        event_dir=staging_plan.event_dir,
        staging_event_root=staging_plan.event_root,
        scratch_root=Path(scratch_root).expanduser(),
        destination_root=destination_root,
        deployment_event_id=event_id,
    )
    try:
        event_folder_id = _resolve_event_folder(
            storage, field_data_folder_id, staging_plan.event_dir
        )
        raw_data_id = storage.find_child_folder(
            event_folder_id, "raw_data", fields=BOX_LIST_FIELDS
        )
        if not raw_data_id:
            raise NdpTransferError("Box event has no raw_data folder")
    except NdpTransferError as exc:
        plan.errors.append(str(exc))
        return plan

    used_folders: dict[str, str] = {}
    for deployment in staging_plan.deployments:
        try:
            lookup = lookups.deployment_for_id(deployment.deployment_id)
            folder_name = deployment_storage_label(lookup)
            if not folder_name:
                raise NdpTransferError("lookup row has no Box device-folder label")
            folder_id = storage.find_child_folder(
                raw_data_id, folder_name, fields=BOX_LIST_FIELDS
            )
            if not folder_id:
                raise NdpTransferError(
                    f"Box folder raw_data/{folder_name} was not found"
                )
            if str(folder_id) in used_folders:
                raise NdpTransferError(
                    f"Box folder raw_data/{folder_name} is already assigned to "
                    f"{used_folders[str(folder_id)]}"
                )
            used_folders[str(folder_id)] = deployment.deployment_id

            expected = _expected_rows(deployment)
            actual_files = _box_files(storage, str(folder_id))
            by_name: dict[str, list[_BoxFile]] = {}
            for item in actual_files:
                by_name.setdefault(item.name, []).append(item)
            duplicates = sorted(
                name for name, items in by_name.items() if len(items) > 1
            )
            missing = sorted(set(expected) - set(by_name))
            extras = sorted(set(by_name) - set(expected))
            if duplicates:
                raise NdpTransferError(
                    "flattening collision for: " + ", ".join(duplicates[:10])
                )
            if missing:
                raise NdpTransferError(
                    f"{len(missing)} metadata file(s) are missing from Box: "
                    + ", ".join(missing[:10])
                )
            if extras:
                raise NdpTransferError(
                    f"{len(extras)} Box acquisition file(s) are absent from metadata: "
                    + ", ".join(extras[:10])
                )

            size_mismatches = [
                (filename, by_name[filename][0].size, row["size"])
                for filename, row in sorted(expected.items())
                if by_name[filename][0].size != row["size"]
            ]
            if size_mismatches:
                preview = ", ".join(
                    f"{name} (Box {actual}, metadata {recorded})"
                    for name, actual, recorded in size_mismatches[:10]
                )
                raise NdpTransferError(
                    f"{len(size_mismatches)} Box size mismatch(es): {preview}"
                )
            sha1_mismatches = [
                filename
                for filename, row in sorted(expected.items())
                if row["sha1"] and by_name[filename][0].sha1 != row["sha1"]
            ]
            if sha1_mismatches:
                raise NdpTransferError(
                    f"{len(sha1_mismatches)} Box SHA-1 mismatch(es): "
                    + ", ".join(sha1_mismatches[:10])
                )

            resolved: list[TransferFile] = []
            for filename, row in sorted(expected.items()):
                source = by_name[filename][0]
                resolved.append(
                    TransferFile(
                        source.file_id,
                        filename,
                        source.relative_path,
                        source.size,
                        row["sha256"],
                        source.sha1,
                    )
                )
            destination = join_destination(
                destination_root, event_id, deployment.deployment_id
            )
            plan.deployments.append(
                DeploymentTransfer(
                    deployment.deployment_id,
                    str(folder_id),
                    folder_name,
                    destination,
                    tuple(resolved),
                )
            )
        except (NdpTransferError, OSError) as exc:
            plan.errors.append(f"{deployment.deployment_id}: {exc}")

    plan.scratch_required_bytes = required_scratch_bytes(
        plan.deployments, retain_all=retain_scratch
    )
    try:
        plan.scratch_free_bytes = (
            available_scratch_bytes(plan.scratch_root)
            if scratch_free_bytes is None
            else int(scratch_free_bytes)
        )
    except (NdpTransferError, OSError) as exc:
        plan.errors.append(str(exc))
        return plan
    if plan.scratch_free_bytes < plan.scratch_required_bytes:
        plan.errors.append(
            "insufficient scratch space: "
            f"{plan.scratch_free_bytes} available, "
            f"{plan.scratch_required_bytes} required"
        )
    return plan
