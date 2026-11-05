"""Readable activity rows with the original diagnostic available for copying."""

import re
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QKeySequence, QPalette
from PySide6.QtWidgets import QAbstractItemView, QApplication, QComboBox, QHeaderView, QTreeWidgetItem

from cassn.gui.severity_tree import SeverityTree


# Workers send both "⚠ [device] message" and "[device] Warning: message".
_PREFIX = re.compile(
    r"^(?:(QC)\s+(?=\[)|(\[[^\]\n]+\])\s*|"
    r"(ERROR:|Warning:|Pass:|✗|⚠|✓)\s*)", re.IGNORECASE
)
_LEVELS = {
    "error:": "Error", "✗": "Error", "warning:": "Warning", "⚠": "Warning",
    "pass:": "Passed", "✓": "Done",
}


def activity_entry(message):
    remaining = message.strip()
    device = ""
    level = "Info"
    while match := _PREFIX.match(remaining):
        quality_check, label, marker = match.groups()
        if quality_check:
            level = "Warning"
        if label:
            device = label[1:-1]
        if marker:
            level = _LEVELS[marker.lower()]
        remaining = remaining[match.end():].lstrip()
    return level, device, remaining or message.strip()


def activity_summary(body):
    # Lead with the operator's action. Keep paths, hashes and causes in the detail row.
    if body.startswith("Source card disconnected or unmounted while copying from "):
        return "Card disconnected. Reconnect it and retry."
    if body.startswith("Equal-byte acquisition requires review:"):
        return "Two files have identical contents. Keep the card and review the files."
    if body.startswith("Staged bytes conflict with inventory at "):
        return "The staged file differs from the inventory. Review the file before retrying."
    first_line = body.partition("\n")[0]
    if len(first_line) <= 140:
        return first_line
    return first_line[:137].rsplit(" ", 1)[0] + "…"


def operation_message(message, success, warning=False):
    if warning:
        return f"Warning: {message}"
    if success:
        return f"✓ {message}"
    return f"Error: {message}"


class ActivityLog(SeverityTree):
    def __init__(self):
        super().__init__()
        self.device_filter = QComboBox()
        self.device_filter.setAccessibleName("Filter activity by device")
        self.device_filter.addItem("All devices", "")
        self.device_filter.currentIndexChanged.connect(self._filter_rows)
        self.setAccessibleName("Activity details")
        self.setColumnCount(3)
        self.setHeaderLabels(["Activity", "Status", "Time"])
        self.header().setStretchLastSection(False)
        self.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2):
            self.header().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.setObjectName("activityRows")
        self.setStyleSheet("""
            QTreeWidget#activityRows { selection-background-color: transparent; }
            QTreeWidget#activityRows::item:selected { background: transparent; }
        """)
        self.setAlternatingRowColors(True)
        self.setWordWrap(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setIndentation(16)
        self.setMaximumHeight(210)

    def clear(self):
        super().clear()
        self.device_filter.clear()
        self.device_filter.addItem("All devices", "")

    def _filter_rows(self):
        device = self.device_filter.currentData()
        for row in range(self.topLevelItemCount()):
            item = self.topLevelItem(row)
            item.setHidden(bool(device and item.data(0, Qt.UserRole + 1) != device))

    def _device_index(self, device):
        index = self.device_filter.findData(device)
        if index < 0:
            self.device_filter.addItem(device, device)
            return self.device_filter.count() - 1
        return index

    def select_device(self, device):
        self.device_filter.setCurrentIndex(self._device_index(device))

    def add_message(self, message):
        level, device, body = activity_entry(message)
        summary = activity_summary(body)
        has_detail = summary != body
        if device:
            summary = f"{device} · {summary}"
        timestamp = datetime.now().strftime("%H:%M:%S")
        item = QTreeWidgetItem([summary, level, timestamp])
        item.setData(0, Qt.UserRole, f"[{timestamp}] {message}")
        scroll = self.verticalScrollBar()
        follow = scroll.value() == scroll.maximum()
        if device:
            self._device_index(device)
        item.setData(0, Qt.UserRole + 1, device)
        self.addTopLevelItem(item)
        selected = self.device_filter.currentData()
        item.setHidden(bool(selected and selected != device))
        if has_detail:
            detail = QTreeWidgetItem(item, [body])
            detail.setFirstColumnSpanned(True)
        if level in {"Warning", "Error"}:
            dark = self.palette().color(QPalette.Base).lightness() < 128
            colors = {"Error": ("#513330", "#fff1ef"), "Warning": ("#4c422a", "#fff5d9")} if dark else {
                "Error": ("#fbe2df", "#662420"), "Warning": ("#fff1c7", "#503d12"),
            }
            background, text = colors[level]
            for entry in (item, item.child(0)):
                if entry is not None:
                    for column in range(self.columnCount()):
                        entry.setBackground(column, QColor(background))
                        entry.setForeground(column, QColor(text))
            font = item.font(1)
            font.setBold(True)
            item.setFont(1, font)
        if follow:
            self.scrollToBottom()

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.Copy) and self.selectedItems():
            entries = [item.data(0, Qt.UserRole) or item.text(0) for item in self.selectedItems()]
            QApplication.clipboard().setText("\n".join(entries))
            return
        super().keyPressEvent(event)
