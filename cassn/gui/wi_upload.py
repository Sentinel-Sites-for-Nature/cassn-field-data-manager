"""Optional private helper protocol v1. Run one short command per GUI request.

The executable receives preview, submit or status as argv[1], and one JSON
object on stdin. Echo version, event and request_id in each JSON response.
Preview is read-only and pins immutable manifests. Submit must durably accept
the exact approved scopes, idempotently by request_id and selection. Status is
read-only; it must resolve fresh counts or return unknown. A missing request
is distinct from unavailable status. Only the private service contacts WI.

Preview receives deployments [{deployment, images}] and returns scopes with
the same names/counts, ready, optional hold, and selection_sha256. This digest
pins the candidate originals and destination before WI registration. Submit
and status receive request_id plus scopes [{deployment, images, selection_sha256}].
Return fresh=true and rows [{deployment, selection_sha256, stage, confirmed,
manifest_sha256?}]. The helper resolves the manifest after private admission.
A verified row must include integrity {kind, manifest_sha256, image_count,
receipt_sha256}, checked against the exact admitted project/deployment. Status
may return fresh=true, missing=true only after reconciling a durable absence.
The helper must reject changed selections and preserve original-file claims
on any replay. Return safe summaries only, never credentials or image lists.
Held rows may include a hold_reason and request_sha. For an eligible
pre-admission hold, preview and status return retry_of (its inbox SHA).
The confirmed replacement includes retry_of in its scopes and request_id.
An ordinary lost-reply replay keeps its existing request_id.
"""

import csv
import json
import re
from collections import Counter

from PySide6.QtCore import QObject, QProcess, QTimer, Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QGroupBox, QLabel, QPushButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from cassn.gui.workspace import UploadProgressBar
from PySide6.QtWidgets import QFrame, QHeaderView

from cassn.gui.appearance import workspace_label, workspace_stylesheet
from cassn.core.ingest_safety import require_no_ingest_holds
from cassn.core.wi_requests import (
    all_requests_verified, checked_status, complete, read_requests, replacement_sources, save_requests, scope_key,
    superseded, validate_scopes,
)


PROJECT_NAMES = {"2010517": "UCNRS Small Animal", "2010527": "UCNRS Mammal"}


def _exported_projects(folder):
    """Read the small deployment exports used by the uploader's preview."""
    projects = {}
    for device in ("ML", "SA"):
        path = folder / "WI_metadata" / f"wildlife_insights_{device}_deployments.csv"
        try:
            with path.open(encoding="utf-8-sig", newline="") as stream:
                for row in csv.DictReader(stream):
                    name = row["deployment_id"]
                    projects[name] = row["project_id"] if name not in projects else None
        except FileNotFoundError:
            continue
    return projects


