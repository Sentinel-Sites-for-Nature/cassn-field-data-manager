"""Fixed local SoundHub history shared by upload, repeat detection, and cleanup."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile

from cassn.profile import PROFILE
from cassn.core.hashing import sha256


RECEIPT_BASE = Path.home() / "cassn/soundhub"
LEGACY_DRIVE_RECEIPTS = Path("/Volumes/Expansion/transfers")


def receipt_root(profile=None):
    # The original, unbranded app submits to the UC SoundHub project.
    profile = profile or PROFILE
    if profile not in {"uc", "csu", "legacy"}:
        raise ValueError("Unknown SoundHub receipt profile")
    return RECEIPT_BASE / ("uc" if profile == "legacy" else profile) / "transfers"


def _records(directory):
    records = {}

    def failed(exc):
        raise exc

    for base, dirs, names in os.walk(directory, onerror=failed):
        dirs[:] = [name for name in dirs if not name.startswith("._") and name != ".DS_Store"]
        for name in [*dirs, *names]:
            path = Path(base) / name
            if name.startswith("._") or name == ".DS_Store":
                continue
            if path.is_symlink():
                raise ValueError(f"Unexpected link in SoundHub transfer archive: {path}")
            if name in dirs:
                records[path.relative_to(directory)] = None
            else:
                if not path.is_file():
                    raise ValueError(f"Unexpected file in SoundHub transfer archive: {path}")
                records[path.relative_to(directory)] = sha256(path)
    return records


def prepare_receipts(staging_root, profile=None):
    """Copy UC's legacy Mac records once. Publish a complete copy atomically."""
    target = receipt_root(profile)
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise OSError(f"Invalid SoundHub transfer folder: {target}")
    if (profile or PROFILE) == "csu":
        return target
    source = target.parents[1] / "transfers"
    other = LEGACY_DRIVE_RECEIPTS
    selected = Path(staging_root).expanduser().parent / "transfers"
    if target.is_dir():
        if source.exists() and (source.is_symlink() or not _records(source).items() <= _records(target).items()):
            raise ValueError(f"Legacy SoundHub records differ from {target}. Reconcile them before uploading.")
        return target
    if not any(path.exists() for path in (source, other, selected)):
        return target
    if not source.is_dir() or source.is_symlink():
        raise ValueError(f"Reconcile the legacy receipts with the Mac archive at {source}")
    if other.is_symlink() or not other.is_dir():
        raise ValueError(f"Connect the legacy staging drive to compare {other} before migrating receipts.")
    original = _records(source)
    if _records(other) != original:
        raise ValueError(f"SoundHub transfer archives differ. Reconcile {source} and {other}.")
    if selected not in (source, other, target) and selected.exists() and _records(selected) != original:
        raise ValueError(f"SoundHub transfer archives differ. Reconcile {source} and {selected}.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as temp:
        candidate = Path(temp) / "transfers"
        shutil.copytree(source, candidate, ignore=shutil.ignore_patterns("._*", ".DS_Store"))
        if _records(candidate) != original or _records(source) != original or _records(other) != original:
            raise ValueError("SoundHub receipts changed during migration.")
        try:
            candidate.rename(target)
        except OSError:
            if not target.is_dir() or not original.items() <= _records(target).items():
                raise
    return target


def receipt_journals(staging_root, profile=None):
    """Read only the fixed directory. An unmigrated archive is never an empty history."""
    target = receipt_root(profile)
    if target.is_symlink():
        raise ValueError(f"Invalid SoundHub receipt folder: {target}")
    if not target.exists():
        if (profile or PROFILE) != "csu" and any(p.exists() for p in
                (target.parents[1] / "transfers", LEGACY_DRIVE_RECEIPTS, Path(staging_root).parent / "transfers")):
            raise ValueError("SoundHub receipt migration is pending. Open FDM with the staging drive connected.")
        return
    for attempt in sorted(target.iterdir()):
        if not attempt.name.startswith(".") and attempt.is_dir():
            journal = attempt / "transfer.json"
            if attempt.is_symlink() or journal.is_symlink() or not journal.is_file():
                raise ValueError(f"Missing or linked SoundHub transfer record: {journal}")
            yield journal
