import pytest

from cassn.core.aru_status import recording_aru_status, deployment_aru_status


@pytest.mark.parametrize("reason,expected", [
    (None, "functioning"), ("", "functioning"),
    (" LOW   Voltage ", "low voltage"), ("low voltage", "low voltage"),
    ("file size limit", "functioning"), ("SD card write error", "functioning"),
    ("switch position change", "functioning"), ("magnetic switch", "functioning"),
    ("microphone change", "functioning"), ("low_voltage", "functioning"),
    ("not low voltage", "functioning"),
])
def test_recording_status(reason, expected):
    assert recording_aru_status(reason) == expected


def test_aggregation_is_order_independent_and_excludes_sidecars():
    rows = [{"recording_stop_reason": "low voltage", "file_type": "audio"},
            {"recording_stop_reason": "", "file_type": "audio"}]
    assert deployment_aru_status(rows) == "low voltage"
    assert deployment_aru_status(list(reversed(rows))) == "low voltage"
    rows[0]["file_type"] = "config"
    assert deployment_aru_status(rows) == "functioning"
