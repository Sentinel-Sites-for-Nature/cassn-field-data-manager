"""Tests for card-scan helpers (cassn.core.inventory)."""
import json
import os

import pytest

from cassn.core.inventory import (
    already_copied_relpaths,
    build_deployment_filename,
    count_expected_files,
    deduplicate_exact_storage_entries,
    default_storage_relpath,
    find_all_sessions,
    format_staged_event_tree,
    index_inventory_by_storage_relpath,
    inventory_by_source_relpath,
    inventory_storage_relpath,
    next_plain_file_sequence,
    refresh_legacy_device_manifest,
    reconcile_device_dir,
    set_inventory_storage_relpath,
    sorted_walk,
    write_session,
)


def test_staged_event_tree_shows_generated_structure_without_listing_media(tmp_path):
    event = tmp_path / "UC_TestSite_20260801"
    raw = event / "raw_data"
    bird = raw / "p1_BD"
    camera = raw / "p2_ML"
    bird.mkdir(parents=True)
    camera.mkdir()
    (bird / "recording.wav").write_bytes(b"audio")
    (camera / "image.jpg").write_bytes(b"image")
    wi = event / "WI_metadata"
    wi.mkdir()
    (wi / "wildlife_insights_ML_deployments.csv").write_text("header\n")
    soundhub = event / "soundhub"
    soundhub.mkdir()
    (soundhub / "deployment.csv").write_text("header\n")
    (event / "deployment_event_record.json").write_text("{}")

    tree = format_staged_event_tree(
        event,
        [
            {"device_label": "p1_BD"},
            {"device_label": "p1_BD"},
            {"device_label": "p2_ML"},
        ],
    )

    assert tree.startswith("UC_TestSite_20260801/\n")
    assert "WI_metadata/" in tree
    assert "wildlife_insights_ML_deployments.csv" in tree
    assert "soundhub/" in tree
    assert "deployment.csv" in tree
    assert "p1_BD/ (2 inventoried files)" in tree
    assert "p2_ML/ (1 inventoried file)" in tree
    assert "recording.wav" not in tree
    assert "image.jpg" not in tree


def test_prospective_filename_preserves_deployment_sequence_suffix():
    assert build_deployment_filename(
        "UC_Angelo_plot1_BD_20260709-seq01", "00001", ".WAV"
    ) == "UC_Angelo_plot1_BD_20260709-seq01_00001.WAV"
    assert build_deployment_filename(
        "UC_Angelo_plot1_BD_20260709-seq01", "CONFIG_01", ".TXT"
    ) == "UC_Angelo_plot1_BD_20260709-seq01_CONFIG_01.TXT"

    with pytest.raises(ValueError, match="deployment_id is required"):
        build_deployment_filename("", "00001", ".WAV")


def test_sorted_walk_orders_dirs_and_files(tmp_path):
    # Create Reconyx burst folders and frames out of alphabetical order on disk.
    for folder in ("102RECNX", "100RECNX", "101RECNX"):
        d = tmp_path / folder
        d.mkdir()
        (d / "RCNX0002.JPG").write_bytes(b"x")
        (d / "RCNX0001.JPG").write_bytes(b"x")

    visited = []
    for root, dirs, files in sorted_walk(tmp_path):
        for f in files:
            visited.append(f"{os.path.basename(root)}/{f}")

    # Folders visited 100 -> 101 -> 102, and frames 0001 -> 0002 within each,
    # regardless of the order they were created on disk.
    assert visited == [
        "100RECNX/RCNX0001.JPG", "100RECNX/RCNX0002.JPG",
        "101RECNX/RCNX0001.JPG", "101RECNX/RCNX0002.JPG",
        "102RECNX/RCNX0001.JPG", "102RECNX/RCNX0002.JPG",
    ]


def test_count_expected_files_matches_classifier(tmp_path):
    (tmp_path / "RCNX0001.JPG").write_bytes(b"x")   # image -> counted
    (tmp_path / "RCNX0002.TIF").write_bytes(b"x")   # image (classifier keeps .tif) -> counted
    (tmp_path / "REC.WAV").write_bytes(b"x")         # audio -> counted
    (tmp_path / "CONFIG.TXT").write_bytes(b"x")      # config -> counted
    (tmp_path / "notes.pdf").write_bytes(b"x")       # other -> NOT counted
    (tmp_path / ".DS_Store").write_bytes(b"x")       # hidden -> NOT counted
    assert count_expected_files(tmp_path) == 4


def test_already_copied_relpaths_distinguishes_same_basename():
    inv = [
        {"device_label": "p1_ML", "source_relpath": "100RECNX/RCNX0001.JPG"},
        {"device_label": "p1_ML", "source_relpath": "101RECNX/RCNX0001.JPG"},
        {"device_label": "p2_SA", "source_relpath": "100RECNX/RCNX0001.JPG"},
    ]
    # Both same-basename files for THIS device are tracked as distinct paths;
    # the other device's identical basename is excluded.
    assert already_copied_relpaths(inv, "p1_ML") == {
        "100RECNX/RCNX0001.JPG",
        "101RECNX/RCNX0001.JPG",
    }


