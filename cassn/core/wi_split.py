"""Split oversized camera-image device folders into WI-uploadable parts.

Wildlife Insights accepts at most 15,000 images per upload. A single camera
device folder (``pN_ML`` / ``pN_SA``) routinely holds more, so before those
images can be pushed to WI they have to be divided into sub-batches, each at or
under the limit. This module is the pure planning + execution logic;
``utils/split_for_wi.py`` is the CLI over it, and the GUI can call the same
functions when the feature is wired into the app.

Design choices that keep the operation safe and reversible:

* **Bursts stay together.** A Reconyx-style trigger writes several frames that
  share an event key (``..._00001_1``, ``..._00001_2``, ``..._00001_3``). A part
  boundary never cuts through one, so every WI upload holds whole trigger
  events. Parts land at or under the limit, never over. A malformed single event
  larger than the limit is a blocking error.
* **Move, don't copy.** Images are relocated into ``<device>_1``, ``<device>_2``
  ... subfolders of the device dir. Each same-volume file move is atomic, but
  the complete operation is intentionally resumable rather than transactional.
  Non-image sidecars are left untouched.
* **Reversible.** :func:`undo_device_split` moves every part's images back up and
  removes the emptied subfolders.
* **Idempotent and resumable.** The plan is built over the *full* inventory —
  images still loose in the device root plus any already sitting in
  ``<device>_<n>`` subfolders — and :func:`apply_device_split` only moves files
  that are not yet in their planned spot. So an interrupted split (crash, quit,
  sleep) is finished cleanly by simply re-running, and a completed split is a
  no-op. Part folder names never end in ``_ML`` / ``_SA``, so a re-scan can't
  recurse into prior output.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from cassn.config import IMAGE_EXTENSIONS
from cassn.core.inventory import set_inventory_storage_relpath

WI_UPLOAD_LIMIT = 15_000
DEFAULT_DEVICE_SUFFIXES = ("_ML", "_SA")

# A burst frame is a trailing "_<seq>_<frame>" on the stem: strip the frame index
# to get the event key shared by all frames of one trigger. Requires the two
# trailing number groups so a single-frame name (no frame index) is not
# over-grouped — it falls back to its own event.
_EVENT_KEY_RE = re.compile(r"^(?P<key>.*_\d+)_\d{1,3}$")


class SplitError(RuntimeError):
    """Base class for a WI split that cannot proceed safely."""


class InvalidLimitError(SplitError):
    """Raised when the requested part size is not a positive integer."""


class DuplicateImageError(SplitError):
    """Raised when one filename appears in more than one device location."""


class OversizedBurstError(SplitError):
    """Raised when preserving one trigger burst would violate the part limit."""


class SplitCollisionError(SplitError):
    """Raised when the filesystem changed or a destination would be overwritten."""


class SplitVerificationError(SplitError):
    """Raised when the post-move folder structure does not match its plan."""


def _validate_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise InvalidLimitError(f"Image limit must be a positive integer; got {limit!r}")
    return limit


def normalize_suffixes(suffixes) -> tuple[str, ...]:
    """Normalize device suffixes to a leading-underscore form (``ML`` -> ``_ML``)."""
    out = []
    for s in suffixes:
        s = s.strip()
        if s and not s.startswith("_"):
            s = "_" + s
        if s:
            out.append(s)
    return tuple(out)


def event_key(filename: str) -> str:
    """Return the group key shared by all burst frames of one trigger event.

    Strips the extension and a trailing ``_<frame>`` index when the stem has the
    ``..._<seq>_<frame>`` burst shape. A name without a frame index is its own
    event (returns its bare stem), so single-frame folders are never over-grouped.
    """
    stem = Path(filename).stem
    m = _EVENT_KEY_RE.match(stem)
    return m.group("key") if m else stem


def is_part_dir_name(name: str, device_name: str) -> bool:
    """True if ``name`` is a ``<device_name>_<digits>`` part folder."""
    prefix = device_name + "_"
    return name.startswith(prefix) and name[len(prefix):].isdigit()


def _is_image(p: Path) -> bool:
    return p.is_file() and not p.name.startswith(".") and p.suffix.lower() in IMAGE_EXTENSIONS


def _is_image_entry(entry: os.DirEntry) -> bool:
    """Check a scandir entry without issuing a separate stat when possible."""
    return (
        not entry.name.startswith(".")
        and Path(entry.name).suffix.lower() in IMAGE_EXTENSIONS
        and entry.is_file(follow_symlinks=False)
    )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def find_target_devices(root, suffixes=DEFAULT_DEVICE_SUFFIXES) -> list[Path]:
    """Find every device folder under ``root`` whose name ends in a suffix.

    Device dirs (and their part subfolders) are pruned from the walk so their
    tens of thousands of images are never enumerated — important for speed on a
    network-backed store like Box.
    """
    root = Path(root)
    suffixes = normalize_suffixes(suffixes)
    found: list[Path] = []
    for dirpath, dirnames, _ in os.walk(root):
        keep = []
        for d in dirnames:
            if d.startswith("."):
                continue
            if d.endswith(suffixes):
                found.append(Path(dirpath) / d)
            else:
                keep.append(d)
        dirnames[:] = keep  # don't descend into device dirs or hidden dirs
    return sorted(found)


def list_device_images(device_dir) -> list[str]:
    """Image filenames sitting loose in the device root (non-recursive), sorted."""
    with os.scandir(device_dir) as entries:
        return sorted(entry.name for entry in entries if _is_image_entry(entry))


def list_part_dirs(device_dir) -> list[str]:
    """Names of existing ``<device>_<n>`` part subfolders, sorted numerically."""
    device_dir = Path(device_dir)
    name = device_dir.name
    with os.scandir(device_dir) as entries:
        parts = [
            entry.name
            for entry in entries
            if (
                is_part_dir_name(entry.name, name)
                and entry.is_dir(follow_symlinks=False)
            )
        ]
    return sorted(parts, key=lambda n: int(n.rsplit("_", 1)[1]))


def _scan_inventory(device_dir) -> tuple[dict[str, Path], set[str], dict[str, set[str]]]:
    """Return image locations, loose filenames, and per-part filenames.

    Duplicate basenames are rejected instead of being silently collapsed. WI
    uses basenames as image identities during this operation, so continuing
    with two different paths for one name could hide or overwrite data.
    """
    device_dir = Path(device_dir)
    loc: dict[str, Path] = {}
    loose: set[str] = set()
    part_files: dict[str, set[str]] = {}
    part_dirs: list[Path] = []

    def record(name: str, path: Path, *, part_name: str | None = None) -> None:
        previous = loc.get(name)
        if previous is not None:
            raise DuplicateImageError(
                f"Duplicate image filename {name!r}: {previous} and {path}"
            )
        loc[name] = path
        if part_name is None:
            loose.add(name)
        else:
            part_files[part_name].add(name)

    # scandir streams entries and normally gets their type from the directory
    # listing itself. That avoids Path.iterdir/os.listdir materializing a huge
    # Box folder and then issuing a separate stat for every image.
    with os.scandir(device_dir) as entries:
        for entry in entries:
            if _is_image_entry(entry):
                record(entry.name, Path(entry.path))
            elif (
                is_part_dir_name(entry.name, device_dir.name)
                and entry.is_dir(follow_symlinks=False)
            ):
                part_dir = Path(entry.path)
                part_dirs.append(part_dir)
                part_files[entry.name] = set()

    for part_dir in sorted(part_dirs, key=lambda p: int(p.name.rsplit("_", 1)[1])):
        with os.scandir(part_dir) as entries:
            for entry in entries:
                if _is_image_entry(entry):
                    record(entry.name, Path(entry.path), part_name=part_dir.name)
    return loc, loose, part_files


def collect_inventory(device_dir) -> dict[str, Path]:
    """Map every uniquely named device image to its current path."""
    loc, _loose, _part_files = _scan_inventory(device_dir)
    return loc


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

@dataclass
class Part:
    name: str
    files: list[str]


@dataclass
class DevicePlan:
    device_dir: Path
    filenames: list[str]                 # full inventory (loose + in parts), sorted
    limit: int = WI_UPLOAD_LIMIT
    loc: dict[str, Path] = field(default_factory=dict)
    parts: list[Part] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def device_name(self) -> str:
        return self.device_dir.name

    @property
    def image_count(self) -> int:
        return len(self.filenames)

    @property
    def needs_split(self) -> bool:
        return len(self.parts) > 1

    def pending_moves(self) -> int:
        """How many images are not yet sitting in their planned part folder."""
        n = 0
        for part in self.parts:
            part_dir = self.device_dir / part.name
            for fn in part.files:
                if self.loc.get(fn) != part_dir / fn:
                    n += 1
        return n

    @property
    def fully_split(self) -> bool:
        return self.needs_split and self.pending_moves() == 0


def plan_parts(images, limit=WI_UPLOAD_LIMIT, keep_bursts=True) -> list[list[str]]:
    """Divide an ordered filename list into parts of at most ``limit`` each.

    When ``keep_bursts`` is set, consecutive files sharing an event key are kept
    in the same part. Returns a single part (the whole list) when no split is
    needed.
    """
    limit = _validate_limit(limit)
    images = list(images)
    if len(images) <= limit:
        return [images]

    if keep_bursts:
        groups: list[list[str]] = []
        cur_key = None
        for name in images:
            k = event_key(name)
            if not groups or k != cur_key:
                groups.append([name])
                cur_key = k
            else:
                groups[-1].append(name)
    else:
        groups = [[name] for name in images]

    parts: list[list[str]] = []
    cur: list[str] = []
    for g in groups:
        if len(g) > limit:
            raise OversizedBurstError(
                f"Trigger event {event_key(g[0])!r} contains {len(g)} images, "
                f"exceeding the {limit}-image limit"
            )
        if cur and len(cur) + len(g) > limit:
            parts.append(cur)
            cur = []
        cur.extend(g)
    if cur:
        parts.append(cur)
    return parts


def plan_device(device_dir, limit=WI_UPLOAD_LIMIT, keep_bursts=True) -> DevicePlan:
    """Build a :class:`DevicePlan` from the device's full image inventory."""
    limit = _validate_limit(limit)
    device_dir = Path(device_dir)
    loc = collect_inventory(device_dir)
    filenames = sorted(loc)
    plan = DevicePlan(device_dir=device_dir, filenames=filenames, limit=limit, loc=loc)

    if len(filenames) <= limit:
        if any(path.parent != device_dir for path in loc.values()):
            plan.warnings.append(
                "part folders present but total images are within the limit — an "
                "odd state (limit changed?); run --undo to flatten, then re-split."
            )
        return plan

    for i, files in enumerate(plan_parts(filenames, limit, keep_bursts), start=1):
        part = Part(name=f"{device_dir.name}_{i}", files=files)
        plan.parts.append(part)
    return plan


