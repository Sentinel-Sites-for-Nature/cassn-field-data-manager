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
  events. Parts land at or under the limit, never over (barring the impossible
  case of a single event larger than the limit, which is surfaced as a warning).
* **Move, don't copy.** Images are relocated into ``<device>_1``, ``<device>_2``
  ... subfolders of the device dir. No duplicate storage, and the same-volume
  rename is atomic, so a part folder is either fully populated or not created.
* **Manifest untouched.** The ``<device>_manifest.json`` fixity sidecar stays in
  the device dir. Splitting is meant as a terminal WI-prep step, run after Box
  upload and QC, so the device's fixity record is left exactly as written.
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

WI_UPLOAD_LIMIT = 15_000
DEFAULT_DEVICE_SUFFIXES = ("_ML", "_SA")

# A burst frame is a trailing "_<seq>_<frame>" on the stem: strip the frame index
# to get the event key shared by all frames of one trigger. Requires the two
# trailing number groups so a single-frame name (no frame index) is not
# over-grouped — it falls back to its own event.
_EVENT_KEY_RE = re.compile(r"^(?P<key>.*_\d+)_\d{1,3}$")


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
    return sorted(p.name for p in Path(device_dir).iterdir() if _is_image(p))


def list_part_dirs(device_dir) -> list[str]:
    """Names of existing ``<device>_<n>`` part subfolders, sorted numerically."""
    device_dir = Path(device_dir)
    name = device_dir.name
    parts = [
        p.name
        for p in device_dir.iterdir()
        if p.is_dir() and is_part_dir_name(p.name, name)
    ]
    return sorted(parts, key=lambda n: int(n.rsplit("_", 1)[1]))


def collect_inventory(device_dir) -> dict[str, Path]:
    """Map every device image (loose or already in a part) to its current path.

    A filename found inside a part folder wins over a loose copy of the same
    name, but with atomic moves a file only ever exists in one place.
    """
    device_dir = Path(device_dir)
    loc: dict[str, Path] = {}
    for p in device_dir.iterdir():
        if _is_image(p):
            loc[p.name] = p
    for part_name in list_part_dirs(device_dir):
        for p in (device_dir / part_name).iterdir():
            if _is_image(p):
                loc[p.name] = p
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
        if cur and len(cur) + len(g) > limit:
            parts.append(cur)
            cur = []
        cur.extend(g)
    if cur:
        parts.append(cur)
    return parts


def plan_device(device_dir, limit=WI_UPLOAD_LIMIT, keep_bursts=True) -> DevicePlan:
    """Build a :class:`DevicePlan` from the device's full image inventory."""
    device_dir = Path(device_dir)
    loc = collect_inventory(device_dir)
    filenames = sorted(loc)
    plan = DevicePlan(device_dir=device_dir, filenames=filenames, loc=loc)

    if len(filenames) <= limit:
        if list_part_dirs(device_dir):
            plan.warnings.append(
                "part folders present but total images are within the limit — an "
                "odd state (limit changed?); run --undo to flatten, then re-split."
            )
        return plan

    for i, files in enumerate(plan_parts(filenames, limit, keep_bursts), start=1):
        part = Part(name=f"{device_dir.name}_{i}", files=files)
        if len(files) > limit:
            plan.warnings.append(
                f"{part.name} holds {len(files)} images (> limit {limit}): a single "
                f"trigger event exceeds the limit and cannot be split further."
            )
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

def apply_device_split(plan: DevicePlan, move=True, dry_run=True, log=None) -> dict:
    """Execute a plan: create part subfolders and place each image.

    Only files not already sitting in their planned part are touched, so this is
    idempotent and safe to re-run after an interruption. Moving uses an atomic
    same-volume rename; ``move=False`` copies instead (preserving mtimes),
    leaving the originals loose in the device root.
    """
    log = log or (lambda *_: None)
    result = {"device": str(plan.device_dir), "created": [], "placed": 0, "skipped": False}
    if not plan.needs_split:
        result["skipped"] = True
        return result

    for part in plan.parts:
        part_dir = plan.device_dir / part.name
        todo = [fn for fn in part.files if plan.loc.get(fn) != part_dir / fn]
        if dry_run:
            already = len(part.files) - len(todo)
            note = f" ({already} already placed)" if already else ""
            log(f"    {part.name}/  <- {len(part.files)} images ({len(todo)} to move){note}")
            continue
        if todo and not part_dir.exists():
            part_dir.mkdir()
            result["created"].append(part.name)
        for fn in todo:
            src = plan.loc.get(fn)
            dst = part_dir / fn
            if dst.exists() or src is None or not src.exists():
                continue
            if move:
                shutil.move(str(src), str(dst))
            else:
                shutil.copy2(str(src), str(dst))
            result["placed"] += 1
        log(f"    {part.name}/  <- {len(part.files)} images")
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
