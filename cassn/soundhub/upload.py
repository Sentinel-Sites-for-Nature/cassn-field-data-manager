"""
The S3 push to SoundHub's landing zone, and its verification.

Brian asked for ``aws s3 cp --recursive`` because it beats ``sync`` on a
one-way transfer. This uses boto3 instead, which drives the same multipart
transfer manager the CLI does — so the transfer characteristic he cared about
is unchanged — while adding what a GUI needs and a shell-out cannot give:
per-file progress, mid-transfer cancellation, resume across runs, and a
SHA-256 that S3 validates server-side on arrival.

Two constraints shape everything here:

* **The role cannot delete.** ``PutObject`` and ``ListBucket`` only. A key
  written to the wrong place stays there until Brian removes it by hand, so the
  destination key is derived from the staged tree and never rebuilt from
  strings.
* **The landing zone is drained after ingest.** Once SoundHub processes a
  submission the objects leave ``upload/``. An empty prefix therefore proves
  nothing, and :func:`verify_project` is only meaningful immediately after a
  push. Record the verified result in the audio CSV's provenance columns; do
  not re-derive submission state from a later listing.
"""

from __future__ import annotations

import json
from pathlib import Path

from cassn.config import (
    CONFIG_JSON,
    SOUNDHUB_BUCKET,
    SOUNDHUB_PROJECT_SHORT_NAME,
    SOUNDHUB_REGION,
    SOUNDHUB_STAGING_DEFAULT,
    SOUNDHUB_UPLOAD_PREFIX,
)
from cassn.soundhub.staging import project_root

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    BOTO3_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only on installs without boto3
    BOTO3_AVAILABLE = False
    BotoCoreError = ClientError = Exception
    print("Warning: boto3 not available. Install with: pip install boto3")


class SoundHubUploadError(Exception):
    """An upload or verification step failed."""


def boto3_available() -> bool:
    """True when the AWS SDK imported successfully."""
    return BOTO3_AVAILABLE


def load_soundhub_config(config_path: Path = CONFIG_JSON) -> dict:
    """Read the ``soundhub`` block of ``config.json``, falling back to defaults.

    Every key is optional; an install with no ``soundhub`` block gets the
    constants from :mod:`cassn.config`. Credentials are deliberately *not* read
    here — boto3 resolves those from the standard AWS chain (``~/.aws``,
    environment, instance role), so the app never handles the keys itself.
    """
    settings = {
        "staging_root": str(SOUNDHUB_STAGING_DEFAULT),
        "bucket": SOUNDHUB_BUCKET,
        "upload_prefix": SOUNDHUB_UPLOAD_PREFIX,
        "project_short_name": SOUNDHUB_PROJECT_SHORT_NAME,
        "region": SOUNDHUB_REGION,
        "aws_profile": None,
    }
    try:
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8-sig") as f:
                settings.update(json.load(f).get("soundhub", {}) or {})
    except Exception as e:
        print(f"Warning: could not load soundhub settings from config.json: {e}")
    return settings


def _client(settings: dict):
    if not BOTO3_AVAILABLE:
        raise SoundHubUploadError(
            "boto3 is not installed. Install it with: pip install boto3"
        )
    session = boto3.session.Session(
        profile_name=settings.get("aws_profile") or None,
        region_name=settings.get("region") or None,
    )
    return session.client("s3")


def project_prefix(settings: dict) -> str:
    """The S3 key prefix the staged project directory maps onto."""
    return f"{settings['upload_prefix']}/{settings['project_short_name']}"


