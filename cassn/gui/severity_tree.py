"""Draw severity across a tree row, including the disclosure gutter."""

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QPalette, QPen
from PySide6.QtWidgets import QTreeWidget


class SeverityTree(QTreeWidget):
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
