"""Shared, responsive progress columns for the Review & Upload panels."""

from PySide6.QtCore import QEvent, QRect, QSize, Qt
from PySide6.QtWidgets import QSizePolicy, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from cassn.gui.appearance import workspace_label
from cassn.gui.workspace import UploadProgressBar


class ProgressTable(QTreeWidget):
    def __init__(self, heading):
        super().__init__()
        self._fitting = False
        self._single_bar = None
        self.setHeaderLabels([heading, "Progress"])
        self.setWordWrap(True)
        self.setRootIsDecorated(False)
        self.header().setStretchLastSection(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

    def add_bar(self, name, bar, *, detail="", detail_tip="", index=None):
        item = QTreeWidgetItem([name])
        self.insertTopLevelItem(self.topLevelItemCount() if index is None else index, item)
        item.setToolTip(0, name)
        identity = QWidget()
        labels = QVBoxLayout(identity)
        labels.setContentsMargins(0, 0, 0, 0)
        labels.setSpacing(2)
        title = workspace_label(name.replace("_", "_\u200b"), "contextTitle", wrap=True)
        title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        labels.addWidget(title)
        if detail:
            subtitle = workspace_label(detail, "muted", wrap=True)
            subtitle.setToolTip(detail_tip)
            subtitle.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            labels.addWidget(subtitle)
        for column, widget in enumerate((identity, bar)):
            cell = QWidget()
            cell.setAutoFillBackground(True)
            cell.setBackgroundRole(self.viewport().backgroundRole())
            box = QVBoxLayout(cell)
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(2)
            box.addWidget(widget)
            self.setItemWidget(item, column, cell)
        bar.setProperty("wrapProgressText", True)
        return item

    def add_single_bar(self, name, bar):
        """Hide the entire row whenever the existing workflow hides its only bar."""
        self.setHeaderHidden(True)
        self.add_bar(name, bar)
        self._single_bar = bar
        bar.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is self._single_bar and event.type() in (QEvent.ShowToParent, QEvent.HideToParent):
            self.setVisible(not obj.isHidden())
        return super().eventFilter(obj, event)

    def bar_for_item(self, item):
        cell = self.itemWidget(item, 1)
        return cell.findChild(UploadProgressBar) if cell else None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self.fit_rows()

    def showEvent(self, event):
        super().showEvent(event)
        self.fit_rows()

    def fit_rows(self):
        if self._fitting:
            return
        self._fitting = True
        try:
            width = self.viewport().width()
            metrics = self.fontMetrics()
            compact = width < metrics.horizontalAdvance("Camera deployment    Verifying 9,999 of 9,999 photos · 100%") + 32
            self.setColumnHidden(0, compact)
            if not compact:
                self.setColumnWidth(0, min(420, width // 2))
            name_width = max(1, (width if compact else self.columnWidth(0)) - 14)
            bar_width = max(1, (width if compact else width - self.columnWidth(0)) - 14)
            for i in range(self.topLevelItemCount()):
                item = self.topLevelItem(i)
                left, right = self.itemWidget(item, 0), self.itemWidget(item, 1)
                if not left or not right:
                    continue
                if compact and left.layout().count():
                    right.layout().insertWidget(0, left.layout().itemAt(0).widget())
                elif not compact and not left.layout().count():
                    left.layout().addWidget(right.layout().itemAt(0).widget())
                identity = (right if compact else left).layout().itemAt(0).widget()
                bar = self.bar_for_item(item)
                label = bar.text() if bar.text() else bar.format()
                bar_height = max(bar.fontMetrics().height(), bar.fontMetrics().boundingRect(
                    QRect(0, 0, bar_width, 8192), Qt.TextWordWrap, label).height()) + 6
                if bar.minimumHeight() != bar_height:
                    bar.setStyleSheet(f"min-height: {bar_height}px; max-height: {bar_height}px;")
                name_height = identity.layout().heightForWidth(name_width)
                identity.setFixedHeight(name_height)
                height = name_height + 2 + bar_height if compact else max(name_height, bar_height)
                item.setSizeHint(1, QSize(0, height + 19))
            self.doItemsLayout()
            rows = sum(self.visualItemRect(self.topLevelItem(i)).height() for i in range(self.topLevelItemCount()))
            heading = 0 if self.isHeaderHidden() else self.header().sizeHint().height()
            self.setFixedHeight(rows + heading + 4)
        finally:
            self._fitting = False
