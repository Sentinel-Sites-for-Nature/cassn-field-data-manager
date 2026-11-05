"""Selected evidence, safe file access and progressive disclosure in the QC dialog."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from cassn.core.qc_review import collection_review_findings
from cassn.core.quality_control import qc_issue
from cassn.gui.qc_review import CollectionReviewDialog


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def findings():
    file = {"device_label": "p4_ML", "filename": "bad.jpg"}
    entries = [
        {"check": "file_size_floor", "_device": "p4_ML", "severity": "warning",
         "description": "The long policy belongs in technical details.", "message": "Original size diagnostic",
         "findings": [qc_issue("small_file", "warning", "Unusually small file", "Only 10 bytes.", "Inspect the file.", files=[file])]},
        {"check": "camera_timezone_consistency", "_device": "p4_ML", "severity": "warning",
         "message": "Original offset diagnostic",
         "findings": [qc_issue("resolved_camera_offset", "info", "Timezone interpretation resolved", "Pacific civil time used.",
                               "Clock accuracy is not verified.", count=100_000, unit="photos")]},
    ]
    return collection_review_findings(entries, [])


def test_default_view_shows_unusual_evidence_and_hides_policy(app, tmp_path):
    dialog = CollectionReviewDialog(findings(), [], tmp_path)
    try:
        assert dialog.tree.topLevelItemCount() == 2
        assert "bad.jpg" in dialog.details.toPlainText()
        assert "10 bytes" in dialog.details.toPlainText()
        assert "long policy" not in dialog.details.toPlainText()
        dialog.tree.setCurrentItem(dialog.tree.topLevelItem(1))
        assert dialog.file_picker.count() == 0
        assert "100,000" in dialog.tree.currentItem().text(1)
        assert "bad.jpg" not in dialog.details.toPlainText()
        assert not dialog.open_file.isEnabled()
    finally:
        dialog.close()


def test_open_uses_current_split_inventory_path_and_requires_explicit_click(app, tmp_path, monkeypatch):
    row = {"device_label": "p4_ML", "new_filename": "bad.jpg",
           "storage_relpath": "raw_data/p4_ML/p4_ML_2/bad.jpg"}
    path = tmp_path / row["storage_relpath"]
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic")
    opened = []
    monkeypatch.setattr("cassn.gui.qc_review.QDesktopServices.openUrl", lambda url: opened.append(url.toLocalFile()) or True)
    dialog = CollectionReviewDialog(findings(), [row], tmp_path)
    try:
        assert not opened
        assert dialog.open_file.isEnabled()
        dialog.open_file.click()
        assert opened == [str(path)]
    finally:
        dialog.close()


def test_early_stop_opens_only_affected_audio_folder(app, tmp_path, monkeypatch):
    def audio(name, reason):
        return {"device_label": "p3_BD", "file_type": "audio", "new_filename": name,
                "storage_relpath": f"raw_data/p3_BD/{name}", "recording_stop_reason": reason}

    rows = [audio("early.wav", "low voltage"), audio("normal.wav", "File size limit")]
    folder = tmp_path / "raw_data/p3_BD"
    folder.mkdir(parents=True)
    entry = {"_device": "p3_BD", "check": "recording_stop_reason", "severity": "warning",
             "message": "1 recording(s) stopped due to: low voltage"}
    dialog = CollectionReviewDialog(collection_review_findings([entry], rows), rows, tmp_path)
    opened = []
    monkeypatch.setattr("cassn.gui.qc_review.QDesktopServices.openUrl", lambda url: opened.append(url.toLocalFile()) or True)
    try:
        assert dialog.file_picker.count() == 1 and "early.wav" in dialog.file_picker.currentText()
        assert dialog.open_file.text() == "Open folder"
        dialog.open_file.click()
        assert opened == [str(folder)]
    finally:
        dialog.close()


@pytest.mark.parametrize("stored", ["../outside/bad.jpg", "/tmp/bad.jpg", "raw_data/p1_ML/bad.jpg"])
def test_invalid_inventory_paths_cannot_be_opened(app, tmp_path, stored):
    row = {"device_label": "p4_ML", "new_filename": "bad.jpg", "storage_relpath": stored}
    dialog = CollectionReviewDialog(findings(), [row], tmp_path)
    try:
        assert not dialog.open_file.isEnabled()
    finally:
        dialog.close()


def test_acknowledgement_persists_only_for_unchanged_evidence(app, tmp_path):
    saved = set()
    def save(keys):
        saved.update(keys)
        return True

    rows = findings()
    dialog = CollectionReviewDialog(rows, [], tmp_path, save_acknowledgements=save)
    assert not dialog.proceed.isEnabled()
    dialog.acknowledge.click()
    assert dialog.proceed.isEnabled()
    assert dialog.tree.currentItem().text(2) == "Acknowledged"
    dialog.close()
    reopened = CollectionReviewDialog(rows, [], tmp_path, acknowledged=saved)
    assert reopened.proceed.isEnabled()
    reopened.close()
    rows[0]["message"] += " A second file now needs review."
    changed = CollectionReviewDialog(rows, [], tmp_path, acknowledged=saved,
                                    save_acknowledgements=lambda keys: False)
    assert not changed.proceed.isEnabled()
    changed.acknowledge.click()
    assert not changed.proceed.isEnabled()
    changed.close()


def test_low_voltage_groups_devices_and_keeps_their_files(app, tmp_path):
    entries, inventory = [], []
    for device in ("p1_BD", "p2_BD", "p3_BD"):
        entries.append({"_device": device, "check": "recording_stop_reason", "severity": "warning",
                        "message": "1 recording(s) stopped due to: low voltage"})
        inventory.append({"device_label": device, "file_type": "audio", "new_filename": device + ".wav",
                          "recording_stop_reason": "low voltage"})
    finding, = collection_review_findings(entries, inventory)
    assert finding["title"] == "Recording stopped early due to low voltage"
    assert finding["affected"] == "p1_BD, p2_BD, p3_BD"
    assert len(finding["files"]) == 3
