"""Shared presentation helpers, safe to import before choosing a project."""
from functools import lru_cache
from pathlib import Path
import re

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QIcon, QPalette, QPixmap
from PySide6.QtWidgets import QGraphicsOpacityEffect, QGroupBox, QHBoxLayout, QLabel, QMenu, QSizePolicy, QVBoxLayout

ARTWORK = Path(__file__).resolve().parents[2] / "assets" / "workspace"


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
QFrame#workspaceSidebar {{ background: {background}; border-right: 1px solid {line}; }}
QScrollArea#sidebarScroll, QWidget#sidebarContent {{ background: {background}; border: none; }}
QFrame#sidebarBrandBlock {{ background: #174c3c; border: none; border-radius: 10px; }}
QFrame#sidebarOrganization {{ background: #ffffff; border: 1px solid {line}; border-radius: 10px; }}
QFrame#sidebarStatus {{ background: transparent; border: none; border-top: 1px solid {line}; }}
QLabel[role="organizationCaption"] {{ color: #5c6d79; font-size: 12px; font-weight: 600; }}
QFrame#setupCard {{ background: {panel}; border: 1px solid {line}; border-radius: 10px; }}
QLabel {{ background: transparent; }}
QLabel[role="brand"] {{ font-size: 15px; font-weight: 600; }}
QLabel[role="sidebarBrand"] {{ color: #ffffff; font-size: 22px; font-weight: 600; }}
QLabel[role="sidebarSection"] {{ font-size: 19px; font-weight: 600; }}
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
QPushButton#boxConnectButton {{ background: #ffffff; color: #1267cf; border: 1px solid #b9cfe8; font-size: 16px; font-weight: 600; border-radius: 8px; padding: 12px 20px; }}
QPushButton#boxConnectButton:hover {{ background: #edf5ff; border-color: #1267cf; }}
QPushButton#boxConnectButton:pressed {{ background: #dcecff; }}
QPushButton#boxConnectButton:focus {{ border: 2px solid #1267cf; }}
QPushButton#boxConnectButton:disabled {{ background: {soft}; color: {muted}; border-color: {line}; }}
QPushButton[role="navigation"] {{ text-align: left; background: transparent; border: 1px solid transparent; border-radius: 9px; padding: 0; }}
QPushButton[role="navigation"]:hover {{ background: {soft}; }}
QPushButton[role="navigation"]:checked {{ background: {tint}; border-color: {accent}; }}
QLabel[role="stepTitle"] {{ font-size: 14px; font-weight: 500; }}
QLabel[role="stepNumber"] {{ background: {panel}; color: {muted}; border: 1px solid {line}; border-radius: 8px; min-width: 28px; max-width: 28px; min-height: 28px; max-height: 28px; font-size: 12px; font-weight: 600; }}
QLabel[role="stepNumber"][current="true"] {{ background: {accent}; color: {on_accent}; border-color: {accent}; }}
QLabel[role="stepTitle"][current="true"] {{ color: {accent}; font-weight: 600; }}
QPushButton:focus, QToolButton:focus, QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QSpinBox:focus {{ border: 1px solid {accent}; }}
QGroupBox {{ background: {panel}; border: 1px solid {line}; border-radius: 10px; margin-top: 18px; padding: 18px 12px 12px; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 12px; padding: 0 5px; color: {text}; }}
QGroupBox[role="cardJob"] {{ padding: 0px 8px 8px; }}
QGroupBox[role="platformPanel"] {{ margin-top: 0; padding: 8px; }}
QLabel[role="platformLogo"] {{ background: white; border-radius: 3px; padding: 2px 4px; }}
QToolButton#prepareUpload {{ background: {accent}; color: {on_accent}; border-color: {accent}; padding: 10px 16px; font-weight: 600; }}
QToolButton#prepareUpload::menu-indicator {{ image: none; width: 0; height: 0; }}
QToolButton#prepareUpload:pressed {{ background: {"#a4dccc" if dark else "#174c3c"}; color: {on_accent}; }}
QGroupBox[role="cardJob"] QPushButton, QGroupBox[role="cardJob"] QToolButton {{ padding: 5px 9px; }}
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
QProgressBar#boxUploadProgress {{ min-height: 24px; max-height: 16777215px; color: {text}; text-align: center; }}
QProgressBar#boxUploadProgress::chunk {{ background: {accent}; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: {background}; }}
QWidget#cardJobsArea {{ background: {panel}; }}
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



def workspace_label(text: str, role: str, *, wrap: bool = False) -> QLabel:
    label = QLabel(text)
    label.setProperty("role", role)
    label.setWordWrap(wrap)
    label.setTextFormat(Qt.PlainText)
    return label


_PLATFORMS = {
    "box": ("Box", "box.webp"),
    "soundhub": ("Wildlife Sound Hub", "wildlife-soundhub.png"),
    "wildlife_insights": ("Wildlife Insights", "wildlife-insights.png"),
}


@lru_cache(maxsize=64)
def platform_pixmap(platform, *, width, height, ratio=1):
    """Keep the original aspect ratio and limit sampling to the source pixels."""
    source = QPixmap(str(ARTWORK / _PLATFORMS[platform][1]))
    if source.isNull():
        return source
    bounds = QSize(min(source.width(), max(1, round(width * ratio))),
                   min(source.height(), max(1, round(height * ratio))))
    image = source.scaled(bounds, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    image.setDevicePixelRatio(ratio)
    return image


class _PlatformLogo(QLabel):
    def __init__(self, platform, height):
        super().__init__()
        self.platform = platform
        self.height_at_normal_zoom = height
        self.setProperty("role", "platformLogo")
        self.setAccessibleName(f"{_PLATFORMS[platform][0]} logo")
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._refresh()

    def _refresh(self):
        height = self.height_at_normal_zoom * self.fontMetrics().height() / 16
        width = min(height * 5, self.maximumWidth() - 8)
        height = min(height, self.maximumHeight() - 4)
        self.setPixmap(platform_pixmap(self.platform, width=width, height=height,
                                       ratio=self.devicePixelRatioF()))

    def event(self, event):
        result = super().event(event)
        if event.type() in (QEvent.FontChange, QEvent.DevicePixelRatioChange, QEvent.Show):
            self._refresh()
        return result


def platform_logo(platform, *, height=20):
    return _PlatformLogo(platform, height)


class PlatformPanel(QGroupBox):
    """Keep a platform mark beside its changing job title at every window width."""

    def __init__(self, platform, title):
        super().__init__()
        self._platform_name = _PLATFORMS[platform][0]
        self.setProperty("role", "platformPanel")
        self.body = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(platform_logo(platform), alignment=Qt.AlignVCenter)
        self._heading = workspace_label("", "sectionTitle", wrap=True)
        header.addWidget(self._heading, 1)
        self.body.addLayout(header)
        self.setTitle(title)

    def setTitle(self, title):
        self._heading.setText(title)
        self.setAccessibleName(f"{self._platform_name} {title}")

    def title(self):
        return self._heading.text()


class PlatformMenu(QMenu):
    """Native actions handle selection, keyboard navigation, tooltips and disabled states."""

    def __init__(self, entries, parent):
        super().__init__(parent)
        self._logos = {}
        for action, platform in entries:
            action.setIcon(QIcon(str(ARTWORK / _PLATFORMS[platform][1])))
            # Keep the wordmark wide and expose its name as a real accessible label.
            action.setIconVisibleInMenu(False)
            self.addAction(action)
            logo = platform_logo(platform, height=24)
            logo.setParent(self)
            logo.setAlignment(Qt.AlignCenter)
            logo.setAttribute(Qt.WA_TransparentForMouseEvents)
            opacity = QGraphicsOpacityEffect(logo)
            logo.setGraphicsEffect(opacity)
            self._logos[action] = logo
            action.changed.connect(self._place_logos)
        self.setToolTipsVisible(True)
        self.aboutToShow.connect(self._fit_rows)
        self._fit_rows()

    def _fit_rows(self):
        unit = self.fontMetrics().height()
        column = 5 * unit
        self.setStyleSheet(f"""
            QMenu::item {{ padding: {unit}px {unit}px {unit}px {column + unit}px; }}
        """)
        for logo in self._logos.values():
            logo.setFixedSize(column, 2 * unit)
            logo._refresh()
        self._place_logos()

    def _place_logos(self):
        for action, logo in self._logos.items():
            rect = self.actionGeometry(action)
            logo.setVisible(action.isVisible() and rect.isValid())
            if action.isVisible() and rect.isValid():
                logo.move(rect.left() + self.fontMetrics().height() // 2,
                          rect.center().y() - logo.height() // 2)
                logo.setEnabled(action.isEnabled())
                logo.graphicsEffect().setOpacity(1 if action.isEnabled() else 0.5)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_logos()

    def showEvent(self, event):
        super().showEvent(event)
        self._place_logos()
