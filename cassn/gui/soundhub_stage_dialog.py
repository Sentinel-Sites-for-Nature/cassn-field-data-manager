"""Local SoundHub staging confirmation."""
import csv

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QPushButton, QScrollArea, QVBoxLayout, QWidget

from cassn.gui.appearance import style_cancel_button, workspace_label, workspace_stylesheet
from cassn.gui.dialogs import WindowDialog
from cassn.soundhub.stage_display import batch_bytes, event_preview
from cassn.soundhub.staging import SoundHubStagingError


class _PreviewSignals(QObject):
    event = Signal(str)
    size = Signal(str)


class _ReadPreview(QRunnable):
    def __init__(self, event_folder, staging_root):
        super().__init__()
        self.event_folder = event_folder
        self.staging_root = staging_root
        self.signals = _PreviewSignals()

    def run(self):
        try:
            counts = event_preview(self.event_folder, self.staging_root)
            event = (
                f"{counts['uploaded']:,} confirmed uploaded (local records)\n"
                f"{counts['staged']:,} already staged locally\n"
                f"{counts['waiting']:,} waiting to be staged"
            )
        except (OSError, ValueError, KeyError, TypeError, csv.Error, SoundHubStagingError):
            event = "Event counts unavailable from the local records."
        self.signals.event.emit(event)
        try:
            size = batch_bytes(self.staging_root)
            text = f"Local batch size: {size / 1e9:,.2f} GB"
        except (OSError, ValueError, KeyError, TypeError, csv.Error):
            text = "Local batch size unavailable."
        self.signals.size.emit(text)


class SoundHubStageDialog(WindowDialog):
    def __init__(self, parent, event_name, deployments, recordings, progress, *,
                 event_folder=None, staging_root=None, resuming=False):
        super().__init__(parent)
        self.setWindowTitle("SoundHub staging")
        self.setObjectName("fieldWorkspace")
        zoom = getattr(getattr(parent, "workspace", None), "zoom", 100)
        self.setStyleSheet(workspace_stylesheet(self.palette(), zoom))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)
        continuing = progress is not None or resuming
        heading = "Continue staging bird audio?" if continuing else "Stage bird audio?"
        layout.addWidget(workspace_label(heading, "pageTitle", wrap=True))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        content = QVBoxLayout(body)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(12)
        content.setAlignment(Qt.AlignTop)
        card = QFrame()
        card.setObjectName("setupCard")
        facts = QVBoxLayout(card)
        facts.setContentsMargins(14, 12, 14, 12)
        facts.addWidget(workspace_label("Current event", "muted"))
        facts.addWidget(workspace_label(event_name, "contextTitle", wrap=True))
        self.event_facts = workspace_label("Reading local staging and upload records…", "muted", wrap=True)
        facts.addWidget(self.event_facts)
        if progress is not None:
            done, total = progress
            facts.addWidget(workspace_label(f"{done:,} completed · {total - done:,} remaining", "contextTitle", wrap=True))
        content.addWidget(card)
        batch = (f"Current batch: {deployments:,} deployment{'s' if deployments != 1 else ''}"
                 f" · {recordings:,} recording{'s' if recordings != 1 else ''}")
        content.addWidget(workspace_label(batch, "contextTitle", wrap=True))
        self.size_label = workspace_label("Checking local batch size…", "muted", wrap=True)
        content.addWidget(self.size_label)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        actions = QHBoxLayout()
        actions.setSpacing(14)
        cancel = style_cancel_button(QPushButton("Cancel"))
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        actions.addStretch()
        stage = QPushButton("Continue staging" if continuing else "Stage bird audio")
        stage.setProperty("role", "primary")
        stage.setDefault(True)
        stage.clicked.connect(self.accept)
        actions.addWidget(stage)
        layout.addLayout(actions)
        screen = parent.screen() if parent else QApplication.primaryScreen()
        available = screen.availableGeometry()
        self.resize(min(round(470 * zoom / 100), available.width() - 32),
                    min(round(390 * zoom / 100), available.height() - 64))
        if event_folder is not None and staging_root is not None:
            reader = _ReadPreview(event_folder, staging_root)
            reader.signals.event.connect(self._show_event, Qt.QueuedConnection)
            reader.signals.size.connect(self._show_size, Qt.QueuedConnection)
            QThreadPool.globalInstance().start(reader)
        else:
            self.event_facts.setText("Local event counts unavailable.")
            self.size_label.setText("Local batch size unavailable.")

    @Slot(str)
    def _show_event(self, text):
        self.event_facts.setText(text)

    @Slot(str)
    def _show_size(self, text):
        self.size_label.setText(text)
