"""Wildlife Insights camera feature vocabulary and Survey123 conversion.

Survey habitat descriptions are not themselves WI feature categories. Preserve
the original Survey123 exports and reviewed source versions when converting.
"""

from __future__ import annotations


WI_FEATURE_TYPES = frozenset({
    "None", "Road paved", "Road dirt", "Trail hiking", "Trail game",
    "Road underpass", "Road overpass", "Road bridge", "Culvert", "Burrow",
    "Nest site", "Carcass", "Water source", "Fruiting tree", "Other",
})
_SURVEY_CHOICES = {value.casefold(): value for value in WI_FEATURE_TYPES}


def feature_type_from_survey123(attributes: dict, device_type: str) -> str:
    """Translate the existing first-choice rule into the WI vocabulary.

    ML uses mlCameraLocation. SA retains the first cameraHabitat selection.
    Unsupported nonblank choices and explicit Other both become literal Other;
    free-text *_other descriptions stay in the source exports. Missing answers
    stay blank, and explicit None retains its separate meaning.
    """
    if device_type == "ML":
        raw = attributes.get("mlCameraLocation")
    elif device_type == "SA":
        raw = str(attributes.get("cameraHabitat") or "").split(",")[0]
    else:
        return ""
    choice = " ".join(str(raw or "").replace("_", " ").split()).casefold()
    return _SURVEY_CHOICES.get(choice, "Other") if choice else ""


def validate_wi_feature_type(value: str | None) -> str:
    """Reject incompatible canonical/export values instead of hiding a mismatch.

    Blank is retained for existing optional-field behavior. Translation belongs
    at the Survey123-to-curated-source boundary, not at WI export time.
    """
    value = value or ""
    if value and value not in WI_FEATURE_TYPES:
        raise ValueError(
            f"Invalid Wildlife Insights feature_type {value!r}; "
            "translate unsupported Survey123 choices to 'Other' in deployments.csv "
            "and regenerate the image metadata before exporting"
        )
    return value
