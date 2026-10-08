from __future__ import annotations

import csv
import io

import pytest

from cassn.config import AUDIO_FIELDS, IMAGE_FIELDS
from utils.rename_osdf_provenance import (
    LEGACY_FIELDS,
    OSDF_FIELDS,
    ProvenanceRenameError,
    apply_file,
    inspect_file,
)


def _payload(*, status="False", uploader="", uploaded_at="") -> bytes:
    fields = [
        "filename",
        "is_uploaded_to_pelican",
        "pelican_uploader",
        "pelican_upload_datetime",
        "notes",
    ]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\r\n")
    writer.writeheader()
    writer.writerow(
        {
            "filename": "recording.wav",
            "is_uploaded_to_pelican": status,
            "pelican_uploader": uploader,
            "pelican_upload_datetime": uploaded_at,
            "notes": "a note with, a comma",
        }
    )
    return b"\xef\xbb\xbf" + stream.getvalue().encode("utf-8")


def test_current_metadata_generates_osdf_not_pelican_provenance_fields():
    for fields in (IMAGE_FIELDS, AUDIO_FIELDS):
        assert OSDF_FIELDS <= set(fields)
        assert not LEGACY_FIELDS.intersection(fields)


def test_preview_then_in_place_apply_changes_only_the_header(tmp_path):
    path = tmp_path / "audio_file_metadata.csv"
    original = _payload()
    path.write_bytes(original)

    plan = inspect_file(path)

    assert plan.needs_change
    assert plan.rows == 1
    assert path.read_bytes() == original

    result = apply_file(plan, in_place=True)
    migrated = path.read_bytes()

    assert result is not None
    assert not inspect_file(path).needs_change
    assert migrated.splitlines(keepends=True)[1:] == original.splitlines(keepends=True)[1:]
    header = migrated.splitlines()[0].decode("utf-8-sig")
    assert "is_uploaded_to_osdf" in header
    assert "pelican" not in header


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "image_file_metadata.csv"
    path.write_bytes(_payload())
    apply_file(inspect_file(path), in_place=True)
    migrated = path.read_bytes()

    plan = inspect_file(path)

    assert not plan.needs_change
    assert apply_file(plan, in_place=True) is None
    assert path.read_bytes() == migrated


@pytest.mark.parametrize(
    ("status", "uploader", "uploaded_at"),
    [
        ("True", "", ""),
        ("False", "Someone", ""),
        ("False", "", "2026-09-03T12:00:00Z"),
    ],
)
def test_used_legacy_provenance_is_refused(
    tmp_path, status, uploader, uploaded_at
):
    path = tmp_path / "image_file_metadata.csv"
    path.write_bytes(
        _payload(status=status, uploader=uploader, uploaded_at=uploaded_at)
    )

    with pytest.raises(ProvenanceRenameError, match="used Pelican"):
        inspect_file(path)


def test_mixed_legacy_and_osdf_schema_is_refused(tmp_path):
    path = tmp_path / "image_file_metadata.csv"
    path.write_bytes(
        _payload().replace(b"pelican_uploader", b"osdf_uploader", 1)
    )

    with pytest.raises(ProvenanceRenameError, match="all three"):
        inspect_file(path)
