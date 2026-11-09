"""Display-only event totals. A filled transfer bar is not a verification receipt."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from cassn.box.upload_history import published_file_keys
from cassn.core.inventory import inventory_storage_relpath
from cassn.soundhub.export import event_staging_summary, read_bd_audio_rows
from cassn.soundhub.staging import work_root


@dataclass
class EventProgress:
    total: int
    remaining: set[str]
    recorders: dict[str, set[str]] = field(default_factory=dict)

    @property
    def done(self):
        return self.total - len(self.remaining)

    @classmethod
    def box(cls, folder, inventory):
        known = published_file_keys(folder, inventory) or set()
        paths = {inventory_storage_relpath(row): (row.get("deployment_id"), row.get("new_filename"))
                 for row in inventory}
        if not inventory or "" in paths or len(paths) != len(inventory):
            return None
        return cls(len(paths), {path for path, key in paths.items() if key not in known})

    @classmethod
    def from_box_paths(cls, inventory, present):
        expected = {inventory_storage_relpath(row) for row in inventory}
        return cls(len(expected), expected - present)

    @classmethod
    def soundhub(cls, folder, root, summary=None):
        if summary is None:
            summary = event_staging_summary(folder, root)
        rows = read_bd_audio_rows(folder)
        if not rows:
            return None
        remaining = set()
        recorders = {}
        for row in rows:
            name = row["filename"]
            flac = Path(row["deployment_id"]) / Path(name).with_suffix(".flac").name
            if flac.is_absolute() or ".." in flac.parts:
                return None
            key = flac.as_posix()
            files = recorders.setdefault(row["deployment_id"], set())
            if key in files:
                return None
            files.add(key)
            work = work_root(root) / flac
            if flac.as_posix() not in summary.matched_paths and not (work.is_file() and work.stat().st_size > 0):
                remaining.add(key)
        return cls(len(rows), remaining, recorders)