def staged_objects(
    staging_root,
    settings: dict,
    *,
    deployment_ids: set[str] | None = None,
) -> list[dict]:
    """Every staged file paired with the S3 key it will occupy.

    The key is the file's path relative to the project directory, so the local
    tree *is* the key layout — nothing is assembled from name fragments.
    """
    root = project_root(staging_root)
    if not root.exists():
        raise SoundHubUploadError(f"Nothing staged for SoundHub at {root}")
    prefix = project_prefix(settings)
    objects = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        relative = path.relative_to(root).as_posix()
        if deployment_ids is not None and "/" in relative:
            deployment_id = relative.split("/", 1)[0]
            if deployment_id not in deployment_ids:
                continue
        objects.append(
            {
                "path": path,
                "relative": relative,
                "key": f"{prefix}/{relative}",
                "size": path.stat().st_size,
            }
        )
    return objects


def _existing_sizes(client, settings: dict) -> dict[str, int]:
    """Size of every object already under the project prefix, keyed by S3 key."""
    found: dict[str, int] = {}
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(
        Bucket=settings["bucket"], Prefix=project_prefix(settings) + "/"
    ):
        for item in page.get("Contents", []):
            found[item["Key"]] = item["Size"]
    return found


def upload_project(
    staging_root=None,
    *,
    settings: dict | None = None,
    progress=None,
    is_cancelled=None,
    deployment_ids: set[str] | None = None,
) -> dict:
    """Push the staged project tree to the SoundHub bucket.

    Objects already present at the same size are skipped, which makes an
    interrupted transfer resumable simply by running it again — the behaviour
    ``cp --recursive`` lacks and the reason Brian's fallback advice was to
    switch to ``sync`` after a failure.
    """
    settings = settings or load_soundhub_config()
    staging_root = staging_root or settings["staging_root"]
    client = _client(settings)

    objects = staged_objects(
        staging_root, settings, deployment_ids=deployment_ids
    )
    already = _existing_sizes(client, settings)

    uploaded = skipped = 0
    uploaded_bytes = 0
    total = len(objects)

    for index, obj in enumerate(objects, start=1):
        if is_cancelled is not None and is_cancelled():
            return {
                "cancelled": True,
                "uploaded": uploaded,
                "skipped": skipped,
                "uploaded_bytes": uploaded_bytes,
                "total": total,
            }

        if progress is not None:
            progress(index, total, obj["path"].name)

        # The two project CSVs are cumulative and change as deployments are
        # added, so they are always re-put; media objects are immutable.
        is_manifest = "/" not in obj["relative"]
        if not is_manifest and already.get(obj["key"]) == obj["size"]:
            skipped += 1
            continue

        try:
            client.upload_file(
                str(obj["path"]),
                settings["bucket"],
                obj["key"],
                ExtraArgs={"ChecksumAlgorithm": "SHA256"},
            )
        except (BotoCoreError, ClientError) as e:
            raise SoundHubUploadError(f"Upload failed for {obj['key']}: {e}") from e

        uploaded += 1
        uploaded_bytes += obj["size"]

    return {
        "cancelled": False,
        "uploaded": uploaded,
        "skipped": skipped,
        "uploaded_bytes": uploaded_bytes,
        "total": total,
    }


def verify_project(
    staging_root=None,
    *,
    settings: dict | None = None,
    deployment_ids: set[str] | None = None,
) -> dict:
    """Reconcile the staged tree against what is actually in the bucket.

    Run this immediately after :func:`upload_project`. A later run will report
    everything missing once SoundHub has drained the landing zone, which is
    expected and is not an upload failure.
    """
    settings = settings or load_soundhub_config()
    staging_root = staging_root or settings["staging_root"]
    client = _client(settings)

    objects = staged_objects(
        staging_root, settings, deployment_ids=deployment_ids
    )
    already = _existing_sizes(client, settings)

    missing = [o["key"] for o in objects if o["key"] not in already]
    mismatched = [
        {"key": o["key"], "local": o["size"], "remote": already[o["key"]]}
        for o in objects
        if o["key"] in already and already[o["key"]] != o["size"]
    ]

    return {
        "checked": len(objects),
        "present": len(objects) - len(missing),
        "missing": missing,
        "mismatched": mismatched,
        "ok": not missing and not mismatched,
        "remote_total": len(already),
    }
