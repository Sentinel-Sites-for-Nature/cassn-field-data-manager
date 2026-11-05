"""Exercise the actual helper process boundary for independent operator requests."""

from cassn.core.wi_requests import scope_key
from cassn.gui.wi_upload import ACTIVE_POLL_MS, QUEUE_POLL_MS
from test_wi_upload_gui import prepare, wait_for, work


def test_one_poll_updates_every_unfinished_request_including_multicamera_selection(work, tmp_path):
    window = work()
    prepare(window)
    control = window.wi_control
    control.refresh()

    def scope(suffix):
        return {"deployment": f"camera_{suffix}", "images": 10, "selection_sha256": str(suffix) * 64}

    for group in ([scope(1)], [scope(2), scope(3)], [scope(4)]):
        key = scope_key(control.folder.name, group)
        control.jobs[key] = {"scopes": group}
        (tmp_path / key).write_text("accepted")
    control.render()
    assert control.timer.interval() == QUEUE_POLL_MS

    # Stop subsequent automatic ticks. All four cameras must receive fresh
    # status from one sweep alone, even when submitted in three separate clicks.
    control.timer.stop()
    control.poll()
    wait_for(lambda: sum(len(j.get("rows", [])) for j in control.jobs.values()) == 4)
    assert control.timer.interval() == ACTIVE_POLL_MS
    assert (tmp_path / "calls").read_text().splitlines() == ["status"] * 3
    assert all(row["confirmed"] == 10 and row["stage"] == "uploading"
               for job in control.jobs.values() for row in job["rows"])
    assert not control.can_dismiss()
    control.timer.stop()

    # An older but valid snapshot retains the previously seen journal count.
    key, job = next(iter(control.jobs.items()))
    old = dict(job["rows"][0], confirmed=3)
    control.accept_status(key, {"rows": [old]})
    assert control.jobs[key]["rows"][0]["confirmed"] == 10
    assert control.jobs[key]["rows"][0]["stage"] == "uploading"