def plan_root(root, limit=WI_UPLOAD_LIMIT, suffixes=DEFAULT_DEVICE_SUFFIXES,
              keep_bursts=True) -> list[DevicePlan]:
    """Plan every device folder found under ``root``."""
    return [
        plan_device(d, limit=limit, keep_bursts=keep_bursts)
        for d in find_target_devices(root, suffixes)
    ]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

@dataclass
class SplitVerification:
    """Result of the inexpensive filename/count verification after a split."""

    device_dir: Path
    image_count: int
    part_counts: dict[str, int]
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def verify_device_split(plan: DevicePlan) -> SplitVerification:
    """Verify that ``plan`` is fully realized without reading image contents."""
    loc, loose, part_files = _scan_inventory(plan.device_dir)
    expected_parts = {part.name: set(part.files) for part in plan.parts}
    result = SplitVerification(
        device_dir=plan.device_dir,
        image_count=len(loc),
        part_counts={name: len(files) for name, files in sorted(part_files.items())},
    )

    if loose:
        result.errors.append(f"{len(loose)} image(s) remain loose in the device folder")

    actual_names = set(loc)
    expected_names = set(plan.filenames)
    missing_names = expected_names - actual_names
    unexpected_names = actual_names - expected_names
    if missing_names:
        result.errors.append(f"{len(missing_names)} planned image(s) are missing")
    if unexpected_names:
        result.errors.append(f"{len(unexpected_names)} unexpected image(s) were found")

    missing_parts = set(expected_parts) - set(part_files)
    unexpected_parts = set(part_files) - set(expected_parts)
    if missing_parts:
        result.errors.append(
            "Missing part folder(s): " + ", ".join(sorted(missing_parts))
        )
    if unexpected_parts:
        result.errors.append(
            "Unexpected part folder(s): " + ", ".join(sorted(unexpected_parts))
        )

    for part_name, expected_files in expected_parts.items():
        actual_files = part_files.get(part_name, set())
        if len(actual_files) > plan.limit:
            result.errors.append(
                f"{part_name} contains {len(actual_files)} images, exceeding limit {plan.limit}"
            )
        missing = expected_files - actual_files
        extra = actual_files - expected_files
        if missing or extra:
            result.errors.append(
                f"{part_name} differs from plan: {len(missing)} missing, {len(extra)} unexpected"
            )

    return result


