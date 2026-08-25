"""One shared, verified SoundHub submission workflow for the CLI and GUI."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from cassn.soundhub.provenance import (
    DEFAULT_SUBMITTER,
    ProvenancePlan,
    SoundHubProvenanceError,
    apply_submission_provenance,
    plan_submission_provenance,
    write_submission_report,
)
from cassn.soundhub.upload import (
    SoundHubUploadError,
    load_soundhub_config,
    staged_objects,
    upload_project,
    verify_project,
)


class SoundHubSubmissionError(Exception):
    """A submission phase failed, with enough context for safe recovery."""

    def __init__(self, message: str, *, phase: str, result: dict | None = None):
        super().__init__(message)
        self.phase = phase
        self.result = result or {}


@dataclass
class SoundHubSubmissionPlan:
    """Read-only snapshot of the exact pending batch and its Box mapping."""

    staging_root: Path
    settings: dict
    provenance: ProvenancePlan
    objects: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def total_bytes(self) -> int:
        return sum(item["size"] for item in self.objects)

    @property
    def object_signature(self) -> tuple[tuple[str, int], ...]:
        return tuple(sorted((item["key"], item["size"]) for item in self.objects))


def plan_soundhub_submission(
    staging_root=None,
    *,
    settings: dict | None = None,
    box_year_root: Path | None = None,
) -> SoundHubSubmissionPlan:
    """Validate and describe the next pending batch without writing anywhere."""
    settings = dict(settings or load_soundhub_config())
    staging_root = Path(staging_root or settings["staging_root"]).expanduser().resolve()
    provenance = plan_submission_provenance(staging_root, box_year_root)
    plan = SoundHubSubmissionPlan(
        staging_root=staging_root,
        settings=settings,
        provenance=provenance,
        errors=list(provenance.errors),
    )
    if provenance.submitted_keys and provenance.pending_keys:
        plan.errors.append(
            "staging mixes recordings from a completed submission with new "
            "pending recordings; clear the completed batch and stage the new "
            "deployment events again"
        )
    if plan.errors or not provenance.pending_keys:
        return plan

    try:
        plan.objects = staged_objects(
            staging_root,
            settings,
            deployment_ids=provenance.pending_deployment_ids,
        )
    except SoundHubUploadError as exc:
        plan.errors.append(str(exc))
        return plan

    media = [item for item in plan.objects if "/" in item["relative"]]
    manifests = {
        item["relative"] for item in plan.objects if "/" not in item["relative"]
    }
    if len(media) != len(provenance.pending_keys):
        plan.errors.append(
            "pending Box metadata maps to "
            f"{len(provenance.pending_keys)} recording(s), but staging selected "
            f"{len(media)} FLAC object(s)"
        )
    if manifests != {"deployment.csv", "recording.csv"}:
        plan.errors.append(
            "staging must contain exactly the two project manifests: "
            "deployment.csv and recording.csv"
        )
    return plan


def execute_soundhub_submission(
    plan: SoundHubSubmissionPlan,
    *,
    submitter: str = DEFAULT_SUBMITTER,
    progress=None,
    is_cancelled=None,
) -> dict:
    """Upload, immediately verify, then record Box provenance and reports."""
    if not plan.ok:
        raise SoundHubSubmissionError(
            "; ".join(plan.errors), phase="preflight"
        )
    if not plan.provenance.pending_keys:
        raise SoundHubSubmissionError(
            "No unsubmitted staged recordings remain.", phase="preflight"
        )

    current = staged_objects(
        plan.staging_root,
        plan.settings,
        deployment_ids=plan.provenance.pending_deployment_ids,
    )
    current_signature = tuple(
        sorted((item["key"], item["size"]) for item in current)
    )
    if current_signature != plan.object_signature:
        raise SoundHubSubmissionError(
            "Staging changed after preflight; review the updated batch before uploading.",
            phase="preflight",
        )

    result = {
        "success": False,
        "cancelled": False,
        "phase": "upload",
        "upload": {},
        "verification": None,
        "provenance": None,
        "reports": [],
    }
    try:
        upload = upload_project(
            plan.staging_root,
            settings=plan.settings,
            progress=progress,
            is_cancelled=is_cancelled,
            deployment_ids=plan.provenance.pending_deployment_ids,
        )
    except SoundHubUploadError as exc:
        raise SoundHubSubmissionError(
            str(exc), phase="upload", result=result
        ) from exc
    result["upload"] = upload
    if upload["cancelled"]:
        result["cancelled"] = True
        return result

    result["phase"] = "verification"
    try:
        verification = verify_project(
            plan.staging_root,
            settings=plan.settings,
            deployment_ids=plan.provenance.pending_deployment_ids,
        )
    except SoundHubUploadError as exc:
        raise SoundHubSubmissionError(
            str(exc), phase="verification", result=result
        ) from exc
    result["verification"] = verification
    if not verification["ok"]:
        return result

    result["phase"] = "box_provenance"
    try:
        provenance = apply_submission_provenance(
            plan.provenance, submitter=submitter
        )
        reports = write_submission_report(
            plan.provenance,
            settings=plan.settings,
            upload_result=upload,
            verification=verification,
            provenance_result=provenance,
            planned_objects=plan.objects,
        )
    except SoundHubProvenanceError as exc:
        raise SoundHubSubmissionError(
            str(exc), phase="box_provenance", result=result
        ) from exc

    result.update(
        success=True,
        phase="complete",
        provenance=provenance,
        reports=reports,
    )
    return result
