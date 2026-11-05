"""Private-helper boundary exercised with an isolated executable and synthetic data."""
import json
import sys
import time

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLabel, QTreeWidget

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
    if (root/'retry-preview').exists():
        previous=json.loads((root/'retry-preview').read_text())
        for s in out['scopes']:
            if s['deployment'] in previous:s['retry_of']=previous[s['deployment']]
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
        if stage=='held':
            sha=hashlib.sha256((req['request_id']+s['deployment']).encode()).hexdigest()
            row=dict(deployment=s['deployment'],selection_sha256=s['selection_sha256'],request_sha=sha,
                     retry_of=sha,stage='held',confirmed=0,hold_reason='Added deployment held: expected one exact camera 08021607; found 0')
        if stage=='verified':
            row['integrity']=dict(kind='integrity_verified_image_batch',manifest_sha256='a'*64,image_count=s['images'],receipt_sha256='b'*64)
        if stage=='verifying':
            row['verified_count']=int((root/'verified-count').read_text()) if (root/'verified-count').exists() else 1
        if stage=='delivery':
            row=dict(deployment=s['deployment'],selection_sha256=s['selection_sha256'],stage='held',
                     confirmed=0,delivery_pending=True,hold_reason='Delivery is incomplete.')
        row.setdefault('request_sha', hashlib.sha256((req['request_id']+s['deployment']).encode()).hexdigest())
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
    exports = window.current_deployment_folder / "WI_metadata"
    exports.mkdir(exist_ok=True)
    for code, project in (("ML", "2010527"), ("SA", "2010517")):
        (exports / f"wildlife_insights_{code}_deployments.csv").write_text(
            f"deployment_id,project_id\nfull-deployment-p1_{code},{project}\n")
    window.save_session()


def start(control, decision):
    inspected = []

    def click():
        dlg = QApplication.activeModalWidget()
        if not isinstance(dlg, QDialog):
            QTimer.singleShot(10, click)
            return
        tree = dlg.findChild(QTreeWidget)
        logos = [label for label in dlg.findChildren(QLabel) if label.accessibleName() == "Wildlife Insights logo"]
        assert len(logos) == 1 and not logos[0].pixmap().isNull()
        assert logos[0].pixmap().hasAlphaChannel()
        assert not any(label.text() == "Wildlife Insights" for label in dlg.findChildren(QLabel))
        assert any(label.text() == control.folder.name for label in dlg.findChildren(QLabel))
        inspected.extend((tree.topLevelItem(i).text(0), tree.topLevelItem(i).text(1))
                         for i in range(tree.topLevelItemCount()))
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
    assert control.panel_button.text() == "Upload images"
    names = start(control, QDialogButtonBox.Cancel)
    assert dict(names) == {"full-deployment-p1_ML": "UCNRS Mammal",
                           "full-deployment-p1_SA": "UCNRS Small Animal"}
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
    assert ctrl.panel_button.text() == "Continue WI uploads"
    assert ctrl.dismiss_button.isHidden()
    (tmp_path / "stage").write_text("verifying")
    ctrl.poll()
    wait_for(lambda: ctrl.process is None)
    bar = ctrl.bar_for_item(ctrl.tree.topLevelItem(0))
    assert bar.value() == 1 and bar.text() == "Verifying 1 of 2 photos · 50%"
    for count in (2, 0):
        (tmp_path / "verified-count").write_text(str(count))
        ctrl.poll()
        wait_for(lambda: ctrl.process is None)
        assert ctrl.bar_for_item(ctrl.tree.topLevelItem(0)).text() == "Verifying 2 of 2 photos · 100%"
        assert ctrl.dismiss_button.isHidden()
        assert not inspect_deployment_for_cleanup(folder, tmp_path, None, "", force=True).clearable
    (tmp_path / "stage").write_text("verified")
    ctrl.poll()
    wait_for(lambda: ctrl.process is None)
    assert "upload verified" in ctrl.bar_for_item(ctrl.tree.topLevelItem(0)).format()
    assert ctrl.verified_summary.text() == "2 cameras verified · 4 photos"
    assert not ctrl.group.disclosure.isChecked()
    ctrl.group.disclosure.click()
    assert ctrl.tree.isVisibleTo(ctrl.group)
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
    ctrl.refresh_new_cameras()
    assert ctrl.new_cameras.text() == "1 new camera in staging · 1 photo"
    assert ctrl.dismiss_button.isHidden()


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
    assert ctrl.bar_for_item(ctrl.tree.topLevelItem(0)).format() == "Status unknown"
    (tmp_path / "stale").unlink()
    (tmp_path / key).unlink()
    ctrl.poll()
    wait_for(lambda: ctrl.process is None)
    assert not ctrl.delivery_keys
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
    assert key in ctrl.delivery_keys
    assert (tmp_path / "calls").read_text().splitlines().count("submit") == 1
    start(ctrl, QDialogButtonBox.Cancel)
    start(ctrl, QDialogButtonBox.Ok)
    assert (tmp_path / key).exists()
    assert list(read_requests(win.current_deployment_folder)) == [key]


