"""One shared, verified SoundHub submission workflow for the CLI and GUI."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

from cassn.soundhub.export import validate_staging_manifests

from cassn.soundhub.provenance import (
    DEFAULT_SUBMITTER,
    ProvenancePlan,
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
from cassn.soundhub.staging import SoundHubStagingError
from cassn.soundhub.transfers import TransferArchive


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
    validation_errors: list[str] = []
    try:
        validate_staging_manifests(staging_root)
    except SoundHubStagingError as exc:
        validation_errors.append(str(exc))
    provenance = plan_submission_provenance(staging_root, box_year_root)
    plan = SoundHubSubmissionPlan(
        staging_root=staging_root,
        settings=settings,
        provenance=provenance,
        errors=validation_errors + list(provenance.errors),
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

    try:
        # A verified transfer must be repaired, never sent again just because
        # its Box flags could not be saved. The S3 landing zone may be drained.
        for journal in (plan.staging_root.parent / "transfers").glob("*/transfer.json"):
            try:
                prior = json.loads(journal.read_text())
            except (OSError, ValueError) as exc:
                raise SoundHubSubmissionError(
                    f"Cannot check previous transfer record; upload not started: {journal}: {exc}",
                    phase="preflight") from exc
            signature = tuple(sorted((item["key"], item["size"])
                                     for item in prior.get("planned", {}).get("objects", [])))
            if ((prior.get("verification") or {}).get("ok")
                    and prior.get("status") != "complete"
                    and prior.get("destination") == (
                        f"s3://{plan.settings['bucket']}/{plan.settings['upload_prefix']}/"
                        f"{plan.settings['project_short_name']}/")
                    and signature == plan.object_signature):
                raise SoundHubSubmissionError(
                    f"This batch already passed S3 verification. Repair the Box record "
                    f"instead of uploading again: {journal.parent}", phase="preflight")
        archive = TransferArchive.begin(plan, submitter)
    except OSError as exc:
        raise SoundHubSubmissionError(
            f"Could not save the local transfer record; upload not started: {exc}",
            phase="local_record",
        ) from exc

    result = {
        "success": False,
        "cancelled": False,
        "phase": "upload",
        "upload": {},
        "verification": None,
        "provenance": None,
        "reports": [],
        "transfer_directory": archive.directory,
    }
    try:
        upload = upload_project(
            plan.staging_root,
            settings=plan.settings,
            progress=progress,
            is_cancelled=is_cancelled,
            deployment_ids=plan.provenance.pending_deployment_ids,
            log_path=archive.directory / "upload.log",
        )
        result["upload"] = upload
        archive.update(upload=upload)
        if upload["cancelled"]:
            result["cancelled"] = True
            archive.finish("cancelled", phase="upload")
            return result

        result["phase"] = "verification"
        archive.update(phase="verification")
        verification = verify_project(
            plan.staging_root,
            settings=plan.settings,
            deployment_ids=plan.provenance.pending_deployment_ids,
        )
        result["verification"] = verification
        # Preserve S3 success before any Box write; the landing zone is drained
        # after ingestion and cannot serve as the durable submission record.
        archive.update(verification=verification)
        if not verification["ok"]:
            archive.finish("failed", phase="verification",
                           error="S3 objects are missing or differ in size")
            return result

        result["phase"] = "box_provenance"
        archive.update(phase="box_provenance")
        if plan.provenance.box_store is not None:
            plan.provenance.box_store.reconnect()
        provenance = apply_submission_provenance(
            plan.provenance, submitter=submitter
        )
        result["provenance"] = provenance
        archive.update(provenance=provenance)
        reports = write_submission_report(
            plan.provenance,
            settings=plan.settings,
            upload_result=upload,
            verification=verification,
            provenance_result=provenance,
            planned_objects=plan.objects,
            local_report_path=archive.directory / "submission_report.md",
        )
        archive.finish("complete", phase="complete", reports=reports)
        result.update(success=True, phase="complete", reports=reports)
    except KeyboardInterrupt:
        archive.finish("interrupted", phase=result["phase"], error="KeyboardInterrupt")
        raise
    except Exception as exc:
        message = str(exc)
        try:
            archive.finish("failed", phase=result["phase"],
                           error=f"{type(exc).__name__}: {exc}")
        except OSError as record_error:
            message += f"; could not update transfer record at {archive.directory}: {record_error}"
        raise SoundHubSubmissionError(
            message, phase=result["phase"], result=result
        ) from exc
    return result
