"""Actionable QC presentation built from retained evidence, without reading media.

Expected resolved offsets retain aggregate counts only. Unusual issues retain
stable device/filename identities, so several symptoms can be shown together.
Legacy and unknown checks always have a visible diagnostic fallback.
"""
from __future__ import annotations

from collections import defaultdict
import re

from cassn.core.classification import format_size_floor
from cassn.core.quality_control import (
    check_camera_serial, check_camera_timezones, check_file_size_floor,
    check_sequence_integrity, qc_file_reference, qc_issue,
)


CHECK_TITLES = {
    "file_size_floor": "Unusually small files",
    "sequence_gap": "Burst sequences need review",
    "camera_serial_match": "Camera identity needs review",
    "camera_timezone_consistency": "Camera timestamps need review",
    "temporal_plausibility": "Recording dates need review",
    "coordinate_validation": "Coordinates need review",
    "hash_verification": "File copy verification failed",
    "expected_file_count": "Copied file count needs review",
    "duplicate_detection": "Duplicate files detected",
    "camera_date_review": "Camera dates need review",
    "recording_stop_reason": "Recording stopped early",
}


_OUTSTANDING_TASKS = {
    "All devices complete": "Complete collection for all devices",
    "Hash verification passed": "Verify file hashes",
    "Box upload completed": "Complete the Box upload",
    "No QC errors recorded": "Review unresolved QC errors",
    "Camera timezone QC has no errors": "Review camera timezone errors",
}


def checklist_task(label: str) -> str:
    """Name an unmet requirement as work to do, rather than a completed result."""
    return _OUTSTANDING_TASKS.get(label, label)


def departure_warning_message(labels: list[str]) -> str:
    tasks = [checklist_task(label) for label in labels]
    noun = "task" if len(tasks) == 1 else "tasks"
    return f"Proceeded with {len(tasks)} outstanding {noun}: " + "; ".join(tasks)


def _departure_review(entry, diagnostic):
    message = str(entry.get("message") or "")
    legacy = re.fullmatch(r"Acknowledged with (\d+) unmet item\(s\): (.+)", message)
    if legacy:
        labels = legacy[2].split(", ")
        if len(labels) == int(legacy[1]):
            message = departure_warning_message(labels)
    when = str(entry.get("timestamp") or "").replace("T", " ")
    level = "error" if entry["severity"] == "error" else "warning"
    return {
        "severity": entry["severity"], "title": f"Saved deployment checklist {level}",
        "affected": "Deployment", "files": [],
        "action": "Complete any tasks that are still outstanding; this saved warning is not a current completion status.",
        "message": (f"Saved checklist {level}{(' from ' + when) if when else ''}:\n{message}\n\n"
                    "This checklist entry records requirements that were still unmet at that time. "
                    "Acknowledging it does not complete an upload or verify file hashes.\n\n"
                    "This is a deployment-wide checklist; no individual file list applies."),
        "technical": diagnostic,
    }


def quantity(count, unit):
    """Format the small set of plural units used in the review."""
    return f"{count:,} {unit[:-1] if count == 1 and unit.endswith('s') else unit}"


