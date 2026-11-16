"""Review and remove the open deployment after a fresh shared clearance check."""
from html import escape
from datetime import datetime

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import QFrame, QGroupBox, QHBoxLayout, QLabel, QProgressBar, QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from cassn.box.auth import get_box_client
from cassn.box.client import BoxStorage
from cassn.core.staging_cleanup import clear_verified_deployment, inspect_deployment_for_cleanup
from cassn.gui.qc_review import HoldToAcknowledge


class HoldToClear(HoldToAcknowledge):
    idle_text = "Hold to clear"


class ClearanceWorker(QThread):
    stage = Signal(str)

    def __init__(self, folder, root, config, config_path, preview=None, lease=None):
        super().__init__()
        self.folder, self.root = folder, root
        self.config, self.config_path = config, config_path
        self.preview, self.lease = preview, lease
        self.result = None
        self.error = ""

    def progress(self, label):
        if self.isInterruptionRequested():
            raise InterruptedError("Check cancelled.")
        self.stage.emit(label)

    def run(self):
        try:
            if self.preview:
                self.result = clear_verified_deployment(self.preview, event_lease=self.lease, progress=self.progress)
            else:
                self.progress("Connecting to Box")
                client = get_box_client(self.config)
                storage = BoxStorage(client) if client is not None else None
                self.result = inspect_deployment_for_cleanup(self.folder, self.root, storage,
                    self.config.field_data_folder_id, profile=self.config.profile,
                    config_path=self.config_path, progress=self.progress)
        except Exception as exc:
            self.error = str(exc)