def test_already_copied_relpaths_ignores_entries_without_relpath():
    inv = [
        {"device_label": "p1_ML", "source_relpath": "a/x.JPG"},
        {"device_label": "p1_ML"},  # pre-fix entry, no source_relpath
    ]
    assert already_copied_relpaths(inv, "p1_ML") == {"a/x.JPG"}


def test_inventory_storage_relpath_falls_back_for_legacy_entries():
    entry = {"device_label": "p1_ML", "new_filename": "image.jpg"}

    assert default_storage_relpath("p1_ML", "image.jpg") == (
        "raw_data/p1_ML/image.jpg"
    )
    assert inventory_storage_relpath(entry) == "raw_data/p1_ML/image.jpg"


def test_inventory_storage_relpath_supports_nested_split_paths():
    entry = {
        "device_label": "p1_ML",
        "new_filename": "image.jpg",
        "storage_relpath": r"raw_data\p1_ML\p1_ML_2\image.jpg",
    }

    assert inventory_storage_relpath(entry) == (
        "raw_data/p1_ML/p1_ML_2/image.jpg"
    )
    assert set_inventory_storage_relpath(
        entry, "raw_data/p1_ML/p1_ML_3/image.jpg"
    ) == "raw_data/p1_ML/p1_ML_3/image.jpg"
    assert entry["storage_relpath"] == "raw_data/p1_ML/p1_ML_3/image.jpg"


@pytest.mark.parametrize(
    "storage_relpath",
    [
        "/raw_data/p1_ML/image.jpg",
        "raw_data/p1_ML/../p2_ML/image.jpg",
        "elsewhere/p1_ML/image.jpg",
        "raw_data/p2_ML/image.jpg",
        "raw_data/p1_ML/not-image.jpg",
    ],
)
def test_inventory_storage_relpath_rejects_invalid_or_inconsistent_paths(
    storage_relpath,
):
    entry = {
        "device_label": "p1_ML",
        "new_filename": "image.jpg",
        "storage_relpath": storage_relpath,
    }

    with pytest.raises(ValueError):
        inventory_storage_relpath(entry)


def test_index_inventory_by_storage_relpath_rejects_collisions():
    entries = [
        {"device_label": "p1_ML", "new_filename": "image.jpg"},
        {
            "device_label": "p1_ML",
            "new_filename": "image.jpg",
            "storage_relpath": "raw_data/p1_ML/image.jpg",
        },
    ]

    with pytest.raises(ValueError, match="Duplicate inventory storage path"):
        index_inventory_by_storage_relpath(entries)


def test_exact_duplicate_config_record_is_safely_collapsed():
    record = {
        "device_label": "p1_BD",
        "new_filename": "UC_S_plot1_BD_20260305_CONFIG_01.txt",
        "source_relpath": "CONFIG.TXT",
        "file_hash_sha256": "abc123",
        "file_hash_sha1": "def456",
    }
    entries = [record, dict(record)]

    removed = deduplicate_exact_storage_entries(entries)

    assert len(removed) == 1
    assert entries == [record]


def test_conflicting_storage_collision_is_never_auto_repaired():
    entries = [
        {
            "device_label": "p1_BD",
            "new_filename": "config.txt",
            "source_relpath": "CONFIG.TXT",
            "file_hash_sha256": "first",
        },
        {
            "device_label": "p1_BD",
            "new_filename": "config.txt",
            "source_relpath": "CONFIG.TXT",
            "file_hash_sha256": "different",
        },
    ]

    with pytest.raises(ValueError, match="Conflicting duplicate"):
        deduplicate_exact_storage_entries(entries)


def test_inventory_by_source_relpath_is_device_scoped_and_unique():
    p1 = {
        "device_label": "p1_BD",
        "source_relpath": "20260420_000000.WAV",
    }
    p2 = {
        "device_label": "p2_BD",
        "source_relpath": "20260420_000000.WAV",
    }
    assert inventory_by_source_relpath([p1, p2], "p1_BD") == {
        "20260420_000000.WAV": p1
    }


def test_next_plain_sequence_uses_max_suffix_not_record_count():
    entries = [
        {"device_label": "p1_BD", "new_filename": "UC_S_plot1_BD_20260710_00001.wav"},
        {"device_label": "p1_BD", "new_filename": "UC_S_plot1_BD_20260710_00014.wav"},
        {"device_label": "p2_BD", "new_filename": "UC_S_plot2_BD_20260710_00099.wav"},
        {"device_label": "p1_BD", "new_filename": "UC_S_plot1_BD_20260305_CONFIG_01.txt"},
    ]

    assert next_plain_file_sequence(entries, "p1_BD") == 15


