"""A compact collection check summary with selected evidence."""
from __future__ import annotations

from collections import Counter
from html import escape
from pathlib import Path

from PySide6.QtCore import QEvent, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPalette
from PySide6.QtWidgets import (
    QComboBox, QFrame, QDialogButtonBox, QHeaderView, QHBoxLayout, QLabel,
    QPushButton, QScrollArea, QTextEdit, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from cassn.core.inventory import inventory_storage_relpath
from cassn.gui.appearance import workspace_label, workspace_stylesheet
from cassn.gui.severity_tree import SeverityTree
from cassn.gui.dialogs import WindowDialog, WindowMessageBox


# Text identifies each level even when the user cannot distinguish the colors.
_LEVELS = {
    "error": ("Investigate", "#fbe2df", "#513330"),
    "warning": ("Review", "#fff1c7", "#4c422a"),
    "info": ("Notice", "#e5f3e9", "#293f34"),
    "pending": ("Pending", "#eff1f2", "#333c43"),
}


def acknowledgement_key(finding):
    if finding["severity"] not in {"error", "warning"}:
        return None
    return finding["evidence_key"]


class FindingsTable(SeverityTree):
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setColumnWidth(1, self.viewport().width() // 3)


class HoldToAcknowledge(QPushButton):
    held = Signal()

    def __init__(self):
        super().__init__("Hold 2 seconds to acknowledge all")
        self.setAutoDefault(False)
        self.setToolTip("Hold the mouse button or Space for two seconds. Release early to cancel.")
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.setInterval(2000)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._finish)
        self.pressed.connect(self._start)
        self.released.connect(self._cancel)

    def _start(self):
        self.setText("Keep holding…")
        self.timer.start()

    def _cancel(self):
        self.timer.stop()
        self.setText("Hold 2 seconds to acknowledge all")

    def _finish(self):
        if self.isDown() and self.isEnabled() and self.isVisible():
            self.held.emit()
        self._cancel()

    def event(self, event):
        if event.type() in (QEvent.Leave, QEvent.Hide, QEvent.FocusOut, QEvent.WindowDeactivate):
            self._cancel()
        return super().event(event)


class CollectionReviewDialog(WindowDialog):
    def __init__(self, findings, inventory, deployment_folder, parent=None, *,
                 acknowledged=(), save_acknowledgements=None):
        super().__init__(parent)
        self.setWindowTitle("Card ingest checks")
        self.resize(900, 640)
        self._save_acknowledgements = save_acknowledgements
        self._acknowledged = set(acknowledged)
        self._required = set()
        self._folder = Path(deployment_folder)
        self._inventory = {
            (row.get("device_label", ""), row.get("new_filename", "")): row
            for row in inventory
        }
        counts = Counter(f["severity"] for f in findings)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 12)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
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
        self.tree = FindingsTable(content)
        self.tree.setObjectName("qcFindings")
        self.tree.setAccessibleName("Card ingest findings")
        self.tree.setHeaderLabels(["Finding", "Affected", "Status"])
        self.tree.setRootIsDecorated(any(f.get("pending_devices") for f in findings))
        self.tree.setWordWrap(True)
        self.tree.setTextElideMode(Qt.ElideNone)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.Interactive)
        self.tree.header().setSectionResizeMode(2, QHeaderView.Fixed)
        dark = self.palette().color(QPalette.Window).lightness() < 128
        for finding in findings:
            label, light_color, dark_color = _LEVELS[finding["severity"]]
            color = QColor(dark_color if dark else light_color)
            item = QTreeWidgetItem([
                f"{label} · " + finding["title"].replace(" + ", "\n"), finding["affected"],
            ])
            for column in range(self.tree.columnCount()):
                item.setBackground(column, color)
            item.setToolTip(0, finding["title"])
            item.setData(0, Qt.UserRole, finding)
            key = acknowledgement_key(finding)
            if key:
                self._required.add(key)
                item.setData(0, Qt.UserRole + 1, key)
                item.setText(2, "Acknowledged" if key in self._acknowledged else "To review")
            self.tree.addTopLevelItem(item)
            for device, description in finding.get("pending_devices", []):
                child = QTreeWidgetItem(item, [f"Pending · {description}", device])
                for column in range(self.tree.columnCount()):
                    child.setBackground(column, color)
                child.setData(0, Qt.UserRole, dict(finding, message=f"{device} has no staged files."))
        layout.addWidget(self.tree, 3)
        self.tree.ensurePolished()
        self.tree.setColumnWidth(2, self.tree.fontMetrics().horizontalAdvance("Acknowledged") + 28)
        visible_rows = min(4, self.tree.topLevelItemCount())
        if visible_rows:
            self.tree.setMinimumHeight(self.tree.header().sizeHint().height() + 8
                                       + sum(self.tree.sizeHintForRow(i) for i in range(visible_rows)))
        layout.addSpacing(8)
        card = QFrame()
        card.setObjectName("setupCard")
        evidence = QVBoxLayout(card)
        evidence.setContentsMargins(14, 10, 14, 10)
        layout.addWidget(card, 2)
        self.detail_title = workspace_label("", "sectionTitle", wrap=True)
        evidence.addWidget(self.detail_title)
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
        acknowledgement_row = QHBoxLayout()
        self.acknowledge = QPushButton("I reviewed this finding\nand took any necessary action")
        self.acknowledge.setAccessibleName("Acknowledge this finding as reviewed")
        self.acknowledge.setProperty("role", "primary")
        self.acknowledge.clicked.connect(self._acknowledge_current)
        acknowledgement_row.addWidget(self.acknowledge, 1)
        self.hold_all = HoldToAcknowledge()
        self.hold_all.held.connect(lambda: self._save_reviewed(self._required))
        self.hold_all.setVisible(bool(self._required))
        self.hold_all.setEnabled(bool(self._required - self._acknowledged))
        acknowledgement_row.addWidget(self.hold_all)
        outer.addLayout(acknowledgement_row)
        self.hold_all.ensurePolished()
        self.hold_all.setMinimumWidth(self.hold_all.sizeHint().width())
        self.save_error = workspace_label("Could not save the acknowledgement. Try again.", "contextTitle", wrap=True)
        outer.addWidget(self.save_error)
        self.save_error.hide()
        buttons = QDialogButtonBox()
        back = buttons.addButton("Back to card ingest", QDialogButtonBox.RejectRole)
        proceed = buttons.addButton("Continue to review", QDialogButtonBox.AcceptRole)
        self.proceed = proceed
        proceed.setEnabled(self._required <= self._acknowledged)
        proceed.setProperty("role", "primary" if proceed.isEnabled() else "")
        back.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.tree.currentItemChanged.connect(self._select_finding)
        self.file_picker.currentIndexChanged.connect(self._update_open_button)
        if findings:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        else:
            self.acknowledge.hide()
            self.detail_title.hide()
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
        key = item.data(0, Qt.UserRole + 1)
        self.acknowledge.setVisible(bool(key))
        self.acknowledge.setEnabled(bool(key and key not in self._acknowledged))
        self.acknowledge.setText("Finding acknowledged" if key in self._acknowledged else
                                 "I reviewed this finding\nand took any necessary action")
        self.detail_title.setText(finding.get("detail_title", finding["title"]))
        explanation = finding.get("explanation", "")
        lines = [escape(text) for text in (explanation, finding["action"]) if text]
        self.next_step.setText("<br>".join(lines))
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

    def _acknowledge_current(self):
        item = self.tree.currentItem()
        key = item.data(0, Qt.UserRole + 1)
        if key not in self._required:
            return
        self._save_reviewed({key})

    def _save_reviewed(self, keys):
        updated = (self._acknowledged | keys) & self._required
        if self._save_acknowledgements and not self._save_acknowledgements(updated):
            self.save_error.show()
            return
        self.save_error.hide()
        self._acknowledged = updated
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            if item.data(0, Qt.UserRole + 1) in updated:
                item.setText(2, "Acknowledged")
        self.acknowledge.setEnabled(False)
        self.acknowledge.setText("Finding acknowledged")
        complete = self._required <= updated
        self.hold_all.setEnabled(not complete)
        self.proceed.setEnabled(complete)
        if complete:
            self.proceed.setProperty("role", "primary")
            self.proceed.style().unpolish(self.proceed)
            self.proceed.style().polish(self.proceed)

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
