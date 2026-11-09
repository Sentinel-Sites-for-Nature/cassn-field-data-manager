"""Select a saved field session without changing its files."""

from datetime import timezone

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHeaderView, QHBoxLayout, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout

from cassn.gui.appearance import workspace_label, workspace_stylesheet
from cassn.gui.dialogs import WindowDialog
from cassn.lookups import normalize_deployment_event_metadata


class SessionChooser(WindowDialog):
    def __init__(self, sessions, parent):
        super().__init__(parent)
        self.setWindowTitle("Open a deployment")
        self.setObjectName("fieldWorkspace")
        self.setStyleSheet(workspace_stylesheet(self.palette(), parent.workspace.zoom))
        self.resize(780, 480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        layout.addWidget(workspace_label("Resume a saved session", "pageTitle", wrap=True))
        self.sessions = sessions
        self.tree = QTreeWidget()
        self.tree.setAccessibleName("Saved deployments, most recent first")
        self.tree.setRootIsDecorated(False)
        self.tree.setWordWrap(True)
        self.tree.setHeaderLabels(["Reserve", "Field dates", "Devices complete"])
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in (1, 2):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        for index, session in enumerate(sessions):
            data = session["data"] or {}
            meta = normalize_deployment_event_metadata(data.get("metadata"))
            statuses = data.get("device_statuses", {})
            done = sum(v.get("status") == "Complete" for v in statuses.values())
            start = meta.get("deployment_event_start_date") or "—"
            end = meta.get("deployment_event_end_date") or "—"
            if session["status"] == "ok":
                values = [meta.get("site_name") or meta.get("reserve_name") or session["path"].parent.name,
                          f"{start} → {end}", f"{done} of {max(len(statuses), len(data.get('devices', [])))}"]
            else:
                values = [session["path"].parent.name, "—", "Cannot resume"]
            item = QTreeWidgetItem(values)
            item.setData(0, Qt.UserRole, index)
            self.tree.addTopLevelItem(item)
        layout.addWidget(self.tree, 1)
        card = QFrame()
        card.setObjectName("setupCard")
        info = QVBoxLayout(card)
        info.setContentsMargins(14, 12, 14, 12)
        self.event_name = workspace_label("", "sectionTitle", wrap=True)
        self.detail = workspace_label("", "muted", wrap=True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        info.addWidget(self.event_name)
        info.addWidget(self.detail)
        layout.addWidget(card)
        actions = QHBoxLayout()
        actions.addStretch()
        new = QPushButton("Keep current session" if parent.current_deployment_folder else "Start new")
        new.clicked.connect(self.reject)
        actions.addWidget(new)
        self.resume = QPushButton("Resume selected session")
        self.resume.setProperty("role", "primary")
        self.resume.setDefault(True)
        self.resume.clicked.connect(self.accept)
        actions.addWidget(self.resume)
        layout.addLayout(actions)
        self.tree.currentItemChanged.connect(self._select)
        self.tree.itemDoubleClicked.connect(lambda *_: self.resume.click())
        self.tree.setCurrentItem(self.tree.topLevelItem(0))

    def selected_session(self):
        return self.sessions[self.tree.currentItem().data(0, Qt.UserRole)]

    def _select(self):
        session = self.selected_session()
        folder = session["path"].parent
        readable = session["status"] == "ok"
        self.resume.setEnabled(readable)
        self.event_name.setText(folder.name)
        if readable:
            data = session["data"]
            observer = data.get("metadata", {}).get("observer") or "Unknown"
            saved = session.get("saved_at")
            skipped = sum(v.get("status") == "Skipped" for v in data.get("device_statuses", {}).values())
            lines = [f"Downloaded by {observer}"]
            if saved:
                local = saved.replace(tzinfo=timezone.utc).astimezone()
                lines.append(f"Last saved {local:%b %d, %Y at %H:%M}")
            if skipped:
                lines.append(f"Skipped devices: {skipped}")
            self.detail.setText(" · ".join(lines) + f"\n{folder}")
        else:
            reason = ("This session needs a compatible app version." if session["status"] == "unsupported"
                      else "The app could not read the saved session.")
            self.detail.setText(f"{reason}\n{session['error_msg']}\n{session['path']}")
