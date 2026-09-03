"""Resumable execution of the review-independent NDP media transfer phases.

This module intentionally stops after media sync and remote stat checks.  It
does not publish README/manifest/metadata controls and does not stamp Box
provenance; both depend on the pending manifest and multi-destination review.
"""

from __future__ import annotations

import os
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from cassn.ndp.box_download import TransferCancelled, download_box_file
from cassn.ndp.pelican import PelicanRunner, verify_stat
from cassn.ndp.transfer import MediaTransferPlan, NdpTransferError, join_destination
from cassn.ndp.transfer_state import (
    PHASES,
    TransferState,
    advance,
    load_or_create_state,
    save_state,
)


@dataclass(frozen=True)
class MediaTransferResult:
    downloaded: int
    reused: int
    deployments_synced: int
    objects_statted: int
    checksum_verified: int
    media_complete: bool
    publication_blocked: bool = True


def _scratch_data_root(plan: MediaTransferPlan, deployment_id: str) -> Path:
    root = plan.scratch_root / plan.deployment_event_id / deployment_id / "data"
    try:
        root.resolve(strict=False).relative_to(plan.scratch_root.resolve(strict=False))
    except ValueError as exc:
        raise NdpTransferError(f"scratch path escapes scratch root: {root}") from exc
    return root


def _clear_scratch_data(plan: MediaTransferPlan, deployment_id: str) -> None:
    root = _scratch_data_root(plan, deployment_id)
    if root.is_dir():
        shutil.rmtree(root)
    deployment_root = root.parent
    if deployment_root.is_dir() and not any(deployment_root.iterdir()):
        deployment_root.rmdir()


@contextmanager
def _transfer_lock(state_path: Path):
    """Prevent two processes from advancing the same event state concurrently."""
    lock_path = state_path.with_name(".ndp-transfer.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        try:
            owner = lock_path.read_text(encoding="utf-8").strip()
        except OSError:
            owner = "unknown"
        raise NdpTransferError(
            f"another transfer holds {lock_path} (recorded process {owner})"
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(str(os.getpid()) + "\n")
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def execute_media_transfer(
    plan: MediaTransferPlan,
    box_client,
    pelican: PelicanRunner,
    *,
    progress=None,
    is_cancelled=None,
    clear_scratch: bool = True,
) -> MediaTransferResult:
    """Download, hash, sync, and stat all media, resuming at durable boundaries."""
    if not plan.ok:
        raise NdpTransferError("refusing to execute an invalid media transfer plan")
    pelican.require_available()
    with _transfer_lock(plan.state_path):
        return _execute_media_transfer(
            plan,
            box_client,
            pelican,
            progress=progress,
            is_cancelled=is_cancelled,
            clear_scratch=clear_scratch,
        )


def _execute_media_transfer(
    plan: MediaTransferPlan,
    box_client,
    pelican: PelicanRunner,
    *,
    progress=None,
    is_cancelled=None,
    clear_scratch: bool,
) -> MediaTransferResult:
    state = load_or_create_state(plan)
    save_state(plan.state_path, state)
    downloaded = reused = synced = statted = checksum_verified = 0

    for deployment in plan.deployments:
        if is_cancelled and is_cancelled():
            raise TransferCancelled("media transfer cancelled")
        phase = state.deployments[deployment.deployment_id]
        data_root = _scratch_data_root(plan, deployment.deployment_id)

        if PHASES.index(phase) < PHASES.index("downloaded"):
            for source in deployment.files:
                if is_cancelled and is_cancelled():
                    raise TransferCancelled("media transfer cancelled")
                result = download_box_file(
                    box_client,
                    source,
                    data_root / source.filename,
                    progress=progress,
                    is_cancelled=is_cancelled,
                )
                if result.skipped:
                    reused += 1
                else:
                    downloaded += 1
            advance(state, deployment.deployment_id, "downloaded")
            save_state(plan.state_path, state)
            phase = "downloaded"

        if phase == "downloaded":
            missing = [
                source.filename
                for source in deployment.files
                if not (data_root / source.filename).is_file()
            ]
            if missing:
                raise NdpTransferError(
                    f"{deployment.deployment_id}: downloaded state is missing scratch "
                    f"file(s): {', '.join(missing[:10])}"
                )
            pelican.sync_directory(data_root, deployment.data_destination)
            advance(state, deployment.deployment_id, "synced")
            save_state(plan.state_path, state)
            synced += 1
            phase = "synced"

        previous_stat = state.remote_stats.get(deployment.deployment_id, {})
        needs_stronger_stat = bool(
            phase == "stat_recorded"
            and clear_scratch
            and previous_stat.get("size_only")
        )
        if phase == "synced" or needs_stronger_stat:
            details = []
            for source in deployment.files:
                raw = pelican.stat_object(
                    join_destination(deployment.data_destination, source.filename)
                )
                detail = verify_stat(source, raw)
                details.append(detail)
                statted += 1
                if detail["verification"] == "checksum":
                    checksum_verified += 1
            summary = {
                "objects": len(details),
                "checksum_verified": sum(
                    item["verification"] == "checksum" for item in details
                ),
                "size_only": sum(
                    item["verification"] == "size_only" for item in details
                ),
                "details": details,
            }
            advance(state, deployment.deployment_id, "stat_recorded", stat=summary)
            save_state(plan.state_path, state)
            phase = "stat_recorded"

        if phase == "stat_recorded" and clear_scratch:
            summary = state.remote_stats.get(deployment.deployment_id, {})
            if summary.get("size_only"):
                raise NdpTransferError(
                    f"{deployment.deployment_id}: Pelican returned only size for "
                    "one or more objects; scratch was retained because remote "
                    "content verification is not yet strong enough"
                )
            _clear_scratch_data(plan, deployment.deployment_id)

    return MediaTransferResult(
        downloaded,
        reused,
        synced,
        statted,
        checksum_verified,
        state.media_complete,
    )
