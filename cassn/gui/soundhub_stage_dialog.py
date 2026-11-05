"""Local SoundHub staging confirmation."""

from PySide6.QtWidgets import QFrame, QHBoxLayout, QPushButton, QVBoxLayout

from cassn.gui.appearance import workspace_label, workspace_stylesheet
from cassn.gui.dialogs import WindowDialog


class SoundHubStageDialog(WindowDialog):
    def __init__(self, parent, event_name, deployments, recordings, progress):
        super().__init__(parent)
        self.setWindowTitle("SoundHub staging")
        self.setObjectName("fieldWorkspace")
        zoom = getattr(getattr(parent, "workspace", None), "zoom", 100)
        self.setStyleSheet(workspace_stylesheet(self.palette(), zoom))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        heading = "Continue staging bird audio?" if progress is not None else "Stage bird audio?"
        layout.addWidget(workspace_label(heading, "pageTitle", wrap=True))
        layout.addWidget(workspace_label(
            "Prepare this deployment’s bird recordings for a later SoundHub upload.",
            "subtitle", wrap=True,
        ))
        card = QFrame()
        card.setObjectName("setupCard")
        facts = QVBoxLayout(card)
        facts.setContentsMargins(14, 12, 14, 12)
        facts.addWidget(workspace_label(event_name, "contextTitle", wrap=True))
        batch = (f"Current batch: {deployments:,} deployment{'s' if deployments != 1 else ''}"
                 f" · {recordings:,} recording{'s' if recordings != 1 else ''}")
        facts.addWidget(workspace_label(batch, "muted", wrap=True))
        if progress is not None:
            done, total = progress
            facts.addWidget(workspace_label(f"{done:,} completed · {total - done:,} remaining", "contextTitle"))
        layout.addWidget(card)
        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        stage = QPushButton("Continue staging" if progress is not None else "Stage bird audio")
        stage.setProperty("role", "primary")
        stage.setDefault(True)
        stage.clicked.connect(self.accept)
        actions.addWidget(stage)
        actions.addStretch()
        layout.addLayout(actions)
        self.resize(round(440 * zoom / 100), self.sizeHint().height())
