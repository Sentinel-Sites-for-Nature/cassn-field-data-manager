"""A compact collection check summary with selected evidence."""
from __future__ import annotations

from collections import Counter
from html import escape
from pathlib import Path

from PySide6.QtCore import QRect, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPalette, QPen
from PySide6.QtWidgets import (
    QComboBox, QFrame, QDialogButtonBox, QHeaderView, QHBoxLayout, QLabel,
    QPushButton, QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from cassn.core.inventory import inventory_storage_relpath
from cassn.gui.appearance import workspace_label, workspace_stylesheet
from cassn.gui.dialogs import WindowDialog, WindowMessageBox


# Text identifies each level even when the user cannot distinguish the colors.
_LEVELS = {
    "error": ("Investigate", "#fbe2df", "#513330"),
    "warning": ("Review", "#fff1c7", "#4c422a"),
    "info": ("Notice", "#e5f3e9", "#293f34"),
    "pending": ("Pending", "#eff1f2", "#333c43"),
}


class FindingsTree(QTreeWidget):
    def drawRow(self, painter, option, index):
        # Include the disclosure arrow gutter in the tint. Outline selection
        # without replacing a warning's color with the normal green selection.
        rect = QRect(0, option.rect.y(), self.viewport().width(), option.rect.height())
        painter.fillRect(rect, self.itemFromIndex(index).background(0))
        super().drawRow(painter, option, index)
        if self.selectionModel().isRowSelected(index.row(), index.parent()):
            painter.save()
            dark = self.palette().color(QPalette.Window).lightness() < 128
            painter.setPen(QPen(QColor("#8ed0bd" if dark else "#216e62"), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)
            painter.restore()


class CollectionReviewDialog(WindowDialog):
    def __init__(self, findings, inventory, deployment_folder, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Card ingest checks")
        self.resize(900, 640)
        self._findings = findings
        self._folder = Path(deployment_folder)
        self._inventory = {
            (row.get("device_label", ""), row.get("new_filename", "")): row
            for row in inventory
        }
        counts = Counter(f["severity"] for f in findings)
        layout = QVBoxLayout(self)
        self.setObjectName("fieldWorkspace")
        zoom = getattr(getattr(parent, "workspace", None), "zoom", 100)
        self.setStyleSheet(workspace_stylesheet(self.palette(), zoom) + """
            QTreeWidget#qcFindings { selection-background-color: transparent; }
            QTreeWidget#qcFindings::item:selected { background: transparent; }
            QTextEdit#qcEvidence { border: none; }
        """)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)
        banner = QFrame()
        banner.setObjectName("sidebarBrandBlock")
        header = QVBoxLayout(banner)
        header.setContentsMargins(18, 12, 18, 12)
        heading = "Review card ingest checks" if findings else "Card ingest checks passed"
        header.addWidget(workspace_label(heading, "sidebarBrand"))
        event = workspace_label(self._folder.name, "muted")
        event.setStyleSheet("color: #dcebe5;")
        header.addWidget(event)
        layout.addWidget(banner)
        summary = workspace_label(
            f"Needs investigation: {counts['error']} · To review: {counts['warning']} · Notices: {counts['info']}"
            if findings else "No findings to address.", "contextTitle"
        )
        summary.setObjectName("qcSummary")
        layout.addWidget(summary)
        self.tree = FindingsTree()
        self.tree.setObjectName("qcFindings")
        self.tree.setAccessibleName("Card ingest findings")
        self.tree.setHeaderLabels(["Finding", "Affected"])
        self.tree.setRootIsDecorated(any(f.get("pending_devices") for f in findings))
        self.tree.setWordWrap(True)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        dark = self.palette().color(QPalette.Window).lightness() < 128
        for finding in findings:
            label, light_color, dark_color = _LEVELS[finding["severity"]]
            color = QColor(dark_color if dark else light_color)
            item = QTreeWidgetItem([
                f"{label} · " + finding["title"].replace(" + ", "\n"), finding["affected"],
            ])
            for column in (0, 1):
                item.setBackground(column, color)
            item.setToolTip(0, finding["title"])
            item.setData(0, Qt.UserRole, finding)
            self.tree.addTopLevelItem(item)
            for device, description in finding.get("pending_devices", []):
                child = QTreeWidgetItem(item, [f"Pending · {description}", device])
                for column in (0, 1):
                    child.setBackground(column, color)
                child.setData(0, Qt.UserRole, dict(finding, message=f"{device} has no staged files."))
        layout.addWidget(self.tree, 3)
        self.tree.ensurePolished()
        visible_rows = min(6, self.tree.topLevelItemCount())
        if visible_rows:
            self.tree.setMinimumHeight(self.tree.header().sizeHint().height() + 8
                                       + sum(self.tree.sizeHintForRow(i) for i in range(visible_rows)))
        card = QFrame()
        card.setObjectName("setupCard")
        evidence = QVBoxLayout(card)
        evidence.setContentsMargins(14, 10, 14, 10)
        layout.addWidget(card, 2)
        self.next_step = QLabel()
        self.next_step.setWordWrap(True)
        self.next_step.setObjectName("qcNextStep")
        evidence.addWidget(self.next_step)
        self.details = QTextEdit()
        self.details.setObjectName("qcEvidence")
        self.details.setAccessibleName("Selected finding evidence")
        self.details.setReadOnly(True)
        evidence.addWidget(self.details, 1)
        controls = QHBoxLayout()
        self.file_picker = QComboBox()
        self.file_picker.setObjectName("qcAffectedFiles")
        self.file_picker.setAccessibleName("Affected file")
        self.file_picker.setMinimumContentsLength(20)
        self.file_picker.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        controls.addWidget(self.file_picker, 1)
        self.open_file = QPushButton("Open file")
        self.open_file.setObjectName("qcOpenFile")
        self.open_file.clicked.connect(self._open_file)
        controls.addWidget(self.open_file)
        evidence.addLayout(controls)
        buttons = QDialogButtonBox()
        back = buttons.addButton("Back to card ingest", QDialogButtonBox.RejectRole)
        proceed = buttons.addButton("Continue to review", QDialogButtonBox.AcceptRole)
        proceed.setProperty("role", "primary")
        back.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.tree.currentItemChanged.connect(self._select_finding)
        self.file_picker.currentIndexChanged.connect(self._update_open_button)
        if findings:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        else:
            self.tree.hide()
            self.details.setPlainText("No findings to address.")
            self.file_picker.hide()
            self.open_file.hide()

    def _select_finding(self, *_):
        self.file_picker.clear()
        item = self.tree.currentItem()
        if not item:
            return
        finding = item.data(0, Qt.UserRole)
        self.next_step.setText(f"<b>Next step:</b> {escape(finding['action'])}")
        seen = set()
        for file in finding["files"]:
            key = (file.get("device_label", ""), file["filename"])
            if key in seen:
                continue
            seen.add(key)
            self.file_picker.addItem(f"{key[0]} · {key[1]}", key)
        self.file_picker.setVisible(bool(seen))
        self.open_file.setVisible(bool(seen))
        self._update_open_button()
        self.details.setPlainText(finding["message"])

    def _audio_selected(self):
        row = self._inventory.get(self.file_picker.currentData(), {})
        return row.get("file_type") == "audio" or Path(row.get("new_filename", "")).suffix.lower() in {".wav", ".flac"}

    def _selected_path(self):
        key = self.file_picker.currentData()
        row = self._inventory.get(key) if key else None
        if row is None:
            return None
        try:
            relative = inventory_storage_relpath(row)
            if not relative:
                return None
            path = (self._folder / relative).resolve()
            path.relative_to(self._folder.resolve())
            if self._audio_selected():
                folder = (self._folder / relative).parent.resolve()
                folder.relative_to(self._folder.resolve())
                return folder
            return path
        except (ValueError, OSError):
            return None

    def _update_open_button(self, *_):
        path = self._selected_path()
        self.open_file.setText("Open folder" if self._audio_selected() else "Open file")
        self.open_file.setEnabled(path is not None)
        self.open_file.setToolTip(str(path) if path else "No matching inventory path is available. See the ingest log.")

    def _open_file(self):
        path = self._selected_path()
        if path is None or not (path.is_dir() if self._audio_selected() else path.is_file()):
            WindowMessageBox.information(self, "Location unavailable", "Check the ingest log and storage location.")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            WindowMessageBox.information(self, "Could not open location", f"Open this location manually:\n{path}")