def test_held_camera_retry_requires_confirmation_and_preserves_history(work, tmp_path):
    from cassn.core.wi_requests import require_wi_complete

    window = work()
    prepare(window)
    ctrl = window.wi_control
    (tmp_path / "stage").write_text("held")
    start(ctrl, QDialogButtonBox.Ok)
    original, = ctrl.jobs
    old_job = ctrl.jobs[original]
    previous = {r["deployment"]: r["retry_of"] for r in old_job["rows"]}
    (tmp_path / "retry-preview").write_text(json.dumps(previous))
    assert "Camera 08021607 was missing in WI" in ctrl.tree.topLevelItem(0).text(1)
    assert ctrl.retryable()
    ctrl.unavailable.add(original)
    ctrl.render()
    assert "Last known hold" in ctrl.tree.topLevelItem(0).text(1)
    assert not ctrl.retryable()
    # The menu remains clickable during a poll and opens once the fresh status arrives.
    ctrl.poll()
    assert ctrl.process is not None and ctrl.action.isEnabled()
    start(ctrl, QDialogButtonBox.Cancel)
    assert list(ctrl.jobs) == [original]
    (tmp_path / "stage").write_text("uploading")
    start(ctrl, QDialogButtonBox.Ok)
    replacement, = set(ctrl.jobs) - {original}
    assert ctrl.jobs[original] == old_job
    assert all(s["retry_of"] == previous[s["deployment"]] for s in ctrl.jobs[replacement]["scopes"])
    assert ctrl.tree.topLevelItemCount() == 2
    assert ctrl.selection() == {}
    with pytest.raises(ValueError, match="Retain"):
        require_wi_complete(window.current_deployment_folder)
    (tmp_path / "stage").write_text("verified")
    ctrl.poll()
    wait_for(lambda: ctrl.process is None)
    require_wi_complete(window.current_deployment_folder)
    assert not ctrl.dismiss_button.isHidden()
    assert set(read_requests(window.current_deployment_folder)) == {original, replacement}


def test_partial_retry_cannot_clear_another_held_deployment(tmp_path):
    from cassn.core.wi_requests import require_wi_complete, save_requests, scope_key

    scopes = [dict(deployment=n, images=2, selection_sha256=k*64) for n, k in [("ML","a"), ("SA","b")]]
    held = [dict(deployment=s["deployment"], selection_sha256=s["selection_sha256"], confirmed=0,
                 stage="held", request_sha=k*64) for s,k in zip(scopes, ("c","d"))]
    replacement = [dict(scopes[0], retry_of=held[0]["request_sha"])]
    verified = dict(deployment="ML", selection_sha256="a"*64, stage="verified", confirmed=2,
                    manifest_sha256="e"*64, integrity=dict(kind="integrity_verified_image_batch",
                    manifest_sha256="e"*64, image_count=2, receipt_sha256="f"*64))
    jobs = {scope_key(tmp_path.name, scopes): dict(scopes=scopes, rows=held),
            scope_key(tmp_path.name, replacement): dict(scopes=replacement, rows=[verified])}
    save_requests(tmp_path, jobs)
    with pytest.raises(ValueError, match="Retain"):
        require_wi_complete(tmp_path)


def test_interrupted_inbox_delivery_replays_saved_identity(work, tmp_path):
    window = work()
    prepare(window)
    ctrl = window.wi_control
    (tmp_path / "stage").write_text("delivery")
    start(ctrl, QDialogButtonBox.Ok)
    key, = ctrl.jobs
    assert not ctrl.retryable()
    assert key in ctrl.delivery_keys
    (tmp_path / "stage").write_text("verified")
    start(ctrl, QDialogButtonBox.Cancel)
    start(ctrl, QDialogButtonBox.Ok)
    assert list(ctrl.jobs) == [key] and not ctrl.delivery_keys
    assert all(r["stage"] == "verified" for r in ctrl.jobs[key]["rows"])