def apply_device_split(
    plan: DevicePlan,
    move=True,
    dry_run=True,
    log=None,
    is_cancelled=None,
    progress=None,
) -> dict:
    """Execute a plan: create part subfolders and place each image.

    Only files not already sitting in their planned part are touched, so this is
    idempotent and safe to re-run after an interruption. ``is_cancelled`` is an
    optional callback checked between individual moves; cancellation returns a
    partial, resumable result. ``progress`` receives ``(placed, pending, path)``
    after each atomic move. Copy mode is rejected because it would violate the
    unique-image and no-loose-images split contract.
    """
    log = log or (lambda *_: None)
    is_cancelled = is_cancelled or (lambda: False)
    progress = progress or (lambda *_: None)
    result = {
        "device": str(plan.device_dir),
        "created": [],
        "placed": 0,
        "skipped": False,
        "cancelled": False,
        "verification": None,
    }
    if not move:
        raise SplitError("Copy mode is not supported; WI splitting must move images")
    if not plan.needs_split:
        result["skipped"] = True
        return result

    pending_total = plan.pending_moves()

    # Preflight the complete plan before changing anything. A stale plan or an
    # existing destination is a hard failure, never a reason to skip silently.
    try:
        current_loc, _loose, current_parts = _scan_inventory(plan.device_dir)
    except DuplicateImageError as exc:
        raise SplitCollisionError(str(exc)) from exc
    expected_names = set(plan.filenames)
    current_names = set(current_loc)
    missing_names = expected_names - current_names
    unexpected_names = current_names - expected_names
    if missing_names or unexpected_names:
        raise SplitCollisionError(
            "Filesystem changed after planning: "
            f"{len(missing_names)} planned image(s) missing, "
            f"{len(unexpected_names)} unexpected image(s) present"
        )
    moved_since_plan = [
        name for name in plan.filenames if current_loc[name] != plan.loc.get(name)
    ]
    if moved_since_plan:
        raise SplitCollisionError(
            f"Filesystem changed after planning: {len(moved_since_plan)} image(s) moved; "
            "build a fresh plan before resuming"
        )
    expected_part_names = {part.name for part in plan.parts}
    unexpected_part_names = set(current_parts) - expected_part_names
    if unexpected_part_names:
        raise SplitCollisionError(
            "Unexpected existing part folder(s): "
            + ", ".join(sorted(unexpected_part_names))
        )

    for part in plan.parts:
        part_dir = plan.device_dir / part.name
        if part_dir.exists() and not part_dir.is_dir():
            raise SplitCollisionError(f"Part destination is not a directory: {part_dir}")
        for fn in part.files:
            src = plan.loc.get(fn)
            dst = part_dir / fn
            if src == dst:
                if not dst.is_file():
                    raise SplitCollisionError(f"Planned image is missing: {dst}")
                continue
            if src is None or not src.is_file():
                raise SplitCollisionError(f"Planned source image is missing: {src or fn}")
            if dst.exists():
                raise SplitCollisionError(
                    f"Refusing to overwrite existing destination {dst} with {src}"
                )

    for part in plan.parts:
        part_dir = plan.device_dir / part.name
        todo = [fn for fn in part.files if plan.loc.get(fn) != part_dir / fn]
        if dry_run:
            already = len(part.files) - len(todo)
            note = f" ({already} already placed)" if already else ""
            log(f"    {part.name}/  <- {len(part.files)} images ({len(todo)} to move){note}")
            continue
        if is_cancelled():
            result["cancelled"] = True
            return result
        if todo and not part_dir.exists():
            part_dir.mkdir()
            result["created"].append(part.name)
        for fn in todo:
            if is_cancelled():
                result["cancelled"] = True
                return result
            src = plan.loc.get(fn)
            dst = part_dir / fn
            # Recheck immediately before the move in case the filesystem changed
            # after preflight. Never overwrite or silently skip a conflict.
            if src is None or not src.is_file():
                raise SplitCollisionError(f"Source disappeared during split: {src or fn}")
            if dst.exists():
                raise SplitCollisionError(f"Destination appeared during split: {dst}")
            shutil.move(str(src), str(dst))
            result["placed"] += 1
            progress(result["placed"], pending_total, dst)
        log(f"    {part.name}/  <- {len(part.files)} images")

    verification = verify_device_split(plan)
    result["verification"] = verification
    if not verification.ok:
        raise SplitVerificationError(
            f"Split verification failed for {plan.device_dir}: "
            + "; ".join(verification.errors)
        )
    return result


