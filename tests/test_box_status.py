"""Tests for conservative prior-Box-activity detection."""

from cassn.box.status import has_box_upload_history


def test_no_box_upload_history_for_fresh_deployment(tmp_path):
    assert not has_box_upload_history(tmp_path)
    assert not (tmp_path / "qc").exists()


def test_box_manifest_marks_upload_history(tmp_path):
    qc_dir = tmp_path / "qc"
    qc_dir.mkdir()
    (qc_dir / "box_upload_manifest.json").write_text("{}")

    assert has_box_upload_history(tmp_path)


def test_legacy_root_box_manifest_marks_upload_history(tmp_path):
    (tmp_path / "box_upload_manifest.json").write_text("{}")

    assert has_box_upload_history(tmp_path)


def test_metadata_provenance_marks_upload_history(tmp_path):
    (tmp_path / "image_file_metadata.csv").write_text(
        "filename,is_uploaded_to_box\nimage.jpg,True\n"
    )

    assert has_box_upload_history(tmp_path)


def test_in_memory_upload_completion_marks_history(tmp_path):
    assert has_box_upload_history(tmp_path, current_upload_complete=True)