def small_file_findings(entries: list[dict]) -> list[dict]:
    findings = []
    for row in entries:
        size = row.get("file_size_bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            continue
        hit = check_file_size_floor(size, row.get("file_type", ""), row.get("device_type", ""))
        if hit:
            floor, label = hit
            findings.append(qc_issue(
                "small_file", "warning", "Unusually small file",
                f"Size: {format_size_floor(size)} ({size:,} bytes); minimum: {label} ({floor:,} bytes).",
                "Open the file to check that it is readable and compare it with the source card. Size alone does not prove corruption.",
                files=[qc_file_reference(row)],
            ))
    return findings


def _legacy_findings(entry, rows):
    """Enrich older reports only when complete inventory evidence agrees in severity."""
    check = entry.get("check")
    images = [row for row in rows if row.get("file_type") == "image"]
    findings = []
    if check == "camera_timezone_consistency" and images:
        severity, _ = check_camera_timezones(rows, findings=findings)
    elif check == "camera_serial_match" and images and all(
        "camera_serial_exif" in row and "camera_id" in row for row in images
    ):
        messages = check_camera_serial(rows, entry.get("_device", ""), findings=findings)
        severity = "warning" if messages else "pass"
    elif check == "sequence_gap" and images:
        messages = check_sequence_integrity(rows, entry.get("_device", ""), findings=findings)
        severity = "warning" if messages else "pass"
    elif check == "file_size_floor" and rows and all(
        isinstance(row.get("file_size_bytes"), int) and row["file_size_bytes"] >= 0
        and row.get("device_type") and row.get("file_type") for row in rows
    ):
        findings = small_file_findings(rows)
        severity = "warning" if findings else "pass"
    else:
        return []
    # Preserve historical errors and evidence discrepancies through the raw fallback.
    return findings if severity == entry.get("severity") else []


def _valid_findings(value):
    if not isinstance(value, list) or not value:
        return False
    for issue in value:
        if not isinstance(issue, dict) or not all(isinstance(issue.get(k), str) for k in (
            "code", "severity", "title", "message", "action",
        )) or issue["severity"] not in {"info", "warning", "error"}:
            return False
        if not isinstance(issue.get("count"), int) or issue["count"] < 0:
            return False
        if not isinstance(issue.get("files"), list) or not all(
            isinstance(file, dict) and isinstance(file.get("filename"), str)
            and isinstance(file.get("device_label", ""), str) for file in issue["files"]
        ):
            return False
    return True


def collection_review_findings(entries: list[dict], inventory: list[dict]) -> list[dict]:
    """Group report findings for review; counts refer to displayed rows, never unique files."""
    by_device = defaultdict(list)
    for row in inventory:
        by_device[row.get("device_label", "")].append(row)
    file_issues = defaultdict(list)
    grouped = defaultdict(list)
    fallback = []
    for entry in entries:
        if entry.get("severity") not in {"error", "warning"}:
            continue
        scope = entry.get("_device") or "Deployment"
        diagnostic = f"{scope} · {entry.get('check', 'QC')}: {entry.get('message', '')}"
        if entry.get("description"):
            diagnostic += f"\nCheck explanation: {entry['description']}"
        if entry.get("check") == "pre_departure":
            fallback.append(_departure_review(entry, diagnostic))
            continue
        issues = entry.get("findings")
        if issues is None:
            issues = _legacy_findings(entry, by_device.get(entry.get("_device", ""), []))
        if not _valid_findings(issues) or (
            entry["severity"] == "error" and not any(i["severity"] == "error" for i in issues)
        ) or any(i["severity"] == "info" and (
            entry.get("check") != "camera_timezone_consistency" or i["code"] != "resolved_camera_offset"
        ) for i in issues):
            title = CHECK_TITLES.get(entry.get("check"), entry.get("check") or "QC finding")
            fallback.append({"severity": entry["severity"], "title": title,
                "affected": scope, "action": "Review the check details and ingest log before uploading.",
                "message": f"{scope}: {entry.get('message', '')}\n\nFile details were not retained for this check.",
                "technical": diagnostic, "files": []})
            continue
        for issue in issues:
            issue = dict(issue, scope=scope, technical=diagnostic)
            # Bursts are group-level checks: the observed frames are context, not defective files.
            if issue["files"] and issue["code"] != "sequence_gap" and all(f["filename"] for f in issue["files"]):
                for file in issue["files"]:
                    key = (file.get("device_label") or entry.get("_device", ""), file["filename"])
                    file_issues[key].append(issue)
            else:
                grouped[(issue["code"], issue["severity"], issue["title"])].append(issue)

    # Combine symptoms by stable file identity, then group files with the same symptoms.
    combinations = defaultdict(list)
    for identity, issues in file_issues.items():
        unique = {issue["code"]: issue for issue in issues}
        signature = tuple(sorted((code, issue["severity"], issue["title"]) for code, issue in unique.items()))
        combinations[signature].append((identity, list(unique.values())))
    result = fallback
    for signature, affected in combinations.items():
        severity = "error" if any(level == "error" for _, level, _ in signature) else "warning"
        files = [{"device_label": device, "filename": name} for (device, name), _ in affected]
        issues = affected[0][1]
        actions = list(dict.fromkeys(issue["action"] for issue in issues))
        detail = []
        for (device, filename), symptoms in affected:
            detail.append(f"{device} · {filename}\n" + "\n".join(
                f"• {issue['title']}: {issue['message']}" for issue in symptoms))
        result.append({"severity": severity, "title": " + ".join(issue["title"] for issue in issues),
            "affected": f"{quantity(len(files), 'files')} · {quantity(len({f['device_label'] for f in files}), 'devices')}",
            "action": "Inspect the affected file(s) and original metadata." if len(issues) > 1 else actions[0],
            "message": "\n\n".join(detail) + "\n\nNext steps\n" + "\n".join(actions),
            "technical": "\n\n".join(dict.fromkeys(i["technical"] for _, items in affected for i in items)),
            "files": files})
    for (code, severity, title), issues in grouped.items():
        devices = {i["scope"] for i in issues}
        count = sum(i["count"] for i in issues)
        unit = issues[0].get("unit", "items")
        detail = "\n\n".join(f"{i['scope']} · {quantity(i['count'], i.get('unit', 'items'))}\n{i['message']}"
            + ("\nObserved files:\n" + "\n".join(f["filename"] or "Unnamed file" for f in i["files"]) if i["files"] else "")
            for i in issues)
        result.append({"severity": severity, "title": title,
            "affected": f"{quantity(count, unit)} · {quantity(len(devices), 'devices')}",
            "action": issues[0]["action"], "message": detail + "\n\n" + issues[0]["action"],
            "technical": "\n\n".join(dict.fromkeys(i["technical"] for i in issues)),
            "files": [f for i in issues for f in i["files"]]})
    return sorted(result, key=lambda i: {"error": 0, "warning": 1, "info": 2}[i["severity"]])
