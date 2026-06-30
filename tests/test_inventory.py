"""Tests for deterministic card traversal (cassn.core.inventory.sorted_walk)."""
import os

from cassn.core.inventory import sorted_walk


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