def sync_inventory_storage_paths(deployment_folder, plan: DevicePlan, file_inventory) -> int:
    """Make image inventory paths match one device's current physical layout.

    This is deliberately called after successful, cancelled, and failed split
    attempts. A cancelled operation may have completed some atomic moves, so
    persisting those actual locations is what makes the next attempt resumable.
    Returns the number of image inventory entries synchronized.
    """
    deployment_folder = Path(deployment_folder)
    locations = collect_inventory(plan.device_dir)
    entries_by_name: dict[str, dict] = {}
    for entry in file_inventory:
        if entry.get("device_label") != plan.device_name:
            continue
        if entry.get("file_type") != "image":
            continue
        filename = entry.get("new_filename", "")
        if not filename:
            continue
        if filename in entries_by_name:
            raise SplitError(
                f"Duplicate image inventory entry for {plan.device_name}/{filename}"
            )
        entries_by_name[filename] = entry

    physical_names = set(locations)
    inventory_names = set(entries_by_name)
    missing_inventory = physical_names - inventory_names
    missing_files = inventory_names - physical_names
    if missing_inventory or missing_files:
        raise SplitError(
            f"Image inventory does not match {plan.device_name}: "
            f"{len(missing_inventory)} uninventoried file(s), "
            f"{len(missing_files)} inventoried file(s) missing"
        )

    for filename, path in locations.items():
        try:
            relative_path = path.relative_to(deployment_folder).as_posix()
        except ValueError as exc:
            raise SplitError(
                f"Image path is outside deployment folder: {path}"
            ) from exc
        set_inventory_storage_relpath(entries_by_name[filename], relative_path)
    return len(entries_by_name)


