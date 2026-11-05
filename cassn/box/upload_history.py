"""Read the last confirmed Box metadata for display only."""
from __future__ import annotations

import hashlib
from pathlib import Path

from cassn.box.metadata_merge import CSV_NAMES, read_csv, read_object
from cassn.box.publication import STATE_PATH


def previously_uploaded(folder: Path, inventory: list) -> int | None:
    """Count current originals in this event's published records. None means unknown.

    Each CSV candidate is the accumulated file list for that media type.
    Its hash must match the saved Box version. This historical count never
    establishes current Box verification or permission to clear staging.
    """
    try:
        current = read_object((folder / STATE_PATH).read_bytes())
        folder_id = current["folder_id"]
        if not folder_id:
            return None
        by_key = {(r.get("deployment_id"), r.get("new_filename")):
                  (r.get("file_hash_sha256"), str(r.get("file_size_bytes"))) for r in inventory}
        if len(by_key) != len(inventory):
            return None
        remaining = set(CSV_NAMES)
        matches = set()
        found = False
        attempts = folder / "qc/box_publications"
        for directory in sorted((p for p in attempts.iterdir() if p.is_dir()), reverse=True):
            state = read_object((directory / "attempt.json").read_bytes())
            if (state.get("folder_id") != folder_id or state.get("attempt_id") != directory.name
                    or state.get("phase") != "provenance" or state.get("status") != "published"):
                continue
            for name in list(remaining):
                candidate = directory / (name + ".candidate")
                if not candidate.exists():
                    continue
                result = state["files"][name]
                saved = result["saved"]
                data = candidate.read_bytes()
                if (result["status"] != "verified" or saved["parent_id"] != folder_id or saved["name"] != name
                        or hashlib.sha1(data).hexdigest() != saved["sha1"]):
                    return None
                for row in read_csv(data)[1]:
                    if row.get("deployment_event_id") != folder.name:
                        return None
                    key = (row["deployment_id"], row["filename"])
                    if (row.get("is_uploaded_to_box", "").lower() == "true"
                            and by_key.get(key) == (row["file_hash_sha256"], row.get("file_size_bytes"))):
                        matches.add(key)
                remaining.remove(name)
                found = True
            if not remaining:
                break
        return len(matches) if found else None
    except (OSError, ValueError, KeyError, TypeError):
        return None
