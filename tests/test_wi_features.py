"""Regression coverage for Survey123 habitat choices crossing the WI boundary."""

import csv
from types import SimpleNamespace

import pytest

from cassn.core.wi_features import (
    WI_FEATURE_TYPES,
    feature_type_from_survey123,
    validate_wi_feature_type,
)
from cassn.export.wildlife_insights import build_wi_rows, reshape_image_metadata_to_wi
from cassn.export.metadata_csv import build_metadata_rows


@pytest.mark.parametrize("feature", sorted(WI_FEATURE_TYPES))
@pytest.mark.parametrize("device,field", [("ML", "mlCameraLocation"), ("SA", "cameraHabitat")])
def test_survey_choices_keep_every_permitted_category(feature, device, field):
    assert feature_type_from_survey123({field: feature.lower().replace(" ", "_")}, device) == feature


@pytest.mark.parametrize("feature", [
    "aquatic_feature", "habitat_edge", "ditch", "outcrop_ground", "trail_road",
    "Fallen tree providing woody debris habitat.",
])
def test_incompatible_sa_habitats_become_other(feature):
    assert feature_type_from_survey123({"cameraHabitat": feature}, "SA") == "Other"


def test_multiselect_keeps_first_choice_and_other_never_leaks_free_text():
    assert feature_type_from_survey123({"cameraHabitat": "habitat_edge,trail_game"}, "SA") == "Other"
    assert feature_type_from_survey123({"cameraHabitat": "trail_game,habitat_edge"}, "SA") == "Trail game"
    assert feature_type_from_survey123({"cameraHabitat": "other", "cameraHabitat_other": "Wood pile"}, "SA") == "Other"
    assert feature_type_from_survey123({"mlCameraLocation": "other", "mlCameraLocation_other": "Cave"}, "ML") == "Other"
    assert feature_type_from_survey123({"mlCameraLocation": "cave"}, "ML") == "Other"


def test_missing_answers_and_explicit_none_remain_distinct():
    assert feature_type_from_survey123({}, "SA") == ""
    assert feature_type_from_survey123({"cameraHabitat": "none"}, "SA") == "None"
    assert feature_type_from_survey123({"cameraHabitat": "burrow"}, "BD") == ""
    assert validate_wi_feature_type(None) == ""
    assert validate_wi_feature_type("None") == "None"
    with pytest.raises(ValueError, match="Aquatic feature"):
        validate_wi_feature_type("Aquatic feature")


def test_wi_builders_reject_stale_incompatible_metadata(tmp_path):
    with pytest.raises(ValueError, match="Aquatic feature"):
        build_wi_rows(
            {"site_short_name": "TestSite"},
            [{"device_type": "SA", "plot_number": "1"}],
            {("TestSite", 1, "SA"): {"device_id": "CAM1", "feature_type": "Aquatic feature"}},
            {}, {},
        )
    path = tmp_path / "image_file_metadata.csv"
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["file_type", "device_type", "deployment_id", "feature_type"])
        writer.writeheader()
        writer.writerow({"file_type": "image", "device_type": "SA", "deployment_id": "test", "feature_type": "Aquatic feature"})
    with pytest.raises(ValueError, match="Aquatic feature"):
        reshape_image_metadata_to_wi(path)


def test_image_metadata_rejects_incompatible_canonical_feature():
    lookups = SimpleNamespace(
        soundhub_config={}, wi_config={}, arus={},
        cameras={('TestSite', 1, 'SA'): {
            'deployment_id': 'UC_TestSite_plot1_SA_20260201',
            'deployment_start_date': '2026-01-01', 'deployment_end_date': '2026-02-01',
            'feature_type': 'Habitat edge',
        }},
    )
    with pytest.raises(ValueError, match="Habitat edge"):
        build_metadata_rows(
            {'site_short_name': 'TestSite', 'deployment_event_id': 'UC_TestSite_20260201'},
            [{'device_type': 'SA', 'plot_number': '1', 'file_type': 'image'}], lookups,
        )
