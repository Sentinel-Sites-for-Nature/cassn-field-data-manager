"""Readable activity rows with the original diagnostic available for copying."""

import re
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QKeySequence, QPalette
from PySide6.QtWidgets import QAbstractItemView, QApplication, QHeaderView, QTreeWidget, QTreeWidgetItem


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


class ActivityLog(QTreeWidget):
    def __init__(self):
        super().__init__()
        self.setAccessibleName("Activity details")
        self.setColumnCount(3)
        self.setHeaderLabels(["Activity", "Status", "Time"])
        self.header().setStretchLastSection(False)
        self.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2):
            self.header().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.setAlternatingRowColors(True)
        self.setWordWrap(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setIndentation(16)
        self.setMaximumHeight(210)

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
        self.addTopLevelItem(item)
        if has_detail:
            detail = QTreeWidgetItem(item, [body])
            detail.setFirstColumnSpanned(True)
        if level in {"Warning", "Error"}:
            dark = self.palette().color(QPalette.Base).lightness() < 128
            colors = {"Error": "#ffb5ae", "Warning": "#f0ce95"} if dark else {
                "Error": "#a03431", "Warning": "#845510",
            }
            item.setForeground(1, QColor(colors[level]))
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
