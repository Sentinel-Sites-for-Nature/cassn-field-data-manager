"""Local request references for the optional Wildlife Insights desktop helper."""

import hashlib
import json
import re
from contextlib import contextmanager
from pathlib import Path

from PySide6.QtCore import QLockFile

from cassn.box.upload_state import atomic_json

STATE_FILE = ".fdm-wi-requests.json"
STAGES = {
    "queued", "uploading", "waiting_retry", "verifying", "cleaning", "held",
    "verified", "transferred_or_waiting", "unknown",
}


def scope_key(event, scopes):
    payload = json.dumps([event, scopes], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def validate_scopes(scopes):
    if not isinstance(scopes, list) or not scopes:
        raise ValueError("Missing Wildlife Insights deployment selection.")
    seen = set()
    for item in scopes:
        if not isinstance(item, dict):
            raise ValueError("Invalid Wildlife Insights deployment.")
        name = item.get("deployment")
        if (not isinstance(name, str) or not name or name in seen
                or type(item.get("images")) is not int or item["images"] < 1
                or not isinstance(item.get("selection_sha256"), str)
                or ("retry_of" in item and not re.fullmatch(r"[a-f0-9]{64}", str(item["retry_of"])))
                or not re.fullmatch(r"[a-f0-9]{64}", item["selection_sha256"])):
            raise ValueError("Invalid or duplicate Wildlife Insights deployment.")
        seen.add(name)


def checked_status(scopes, rows):
    """Require one result per approved manifest and bound original-integrity proof."""
    validate_scopes(scopes)
    if not isinstance(rows, list) or len(rows) != len(scopes):
        raise ValueError("Incomplete Wildlife Insights status.")
    expected = {s["deployment"]: s for s in scopes}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid Wildlife Insights status.")
        scope = expected.pop(row.get("deployment"), None)
        if (not scope or row.get("selection_sha256") != scope["selection_sha256"]
                or row.get("stage") not in STAGES or type(row.get("confirmed")) is not int
                or not 0 <= row["confirmed"] <= scope["images"]):
            raise ValueError("Wildlife Insights status differs from the approved selection.")
        manifest = row.get("manifest_sha256")
        if "request_sha" in row and not re.fullmatch(r"[a-f0-9]{64}", str(row["request_sha"])):
            raise ValueError("Invalid uploader request identity.")
        if scope.get("retry_of") and scope["retry_of"] == row.get("request_sha"):
            raise ValueError("The replacement reused the held request.")
        if "hold_reason" in row and (not isinstance(row["hold_reason"], str) or len(row["hold_reason"]) > 1024):
            raise ValueError("Invalid uploader hold reason.")
        if "retry_of" in row and (row["stage"] != "held" or manifest or row["confirmed"] or
                                   not row.get("request_sha") or row["retry_of"] != row["request_sha"]):
            raise ValueError("Retry reference differs from the held request.")
        if "delivery_pending" in row and (row["delivery_pending"] is not True or row["stage"] != "held"
                                          or manifest or row["confirmed"] or "retry_of" in row):
            raise ValueError("Invalid partial request delivery status.")
        if manifest is not None or row["confirmed"] > 0 or row["stage"] == "verified":
            if not isinstance(manifest, str) or not re.fullmatch(r"[a-f0-9]{64}", manifest):
                raise ValueError("Missing admitted Wildlife Insights manifest.")
        if row["stage"] == "verified":
            proof = row.get("integrity")
            if (not isinstance(proof, dict) or proof.get("kind") != "integrity_verified_image_batch"
                    or proof.get("manifest_sha256") != manifest
                    or type(proof.get("image_count")) is not int or proof["image_count"] != scope["images"]
                    or row["confirmed"] != scope["images"]
                    or not re.fullmatch(r"[a-f0-9]{64}", str(proof.get("receipt_sha256", "")))):
                raise ValueError("Missing matching original-image verification receipt.")
    return rows


def read_requests(folder):
    path = Path(folder) / STATE_FILE
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    try:
        if data["version"] != 1 or not isinstance(data["requests"], dict):
            raise ValueError("Invalid Wildlife Insights request file.")
        requests = data["requests"]
        for key, job in requests.items():
            validate_scopes(job["scopes"])
            if key != scope_key(Path(folder).name, job["scopes"]):
                raise ValueError("Saved Wildlife Insights request does not match this event.")
            if "rows" in job:
                checked_status(job["scopes"], job["rows"])
        return requests
    except (TypeError, KeyError, AttributeError) as exc:
        raise ValueError("Unreadable Wildlife Insights request file.") from exc


@contextmanager
def staging_write_lock(folder):
    """Serialize cleanup and request updates outside the deletable event folder."""
    folder = Path(folder).resolve()
    lock = QLockFile(str(folder.parent / f".{folder.name}.fdm-wi.lock"))
    lock.setStaleLockTime(0)  # Qt can recover a dead process, regardless of lock age.
    if not lock.tryLock(0):
        raise ValueError("Local staging is busy. Retry after the current operation.")
    try:
        yield
    finally:
        lock.unlock()


def save_requests(folder, jobs):
    with staging_write_lock(folder):
        if not folder.is_dir() or (folder / ".cassn_cleanup.lock").exists():
            raise ValueError("Local staging is unavailable or cleanup has started.")
        atomic_json(folder / STATE_FILE, {"version": 1, "requests": jobs})


def replacement_sources(jobs):
    """Exact held inbox files superseded by an explicitly saved replacement intent."""
    return {(s["deployment"], s["images"], s["retry_of"])
            for job in jobs.values() for s in job["scopes"] if "retry_of" in s}


def superseded(scope, row, sources):
    return (row.get("stage") == "held" and not row.get("manifest_sha256") and not row.get("confirmed")
            and (scope["deployment"], scope["images"], row.get("request_sha")) in sources)


def complete(job, sources=()):
    rows = {r["deployment"]: r for r in job.get("rows", [])}
    return all(rows.get(s["deployment"], {}).get("stage") == "verified"
               or superseded(s, rows.get(s["deployment"], {}), sources) for s in job["scopes"])


def all_requests_verified(jobs):
    # Resolve historical holds only through replacements with verified receipts.
    verified_sources = set()
    while True:
        resolved = {key: job for key, job in jobs.items() if complete(job, verified_sources)}
        sources = replacement_sources(resolved)
        if sources == verified_sources:
            return len(resolved) == len(jobs)
        verified_sources = sources


def require_wi_complete(folder):
    try:
        if not all_requests_verified(read_requests(folder)):
            raise ValueError("Retain staged originals until Wildlife Insights verification finishes.")
    except OSError as exc:
        raise ValueError("Cannot check the saved Wildlife Insights requests.") from exc
