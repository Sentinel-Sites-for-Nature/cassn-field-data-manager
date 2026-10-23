"""
The S3 push to SoundHub's landing zone, and its verification.

Uploads use Brian's requested ``aws s3 cp --recursive``. A temporary tree of
file symlinks selects only the planned objects without copying large FLACs.
boto3 handles the read-only listings used for resume and immediate verification.

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
import shutil
import subprocess
import tempfile
import time
from collections import deque
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
    log_path: Path | None = None,
) -> dict:
    """Push the staged project tree to the SoundHub bucket.

    Select files needing transfer, then run ``aws s3 cp --recursive``. Existing
    same-size media are skipped; both CSVs are always sent again. The selection
    is local and never uses ``s3 sync``.
    """
    settings = settings or load_soundhub_config()
    staging_root = staging_root or settings["staging_root"]
    aws = shutil.which("aws")
    if aws is None:
        raise SoundHubUploadError("AWS CLI is not installed or not on PATH; SoundHub upload requires aws s3 cp --recursive.")
    if is_cancelled is not None and is_cancelled():
        return dict(cancelled=True, uploaded=0, skipped=0, uploaded_bytes=0, total=0)
    client = _client(settings)

    objects = staged_objects(
        staging_root, settings, deployment_ids=deployment_ids
    )
    already = _existing_sizes(client, settings)

    selected = [obj for obj in objects if "/" not in obj["relative"]
                or already.get(obj["key"]) != obj["size"]]
    skipped = len(objects) - len(selected)
    completed: set[str] = set()
    cancelled = False
    destination = f"s3://{settings['bucket']}/{project_prefix(settings)}/"
    by_destination = {
        f"s3://{settings['bucket']}/{obj['key']}": obj for obj in selected
    }

    # The CLI recursively walks only these links, not the entire staging tree.
    # This preserves the exact selected scope and ignores local-only sidecars.
    with tempfile.TemporaryDirectory(prefix="cassn-soundhub-upload-") as temporary:
        source = Path(temporary) / "files"
        source.mkdir()
        for obj in selected:
            relative = Path(obj["relative"])
            if relative.is_absolute() or ".." in relative.parts:
                raise SoundHubUploadError(f"Invalid staged path: {relative}")
            link = source / relative
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(obj["path"].resolve())

        command = [aws, "s3", "cp", str(source), destination, "--recursive",
                   "--follow-symlinks", "--no-progress", "--checksum-algorithm", "SHA256"]
        for option, setting in (("--profile", "aws_profile"), ("--region", "region")):
            if settings.get(setting):
                command.extend([option, str(settings[setting])])

        if progress is not None:
            progress(skipped, len(objects), "Starting aws s3 cp --recursive")
        # Separate file handles let the CLI write while the GUI reads progress,
        # without pipe backpressure or blocking cancellation on a quiet upload.
        # The shared submission workflow supplies a permanent archive path.
        # Direct low-level calls retain the historical temporary-log behavior.
        log_path = Path(log_path) if log_path is not None else Path(temporary) / "upload.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        recent = deque(maxlen=20)
        with log_path.open("wb") as output, log_path.open(encoding="utf-8", errors="replace") as log:
            try:
                process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
            except OSError as exc:
                raise SoundHubUploadError(f"Could not start AWS CLI: {exc}") from exc

            def read_progress():
                while True:
                    position = log.tell()
                    line = log.readline()
                    if not line:
                        return
                    if not line.endswith("\n") and process.poll() is None:
                        log.seek(position)
                        return
                    line = line.rstrip()
                    recent.append(line)
                    target = line.rpartition(" to ")[2]
                    if line.startswith("upload: ") and target in by_destination and target not in completed:
                        completed.add(target)
                        if progress is not None:
                            progress(skipped + len(completed), len(objects), by_destination[target]["path"].name)

            try:
                while process.poll() is None:
                    read_progress()
                    if is_cancelled is not None and is_cancelled():
                        cancelled = True
                        break
                    time.sleep(0.1)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            read_progress()
            if not cancelled and process.returncode != 0:
                raise SoundHubUploadError(
                    f"aws s3 cp --recursive failed (exit {process.returncode}):\n"
                    + "\n".join(recent)
                )
            if not cancelled and len(completed) != len(selected):
                raise SoundHubUploadError("AWS CLI did not report completion for every selected object; verify staging before retrying.")

    return {
        "cancelled": cancelled,
        "uploaded": len(completed),
        "skipped": skipped,
        "uploaded_bytes": sum(by_destination[key]["size"] for key in completed),
        "total": len(objects),
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