def test_dismiss_only_verified_wi_panel_and_restore(work, tmp_path):
    window = work()
    prepare(window)
    ctrl = window.wi_control
    (tmp_path / "stage").write_text("held")
    start(ctrl, QDialogButtonBox.Ok)
    assert ctrl.dismiss_button.isHidden() and not ctrl.group.isHidden()
    ctrl.dismiss()
    assert not ctrl.group.isHidden()
    (tmp_path / "stage").write_text("verified")
    ctrl.poll()
    wait_for(lambda: ctrl.process is None)
    assert not ctrl.dismiss_button.isHidden()
    folder = window.current_deployment_folder
    before = (folder / STATE_FILE).read_bytes()
    calls = (tmp_path / "calls").read_text()
    ctrl.dismiss_button.click()
    ctrl.render()
    assert not ctrl.group.isHidden() and ctrl.dismiss_button.isHidden()
    assert not ctrl.group.disclosure.isChecked()
    assert (folder / STATE_FILE).read_bytes() == before
    assert (tmp_path / "calls").read_text() == calls

    reopened = work()
    reopened.validate_and_next()
    assert not reopened.wi_control.group.isHidden()
    assert not reopened.wi_control.dismiss_button.isHidden()


def test_reopen_shows_verified_pending_held_and_new_rows_without_writing(work, tmp_path):
    from cassn.core.wi_requests import save_requests, scope_key
    from cassn.gui.workspace import UploadProgressBar

    first = work()
    prepare(first)
    folder = first.current_deployment_folder
    # Leave the two completed inventory cameras available for their first request.
    scopes, rows = [], []
    for number, stage in enumerate(("verified", "verified", "held", "verifying"), 1):
        scope = dict(deployment=f"saved-camera-{number}", images=2, selection_sha256=str(number)*64)
        scopes.append(scope)
        row = dict(deployment=scope["deployment"], selection_sha256=scope["selection_sha256"],
                   stage=stage, confirmed=0 if stage == "held" else 2)
        if stage != "held":
            row["manifest_sha256"] = "a"*64
        if stage == "verified":
            row["integrity"] = dict(kind="integrity_verified_image_batch", image_count=2,
                                    manifest_sha256="a"*64, receipt_sha256="b"*64)
        if number == 1:
            row["integrity"]["project_id"] = 2010527
        if stage == "held":
            row["hold_reason"] = "Missing camera"
        rows.append(row)
    jobs = {scope_key(folder.name, scopes): {"scopes": scopes, "rows": rows}}
    save_requests(folder, jobs)
    saved = (folder / STATE_FILE).read_bytes()
    first.current_deployment_folder = None
    first.close()

    opened = work()
    opened.validate_and_next()
    opened.tabs.setCurrentIndex(2)
    opened.show()
    ctrl = opened.wi_control
    ctrl.timer.stop()
    ctrl.refresh_new_cameras()
    assert not ctrl.group.disclosure.isChecked()
    assert ctrl.verified_summary.text() == "2 cameras verified · 4 photos"
    assert "1 camera held for review" in ctrl.active_summary.text()
    assert "1 camera with unknown status" in ctrl.active_summary.text()
    assert ctrl.new_cameras.text() == "2 new cameras in staging · 4 photos"
    assert ctrl.dismiss_button.isHidden()
    ctrl.group.disclosure.click()
    assert ctrl.tree.topLevelItemCount() == 6
    all_rows = [ctrl.tree.topLevelItem(i) for i in range(6)]
    labels = {}
    for item in all_rows:
        cell = ctrl.tree.itemWidget(item, 1)
        labels[item.text(0)] = [label.text() for col in (0, 1) for label in ctrl.tree.itemWidget(item, col).findChildren(QLabel)]
        assert cell.findChild(UploadProgressBar)
    assert "Destination project: UCNRS Mammal" in labels["saved-camera-1"]
    assert "Destination project: unavailable" in labels["saved-camera-2"]
    assert "Destination project: UCNRS Small Animal" in labels["full-deployment-p1_SA"]
    assert ctrl.bar_for_item(next(r for r in all_rows if r.text(0) == "saved-camera-1")).text().endswith("100%")
    for width, zoom in ((1280, 100), (940, 130), (940, 200)):
        opened.workspace.set_zoom(zoom)
        opened.resize(width, 740)
        for _ in range(3):
            QApplication.processEvents()
        assert ctrl.tree.headerItem().text(0) == "Deployment and destination"
        column = 1 if ctrl.tree.isColumnHidden(0) else 0
        identity_labels = ctrl.tree.itemWidget(all_rows[0], column).findChildren(QLabel)
        assert identity_labels[0].text().replace("\u200b", "") == all_rows[0].text(0)
        assert identity_labels[1].text().startswith("Destination project: ")
        assert identity_labels[1].y() > identity_labels[0].y()
        assert ctrl.tree.verticalScrollBar().maximum() == 0
        assert ctrl.tree.viewport().rect().contains(ctrl.tree.visualItemRect(all_rows[-1]))
    assert (folder / STATE_FILE).read_bytes() == saved
    assert not (tmp_path / "calls").exists()


