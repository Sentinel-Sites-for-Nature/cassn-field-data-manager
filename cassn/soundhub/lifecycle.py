"""Lifecycle safeguards for completed SoundHub staging batches.

Staging is cumulative only until a submission succeeds.  Once every staged
recording has verified Box provenance, that project directory is a closed
batch: new deployments must not be added because its cumulative manifests
describe media from the completed submission.

Cleanup is intentionally separate from upload.  It is a local, operator-run
maintenance action after SoundHub acceptance has been confirmed; it never
touches Box, S3, or source WAVs.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from cassn.config import SOUNDHUB_PROJECT_SHORT_NAME
from cassn.soundhub.provenance import ProvenancePlan, plan_submission_provenance
from cassn.soundhub.staging import fragments_root, project_root


REPORT_GLOB = "*_soundhub_submission.md"
VERIFIED_REPORT_MARKER = "**Status:** Verified successfully"


class SoundHubLifecycleError(Exception):
    """A staging batch cannot be classified or safely cleared."""


@dataclass
class CompletedBatchPlan:
    """Read-only description of a possible completed-batch cleanup."""

    staging_root: Path
    project_root: Path
    fragments_root: Path
    provenance: ProvenancePlan | None = None
    closed: bool = False
    local_file_count: int = 0
    local_bytes: int = 0
    file_signature: tuple[tuple[str, int, int], ...] = ()
    event_roots: tuple[Path, ...] = ()
    report_name: str | None = None
    report_paths: tuple[Path, ...] = ()
    errors: list[str] = field(default_factory=list)

    @property
    def clearable(self) -> bool:
        return self.closed and not self.errors

    @property
    def recording_count(self) -> int:
        return len(self.provenance.target_keys) if self.provenance else 0

    @property
    def deployment_count(self) -> int:
        return len(self.provenance.deployment_ids) if self.provenance else 0

    @property
    def event_count(self) -> int:
        return len(self.provenance.event_ids) if self.provenance else 0

    @property
    def pending_count(self) -> int:
        return len(self.provenance.pending_keys) if self.provenance else 0


@dataclass(frozen=True)
class CompletedBatchClearResult:
    removed_project_root: Path
    removed_fragments_root: Path | None
    removed_files: int
    removed_bytes: int
    report_name: str


def staging_extension_blockers(plan: CompletedBatchPlan) -> list[str]:
    """Return only errors that make the *local* batch unsafe to extend.

    Once a provenance plan exists, remaining errors describe Box metadata or
    submission-report state.  Those are strict upload/cleanup prerequisites,
    but they are independent of adding another validated deployment to local
    staging.  Errors encountered before provenance planning indicate an unsafe
    staging path or layout and remain blocking.
    """
    if plan.provenance is not None:
        return []
    return list(plan.errors)


def _safe_cleanup_paths(staging_root: Path) -> tuple[Path, Path, list[str]]:
    """Resolve the two exact staging children and reject unsafe layouts."""
    errors: list[str] = []
    staging_root = Path(staging_root).expanduser().resolve()
    candidates = (
        (project_root(staging_root), SOUNDHUB_PROJECT_SHORT_NAME, "project"),
        (fragments_root(staging_root), ".cassn_fragments", "fragment"),
    )
    resolved_paths: list[Path] = []
    for candidate, expected_name, label in candidates:
        if candidate.is_symlink():
            errors.append(f"refusing to clear a symlinked {label} directory: {candidate}")
            resolved_paths.append(candidate)
            continue
        resolved = candidate.resolve()
        resolved_paths.append(resolved)
        if resolved.parent != staging_root or resolved.name != expected_name:
            errors.append(
                f"cleanup target is not the expected {label} directory directly "
                f"below the staging root: {resolved}"
            )
    return resolved_paths[0], resolved_paths[1], errors


def _local_files(
    roots: tuple[tuple[str, Path], ...],
) -> tuple[tuple[tuple[str, int, int], ...], list[str]]:
    """Return a no-symlink signature for everything cleanup would remove."""
    errors: list[str] = []
    signature: list[tuple[str, int, int]] = []
    for label, root in roots:
        if not root.exists():
            continue
        if not root.is_dir():
            errors.append(f"cleanup target is not a directory: {root}")
            continue
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                errors.append(f"refusing to clear staging containing a symlink: {path}")
                continue
            if path.is_file():
                stat = path.stat()
                relative = path.relative_to(root).as_posix()
                signature.append((f"{label}/{relative}", stat.st_size, stat.st_mtime_ns))
    return tuple(signature), errors


def _verified_reports(event_root: Path) -> dict[str, Path]:
    reports: dict[str, Path] = {}
    folder = event_root / "soundhub"
    if not folder.is_dir():
        return reports
    for path in sorted(folder.glob(REPORT_GLOB)):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if VERIFIED_REPORT_MARKER in text:
            reports[path.name] = path
    return reports


def plan_completed_batch_cleanup(
    staging_root: Path,
    box_year_root: Path | None = None,
) -> CompletedBatchPlan:
    """Inspect staging and Box records without changing either location."""
    staging_root = Path(staging_root).expanduser().resolve()
    root, fragments, path_errors = _safe_cleanup_paths(staging_root)
    plan = CompletedBatchPlan(staging_root, root, fragments, errors=path_errors)
    if path_errors:
        return plan
    if not root.is_dir():
        plan.errors.append(f"no SoundHub batch is staged at {root}")
        return plan

    signature, signature_errors = _local_files(
        (("project", root), ("fragments", fragments))
    )
    plan.file_signature = signature
    plan.local_file_count = len(signature)
    plan.local_bytes = sum(size for _, size, _ in signature)
    plan.errors.extend(signature_errors)

    provenance = plan_submission_provenance(staging_root, box_year_root)
    plan.provenance = provenance
    if not provenance.ok:
        plan.errors.extend(provenance.errors)
        return plan

    plan.closed = bool(provenance.target_keys) and not provenance.pending_keys
    if not plan.closed:
        return plan
    if provenance.submitted_keys != provenance.target_keys:
        plan.errors.append(
            "the staged recording set is not completely represented by submitted Box rows"
        )
        return plan

    event_roots = tuple(sorted({document.path.parent for document in provenance.documents}))
    plan.event_roots = event_roots
    report_sets: list[set[str]] = []
    reports_by_event: list[dict[str, Path]] = []
    for event_root in event_roots:
        reports = _verified_reports(event_root)
        reports_by_event.append(reports)
        report_sets.append(set(reports))
        if not reports:
            plan.errors.append(
                "no verified SoundHub submission report found in "
                f"{event_root / 'soundhub'}"
            )
    if plan.errors:
        return plan

    common = set.intersection(*report_sets) if report_sets else set()
    if not common:
        plan.errors.append(
            "the staged deployment events do not share one verified batch report"
        )
        return plan

    plan.report_name = sorted(common)[-1]
    plan.report_paths = tuple(reports[plan.report_name] for reports in reports_by_event)
    return plan


def clear_completed_batch(plan: CompletedBatchPlan) -> CompletedBatchClearResult:
    """Revalidate and remove only the project mirror and durable fragments."""
    if not plan.clearable:
        raise SoundHubLifecycleError("cannot clear a batch that did not pass cleanup preflight")

    fresh = plan_completed_batch_cleanup(
        plan.staging_root,
        plan.provenance.box_year_root if plan.provenance else None,
    )
    if not fresh.clearable:
        detail = "; ".join(fresh.errors) or "batch is no longer complete"
        raise SoundHubLifecycleError(f"cleanup preflight changed: {detail}")
    if fresh.file_signature != plan.file_signature:
        raise SoundHubLifecycleError(
            "staging changed after cleanup preflight; run the dry run again"
        )
    if fresh.report_name != plan.report_name:
        raise SoundHubLifecycleError(
            "the verified batch report changed after cleanup preflight"
        )

    removed_fragments = fresh.fragments_root if fresh.fragments_root.exists() else None
    if removed_fragments is not None:
        shutil.rmtree(removed_fragments)
    shutil.rmtree(fresh.project_root)
    return CompletedBatchClearResult(
        removed_project_root=fresh.project_root,
        removed_fragments_root=removed_fragments,
        removed_files=fresh.local_file_count,
        removed_bytes=fresh.local_bytes,
        report_name=fresh.report_name or "",
    )
