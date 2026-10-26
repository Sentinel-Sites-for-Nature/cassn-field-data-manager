"""Conservative source evidence and persisted ingest holds (no UID migration)."""
from __future__ import annotations

import json
from pathlib import Path

from cassn.core.quality_control import append_qc_report, qc_path_for


class IngestSafetyError(ValueError):
    """An acquisition needs review; its source and staged files must survive."""


def source_evidence(path) -> dict:
    """Evidence for retrying a retained source, not proof of scientific identity.

    A remounted/replaced source can fail this conservative comparison. Cards
    must be quiescent: stat values cannot establish a live filesystem snapshot.
    """
    path = Path(path)
    stat = path.stat()
    return {"path": str(path.resolve()), "device": stat.st_dev,
            "inode": stat.st_ino, "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns}


def hold_ingest(folder, device, message):
    append_qc_report(folder, "ingest_safety", device, "error", message)
    raise IngestSafetyError(message)


def require_no_ingest_holds(folder, *, device=None, checks=None):
    """Used by upload/cleanup, including after a dismissed dialog or restart."""
    path = qc_path_for(Path(folder), "qc_report.json")
    if not path.exists():
        return
    try:
        report = json.loads(path.read_text())
        entries = report["current_state"]
        if not isinstance(entries, list):
            raise ValueError("invalid current_state")
        selected = {"ingest_safety", "card_ingest"} if checks is None else checks
        holds = [entry for entry in entries
                 if entry.get("check") in selected
                 and (device is None or entry.get("_device") == device)
                 and entry.get("severity") == "error"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise IngestSafetyError(f"Cannot check ingest holds in {path}: {exc}") from exc
    if holds:
        raise IngestSafetyError(
            f"Unresolved ingest failure; review {path} before uploading or cleanup: "
            + "; ".join(str(entry.get("message", "")) for entry in holds)
        )
