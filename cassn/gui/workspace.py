"""Presentation shell for the field workspace; workflow decisions stay in the wizard."""

import re

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QAction, QKeySequence, QPalette, QPixmap, QTransform
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QTabWidget,
    QVBoxLayout, QWidget, QStyledItemDelegate, QStyle,
)

from cassn.config import BUNDLE_DIR


PAGE_NAMES = ("Deployments and devices", "Card ingest", "Review & upload")
ARTWORK = BUNDLE_DIR / "assets" / "workspace"


def workspace_stylesheet(palette: QPalette, zoom: int = 100) -> str:
    """Use the OS appearance with restrained forest accents and readable surfaces."""
    dark = palette.color(QPalette.Window).lightness() < 128
    if dark:
        background, panel, text, muted = "#191e24", "#242b33", "#edf1f4", "#acbac6"
        line, soft, accent, tint, on_accent = "#3e4b56", "#303b45", "#8ed0bd", "#29453e", "#152d25"
    else:
        background, panel, text, muted = "#f4f6f7", "#ffffff", "#24333d", "#5c6d79"
        line, soft, accent, tint, on_accent = "#dce3e7", "#edf1f3", "#216e62", "#e6f1ed", "#ffffff"
    stylesheet = f"""
QWidget#fieldWorkspace {{ background: {background}; color: {text}; }}
QWidget {{ color: {text}; font-family: '.AppleSystemUIFont', 'Segoe UI', sans-serif; font-size: 13px; }}
QFrame#workspaceSidebar, QFrame#workspaceHeader {{ background: {panel}; }}
QFrame#workspaceSidebar {{ border-right: 1px solid {line}; }}
QFrame#workspaceHeader {{ border-bottom: 1px solid {line}; }}
QLabel {{ background: transparent; }}
QLabel[role="brand"] {{ font-size: 22px; font-weight: 600; }}
QLabel[role="brandMark"] {{ background: {accent}; color: {on_accent}; border-radius: 9px; padding: 10px; font-weight: 600; }}
QLabel[role="muted"], QLabel[role="subtitle"] {{ color: {muted}; }}
QLabel[role="subtitle"] {{ margin-bottom: 10px; }}
QLabel[role="pageTitle"] {{ font-size: 25px; font-weight: 600; margin-top: 2px; }}
QLabel[role="sectionTitle"] {{ font-size: 15px; font-weight: 600; }}
QLabel[role="contextTitle"] {{ font-weight: 600; font-size: 14px; }}
QLabel[role="connected"] {{ color: {accent}; }}
QLabel#workspaceEmptyCards {{ color: {muted}; background: {panel}; border: 1px dashed {line}; border-radius: 10px; padding: 24px; }}
QPushButton, QToolButton {{ background: {panel}; border: 1px solid {line}; border-radius: 6px; padding: 8px 12px; }}
QPushButton:hover, QToolButton:hover {{ background: {soft}; }}
QPushButton:pressed, QToolButton:pressed {{ background: {tint}; }}
QPushButton:disabled, QToolButton:disabled {{ color: {muted}; background: {soft}; }}
QPushButton[role="primary"] {{ background: {accent}; color: {on_accent}; border-color: {accent}; font-weight: 600; }}
QPushButton[role="primary"]:disabled {{ background: {soft}; color: {muted}; border-color: {line}; }}
QPushButton[role="navigation"] {{ text-align: left; background: transparent; border: none; padding: 12px; }}
QPushButton[role="navigation"]:hover {{ background: {soft}; }}
QPushButton[role="navigation"]:checked {{ background: {tint}; color: {accent}; font-weight: 600; }}
QPushButton:focus, QToolButton:focus, QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QSpinBox:focus {{ border: 1px solid {accent}; }}
QGroupBox {{ background: {panel}; border: 1px solid {line}; border-radius: 10px; margin-top: 18px; padding: 18px 12px 12px; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 12px; padding: 0 5px; color: {text}; }}
QLineEdit, QComboBox, QDateEdit, QSpinBox {{ background: {panel}; color: {text}; border: 1px solid {line}; border-radius: 5px; padding: 7px; selection-background-color: {accent}; selection-color: {on_accent}; }}
QLineEdit:read-only, QDateEdit:read-only {{ background: {background}; }}
QComboBox QAbstractItemView {{ background: {panel}; color: {text}; selection-background-color: {tint}; selection-color: {text}; }}
QCheckBox {{ spacing: 7px; }}
QTreeWidget, QTextEdit {{ background: {panel}; color: {text}; border: 1px solid {line}; border-radius: 6px; selection-background-color: {tint}; selection-color: {text}; alternate-background-color: {background}; }}
QTreeWidget {{ outline: none; }}
QTreeWidget::item {{ padding: 8px 5px; border-bottom: 1px solid {soft}; }}
QTreeWidget::item:selected {{ background: {tint}; color: {text}; }}
QHeaderView::section {{ background: {background}; color: {muted}; border: none; border-bottom: 1px solid {line}; padding: 10px 5px; font-weight: normal; }}
QProgressBar {{ background: {soft}; border: none; border-radius: 4px; min-height: 8px; max-height: 8px; color: transparent; }}
QProgressBar::chunk {{ background: {accent}; border-radius: 4px; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: {background}; }}
QScrollBar:vertical {{ background: {background}; width: 10px; margin: 0; }}
QScrollBar:horizontal {{ background: {background}; height: 10px; margin: 0; }}
QScrollBar::handle {{ background: {line}; border-radius: 4px; min-width: 24px; min-height: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QTabWidget::pane {{ border: none; background: {background}; }}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:hover {{ background: {line}; }}
QMenu {{ background: {panel}; color: {text}; border: 1px solid {line}; padding: 5px; }}
QMenu::item {{ padding: 9px 18px; }}
QMenu::item:selected {{ background: {tint}; }}
QMenu::item:disabled {{ color: {muted}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox {{ padding-right: 30px; }}
QComboBox::down-arrow {{ image: url({(ARTWORK / 'chevron.svg').as_posix()}); width: 16px; height: 16px; }}
QToolTip {{ background: {panel}; color: {text}; border: 1px solid {line}; padding: 5px; }}
"""
    return re.sub(r"font-size: (\d+)px", lambda m: f"font-size: {round(int(m[1]) * zoom / 100)}px", stylesheet)