class WildlifeInsightsControl(QObject):
    def __init__(self, window, helper, layout):
        super().__init__(window)
        self.window = window
        self.helper = helper
        self.folder = None
        self.jobs = {}
        self.unavailable = set()
        self.process = None
        self.delivery_keys = set()
        self.menu_waiting = False
        self.closed = False
        self.panel_error = False
        self.next_job = 0
        self.read_error = False
        self.group = QGroupBox("Wildlife Insights")
        box = QVBoxLayout(self.group)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Deployment", "Progress"])
        self.tree.setWordWrap(True)
        self.tree.setRootIsDecorated(False)
        self.tree.header().setStretchLastSection(True)
        self.tree.setMaximumHeight(165)
        box.addWidget(self.tree)
        self.notice = QLabel()
        self.notice.setTextFormat(Qt.PlainText)
        self.notice.setWordWrap(True)
        box.addWidget(self.notice)
        self.dismissed = None
        self.dismiss_button = QPushButton("Dismiss")
        self.dismiss_button.clicked.connect(self.dismiss)
        self.dismiss_button.hide()
        box.addWidget(self.dismiss_button, alignment=Qt.AlignRight)
        layout.addWidget(self.group)
        self.group.hide()
        self.action = window._menu_action("Upload images to Wildlife Insights", self.preview,
                                          "Review the selected local camera deployments.")
        self.timer = QTimer(self)
        self.timer.setInterval(3000)
        self.timer.timeout.connect(self.poll)
        self.action.setEnabled(False)

    def refresh(self):
        if self.closed:
            return
        folder = self.window.current_deployment_folder
        if folder != self.folder:
            self.folder = folder
            self.dismissed = None
            self.jobs = {}
            self.read_error = False
            self.panel_error = False
            self.notice.clear()
            self.delivery_keys.clear()
            self.menu_waiting = False
            try:
                self.jobs = read_requests(folder) if folder else {}
                self.unavailable = set(self.jobs)
            except (OSError, ValueError) as exc:
                self.read_error = True
                self.notice.setText(str(exc))
            self.render()
        self.action.setEnabled(bool(folder and not self.read_error and (not self.process or self.process.property("operation") == "status")
                                    and not self.window.card_ingest_threads))

    def render(self):
        self.tree.clear()
        sources = replacement_sources(self.jobs)
        for key, job in self.jobs.items():
            rows = {r["deployment"]: r for r in job.get("rows", [])}
            for scope in job["scopes"]:
                name, total = scope["deployment"], scope["images"]
                row = rows.get(name, {})
                if superseded(scope, row, sources):
                    continue
                stage = row.get("stage", "unknown")
                count = row.get("confirmed", 0)
                if key in self.unavailable and stage not in {"verified", "held"}:
                    stage = "unknown"
                item = QTreeWidgetItem(self.tree, [name])
                item.setToolTip(0, name)
                labels = {
                    "queued": "Queued", "waiting_retry": "Waiting to retry",
                    "cleaning": "Finishing checks", "held": "Needs review",
                    "transferred_or_waiting": "Waiting for verification",
                    "unknown": "Status unknown", "verifying": "Verifying originals…",
                    "verified": f"Wildlife Insights upload verified · {total:,} of {total:,}",
                    "uploading": f"Uploading {count:,} of {total:,}",
                }
                if stage == "held":
                    reason = row.get("hold_reason") or "Check the uploader’s hold receipt."
                    label = "Last known hold" if key in self.unavailable else "Needs review"
                    missing = re.fullmatch(r"Added deployment held: expected one exact camera ([A-Za-z0-9_-]+); found 0", reason)
                    if missing:
                        item.setText(1, f"{label} · Camera {missing[1]} was missing in WI at the last attempt.")
                    else:
                        item.setText(1, f"{label} · {reason}")
                    item.setToolTip(1, reason)
                elif stage == "verifying":
                    self.tree.setItemWidget(item, 1, QLabel(labels[stage]))
                else:
                    bar = UploadProgressBar()
                    bar.setRange(0, total)
                    bar.setValue(count)
                    bar.setFormat(labels[stage])
                    bar.setAccessibleName(labels[stage])
                    self.tree.setItemWidget(item, 1, bar)
        self.tree.resizeColumnToContents(0)
        self.tree.setColumnWidth(0, min(self.tree.columnWidth(0), 420))
        finished = self.can_dismiss()
        if not finished:
            self.dismissed = None
        self.dismiss_button.setVisible(finished)
        self.group.setVisible(bool(self.jobs or self.notice.text()) and self.dismissed != frozenset(self.jobs))
        if any(not complete(j, sources) for j in self.jobs.values()):
            self.timer.start()
        else:
            self.timer.stop()

    def message(self, text, *, error=True):
        self.dismissed = None
        self.panel_error = error
        self.notice.setText(text)
        self.dismiss_button.setVisible(self.can_dismiss())
        self.group.show()

    def can_dismiss(self):
        return bool(self.jobs and not self.read_error and not self.panel_error and all_requests_verified(self.jobs))

    def dismiss(self):
        if self.can_dismiss():
            self.dismissed = frozenset(self.jobs)
            self.group.hide()

    def call(self, operation, payload, receive):
        if self.closed or self.process or not self.folder:
            return
        folder = self.folder
        body = dict(payload, version=1, event=folder.name, folder=str(folder))
        process = QProcess(self)
        process.setProperty("operation", operation)
        self.process = process
        self.refresh()
        process.setProgram(self.helper)
        process.setArguments([operation])
        output = bytearray()
        error = []
        deadline = QTimer(process)
        deadline.setSingleShot(True)

        def stop(reason):
            error.append(reason)
            process.kill()

        def collect():
            output.extend(process.readAllStandardOutput().data())
            if len(output) > 1024 * 1024:
                stop("Wildlife Insights helper response exceeded the size limit.")

        def finish(*_):
            if self.process is not process:
                return
            deadline.stop()
            collect()
            self.process = None
            succeeded = False
            try:
                if self.closed or self.window.current_deployment_folder != folder:
                    return
                if error or process.exitStatus() != QProcess.NormalExit or process.exitCode() != 0:
                    raise ValueError(error[0] if error else "Wildlife Insights helper unavailable. Check its local configuration.")
                result = json.loads(output)
                if (not isinstance(result, dict) or result.get("version") != 1
                        or result.get("event") != body["event"]
                        or result.get("request_id") != body.get("request_id")):
                    raise ValueError("Wildlife Insights helper response did not match the request.")
                if operation in {"status", "submit"} and result.get("fresh") is not True:
                    raise ValueError("The helper has no current service status.")
                receive(result)
                succeeded = True
            except (OSError, ValueError, KeyError, TypeError) as exc:
                key = body.get("request_id")
                if key in self.jobs:
                    self.unavailable.add(key)
                    self.delivery_keys.discard(key)
                    self.render()
                self.message(f"Wildlife Insights status unknown. {exc}")
            finally:
                process.deleteLater()
                self.refresh()
                if self.menu_waiting and operation == "status":
                    self.menu_waiting = False
                    if succeeded and not self.closed and self.folder == folder:
                        self.preview()

        def started():
            process.write(json.dumps(body).encode())
            process.closeWriteChannel()

        def failed(code):
            if code == QProcess.FailedToStart:
                error.append("Could not start the configured Wildlife Insights helper.")
                finish()

        process.started.connect(started)
        process.readyReadStandardOutput.connect(collect)
        process.readyReadStandardError.connect(process.readAllStandardError)
        process.finished.connect(finish)
        process.errorOccurred.connect(failed)
        deadline.timeout.connect(lambda: stop("Wildlife Insights helper timed out."))
        deadline.start(30000)
        process.start()

    def retryable(self):
        sources = replacement_sources(self.jobs)
        eligible = {}
        for key, job in self.jobs.items():
            if key in self.unavailable:
                continue
            for row in job.get("rows", []):
                name = row["deployment"]
                if row.get("retry_of"):
                    scope = next(s for s in job["scopes"] if s["deployment"] == name)
                    if not superseded(scope, row, sources):
                        eligible[name] = row["retry_of"]
        return eligible

    def selection(self):
        require_no_ingest_holds(self.folder)
        if self.window.metadata.get("deployment_event_id") != self.folder.name:
            raise ValueError("The selected event does not match the local staging folder.")
        labels = set()
        for _, _, code, label in self.window.devices:
            item = self.window._device_item(label)
            if code in {"ML", "SA"} and item and item.text(2) == "Complete":
                labels.add(label)
        counts = Counter()
        for row in self.window.file_inventory:
            if row.get("file_type") == "image" and row.get("device_label") in labels:
                name = row.get("deployment_id")
                if not name:
                    raise ValueError("A selected image has no deployment ID.")
                counts[name] += 1
        sources = replacement_sources(self.jobs)
        eligible = self.retryable()
        for job in self.jobs.values():
            rows = {row["deployment"]: row for row in job.get("rows", [])}
            for scope in job["scopes"]:
                name = scope["deployment"]
                count = counts.get(name)
                if count is not None and count != scope["images"]:
                    raise ValueError("A previously submitted deployment changed. Review it before continuing.")
                row = rows.get(name, {})
                if not superseded(scope, row, sources) and not (name in eligible and row.get("retry_of") == eligible[name]):
                    counts.pop(name, None)
        return dict(sorted(counts.items()))

    def preview(self):
        self.refresh()
        if not self.action.isEnabled():
            return
        if self.process:
            self.menu_waiting = True
            return
        # On reopen, refresh missing status before deciding which cameras to offer.
        sources = replacement_sources(self.jobs)
        stale = {key for key in self.unavailable - self.delivery_keys if not complete(self.jobs[key], sources)}
        if stale:
            self.menu_waiting = True
            self.check_status(sorted(stale)[0])
            return
        try:
            require_no_ingest_holds(self.folder)
            if self.delivery_keys:
                key = sorted(self.delivery_keys)[0]
                scopes = [dict(s, ready=True) for s in self.jobs[key]["scopes"]]
                self.confirm({"scopes": scopes}, {s["deployment"]: s["images"] for s in scopes}, delivery_key=key)
                return
            counts = self.selection()
        except (OSError, ValueError) as exc:
            self.message(str(exc))
            return
        if not counts:
            self.message("No new completed camera deployments to submit.", error=False)
            return
        specs = [dict(deployment=k, images=v) for k, v in counts.items()]
        self.call("preview", {"deployments": specs}, lambda result: self.confirm(result, counts))

    def confirm(self, result, counts, *, delivery_key=None):
        scopes = result["scopes"]
        validate_scopes(scopes)
        if {s["deployment"]: s["images"] for s in scopes} != counts:
            raise ValueError("Preview does not match the selected camera deployments.")
        retries = self.retryable()
        if not delivery_key and any(s.get("ready") and s.get("retry_of") != retries.get(s["deployment"]) for s in scopes):
            raise ValueError("The held request changed. Refresh its status and review the selection again.")
        scope_list = sorted(scopes, key=lambda s: s["deployment"])
        folder = self.folder
        dialog = QDialog(self.window)
        dialog.setWindowTitle("Upload images to Wildlife Insights")
        dialog.setObjectName("fieldWorkspace")
        dialog.setStyleSheet(workspace_stylesheet(dialog.palette(), self.window.workspace.zoom))
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(14)
        banner = QFrame()
        banner.setObjectName("sidebarBrandBlock")
        heading = QVBoxLayout(banner)
        heading.setContentsMargins(18, 14, 18, 14)
        heading.addWidget(workspace_label("Wildlife Insights", "sidebarBrand"))
        event_label = workspace_label(folder.name, "subtitle", wrap=True)
        event_label.setStyleSheet("color: #e0ebde; margin: 0;")
        heading.addWidget(event_label)
        layout.addWidget(banner)
        layout.addWidget(workspace_label("Resume the saved upload" if delivery_key else "Select camera deployments", "sectionTitle"))
        tree = QTreeWidget()
        tree.setRootIsDecorated(False)
        tree.setAccessibleName("Camera deployments to upload")
        tree.setHeaderLabels(["Deployment", "WI project", "Photos"])
        tree.header().setStretchLastSection(False)
        tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        projects = _exported_projects(folder)
        for s in scope_list:
            ready = s.get("ready") is True and not s.get("hold")
            name = s["deployment"]
            project_id = projects.get(name)
            destination = PROJECT_NAMES.get(project_id, project_id or "Unavailable")
            detail = name if ready else f"{name}\n{s.get('hold') or 'Needs review'}"
            item = QTreeWidgetItem(tree, [detail, destination, f"{s['images']:,}"])
            item.setCheckState(0, Qt.Checked if ready else Qt.Unchecked)
            if delivery_key:
                item.setFlags(item.flags() & ~Qt.ItemIsUserCheckable)
            if not ready:
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
            item.setToolTip(0, detail)
            if project_id:
                item.setToolTip(1, f"WI project ID: {project_id}")
            item.setTextAlignment(2, Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(tree)
        layout.addWidget(workspace_label(
            "Confirm to resume delivery of this exact selection." if delivery_key else
            "Confirming allows Wildlife Insights to register a missing camera for each selected deployment.",
            "muted", wrap=True))
        summary = workspace_label("", "contextTitle")
        layout.addWidget(summary)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        begin = buttons.button(QDialogButtonBox.Ok)
        begin.setText("Upload images to Wildlife Insights")

        def selected():
            return [scope for index, scope in enumerate(scope_list)
                    if tree.topLevelItem(index).checkState(0) == Qt.Checked]

        begin.setProperty("role", "primary")

        def update_selection():
            picked = selected()
            total = sum(scope["images"] for scope in picked)
            summary.setText(f"Selected deployments: {len(picked)}    ·    Photos: {total:,}")
            begin.setEnabled(bool(picked))

        update_selection()
        tree.itemChanged.connect(update_selection)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.resize(680, 305 + min(len(scope_list), 6) * 30)
        self.timer.stop()
        try:
            if dialog.exec() != QDialog.Accepted or self.window.current_deployment_folder != folder:
                return
            require_no_ingest_holds(folder)
            if delivery_key:
                if delivery_key not in self.delivery_keys:
                    raise ValueError("The saved request changed. Open the preview again.")
                self.submit(delivery_key)
                return
            if self.selection() != counts:
                raise ValueError("Selection changed. Open the preview again.")
            approved = [{k: s[k] for k in ("deployment", "images", "selection_sha256", "retry_of") if k in s}
                        for s in selected()]
            if not approved:
                return
            key = scope_key(folder.name, approved)
            jobs = dict(self.jobs)
            jobs[key] = {"scopes": approved}
            save_requests(folder, jobs)  # Persist exact intent before the first possible write.
            self.jobs = jobs
            self.submit(key)
        finally:
            self.render()

    def submit(self, key):
        self.call("submit", {"request_id": key, "scopes": self.jobs[key]["scopes"]},
                  lambda result: self.accept_status(key, result))

    def accept_status(self, key, result):
        job = self.jobs[key]
        rows = checked_status(job["scopes"], result["rows"])
        previous = {r["deployment"]: r for r in job.get("rows", [])}
        for row in rows:
            old = previous.get(row["deployment"], {})
            if old.get("request_sha") and old["request_sha"] != row.get("request_sha"):
                raise ValueError("The uploader request identity changed.")
            if old.get("manifest_sha256") and old["manifest_sha256"] != row.get("manifest_sha256"):
                raise ValueError("The admitted Wildlife Insights manifest changed.")
            if old.get("stage") == "verified" and (
                    row["stage"] != "verified" or old.get("integrity") != row.get("integrity")):
                raise ValueError("The saved verification receipt changed.")
        jobs = dict(self.jobs)
        jobs[key] = dict(job, rows=rows)
        redraw = jobs != self.jobs or key in self.unavailable or bool(self.notice.text())
        if jobs != self.jobs:
            save_requests(self.folder, jobs)
            self.jobs = jobs
        self.unavailable.discard(key)
        self.panel_error = False
        self.notice.clear()
        if any(row.get("delivery_pending") for row in rows):
            self.delivery_keys.add(key)
        else:
            self.delivery_keys.discard(key)
        if redraw:
            self.render()

    def poll(self):
        self.refresh()
        sources = replacement_sources(self.jobs)
        pending = [k for k, job in self.jobs.items() if not complete(job, sources)]
        if self.closed or self.process or not pending or self.read_error:
            return
        key = pending[self.next_job % len(pending)]
        self.next_job += 1

        self.check_status(key)

    def check_status(self, key):
        def received(result):
            if result.get("missing") is True:
                if self.jobs[key].get("rows"):
                    raise ValueError("The service lost track of an acknowledged request. Review its records.")
                self.unavailable.add(key)
                self.render()
                self.delivery_keys.add(key)
                self.message("Use Prepare & Upload to resume the saved selection.")
            else:
                self.accept_status(key, result)

        self.call("status", {"request_id": key, "scopes": self.jobs[key]["scopes"]}, received)

    def close(self):
        self.closed = True
        self.timer.stop()
        if self.process:
            self.process.kill()
