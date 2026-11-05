"""Display-only event totals. A filled transfer bar is not a verification receipt."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cassn.box.upload_history import published_file_keys
from cassn.core.inventory import inventory_storage_relpath
from cassn.soundhub.export import event_staging_summary, read_bd_audio_rows
from cassn.soundhub.staging import work_root


@dataclass
class EventProgress:
    total: int
    remaining: set[str]

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
    def soundhub(cls, folder, root):
        summary = event_staging_summary(folder, root)
        rows = read_bd_audio_rows(folder)
        names = {r["filename"] for r in rows}
        if not rows or len(names) != len(rows):
            return None
        remaining = set()
        for row in rows:
            name = row["filename"]
            flac = Path(row["deployment_id"]) / Path(name).with_suffix(".flac").name
            if flac.is_absolute() or ".." in flac.parts:
                return None
            work = work_root(root) / flac
            if flac.as_posix() not in summary.matched_paths and not (work.is_file() and work.stat().st_size > 0):
                remaining.add(name)
        return cls(len(names), remaining)
