"""Atomic local state for resumable NDP media transfers."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from cassn.ndp.transfer import MediaTransferPlan, NdpTransferError


STATE_VERSION = 2
PHASES = ("pending", "downloaded", "synced", "stat_recorded")


@dataclass
class TransferState:
    version: int
    deployment_event_id: str
    destination_root: str
    plan_signature: str
    deployments: dict[str, str] = field(default_factory=dict)
    data_destinations: dict[str, str] = field(default_factory=dict)
    remote_stats: dict[str, dict] = field(default_factory=dict)

    @property
    def media_complete(self) -> bool:
        return bool(self.deployments) and all(
            phase == "stat_recorded" for phase in self.deployments.values()
        )


def new_state(plan: MediaTransferPlan) -> TransferState:
    if not plan.ok:
        raise NdpTransferError("cannot create state for an invalid transfer plan")
    return TransferState(
        STATE_VERSION,
        plan.deployment_event_id,
        plan.destination_root,
        plan.signature,
        {deployment.deployment_id: "pending" for deployment in plan.deployments},
        {
            deployment.deployment_id: deployment.data_destination
            for deployment in plan.deployments
        },
    )


def load_state(path: Path) -> TransferState:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        state = TransferState(**raw)
    except (OSError, ValueError, TypeError) as exc:
        raise NdpTransferError(f"could not read transfer state {path}: {exc}") from exc
    if state.version != STATE_VERSION:
        raise NdpTransferError(f"unsupported transfer state version: {state.version}")
    if any(phase not in PHASES for phase in state.deployments.values()):
        raise NdpTransferError("transfer state contains an unknown deployment phase")
    if set(state.data_destinations) != set(state.deployments):
        raise NdpTransferError(
            "transfer state destination keys do not match its deployments"
        )
    return state


def load_or_create_state(plan: MediaTransferPlan) -> TransferState:
    state = load_state(plan.state_path) if plan.state_path.exists() else new_state(plan)
    expected_deployments = {deployment.deployment_id for deployment in plan.deployments}
    if (
        state.plan_signature != plan.signature
        or state.deployment_event_id != plan.deployment_event_id
        or state.destination_root != plan.destination_root
        or set(state.deployments) != expected_deployments
    ):
        raise NdpTransferError(
            "transfer inputs or destination changed after preflight; abandon or restore the original plan"
        )
    return state


def save_state(path: Path, state: TransferState) -> None:
    """Atomically replace local state after a durable transfer boundary."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(state.__dict__, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def advance(state: TransferState, deployment_id: str, phase: str, *, stat=None) -> None:
    if phase not in PHASES:
        raise NdpTransferError(f"unknown transfer phase: {phase}")
    if deployment_id not in state.deployments:
        raise NdpTransferError(
            f"deployment is absent from transfer state: {deployment_id}"
        )
    current = PHASES.index(state.deployments[deployment_id])
    requested = PHASES.index(phase)
    if requested < current or requested > current + 1:
        raise NdpTransferError(
            f"invalid transfer transition for {deployment_id}: "
            f"{state.deployments[deployment_id]} -> {phase}"
        )
    state.deployments[deployment_id] = phase
    if stat is not None:
        state.remote_stats[deployment_id] = stat


def abandonment_targets(state: TransferState) -> tuple[str, ...]:
    """Remote collections requiring explicit deletion to abandon this run."""
    active = {
        deployment_id
        for deployment_id, phase in state.deployments.items()
        if phase in {"synced", "stat_recorded"}
    }
    return tuple(
        state.data_destinations[deployment_id]
        for deployment_id in sorted(active)
        if deployment_id in state.data_destinations
    )