def event_description(event):
    start = event["deployment_event_start_date"]
    end = event["deployment_event_end_date"]
    count = event.get("deployment_count", event.get("device_count", 0))
    dates = f"{start} to {end}" if end else f"Deployed {start} · In field"
    return f"{dates} · {count} deployment{'s' if count != 1 else ''}"


class EventDelegate(QStyledItemDelegate):
    """Keep event names prominent and date/count descriptors secondary in the picker."""

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setHeight(option.fontMetrics.height() * 2 + 20)
        return size

    def paint(self, painter, option, index):
        event = index.data(Qt.UserRole)
        if not event:
            return super().paint(painter, option, index)
        self.initStyleOption(option, index)
        option.text = ""
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, option, painter, option.widget)
        painter.save()
        rect = option.rect.adjusted(10, 4, -10, -4)
        font = option.font
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(option.palette.color(QPalette.Text))
        painter.drawText(rect, Qt.AlignTop | Qt.AlignLeft, option.fontMetrics.elidedText(index.data(), Qt.ElideRight, rect.width()))
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(option.palette.color(QPalette.PlaceholderText))
        painter.drawText(rect, Qt.AlignBottom | Qt.AlignLeft, option.fontMetrics.elidedText(event_description(event), Qt.ElideRight, rect.width()))
        painter.restore()


