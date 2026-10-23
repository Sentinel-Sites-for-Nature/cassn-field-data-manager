"""Public deployment identities selected before importing application configuration."""
import os

HOSTED_CLIENT_ID = "q14sh0pmbaqpqdml783967b9wg4vhy4e"
HOSTED_ORIGIN = "https://cassn-csu-box-auth.tiny-band-cf20.workers.dev"
CSU_DATA_ID = "430058250915"
CSU_CONFIG_ID = "430057609739"
PROJECTS = {
    "csu": {"title": "CSU Field Data Manager", "origin": HOSTED_ORIGIN,
            "data": CSU_DATA_ID, "config": CSU_CONFIG_ID},
    "uc": {"title": "UC Field Data Manager",
           "origin": "https://cassn-uc-box-auth.tiny-band-cf20.workers.dev",
           "data": "377166707326", "config": "377164694031"},
}
PROFILE = os.environ.get("CASSN_PROFILE", "legacy")
if PROFILE not in {"legacy", *PROJECTS}:
    raise RuntimeError("Unknown Field Data Manager profile")
CSU = PROFILE == "csu"
HOSTED = PROFILE in PROJECTS


def session_matches(session, config):
    """Never route a saved deployment into a different project's Box root."""
    marker = session.get("box_project")
    if marker is None:
        return config.provider != "hosted"  # Existing UC sessions predate profiles.
    return marker == {"profile": config.profile, "data_folder": config.field_data_folder_id}
