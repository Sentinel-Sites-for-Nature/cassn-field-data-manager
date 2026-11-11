"""Review and remove the open deployment after a fresh shared clearance check."""
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import QGroupBox, QLabel, QPushButton, QToolButton, QVBoxLayout, QWidget

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
        self.window = window
        self.folder = self.plan = self.worker = None
        self.applying = False
        layout = QVBoxLayout(self)
        self.toggle = QToolButton()
        self.toggle.setCheckable(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        layout.addWidget(self.toggle)
        self.body = QWidget()
        actions = QVBoxLayout(self.body)
        self.message = QLabel()
        self.message.setTextFormat(Qt.PlainText)
        self.message.setWordWrap(True)
        self.message.setTextInteractionFlags(Qt.TextSelectableByMouse)
        actions.addWidget(self.message)
        self.check = QPushButton("Check")
        self.check.clicked.connect(self.preview)
        actions.addWidget(self.check, alignment=Qt.AlignLeft)
        self.hold = HoldToClear()
        self.hold.setAccessibleName("Hold to clear local staging")
        self.hold.setEnabled(False)
        self.hold.held.connect(self.apply)
        actions.addWidget(self.hold, alignment=Qt.AlignLeft)
        layout.addWidget(self.body)
        self.body.hide()
        self.toggle.toggled.connect(self.body.setVisible)
        self.toggle.toggled.connect(lambda opened: self.toggle.setArrowType(Qt.DownArrow if opened else Qt.RightArrow))
        self.toggle.setArrowType(Qt.RightArrow)
        self.status("Check required")

    def status(self, status):
        self.toggle.setText(f"Clear local staging · {status}")

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
        if wi:
            wi.timer.stop()
        if apply:
            w.setEnabled(False)
        else:
            self.plan = None
            self.check.setText("Cancel check")
        self.worker = ClearanceWorker(self.folder, w.staging_root, w.box_config, w.config_file,
                                      self.plan if apply else None, w._deployment_lease)
        self.worker.stage.connect(self.message.setText)
        self.worker.finished.connect(self.done)
        self.worker.start()

    def done(self):
        worker = self.worker
        self.worker = None
        worker.deleteLater()
        self.window.setEnabled(True)
        self.applying = False
        self.check.setText("Check")
        wi = getattr(self.window, "wi_control", None)
        if wi:
            wi.render()
        self.refresh()
        if worker.folder != self.folder:
            return
        if worker.error or worker.isInterruptionRequested():
            self.plan = None
            self.status("Check required")
            self.message.setText(worker.error or "Check cancelled.")
        elif worker.preview:
            self.window.current_deployment_folder = None
            next_window = self.window.manager.new_window(open_sessions=True)
            next_window.statusBar().showMessage(f"Cleared {worker.folder.name}. Report: {worker.result.report_path}")
            self.window.close()
        else:
            self.plan = worker.result
            self.status("Ready to clear" if self.plan.clearable else "Keep local files")
            self.message.setText("\n".join([str(self.folder), f"{self.plan.local_bytes / 1e9:,.2f} GB in local files",
                *self.plan.summary_lines(), *self.plan.reasons, *self.plan.warnings]))
            self.hold.setEnabled(self.plan.clearable)
