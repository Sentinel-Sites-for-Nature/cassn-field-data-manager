"""Tests for occurrence-export deployment naming."""

from utils.generate_occurrences import deployment_event_id_from_deployment_id


def test_event_id_omits_deployment_sequence_suffix():
    assert deployment_event_id_from_deployment_id(
        "UC_TestSite_plot1_ML_20260610-seq01"
    ) == "UC_TestSite_20260610"


def test_unrecognized_deployment_id_is_preserved():
    assert deployment_event_id_from_deployment_id("legacy-id") == "legacy-id"
