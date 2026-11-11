"""A lost Box sign-in stops the upload early and offers to reconnect and resume."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

from cassn.box import threads
from cassn.box.client import BoxStorage
from cassn.box.hosted import HostedError
from cassn.gui import wizard as wizard_module
from cassn.gui.wizard import FieldDataWizard
from tests.test_box_metadata_publication import FakeBox, event, payload, row


def test_sign_in_loss_is_detected_through_wrapped_exceptions():
    assert threads._is_sign_in_loss(HostedError("reconnect"))
    assert threads._is_sign_in_loss(HostedError("denied"))
    wrapped = RuntimeError("upload failed")
    wrapped.__cause__ = HostedError("reconnect")
    assert threads._is_sign_in_loss(wrapped)
    assert threads._is_sign_in_loss(RuntimeError(str(HostedError("reconnect"))))
    assert not threads._is_sign_in_loss(HostedError("network"))
    assert not threads._is_sign_in_loss(RuntimeError("409 already exists"))


def test_lost_sign_in_stops_batch_with_one_clear_message(tmp_path, monkeypatch):
    api = FakeBox()
    api.objects["year"] = {"type": "folder", "name": "2026", "parent": "root"}
    api.objects["reserve"] = {"type": "folder", "name": "Test", "parent": "year"}
    api.objects["event"]["parent"] = "reserve"
    folder = tmp_path / "event"
    rows, inventory = [], []
    for index in range(6):
        name = f"new{index}.wav"
        raw_file = folder / "raw_data/new" / name
        raw_file.parent.mkdir(parents=True, exist_ok=True)
        raw_file.write_bytes(b"recording")
        item = row(name, deployment="new")
        rows.append(item)
        inventory.append({"new_filename": name, "device_label": "new", "file_size_bytes": 9,
                          "file_hash_sha1": item["file_hash_sha1"],
                          "file_hash_sha256": item["file_hash_sha256"]})
    (folder / "audio_file_metadata.csv").write_bytes(payload(rows))
    (folder / "deployment_event_record.json").write_text(json.dumps(event("new", count=6)))
    metadata = event("new", count=6)["deployment_info"]

    attempts = []
    real_upload = BoxStorage.upload_file_with_path

    def failing_upload(self, file_path, deploy_id, rel_path):
        if "raw_data" in rel_path.parts:
            attempts.append(rel_path.as_posix())
            raise HostedError("reconnect")
        return real_upload(self, file_path, deploy_id, rel_path)

    monkeypatch.setattr(BoxStorage, "upload_file_with_path", failing_upload)
    monkeypatch.setattr(threads, "get_box_client", lambda config: api)
    upload = threads.BoxUploadThread(folder, metadata, inventory, box_config=NS(field_data_folder_id="root"),
                                     valid_site_names={"Test"}, upload_workers=1)
    results = []
    upload.finished.connect(lambda *args: results.append(args))
    upload.run()

    success, message = results[-1]
    assert not success
    assert upload.needs_reconnect
    assert upload.is_cancelled  # No further files were scheduled after the first refusal.
    assert len(attempts) < 6
    assert "Box disconnected during the upload" in message
    assert "Connect to Box" in message
    assert "new0.wav" not in message  # No per-file failure dump.
    qc = json.loads((folder / "qc" / "qc_report.json").read_text())
    assert any("Box sign-in ended" in entry["message"] for entry in qc["current_state"])


class _FakeDialog:
    instances = []
    choice = "reconnect"

    def __init__(self, icon, title, text, buttons, parent):
        self.title, self.text, self.buttons = title, text, []
        _FakeDialog.instances.append(self)

    def addButton(self, label, role):
        button = NS(label=label, role=role)
        self.buttons.append(button)
        return button

    def setDefaultButton(self, button):
        self.default = button

    def exec(self):
        return 0

    def clickedButton(self):
        return self.buttons[0] if _FakeDialog.choice == "reconnect" else self.buttons[-1]


def _context(needs_reconnect, connected=True):
    context = NS(
        _box_callback_is_current=lambda name: True,
        upload_thread=NS(needs_reconnect=needs_reconnect),
        set_box_connection_state=MagicMock(),
        connect_to_box=MagicMock(return_value=connected),
        upload_to_box=MagicMock(),
        _update_copy_action=MagicMock(),
        box_authenticated=True, box_upload_complete=False, _box_operation_active=True,
    )
    for name in ("upload_group", "box_count_retry", "cancel_upload_btn", "upload_progress_bar", "upload_status_label", "open_btn",
                 "switch_deployment_btn", "exit_btn", "upload_now_btn", "new_btn",
                 "box_verify_btn", "fixity_btn"):
        setattr(context, name, MagicMock())
    context._offer_box_reconnect = lambda message: FieldDataWizard._offer_box_reconnect(context, message)
    return context


def test_lost_sign_in_marks_box_disconnected_and_resumes_after_reconnect(monkeypatch):
    _FakeDialog.instances.clear()
    _FakeDialog.choice = "reconnect"
    monkeypatch.setattr(wizard_module, "QMessageBox", _FakeDialog)
    for attr in ("Warning", "NoButton", "AcceptRole", "RejectRole"):
        monkeypatch.setattr(_FakeDialog, attr, attr, raising=False)
    context = _context(needs_reconnect=True)

    FieldDataWizard.on_upload_finished(context, False, "Box disconnected during the upload.")

    context.set_box_connection_state.assert_called_once_with(False)
    dialog = _FakeDialog.instances[-1]
    assert dialog.title == "Box Disconnected"
    assert [button.label for button in dialog.buttons] == ["Connect and Resume Upload", "Close"]
    context.connect_to_box.assert_called_once()
    context.upload_to_box.assert_called_once()


def test_declined_or_failed_reconnect_does_not_restart_upload(monkeypatch):
    monkeypatch.setattr(wizard_module, "QMessageBox", _FakeDialog)
    for attr in ("Warning", "NoButton", "AcceptRole", "RejectRole"):
        monkeypatch.setattr(_FakeDialog, attr, attr, raising=False)

    _FakeDialog.choice = "close"
    context = _context(needs_reconnect=True)
    FieldDataWizard.on_upload_finished(context, False, "Box disconnected during the upload.")
    context.connect_to_box.assert_not_called()
    context.upload_to_box.assert_not_called()

    _FakeDialog.choice = "reconnect"
    context = _context(needs_reconnect=True, connected=False)
    FieldDataWizard.on_upload_finished(context, False, "Box disconnected during the upload.")
    context.connect_to_box.assert_called_once()
    context.upload_to_box.assert_not_called()


def test_other_upload_failures_keep_the_plain_warning(monkeypatch):
    warning = MagicMock()
    monkeypatch.setattr(wizard_module.QMessageBox, "warning", warning)
    context = _context(needs_reconnect=False)
    FieldDataWizard.on_upload_finished(context, False, "Upload error: disk full")
    warning.assert_called_once()
    context.set_box_connection_state.assert_not_called()
    context.upload_to_box.assert_not_called()
