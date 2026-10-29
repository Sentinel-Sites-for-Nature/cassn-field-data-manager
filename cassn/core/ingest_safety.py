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


def retained_source_matches(saved, current) -> bool:
    """Compare retained file details across mounts, alongside BOTH saved hashes.

    st_dev identifies a mount, not a physical card: macOS can assign a new
    number on reinsertion. Keep path, inode, size and timestamp checks. This
    comparison alone does not establish identity and must never replace hashing.
    Exact evidence equality is still required before/after each individual read.
    """
    keys = {"path", "device", "inode", "size", "mtime_ns"}
    return (isinstance(saved, dict) and keys <= saved.keys()
            and keys <= current.keys()
            and all(saved[key] == current[key] for key in keys - {"device"}))


def prepare_remounted_source_retry(folder, device, source_dir, inventory) -> bool:
    """Allow rechecking ONLY the old mount-number rejection, preserving its hold.

    Metadata preflight permits entering the normal hash-verified retry; it does
    not resolve anything. The caller may resolve this hold only after every
    retained file and staged copy passes verification and the session is saved.
    Unrelated safety findings and malformed reports continue to block retries.
    """
    try:
        require_no_ingest_holds(folder, device=device, checks={"ingest_safety"})
        return False
    except IngestSafetyError:
        report_path = qc_path_for(Path(folder), "qc_report.json")
        try:
            report = json.loads(report_path.read_text())
            holds = [entry for entry in report["current_state"]
                     if entry.get("check") == "ingest_safety"
                     and entry.get("_device") == device
                     and entry.get("severity") == "error"]
            legacy_suffix = (
                ". Source evidence is missing or changed. Existing staged bytes were preserved; "
                "review this acquisition before retrying."
            )
            if not holds or not all(
                entry.get("message", "").startswith("Cannot establish a retained-source retry for ")
                and entry["message"].endswith(legacy_suffix) for entry in holds
            ):
                raise ValueError("Not a remount retry hold")
            rows = [row for row in inventory if row.get("device_label") == device]
            remounted = False
            for row in rows:
                relative = row.get("source_relpath")
                if not relative:
                    raise ValueError("Missing retained source path")
                current = source_evidence(Path(source_dir) / relative)
                saved = row.get("source_evidence")
                if not retained_source_matches(saved, current):
                    raise ValueError("Retained source details changed")
                remounted |= saved["device"] != current["device"]
            if not rows or not remounted:
                raise ValueError("No remount established")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            # Re-raise the original hold with its useful recovery message.
            raise IngestSafetyError(
                f"Unresolved ingest failure; review {report_path} before retrying. "
                "Reconnect the original card; changed files or other safety findings need review."
            )
        return True


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
