"""
File classification and small filesystem-naming helpers.

Pure functions with no I/O beyond reading a path's suffix. The
deployment-specific filename builder lives in :mod:`cassn.core.inventory`
because it depends on per-device sequence state.
"""

from __future__ import annotations

from pathlib import Path

from cassn.config import (
    AUDIO_EXTENSIONS,
    BAT_AUDIO_SIZE_FLOOR_BYTES,
    BIRD_AUDIO_SIZE_FLOOR_BYTES,
    CONFIG_EXTENSIONS,
    IMAGE_EXTENSIONS,
    IMAGE_SIZE_FLOOR_BYTES,
)


def classify_file(filename) -> str:
    """Classify a file as ``"image"``, ``"audio"``, ``"config"`` or ``"other"``."""
    ext = Path(filename).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in CONFIG_EXTENSIONS:
        return "config"
    return "other"


def file_size_floor_for(file_type: str, device_type: str) -> int | None:
    """Return the suspicious-size threshold (bytes) for media types that have one.

    Images use a single floor; audio floors are device-aware (bird vs bat).
    Returns ``None`` when no floor applies (e.g. config files).
    """
    dev_code = (device_type or "").upper()
    if file_type == "image":
        return IMAGE_SIZE_FLOOR_BYTES
    if file_type == "audio" and dev_code == "BD":
        return BIRD_AUDIO_SIZE_FLOOR_BYTES
    if file_type == "audio" and dev_code == "BT":
        return BAT_AUDIO_SIZE_FLOOR_BYTES
    return None


def format_size_floor(size_bytes: int) -> str:
    """Format a byte threshold in human-readable decimal units."""
    if size_bytes >= 1_000_000_000:
        return f"{size_bytes / 1_000_000_000:g} GB"
    if size_bytes >= 1_000_000:
        return f"{size_bytes / 1_000_000:g} MB"
    return f"{size_bytes // 1024} KB"


def sanitize_box_folder_name(folder_name) -> str:
    """Return a Box-safe folder name matching the app's create/find behavior."""
    return str(folder_name).replace("/", "-").replace("\\", "-")