def prepare_deployment_for_wi(
    deployment_folder,
    file_inventory,
    *,
    limit=WI_UPLOAD_LIMIT,
    suffixes=DEFAULT_DEVICE_SUFFIXES,
    keep_bursts=True,
    log=None,
    progress=None,
    is_cancelled=None,
) -> dict:
    """Prepare all camera folders for the first Box upload.

    Planning, same-volume moves, structural verification, and inventory-path
    synchronization are one resumable workflow. Cancellation is checked between
    devices and individual moves. It never rolls back completed moves; instead,
    their paths are synchronized into ``file_inventory`` so a later call can
    continue safely.
    """
    deployment_folder = Path(deployment_folder)
    raw_data_dir = deployment_folder / "raw_data"
    log = log or (lambda *_: None)
    progress = progress or (lambda *_: None)
    is_cancelled = is_cancelled or (lambda: False)
    result = {
        "devices_scanned": 0,
        "devices_split": 0,
        "parts": 0,
        "images_moved": 0,
        "total_moves": 0,
        "cancelled": False,
    }
    if not raw_data_dir.is_dir():
        return result

    plans: list[DevicePlan] = []
    progress(0, 0, "Planning Wildlife Insights image folders…")
    for device_dir in find_target_devices(raw_data_dir, suffixes):
        if is_cancelled():
            result["cancelled"] = True
            return result
        plan = plan_device(device_dir, limit=limit, keep_bursts=keep_bursts)
        if plan.warnings:
            raise SplitError(f"{plan.device_name}: {'; '.join(plan.warnings)}")
        plans.append(plan)

    result["devices_scanned"] = len(plans)
    result["devices_split"] = sum(plan.needs_split for plan in plans)
    result["parts"] = sum(len(plan.parts) for plan in plans if plan.needs_split)
    result["total_moves"] = sum(plan.pending_moves() for plan in plans)
    progress(0, result["total_moves"], "WI image-folder plan complete")

    completed = 0
    for plan in plans:
        if is_cancelled():
            result["cancelled"] = True
            break

        if not plan.needs_split:
            sync_inventory_storage_paths(deployment_folder, plan, file_inventory)
            continue

        device_base = completed

        def _on_device_progress(device_done, _device_total, path):
            progress(
                device_base + device_done,
                result["total_moves"],
                Path(path).relative_to(deployment_folder).as_posix(),
            )

        try:
            split_result = apply_device_split(
                plan,
                move=True,
                dry_run=False,
                log=log,
                is_cancelled=is_cancelled,
                progress=_on_device_progress,
            )
        except Exception:
            # Moves completed before an error remain valid; reflect their actual
            # locations in session state before surfacing the blocking failure.
            sync_inventory_storage_paths(deployment_folder, plan, file_inventory)
            raise

        sync_inventory_storage_paths(deployment_folder, plan, file_inventory)
        completed += split_result["placed"]
        result["images_moved"] = completed
        if split_result["cancelled"] or is_cancelled():
            result["cancelled"] = True
            break

    if is_cancelled():
        result["cancelled"] = True
    progress(completed, result["total_moves"], "")
    return result


def undo_device_split(device_dir, dry_run=True, log=None) -> dict:
    """Reverse a split: move each part's images back up, remove emptied part dirs."""
    log = log or (lambda *_: None)
    device_dir = Path(device_dir)
    result = {"device": str(device_dir), "restored": 0, "removed_dirs": [], "collisions": []}
    for part_name in list_part_dirs(device_dir):
        part_dir = device_dir / part_name
        for p in sorted(part_dir.iterdir()):
            if not _is_image(p):
                continue
            dst = device_dir / p.name
            if dst.exists():
                result["collisions"].append(p.name)
                continue
            if not dry_run:
                shutil.move(str(p), str(dst))
            result["restored"] += 1

        if dry_run:
            continue
        # A collision leaves its file in place, so leftover stays non-empty and
        # the part dir is kept rather than removed.
        leftover = [p for p in part_dir.iterdir() if not p.name.startswith(".")]
        if not leftover:
            for hidden in list(part_dir.iterdir()):  # sweep a stray .DS_Store
                hidden.unlink()
            part_dir.rmdir()
            result["removed_dirs"].append(part_name)
    return result
