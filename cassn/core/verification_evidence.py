"""Current-batch verification evidence; upload history alone is not a pass.

This records the existing saved-hash checks, not a fresh rehash of media.
Only non-secret destination settings and inventory identities enter the digest.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from cassn.box.publication import STATE_PATH, atomic_json, require_publication_complete
from cassn.core.inventory import index_inventory_by_storage_relpath
from cassn.core.quality_control import build_box_verification_record

FILES = {"hash": "file_hash_verification.json", "box": "box_file_list_verification.json"}


def verification_context(folder, inventory, metadata, box_config) -> dict:
    """Bind results to this staging copy, expected set and Box destination.

    SHA-1 is deliberately excluded: legacy checks can fill missing SHA-1 values.
    Inventory SHA-256, size and identity remain part of the binding.
    """
    if not folder or not isinstance(inventory, list) or not inventory:
        raise ValueError("A nonempty file inventory is required")
    indexed = index_inventory_by_storage_relpath(inventory)
    if len(indexed) != len(inventory):
        raise ValueError("Every inventory row must identify a stored file")
    root_id = getattr(box_config, "field_data_folder_id", None)
    if not isinstance(root_id, str) or not root_id.strip():
        raise ValueError("The Box data destination is not configured")
    folder = Path(folder)
    publication = None
    state_path = folder / STATE_PATH
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("status") != "published":
            raise ValueError("Metadata publication is not complete")
        publication = {key: state.get(key) for key in
                       ("attempt_id", "batch_attempt_id", "folder_id", "status")}
    payload = {
        "folder": str(folder.resolve()),
        "destination": {"root_id": root_id, "profile": getattr(box_config, "profile", "legacy")},
        "event": {key: metadata.get(key, "") for key in
                  ("site_name", "deployment_event_id", "deployment_event_start_date",
                   "deployment_event_end_date")},
        "publication": publication,
        "files": [{"path": path, **{key: entry.get(key, "") for key in
                  ("deployment_id", "file_hash_sha256", "file_size_bytes")}}
                  for path, entry in sorted(indexed.items())],
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"schema_version": 1, "digest": digest, "expected_files": len(indexed),
            "profile": getattr(box_config, "profile", "legacy")}


def read_result(folder, kind, context) -> dict | None:
    """Missing, legacy, malformed, pending and stale evidence cannot pass."""
    try:
        result = json.loads((Path(folder) / "qc" / FILES[kind]).read_text(encoding="utf-8"))
        if (isinstance(result, dict) and result.get("evidence_version") == 1
                and result.get("kind") == kind and context and result.get("context") == context
                and result.get("status") == "passed" and result.get("ok") is True
                and result.get("issues") == [] and isinstance(result.get("checked_at"), str)
                and isinstance(result.get("summary"), str)):
            datetime.fromisoformat(result["checked_at"])
            return result
    except (OSError, ValueError, TypeError):
        pass
    return None


def write_result(folder, kind, context, *, ok=False, summary="", issues=(), pending=False):
    """Save a check result atomically and refresh the combined upload record.

    Write an invalid combined record first. A crash/write failure must not leave
    an older combined pass usable by cleanup after a failed or cancelled rerun.
    """
    folder = Path(folder)
    combined_path = folder / "qc" / "box_upload_verification.json"
    atomic_json(combined_path, {"verified": False, "context": context})
    passed = ok is True and not issues and bool(context)
    result = {"evidence_version": 1, "kind": kind, "context": context, "checked_at": datetime.now(timezone.utc).isoformat(),
              "status": "pending" if pending else ("passed" if passed else "failed"),
              "ok": passed and not pending, "summary": summary, "issues": list(issues)}
    atomic_json(folder / "qc" / FILES[kind], result)
    hash_result = read_result(folder, "hash", context)
    box_result = read_result(folder, "box", context)
    if hash_result and box_result:
        require_publication_complete(folder)
        combined = build_box_verification_record(
            hash_ok=True, hash_summary=hash_result["summary"], hash_issues=[],
            box_summary=box_result["summary"], box_issues=[])
        combined.update(context=context, evidence_version=1,
                        destination={"profile": context["profile"]})
        atomic_json(combined_path, combined)
    return passed and not pending


def readiness(folder, inventory, metadata, box_config) -> tuple[bool, bool]:
    try:
        context = verification_context(folder, inventory, metadata, box_config)
        hash_ok = read_result(folder, "hash", context) is not None
        box_ok = read_result(folder, "box", context) is not None
        # The combined artifact must also have been persisted successfully.
        combined = json.loads((Path(folder) / "qc" / "box_upload_verification.json").read_text())
        upload_ok = (isinstance(combined, dict) and combined.get("verified") is True
                     and combined.get("context") == context and combined.get("evidence_version") == 1)
        return hash_ok, hash_ok and box_ok and upload_ok
    except (OSError, ValueError, TypeError, AttributeError):
        return False, False
