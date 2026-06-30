"""Tests for card-scan helpers (cassn.core.inventory)."""
import os

from cassn.core.inventory import (
    already_copied_relpaths,
    count_expected_files,
    sorted_walk,
)


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
