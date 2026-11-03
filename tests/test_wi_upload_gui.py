"""Private-helper boundary exercised with an isolated executable and synthetic data."""
import json
import sys
import time

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QTreeWidget

from cassn.box.auth import BoxConfig
from cassn.box.client import BoxStorage
from cassn.core.staging_cleanup import inspect_deployment_for_cleanup
from cassn.core.wi_requests import STATE_FILE, checked_status, read_requests
from cassn.gui import wizard
from cassn.gui.preview import sample_lookups


FAKE = '''
import hashlib,json,sys
from pathlib import Path
op=sys.argv[1]
req=json.load(sys.stdin)
root=Path(__file__).parent
with (root/'calls').open('a') as f: print(op,file=f)
out={k:req.get(k) for k in ('version','event','request_id')}
out['fresh']=not (root/'stale').exists()
if op=='preview':
    out['scopes']=[dict(s,ready=True,selection_sha256=hashlib.sha256(s['deployment'].encode()).hexdigest()) for s in req['deployments']]
elif op=='status' and not (root/req['request_id']).exists():
    out['missing']=True
else:
    if op=='submit':
        (root/req['request_id']).write_text('accepted')
        if (root/'lose-reply').exists(): sys.exit(1)
    stage=(root/'stage').read_text()
    rows=[]
    for s in req['scopes']:
        row=dict(deployment=s['deployment'],selection_sha256=s['selection_sha256'],manifest_sha256='a'*64,stage=stage,confirmed=s['images'])
        if stage=='verified':
            row['integrity']=dict(kind='integrity_verified_image_batch',manifest_sha256='a'*64,image_count=s['images'],receipt_sha256='b'*64)
        rows.append(row)
    out['rows']=rows
print(json.dumps(out))
'''


@pytest.fixture
def work(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    helper = tmp_path / "synthetic-helper"
    helper.write_text(f"#!{sys.executable}\n" + FAKE)
    helper.chmod(0o700)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"wi_upload_helper": str(helper), "staging_root": str(tmp_path)}))
    (tmp_path / "stage").write_text("uploading")
    monkeypatch.setattr(wizard, "CONFIG_JSON", config)
    cls = wizard.FieldDataWizard
    monkeypatch.setattr(cls, "find_all_sessions", lambda self: [])
    monkeypatch.setattr(cls, "check_box_auth", lambda self: False)
    monkeypatch.setattr(cls, "_soundhub_staging_root", lambda self: tmp_path / "audio")
    windows = []

    def create(profile="uc"):
        w = cls(lookups=sample_lookups(), box_config=BoxConfig(profile=profile))
        windows.append(w)
        w._card_ejection_timer.stop()
        return w

    yield create
    for w in windows:
        w.current_deployment_folder = None
        w._box_operation_active = False
        w.close()
    app.processEvents()


def wait_for(predicate):
    limit = time.monotonic() + 6
    while time.monotonic() < limit:
        QApplication.processEvents()
        if predicate():
            return
        time.sleep(.005)
    assert predicate()


def prepare(window):
    for plot, slots in window.device_checkboxes.items():
        for placements in slots.values():
            for _, box in placements:
                box.setChecked(plot == 1)
    window.validate_and_next()
    for _, _, code, label in window.devices:
        window._device_item(label).setText(2, "Complete")
        for n in range(2):
            window.file_inventory.append({
                "device_label": label, "device_type": code, "deployment_id": f"full-deployment-{label}",
                "storage_relpath": f"raw_data/{label}/{label}_{n+1}/image{n}.jpg",
                "file_type": "image" if code in {"ML","SA"} else "audio",
            })
    window.save_session()


def start(control, decision):
    inspected = []

    def click():
        dlg = QApplication.activeModalWidget()
        if not isinstance(dlg, QDialog):
            QTimer.singleShot(10, click)
            return
        tree = dlg.findChild(QTreeWidget)
        inspected.extend(tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount()))
        dlg.findChild(QDialogButtonBox).button(decision).click()

    QTimer.singleShot(10, click)
    control.action.trigger()
    wait_for(lambda: inspected and control.process is None)
    return inspected


def test_one_plot_concurrent_reopen_and_receipt(work, tmp_path):
    first = work()
    prepare(first)
    control = first.wi_control
    first._set_box_controls_busy(True)
    first.soundhub_group.show()
    first.upload_group.show()
    names = start(control, QDialogButtonBox.Cancel)
    assert set(names) == {"full-deployment-p1_ML", "full-deployment-p1_SA"}
    folder = first.current_deployment_folder
    assert not read_requests(folder)
    (tmp_path / "lose-reply").touch()
    start(control, QDialogButtonBox.Ok)
    assert len(read_requests(folder)) == 1
    assert "unknown" in control.notice.text()
    assert not BoxStorage.should_upload_file(folder / STATE_FILE)
    assert not inspect_deployment_for_cleanup(folder, tmp_path, None, "", force=True).clearable
    assert not first.upload_group.isHidden() and not first.soundhub_group.isHidden()

    first.current_deployment_folder = None
    first._box_operation_active = False
    first.close()
    again = work()
    again.validate_and_next()
    ctrl = again.wi_control
    assert ctrl.tree.topLevelItemCount() == 2
    (tmp_path / "stage").write_text("verifying")
    ctrl.poll()
    wait_for(lambda: ctrl.process is None)
    assert ctrl.tree.itemWidget(ctrl.tree.topLevelItem(0), 1).text() == "Verifying originals…"
    (tmp_path / "stage").write_text("verified")
    ctrl.poll()
    wait_for(lambda: ctrl.process is None)
    assert "upload verified" in ctrl.tree.itemWidget(ctrl.tree.topLevelItem(0), 1).format()
    assert inspect_deployment_for_cleanup(folder, tmp_path, None, "", force=True).clearable
    again.wi_control.preview()
    assert "No new" in ctrl.notice.text()
    assert (tmp_path / "calls").read_text().splitlines().count("submit") == 1
    again._navigate_workspace(0)
    again.select_all_devices()
    again.validate_and_next()
    assert ctrl.selection() == {}
    again._device_item("p2_ML").setText(2, "Complete")
    again.file_inventory.append(dict(device_label="p2_ML", file_type="image", deployment_id="new-deployment"))
    assert ctrl.selection() == {"new-deployment": 1}


