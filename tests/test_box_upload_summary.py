"""The deployment summary must exist before the Box uploader snapshots files."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from cassn.gui import wizard as wizard_module
from cassn.gui.wizard import FieldDataWizard


def test_box_upload_writes_summary_before_uploader_starts(tmp_path, monkeypatch):
    deployment = tmp_path / "UC_Test_20260902"
    (deployment / "qc").mkdir(parents=True)
    summary = deployment / "qc" / "deployment_summary.txt"
    seen_at_construction = []

    class _FakeUploadThread:
        def __init__(self, folder, *_args, **_kwargs):
            seen_at_construction.append(summary.is_file())
            self.progress = MagicMock()
            self.finished = MagicMock()

        def start(self):
            pass

    monkeypatch.setattr(wizard_module, "BoxUploadThread", _FakeUploadThread)
    context = SimpleNamespace(
        current_deployment_folder=deployment,
        metadata={"site_name": "Test Reserve", "observer": "Tester"},
        file_inventory=[],
        devices=[],
        box_config={},
        upload_progress_bar=MagicMock(),
        upload_status_label=MagicMock(),
        cancel_upload_btn=MagicMock(),
        log=MagicMock(),
        _valid_site_names=lambda: None,
        on_upload_progress=MagicMock(),
        on_upload_finished=MagicMock(),
    )

    FieldDataWizard._start_box_upload_thread(context)

    assert seen_at_construction == [True]
    assert "CA-SSN DEPLOYMENT SUMMARY" in summary.read_text()
