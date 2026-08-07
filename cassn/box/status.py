"""Read-only detection of prior Box-upload activity for a deployment."""

from __future__ import annotations

import csv
from pathlib import Path

from cassn.config import QC_SUBFOLDER


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def has_box_upload_history(deployment_folder, *, current_upload_complete=False) -> bool:
    """Return whether automatic folder reorganization must be skipped.

    A manifest means an upload was at least attempted and may have placed files
    on Box. Provenance values or a verification artifact mean it completed.
    Any of those make automatic splitting unsafe; the existing CLI remains the
    explicit retroactive/recovery tool.
    """
    deployment_folder = Path(deployment_folder)
    if current_upload_complete:
        return True

    qc_dir = deployment_folder / QC_SUBFOLDER
    for filename in ("box_upload_manifest.json", "box_upload_verification.json"):
        if (qc_dir / filename).exists() or (deployment_folder / filename).exists():
            return True

    for filename in ("image_file_metadata.csv", "audio_file_metadata.csv"):
        path = deployment_folder / filename
        if not path.is_file():
            continue
        try:
            with path.open(newline="", encoding="utf-8-sig") as handle:
                if any(_truthy(row.get("is_uploaded_to_box")) for row in csv.DictReader(handle)):
                    return True
        except (OSError, UnicodeError, csv.Error):
            continue
    return False