@pytest.mark.parametrize("bad", ["missing", "count", "manifest", "duplicate"])
def test_verified_requires_exact_original_receipt(bad):
    scope = dict(deployment="example", images=12, selection_sha256="d"*64)
    proof = dict(kind="integrity_verified_image_batch", image_count=12,
                 manifest_sha256="a"*64, receipt_sha256="b"*64)
    row = dict(deployment="example", selection_sha256="d"*64, manifest_sha256="a"*64, stage="verified", confirmed=12, integrity=proof)
    rows = [row]
    if bad == "missing":
        del row["integrity"]
    elif bad == "count":
        proof["image_count"] = 11
    elif bad == "manifest":
        proof["manifest_sha256"] = "c"*64
    else:
        rows *= 2
    with pytest.raises(ValueError):
        checked_status([scope], rows)


def test_hidden_unconfigured_or_csu(work, tmp_path):
    assert not hasattr(work("csu"), "wi_control")
    assert not hasattr(work("legacy"), "wi_control")
    (tmp_path / "config.json").write_text("{}")
    assert not hasattr(work(), "wi_control")


def test_cleanup_and_submission_exclude_each_other(work, tmp_path, monkeypatch):
    from cassn.core import staging_cleanup, wi_requests

    window = work()
    prepare(window)
    folder = window.current_deployment_folder
    scope = dict(deployment="example", images=1, selection_sha256="d"*64)
    key = wi_requests.scope_key(folder.name, [scope])
    jobs = {key: {"scopes": [scope]}}
    plan = inspect_deployment_for_cleanup(folder, tmp_path, None, "", force=True)

    # A preflighted cleanup cannot enter while intent is being published.
    original = wi_requests.atomic_json
    def during_save(path, payload):
        with pytest.raises(staging_cleanup.StagingCleanupError, match="busy"):
            staging_cleanup.clear_verified_deployment(plan)
        original(path, payload)
    monkeypatch.setattr(wi_requests, "atomic_json", during_save)
    wi_requests.save_requests(folder, jobs)
    with pytest.raises(staging_cleanup.StagingCleanupError, match="Retain"):
        staging_cleanup.clear_verified_deployment(plan)

    # Separate scratch event: cleanup wins first. Submission cannot recreate it.
    empty = tmp_path / "other-event"
    empty.mkdir()
    removing = inspect_deployment_for_cleanup(empty, tmp_path, None, "", force=True)
    remove = staging_cleanup.shutil.rmtree
    def during_removal(path):
        with pytest.raises(ValueError, match="busy"):
            wi_requests.save_requests(path, jobs)
        remove(path)
    monkeypatch.setattr(staging_cleanup.shutil, "rmtree", during_removal)
    staging_cleanup.clear_verified_deployment(removing)
    with pytest.raises(ValueError, match="unavailable"):
        wi_requests.save_requests(empty, jobs)
    assert not empty.exists()


def test_stale_status_and_missing_acknowledged_request(work, tmp_path):
    win = work()
    prepare(win)
    ctrl = win.wi_control
    start(ctrl, QDialogButtonBox.Ok)
    key, = ctrl.jobs
    (tmp_path / "stale").touch()
    ctrl.poll()
    wait_for(lambda: ctrl.process is None)
    assert ctrl.tree.itemWidget(ctrl.tree.topLevelItem(0), 1).format() == "Status unknown"
    (tmp_path / "stale").unlink()
    (tmp_path / key).unlink()
    ctrl.poll()
    wait_for(lambda: ctrl.process is None)
    assert ctrl.retry_key is None
    assert "acknowledged request" in ctrl.notice.text()


def test_retry_after_unacknowledged_request_uses_original_key(work, tmp_path):
    win = work()
    prepare(win)
    ctrl = win.wi_control
    (tmp_path / "lose-reply").touch()
    start(ctrl, QDialogButtonBox.Ok)
    key, = ctrl.jobs
    (tmp_path / key).unlink()  # Simulate a proven absence before admission.
    (tmp_path / "lose-reply").unlink()
    ctrl.poll()
    wait_for(lambda: ctrl.process is None)
    assert ctrl.retry_key == key
    assert (tmp_path / "calls").read_text().splitlines().count("submit") == 1
    ctrl.retry_button.click()
    wait_for(lambda: ctrl.process is None)
    assert (tmp_path / key).exists()
    assert list(read_requests(win.current_deployment_folder)) == [key]