def device_heading(code, name):
    heading = QWidget()
    layout = QHBoxLayout(heading)
    layout.setContentsMargins(0, 0, 0, 4)
    image = QLabel()
    pixmap = QPixmap(str(ARTWORK / ("reconyx.png" if code in {"ML", "SA"} else "audiomoth.jpeg")))
    if code == "SA":
        pixmap = pixmap.transformed(QTransform().rotate(90), Qt.SmoothTransformation)
    image.setPixmap(pixmap.scaled(46, 46, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    image.setAccessibleName(f"{code} device")
    layout.addWidget(image)
    text = workspace_label(f"{'🐦 ' if code == 'BD' else '🦇 ' if code == 'BT' else ''}{name}", "sectionTitle", wrap=True)
    layout.addWidget(text, 1)
    return heading


def workspace_label(text: str, role: str, *, wrap: bool = False) -> QLabel:
    label = QLabel(text)
    label.setProperty("role", role)
    label.setWordWrap(wrap)
    label.setTextFormat(Qt.PlainText)
    return label


class FieldWorkspace(QWidget):
    """Ordered ingest navigation around the workflow pages."""

    page_requested = Signal(int)
    compact_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("fieldWorkspace")
        self.setStyleSheet(workspace_stylesheet(QApplication.palette()))
        self.zoom = 100
        self._compact = None
        self._site = "Choose a reserve"
        shell = QHBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("workspaceSidebar")
        self.sidebar = sidebar
        sidebar.setFixedWidth(252)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 28, 18, 22)
        side.setSpacing(8)
        side.addWidget(workspace_label("CA-SSN", "brand"))
        side.addWidget(workspace_label("Field Data Manager", "muted"))
        side.addSpacing(24)
        side.addWidget(workspace_label("Ingest data", "sectionTitle"))
        side.addWidget(workspace_label("Complete steps 1 to 3", "muted"))
        self.navigation = []
        for index, name in enumerate(PAGE_NAMES):
            button = QPushButton(f"{index + 1}  " + name.replace("&", "&&"))
            button.setToolTip(f"Step {index + 1} of 3: {name}")
            button.setProperty("role", "navigation")
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setAccessibleName(name)
            button.clicked.connect(lambda _checked=False, i=index: self.page_requested.emit(i))
            side.addWidget(button)
            self.navigation.append(button)
        side.addStretch()
        self.box_status = workspace_label("", "muted", wrap=True)
        side.addWidget(self.box_status)
        shell.addWidget(sidebar)

        content = QWidget()
        content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        header = QFrame()
        header.setObjectName("workspaceHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(26, 18, 26, 18)
        self.breadcrumb = workspace_label("Deployment", "muted")
        self.breadcrumb.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.header_status = workspace_label("", "muted")
        header_layout.addWidget(self.breadcrumb, 1)
        header_layout.addWidget(self.header_status)
        body.addWidget(header)
        self.pages = QTabWidget()
        self.pages.tabBar().hide()
        self.pages.currentChanged.connect(self._sync_page)
        body.addWidget(self.pages, 1)
        shell.addWidget(content, 1)
        view_menu = parent.menuBar().addMenu("View")
        self.zoom_in_action = QAction("Zoom In", parent)
        self.zoom_in_action.setShortcuts([QKeySequence("Ctrl++"), QKeySequence("Ctrl+=")])
        self.zoom_out_action = QAction("Zoom Out", parent)
        self.zoom_out_action.setShortcut(QKeySequence("Ctrl+-"))
        self.zoom_reset_action = QAction("Actual Size", parent)
        self.zoom_reset_action.setShortcut(QKeySequence("Ctrl+0"))
        for action in (self.zoom_in_action, self.zoom_out_action, self.zoom_reset_action):
            view_menu.addAction(action)
        self.zoom_in_action.triggered.connect(lambda: self.set_zoom(self.zoom + 10))
        self.zoom_out_action.triggered.connect(lambda: self.set_zoom(self.zoom - 10))
        self.zoom_reset_action.triggered.connect(lambda: self.set_zoom(100))

    def set_zoom(self, percent):
        self.zoom = max(80, min(200, percent))
        self.setStyleSheet(workspace_stylesheet(QApplication.palette(), self.zoom))
        self.sidebar.setFixedWidth(round(252 * self.zoom / 100))
        self.zoom_in_action.setEnabled(self.zoom < 200)
        self.zoom_out_action.setEnabled(self.zoom > 80)
        self.zoom_reset_action.setText(f"Actual Size (zoom {self.zoom}%)")

    def _sync_page(self, index):
        for i, button in enumerate(self.navigation):
            button.setChecked(i == index)
        self.set_page_context(index)

    def set_page_context(self, index):
        if 0 <= index < len(PAGE_NAMES):
            site = self._site
            self.breadcrumb.setText(f"{site}  /  {PAGE_NAMES[index]}")
            self.breadcrumb.setToolTip(self.breadcrumb.text())

    def set_context(self, *, site, dates, devices, staging, box_connected, active_jobs):
        self._site = site or "Choose a reserve"
        self.box_status.setText("● Box connected" if box_connected else "○ Box not connected")
        role = "connected" if box_connected else "muted"
        if self.box_status.property("role") != role:
            self.box_status.setProperty("role", role)
            self.box_status.style().unpolish(self.box_status)
            self.box_status.style().polish(self.box_status)
        self.header_status.setText(
            f"{active_jobs} card{'s' if active_jobs != 1 else ''} copying"
            if active_jobs else ""
        )
        self.set_page_context(self.pages.currentIndex())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        compact = self.width() < 1180
        if compact != self._compact:
            self._compact = compact
            self.compact_changed.emit(compact)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.ApplicationPaletteChange:
            self.setStyleSheet(workspace_stylesheet(QApplication.palette(), self.zoom))


class CardJobsScroll(QScrollArea):
    """Reflow jobs when resizing the window or dragging the workspace divider."""

    columns_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._columns = None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        columns = 2 if self.viewport().width() >= 680 else 1
        if columns != self._columns:
            self._columns = columns
            self.columns_changed.emit()


class DeviceGridScroll(QScrollArea):
    """Scroll a wide device table without stretching the surrounding form."""

    def __init__(self, content):
        super().__init__()
        self.setWidgetResizable(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setWidget(content)
        content.installEventFilter(self)

    def eventFilter(self, watched, event):
        if watched is self.widget() and event.type() == QEvent.LayoutRequest:
            height = self.widget().sizeHint().height() + self.horizontalScrollBar().sizeHint().height() + 4
            if self.height() != height:
                self.setFixedHeight(height)
        return super().eventFilter(watched, event)