class CleanupPanel(QGroupBox):
    def __init__(self, window):
        super().__init__()
        self.setProperty("role", "platformPanel")
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self.window = window
        self.folder = self.plan = self.worker = None
        self.applying = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        header = QFrame()
        header.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        header.setStyleSheet("QFrame { background: #f6e2e2; border: none; border-radius: 8px; }")
        heading = QHBoxLayout(header)
        heading.setContentsMargins(18, 14, 18, 14)
        title = QLabel("Clear Local Staging")
        title.setStyleSheet("color: #762b30; font-size: 18px; font-weight: 700;")
        heading.addWidget(title, 1)
        self.state_label = QLabel()
        self.state_label.setTextFormat(Qt.PlainText)
        self.state_label.setStyleSheet("color: #762b30;")
        heading.addWidget(self.state_label)
        self.toggle = QToolButton()
        self.toggle.setObjectName("progressDisclosure")
        self.toggle.setStyleSheet(
            "QToolButton { padding: 5px; border: none; background: transparent; color: #762b30; }"
            "QToolButton:hover, QToolButton:focus { background: #edcccc; }"
        )
        self.toggle.setCheckable(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle.setAccessibleName("Expand or collapse Clear Local Staging")
        heading.addWidget(self.toggle)
        layout.addWidget(header)
        self.body = QWidget()
        actions = QVBoxLayout(self.body)
        actions.setContentsMargins(18, 16, 18, 16)
        actions.setSpacing(12)
        actions.setAlignment(Qt.AlignTop)
        self.explanation = QLabel()
        self.explanation.setTextFormat(Qt.RichText)
        self.explanation.setWordWrap(True)
        self.explanation.setTextInteractionFlags(Qt.TextSelectableByMouse)
        actions.addWidget(self.explanation)
        self.check = QPushButton("Click here to check")
        self.check.clicked.connect(self.preview)
        actions.addWidget(self.check, 0, Qt.AlignLeft)
        self.progress_label = QLabel()
        self.progress_label.setTextFormat(Qt.PlainText)
        self.progress_label.setWordWrap(True)
        actions.addWidget(self.progress_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setTextVisible(False)
        actions.addWidget(self.progress_bar)
        self.progress_label.hide()
        self.progress_bar.hide()
        self.message = QLabel()
        self.message.setTextFormat(Qt.RichText)
        self.message.setWordWrap(True)
        self.message.setTextInteractionFlags(Qt.TextSelectableByMouse)
        actions.addWidget(self.message)
        self.clear_guidance = QLabel(
            "Once every required check passes, press and hold the button to clear this event.\n\n"
            "Clearing permanently removes this event’s local folder, including its original files and local metadata. "
            "Box files, Wildlife Insights uploads, and the separate SoundHub staging folder remain in place."
        )
        self.clear_guidance.setWordWrap(True)
        self.clear_guidance.setTextFormat(Qt.PlainText)
        actions.addWidget(self.clear_guidance)
        buttons = QHBoxLayout()
        self.hold = HoldToClear()
        self.hold.setStyleSheet(
            "QPushButton { background: #ac2737; color: white; border: 1px solid #ac2737; }"
            "QPushButton:hover { background: #922130; }"
            "QPushButton:disabled { background: #f6e2e2; color: #95666b; border: 1px solid #edcccc; }"
        )
        self.hold.setAccessibleName("Hold to clear local staging")
        self.hold.setEnabled(False)
        self.hold.held.connect(self.apply)
        buttons.addWidget(self.hold)
        buttons.addStretch()
        actions.addLayout(buttons)
        layout.addWidget(self.body)
        self.body.hide()
        self.toggle.toggled.connect(self.body.setVisible)
        self.toggle.toggled.connect(lambda opened: self.toggle.setArrowType(Qt.DownArrow if opened else Qt.RightArrow))
        self.toggle.setArrowType(Qt.RightArrow)
        self.status("Check required")

    def status(self, status):
        self.state_label.setText(status)

    def refresh(self):
        folder = self.window.current_deployment_folder
        if folder != self.folder:
            self.hold._cancel()
            self.hold.setEnabled(False)
            self.plan = None
            self.folder = folder
            self.toggle.setChecked(False)
            self.message.clear()
            self.status("Check required")
        self.setVisible(bool(folder))
        if folder:
            prefix, _, date = folder.name.rpartition("_")
            try:
                date = datetime.strptime(date, "%Y%m%d").strftime("%Y-%m-%d")
                name = f"{prefix.replace('_', ' ')} {date}"
            except ValueError:
                name = folder.name.replace("_", " ")
            purpose = f"Check that {name} can be safely removed from local staging."
            self.check.setToolTip(purpose)
            self.check.setAccessibleDescription(purpose)
            self.explanation.setText(
                f"<p><b>{escape(purpose)}</b></p>"
                "<p>Check reviews this event without uploading or deleting files. It checks:</p>"
                "<ol>"
                "<li><b>Box:</b> All event files are uploaded, file lists and hashes match, and metadata publication is complete.</li>"
                "<li><b>Wildlife Insights:</b> All eligible photos are uploaded and verified against this event’s original files.</li>"
                "<li><b>SoundHub:</b> All eligible bird recordings are verified in the selected staging location, "
                "or covered by verified submission receipts.</li>"
                "<li><b>Local event:</b> The folder and inventory agree, required records are available, "
                "and no active work prevents clearance.</li>"
                "</ol>"
                "<p>A check that does not apply to this event does not block clearance. "
                "Missing evidence, failed checks, and checks that cannot finish keep clearance blocked.</p>"
            )

    def preview(self):
        if self.worker:
            self.worker.requestInterruption()
            return
        self.refresh()
        if self.folder:
            self.start(False)

    def apply(self):
        if self.worker or not self.plan or self.folder != self.window.current_deployment_folder:
            self.refresh()
            return
        self.start(True)

    def start(self, apply):
        w = self.window
        wi = getattr(w, "wi_control", None)
        if w._work_running() or (wi and wi.process):
            self.message.setText("Wait for the active work to finish, then run Check again.")
            self.hold.setEnabled(False)
            self.plan = None
            return
        if apply and not w._deployment_lease:
            self.message.setText("Reopen this deployment to reserve it, then run Check again.")
            return
        self.applying = apply
        self.hold.setEnabled(False)
        self.status("Checking")
        self.toggle.setChecked(True)
        self.message.clear()
        self.progress_label.show()
        self.progress_bar.show()
        if wi:
            wi.timer.stop()
        if apply:
            w.setEnabled(False)
        else:
            self.plan = None
            self.check.setText("Cancel check")
        self.worker = ClearanceWorker(self.folder, w.staging_root, w.box_config, w.config_file,
                                      self.plan if apply else None, w._deployment_lease)
        self.worker.stage.connect(self.show_stage)
        self.worker.finished.connect(self.done)
        self.worker.start()

    def show_stage(self, text):
        self.progress_label.setText(text)
        self.state_label.setText(text)
        self.progress_bar.setAccessibleName(text)

    def render_result(self):
        plan = self.plan
        parts = [f"<p><b>{escape(self.folder.name)}</b><br>"
                 f"{plan.local_bytes / 1e9:,.2f} GB in local files</p>"]
        shown = set()
        confirmations = {
            "Box": "All event files are uploaded and verified. File lists and hashes match, and metadata publication is complete.",
            "Wildlife Insights": "All eligible photos are uploaded and verified against this event’s original files.",
            "SoundHub": "All eligible bird recordings are verified in the selected staging location or covered by verified submission receipts.",
            "Local staging": "The folder and inventory agree. Required local records are available.",
        }
        for name, confirmation in confirmations.items():
            details = plan.checks.get(name, {})
            state = details.get("status", "Not checked")
            color, symbol = "#202b33", "—"
            description = state.capitalize()
            if state.lower() == "passed":
                color, symbol = "#18715b", "✓"
                description = confirmation
            elif state.lower() in ("blocked", "incomplete"):
                color, symbol = "#ac2737", "✕"
            parts.append(f'<p><b>{escape(name)}</b><br>'
                         f'<span style="color:{color}">{symbol} {escape(description)}</span>')
            reason = details.get("reason")
            if reason:
                shown.update((reason, f"{name}: {reason}"))
                parts.append(f"<br>{escape(reason)}")
            for deployment, counts in details.get("deployments", {}).items():
                if (state.lower() == "passed" or "expected" not in counts
                        or not (counts.get("missing") or counts.get("unchecked"))):
                    continue
                parts.append(f"<br><br><b>{escape(deployment)}</b><br>"
                             f'{counts["expected"]} recordings expected · '
                             f'{counts.get("submitted", 0)} confirmed submitted · '
                             f'{counts.get("staged", 0)} verified in staging')
                if counts.get("missing"):
                    parts.append(f'<br>{counts["missing"]} recordings missing.')
                if counts.get("unchecked"):
                    parts.append(f'<br>{counts["unchecked"]} recordings could not be checked. '
                                 'Their location and contents have not been verified.')
            parts.append("</p>")
        remaining = [reason for reason in plan.reasons if reason not in shown]
        if remaining:
            parts.append("<p><b>Before you can clear this event</b></p><ul>")
            parts.extend(f"<li>{escape(reason)}</li>" for reason in remaining)
            parts.append("</ul>")
        if plan.warnings:
            parts.append("<p><b>Other findings</b></p><ul>")
            parts.extend(f"<li>{escape(warning)}</li>" for warning in plan.warnings)
            parts.append("</ul>")
        self.message.setText("".join(parts))

    def done(self):
        worker = self.worker
        self.worker = None
        self.progress_label.hide()
        self.progress_bar.hide()
        worker.deleteLater()
        self.window.setEnabled(True)
        self.applying = False
        self.check.setText("Click here to check")
        wi = getattr(self.window, "wi_control", None)
        if wi:
            wi.render()
        self.refresh()
        if worker.folder != self.folder:
            return
        if worker.error or worker.isInterruptionRequested():
            self.plan = None
            self.status("Check required")
            self.message.setText(escape(worker.error or "Check cancelled."))
        elif worker.preview:
            self.window.current_deployment_folder = None
            next_window = self.window.manager.new_window(open_sessions=True)
            next_window.statusBar().showMessage(f"Cleared {worker.folder.name}. Report: {worker.result.report_path}")
            self.window.close()
        else:
            self.plan = worker.result
            self.status("Ready to clear" if self.plan.clearable else "Keep local files")
            self.render_result()
            self.hold.setEnabled(self.plan.clearable)