def test_write_session_reports_success(tmp_path):
    assert write_session(tmp_path, {"schema_version": 1}) == ""
    assert json.loads((tmp_path / "session.json").read_text()) == {
        "schema_version": 1
    }


def test_write_session_reports_replace_failure(tmp_path, monkeypatch):
    def fail_replace(_self, _target):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(type(tmp_path), "replace", fail_replace)

    error = write_session(tmp_path, {"schema_version": 1})

    assert "Could not save session recovery file" in error
    assert "Input/output error" in error


def test_find_sessions_sorts_mixed_timezone_timestamp_styles(tmp_path):
    older = tmp_path / "older"
    newer = tmp_path / "newer"
    older.mkdir()
    newer.mkdir()
    write_session(
        older,
        {
            "schema_version": 1,
            "deployment_folder": str(older),
            "saved_at": "2026-08-27T11:00:00",
        },
    )
    write_session(
        newer,
        {
            "schema_version": 1,
            "deployment_folder": str(newer),
            "saved_at": "2026-08-27T19:05:00+00:00",
        },
    )

    sessions = find_all_sessions(tmp_path)

    assert [session["path"].parent.name for session in sessions] == [
        "newer",
        "older",
    ]


def test_reconcile_device_dir_removes_only_orphans(tmp_path):
    dev = tmp_path / "p2_ML"
    dev.mkdir()
    # Two files the inventory knows about (from the last persisted save)...
    (dev / "UC_S_plot2_ML_20260609_00001_1.jpg").write_bytes(b"x")
    (dev / "UC_S_plot2_ML_20260609_00001_2.jpg").write_bytes(b"x")
    # ...and two staged but never recorded before the crash (the orphans).
    (dev / "UC_S_plot2_ML_20260609_00002_1.jpg").write_bytes(b"x")
    (dev / "UC_S_plot2_ML_20260609_00002_2.jpg").write_bytes(b"x")
    # A retired legacy manifest and a dotfile that must never be touched.
    (dev / "p2_ML_manifest.json").write_bytes(b"{}")
    (dev / ".DS_Store").write_bytes(b"x")

    inv = [
        {"device_label": "p2_ML", "new_filename": "UC_S_plot2_ML_20260609_00001_1.jpg"},
        {"device_label": "p2_ML", "new_filename": "UC_S_plot2_ML_20260609_00001_2.jpg"},
        {"device_label": "p1_ML", "new_filename": "other_device_file.jpg"},  # different device
    ]

    removed = reconcile_device_dir(dev, inv, "p2_ML")

    assert sorted(removed) == [
        "UC_S_plot2_ML_20260609_00002_1.jpg",
        "UC_S_plot2_ML_20260609_00002_2.jpg",
    ]
    survivors = {p.name for p in dev.iterdir()}
    assert survivors == {
        "UC_S_plot2_ML_20260609_00001_1.jpg",
        "UC_S_plot2_ML_20260609_00001_2.jpg",
        "p2_ML_manifest.json",
        ".DS_Store",
    }


def test_reconcile_device_dir_noops_when_clean(tmp_path):
    dev = tmp_path / "p2_ML"
    dev.mkdir()
    (dev / "UC_S_plot2_ML_20260609_00001_1.jpg").write_bytes(b"x")
    inv = [{"device_label": "p2_ML", "new_filename": "UC_S_plot2_ML_20260609_00001_1.jpg"}]

    assert reconcile_device_dir(dev, inv, "p2_ML") == []
    assert (dev / "UC_S_plot2_ML_20260609_00001_1.jpg").exists()


def test_reconcile_device_dir_missing_dir_is_safe(tmp_path):
    # First-ever copy: the device folder may not exist yet.
    assert reconcile_device_dir(tmp_path / "nope", [], "p2_ML") == []


def test_refresh_legacy_manifest_does_not_create_new_file(tmp_path):
    dev = tmp_path / "p1_ML"
    dev.mkdir()

    assert not refresh_legacy_device_manifest("p1_ML", dev, [])
    assert not (dev / "p1_ML_manifest.json").exists()


def test_refresh_legacy_manifest_updates_existing_file(tmp_path):
    dev = tmp_path / "p1_ML"
    dev.mkdir()
    manifest_path = dev / "p1_ML_manifest.json"
    manifest_path.write_text("{}")
    entries = [{
        "new_filename": "image.jpg",
        "file_size_bytes": 123,
        "file_hash_sha256": "sha256-value",
        "file_hash_sha1": "sha1-value",
    }]

    assert refresh_legacy_device_manifest("p1_ML", dev, entries)
    manifest = json.loads(manifest_path.read_text())
    assert manifest["device_label"] == "p1_ML"
    assert manifest["file_count"] == 1
    assert manifest["files"] == [{
        "filename": "image.jpg",
        "size_bytes": 123,
        "sha256": "sha256-value",
        "sha1": "sha1-value",
    }]
