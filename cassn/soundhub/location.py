"""Choose and describe the folder that holds the SoundHub staging batch."""
from __future__ import annotations

import csv
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cassn.config import CONFIG_JSON, SOUNDHUB_PROJECT_SHORT_NAME
from cassn.soundhub.lifecycle import transfer_records_cover
from cassn.soundhub.staging import (
    fragments_root, is_default_staging_root, project_root, work_root,
)
from cassn.soundhub.upload import load_soundhub_config, project_prefix

UNAVAILABLE = "unavailable"
EMPTY = "empty"
OPEN_BATCH = "open_batch"
CLOSED_BATCH = "closed_batch"
NOT_STAGING = "not_staging"

STAGING_FOLDERS = (SOUNDHUB_PROJECT_SHORT_NAME, ".cassn_fragments", ".cassn_work", ".cassn_backups")


def _size(total: int) -> str:
    gigabytes = total / 1e9
    return f"{gigabytes:,.0f} GB" if gigabytes >= 10 else f"{gigabytes:.1f} GB"


@dataclass(frozen=True)
class StagingRootStatus:
    path: Path
    state: str
    recordings: int = 0
    files: int = 0
    bytes: int = 0

    def describe(self) -> str:
        """The status in plain words for the SoundHub panel."""
        if self.state == UNAVAILABLE:
            return "Not available. Connect the drive or choose another location."
        if self.state == EMPTY:
            return "Empty. Staging here starts a new batch."
        if self.state == NOT_STAGING:
            return "Not a SoundHub staging folder."
        if self.state == CLOSED_BATCH:
            return (f"Submitted batch: {self.recordings:,} recordings, {_size(self.bytes)}. "
                    "New deployments cannot be added.")
        if not self.recordings:
            return "Open batch: staging has started, no recordings listed yet."
        return f"Open batch: {self.recordings:,} recordings, {_size(self.bytes)}"


def staging_internal_folder(path) -> str | None:
    """The staging folder name that contains ``path``, when ``path`` is inside one."""
    return next((part for part in Path(path).parts if part in STAGING_FOLDERS), None)


def staging_root_status(staging_root, settings: dict | None = None) -> StagingRootStatus:
    """Classify a staging folder by counting entries; nothing is hashed or written."""
    root = Path(staging_root).expanduser()
    if not root.is_dir():
        missing_default = not root.exists() and is_default_staging_root(root)
        return StagingRootStatus(root, EMPTY if missing_default else UNAVAILABLE)
    if not any(path.is_dir() for path in (project_root(root), fragments_root(root), work_root(root))):
        visible = [name for name in os.listdir(root) if not name.startswith(".")]
        return StagingRootStatus(root, NOT_STAGING if visible else EMPTY)

    project = project_root(root)
    paths: list[str] = []
    if (project / "recording.csv").is_file():
        with (project / "recording.csv").open(encoding="utf-8-sig", newline="") as stream:
            paths = [row.get("path") or "" for row in csv.DictReader(stream)]
    files = total = 0
    if project.is_dir():
        with os.scandir(project) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
                elif entry.is_dir(follow_symlinks=False):
                    with os.scandir(entry.path) as media:
                        for item in media:
                            if (not item.name.startswith(".") and item.name.lower().endswith(".flac")
                                    and item.is_file(follow_symlinks=False)):
                                files += 1
                                total += item.stat(follow_symlinks=False).st_size
    settings = settings or load_soundhub_config()
    destination = f"s3://{settings['bucket']}/{project_prefix(settings)}/"
    closed = transfer_records_cover(root, paths, destination)
    return StagingRootStatus(root, CLOSED_BATCH if closed else OPEN_BATCH, len(paths), files, total)


def save_soundhub_staging_root(staging_root, config_path: Path = CONFIG_JSON) -> Path:
    """Write ``soundhub.staging_root`` to config.json atomically, keeping every other setting."""
    root = Path(os.path.abspath(Path(staging_root).expanduser()))
    config_path = Path(config_path)
    config = {}
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict):
            raise ValueError(f"{config_path} is not a JSON object; it was left unchanged.")
    soundhub = config.get("soundhub") or {}
    if not isinstance(soundhub, dict):
        raise ValueError(f"The soundhub settings in {config_path} are not a JSON object; they were left unchanged.")
    config["soundhub"] = {**soundhub, "staging_root": str(root)}
    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=config_path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            if config_path.exists():
                os.fchmod(stream.fileno(), stat.S_IMODE(config_path.stat().st_mode))
            stream.write(json.dumps(config, indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, config_path)
    finally:
        temporary.unlink(missing_ok=True)
    return root