def test_missing_counter_and_unreadable_export_preserve_status(work, tmp_path):
    window = work()
    prepare(window)
    ctrl = window.wi_control
    folder = window.current_deployment_folder
    (tmp_path / "stage").write_text("uploading")
    start(ctrl, QDialogButtonBox.Ok)
    key, = ctrl.jobs
    (folder / "WI_metadata" / "wildlife_insights_ML_deployments.csv").write_bytes(b"\xff\xff")
    rows = [dict(row, stage="verifying") for row in ctrl.jobs[key]["rows"]]
    ctrl.accept_status(key, {"rows": rows})
    ctrl.timer.stop()
    for index in range(ctrl.tree.topLevelItemCount()):
        item = ctrl.tree.topLevelItem(index)
        assert "count unavailable" in ctrl.bar_for_item(item).text()
        labels = [label.text() for col in (0, 1) for label in ctrl.tree.itemWidget(item, col).findChildren(QLabel)]
        project = "Destination project: unavailable" if item.text(0).endswith("ML") else "Destination project: UCNRS Small Animal"
        assert project in labels
    assert all("verified_count" not in row for row in read_requests(folder)[key]["rows"])
    assert ctrl.dismiss_button.isHidden()


def test_only_staged_cameras_render_without_creating_requests(work, tmp_path):
    window = work()
    prepare(window)
    ctrl = window.wi_control
    ctrl.refresh_new_cameras()
    assert not ctrl.group.isHidden() and not ctrl.group.disclosure.isChecked()
    assert ctrl.tree.topLevelItemCount() == 2
    assert ctrl.new_cameras.text() == "2 new cameras in staging · 4 photos"
    assert ctrl.verified_summary.isHidden() and ctrl.dismiss_button.isHidden()
    assert not ctrl.timer.isActive()
    assert not (ctrl.folder / STATE_FILE).exists()
    assert not (tmp_path / "calls").exists()


@pytest.mark.parametrize(("menu_index", "project"), [(0, "2010527"), (1, "2010517")])
def test_web_button_selects_the_exported_project_without_submitting(work, tmp_path, monkeypatch, menu_index, project):
    window = work()
    prepare(window)
    ctrl = window.wi_control
    ctrl.refresh()
    opened = []
    monkeypatch.setattr("cassn.gui.wi_upload.open_platform_url", lambda _, url: opened.append(url))
    from PySide6.QtWidgets import QMenu
    ctrl.web_button.click()
    menu = ctrl.web_button.findChild(QMenu)
    assert [a.text() for a in menu.actions()] == ["UCNRS Mammal", "UCNRS Small Animal"]
    menu.actions()[menu_index].trigger()
    menu.close()
    assert opened == [f"https://app.wildlifeinsights.org/manage/organizations/2001427/projects/{project}/uploads"]
    assert not (tmp_path / "calls").exists()


def test_verified_without_a_receipt_cannot_be_dismissed(work):
    window = work()
    prepare(window)
    ctrl = window.wi_control
    scopes = [dict(deployment="full-deployment-p1_" + code, images=2, selection_sha256="a"*64)
              for code in ("ML", "SA")]
    ctrl.jobs = {"invalid": dict(scopes=scopes, rows=[dict(scope, stage="verified", confirmed=2,
                                                           manifest_sha256="b"*64) for scope in scopes])}
    ctrl.render()
    assert ctrl.verified_summary.isHidden()
    assert ctrl.active_summary.text() == "2 cameras with unknown status"
    assert not ctrl.can_dismiss()
