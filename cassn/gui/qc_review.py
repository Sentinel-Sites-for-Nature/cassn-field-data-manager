"""A compact collection check summary with selected evidence."""
from __future__ import annotations

from collections import Counter
from html import escape
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QComboBox, QDialogButtonBox, QHeaderView, QHBoxLayout, QLabel,
    QPushButton, QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from cassn.core.inventory import inventory_storage_relpath
from cassn.gui.dialogs import WindowDialog, WindowMessageBox


# Text identifies each level even when the user cannot distinguish the colors.
_LEVELS = {
    "error": ("Investigate", "#aa2828", "#ffaaa6"),
    "warning": ("Review", "#805100", "#ffd185"),
    "info": ("Notice", "#195c92", "#9ad2ff"),
    "pending": ("Pending", "#545c64", "#c3cad1"),
}


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
        heading = QLabel("Review card ingest checks" if findings else "Card ingest checks passed")
        font = heading.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        heading.setFont(font)
        layout.addWidget(heading)
        summary = QLabel(
            f"Needs investigation: {counts['error']} · To review: {counts['warning']} · Notices: {counts['info']}"
            if findings else "No findings to address."
        )
        summary.setObjectName("qcSummary")
        layout.addWidget(summary)
        explanation = QLabel(
            "Select a finding for affected files and next steps. Counts above refer to grouped findings.\n"
            "You can continue to review the staged data. Nothing uploads until you start an upload."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.tree = QTreeWidget()
        self.tree.setObjectName("qcFindings")
        self.tree.setAccessibleName("Card ingest findings")
        self.tree.setHeaderLabels(["Finding", "Affected"])
        self.tree.setRootIsDecorated(any(f.get("pending_devices") for f in findings))
        self.tree.setWordWrap(True)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        dark = self.tree.palette().base().color().lightness() < 128
        for finding in findings:
            label, light_color, dark_color = _LEVELS[finding["severity"]]
            color = QColor(dark_color if dark else light_color)
            item = QTreeWidgetItem([
                f"{label} · " + finding["title"].replace(" + ", "\n"), finding["affected"],
            ])
            item.setData(0, Qt.DecorationRole, color)
            item.setToolTip(0, finding["title"])
            item.setData(0, Qt.UserRole, finding)
            self.tree.addTopLevelItem(item)
            for device, description in finding.get("pending_devices", []):
                child = QTreeWidgetItem(item, [f"Pending · {description}", device])
                child.setData(0, Qt.DecorationRole, color)
                child.setData(0, Qt.UserRole, dict(finding, message=f"{device} has no staged files."))
        layout.addWidget(self.tree, 2)
        self.next_step = QLabel()
        self.next_step.setWordWrap(True)
        self.next_step.setObjectName("qcNextStep")
        layout.addWidget(self.next_step)
        self.details = QTextEdit()
        self.details.setObjectName("qcEvidence")
        self.details.setAccessibleName("Selected finding evidence")
        self.details.setReadOnly(True)
        layout.addWidget(self.details, 3)
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
        layout.addLayout(controls)
        buttons = QDialogButtonBox()
        back = buttons.addButton("Back to card ingest", QDialogButtonBox.RejectRole)
        buttons.addButton("Continue to review", QDialogButtonBox.AcceptRole)
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
