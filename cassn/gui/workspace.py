"""Presentation shell for the field workspace; workflow decisions stay in the wizard."""

from pathlib import Path

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QTabWidget,
    QVBoxLayout, QWidget,
)


PAGE_NAMES = ("Deployment", "Card ingest", "Review & upload")


def workspace_stylesheet(palette: QPalette) -> str:
    """Use the OS appearance with restrained forest accents and readable surfaces."""
    dark = palette.color(QPalette.Window).lightness() < 128
    if dark:
        background, panel, text, muted = "#191e24", "#242b33", "#edf1f4", "#acbac6"
        line, soft, accent, tint, on_accent = "#3e4b56", "#303b45", "#8ed0bd", "#29453e", "#152d25"
    else:
        background, panel, text, muted = "#f4f6f7", "#ffffff", "#24333d", "#5c6d79"
        line, soft, accent, tint, on_accent = "#dce3e7", "#edf1f3", "#216e62", "#e6f1ed", "#ffffff"
    return f"""
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
QToolTip {{ background: {panel}; color: {text}; border: 1px solid {line}; padding: 5px; }}
"""


def workspace_label(text: str, role: str, *, wrap: bool = False) -> QLabel:
    label = QLabel(text)
    label.setProperty("role", role)
    label.setWordWrap(wrap)
    label.setTextFormat(Qt.PlainText)
    return label


class FieldWorkspace(QWidget):
    """Sidebar and persistent deployment context around the existing workflow pages."""

    page_requested = Signal(int)
    compact_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("fieldWorkspace")
        self.setStyleSheet(workspace_stylesheet(QApplication.palette()))
        self._compact = None
        shell = QHBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("workspaceSidebar")
        sidebar.setFixedWidth(200)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 28, 18, 22)
        side.setSpacing(8)
        mark = workspace_label("CA", "brandMark")
        mark.setFixedSize(44, 44)
        mark.setAlignment(Qt.AlignCenter)
        side.addWidget(mark)
        side.addSpacing(8)
        side.addWidget(workspace_label("CA-SSN", "brand"))
        side.addWidget(workspace_label("Field Data Manager", "muted"))
        side.addSpacing(24)
        self.navigation = []
        for index, name in enumerate(PAGE_NAMES):
            button = QPushButton(name.replace("&", "&&"))
            button.setProperty("role", "navigation")
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setAccessibleName(name)
            button.clicked.connect(lambda _checked=False, i=index: self.page_requested.emit(i))
            side.addWidget(button)
            self.navigation.append(button)
        side.addSpacing(24)
        side.addWidget(workspace_label("CURRENT DEPLOYMENT", "muted"))
        self.context_site = workspace_label("Choose a reserve", "contextTitle", wrap=True)
        self.context_dates = workspace_label("", "muted", wrap=True)
        self.context_devices = workspace_label("", "muted", wrap=True)
        side.addWidget(self.context_site)
        side.addWidget(self.context_dates)
        side.addWidget(self.context_devices)
        side.addStretch()
        self.box_status = workspace_label("", "muted", wrap=True)
        self.staging_status = workspace_label("", "muted", wrap=True)
        self.staging_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        side.addWidget(self.box_status)
        side.addSpacing(8)
        side.addWidget(workspace_label("LOCAL STAGING", "muted"))
        side.addWidget(self.staging_status)
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

    def _sync_page(self, index):
        for i, button in enumerate(self.navigation):
            button.setChecked(i == index)
        self.set_page_context(index)

    def set_page_context(self, index):
        if 0 <= index < len(PAGE_NAMES):
            site = self.context_site.text()
            self.breadcrumb.setText(f"{site}  /  {PAGE_NAMES[index]}")
            self.breadcrumb.setToolTip(self.breadcrumb.text())

    def set_context(self, *, site, dates, devices, staging, box_connected, active_jobs):
        self.context_site.setText(site or "Choose a reserve")
        self.context_dates.setText(dates)
        self.context_devices.setText(devices)
        self.staging_status.setText(
            self.staging_status.fontMetrics().elidedText(
                Path(staging).name or staging, Qt.ElideMiddle, 164,
            )
        )
        self.staging_status.setToolTip(staging)
        self.staging_status.setAccessibleName(f"Local staging: {staging}")
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
            self.setStyleSheet(workspace_stylesheet(QApplication.palette()))


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
