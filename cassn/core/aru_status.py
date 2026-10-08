"""CA-SSN status convention: any observed low-voltage interruption wins.

Functioning is a default, not a certification of complete schedule coverage.
SoundHub's accepted vocabulary remains subject to confirmation with Brian.
"""

FUNCTIONING = "functioning"
LOW_VOLTAGE = "low voltage"


def recording_aru_status(stop_reason) -> str:
    normalized = " ".join(str(stop_reason or "").lower().split())
    return LOW_VOLTAGE if normalized == LOW_VOLTAGE else FUNCTIONING


def deployment_aru_status(rows) -> str:
    return LOW_VOLTAGE if any(
        recording_aru_status(row.get("recording_stop_reason")) == LOW_VOLTAGE
        or row.get("_deployment_aru_status") == LOW_VOLTAGE
        for row in rows
        if row.get("file_type", "audio") == "audio"
    ) else FUNCTIONING
