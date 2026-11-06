"""Shared presentation helpers, safe to import before choosing a project."""
from functools import lru_cache
from pathlib import Path
import re

from PySide6.QtCore import QEvent, QRect, QSize, Qt
from PySide6.QtGui import QPalette, QPixmap, QRegion
from PySide6.QtWidgets import QFrame, QGridLayout, QGroupBox, QLabel, QLayout, QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget

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
QLabel[platformStatus="complete"] {{ color: {accent}; font-weight: 700; font-size: 14px; }}
QLabel[platformStatus="warning"] {{ color: {"#f2ce84" if dark else "#805411"}; font-weight: 600; }}
QLabel[platformStatus="error"] {{ color: {"#ffb5ae" if dark else "#a03431"}; font-weight: 700; }}
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
QGroupBox[role="platformPanel"] {{ margin-top: 0; padding: 0; }}
QFrame#platformActions {{ background: {background}; border-top: 1px solid {line}; border-bottom-left-radius: 9px; border-bottom-right-radius: 9px; }}
QFrame#platformHeader {{ background: {"#c8ddd1" if dark else "#deede4"}; border: none;
    border-bottom: 1px solid #b8d1c3; border-top-left-radius: 9px; border-top-right-radius: 9px; }}
QLabel[role="platformTitle"] {{ color: #163f31; font-size: 18px; font-weight: 700; }}
QLabel[role="platformLogo"] {{ background: transparent; padding: 2px 4px; }}
QFrame#platformHeader QLabel[role="platformLogo"] {{ padding: 2px 0; }}
QToolButton#progressDisclosure {{ padding: 5px; border: none; background: transparent; color: #163f31; }}
QToolButton#progressDisclosure:hover, QToolButton#progressDisclosure:focus {{ background: #c1d9cb; }}
QGroupBox[role="cardJob"] QPushButton, QGroupBox[role="cardJob"] QToolButton {{ padding: 5px 9px; }}
QLineEdit, QComboBox, QDateEdit, QSpinBox {{ background: {panel}; color: {text}; border: 1px solid {line}; border-radius: 5px; padding: 7px; selection-background-color: {accent}; selection-color: {on_accent}; }}
QLineEdit:read-only, QDateEdit:read-only {{ background: {background}; }}
QComboBox QAbstractItemView {{ background: {panel}; color: {text}; selection-background-color: {tint}; selection-color: {text}; }}
QCheckBox {{ spacing: 7px; }}
QCheckBox[role="deviceChoice"] {{ spacing: 7px; padding: 0 5px; }}
QCheckBox[role="deviceChoice"]::indicator {{ width: {round(19 * zoom / 100)}px; height: {round(19 * zoom / 100)}px; }}
QTreeWidget, QTextEdit {{ background: {panel}; color: {text}; border: 1px solid {line}; border-radius: 6px; selection-background-color: {tint}; selection-color: {text}; alternate-background-color: {background}; }}
QTreeWidget {{ outline: none; }}
QTreeWidget::item {{ padding: 8px 5px; border-bottom: 1px solid {soft}; }}
QTreeWidget::item:selected {{ background: {tint}; color: {text}; }}
QHeaderView::section {{ background: {background}; color: {muted}; border: none; border-bottom: 1px solid {line}; padding: 10px 5px; font-weight: normal; }}
QProgressBar {{ background: {soft}; border: none; border-radius: 4px; min-height: 8px; max-height: 8px; color: transparent; }}
QProgressBar::chunk {{ background: {accent}; border-radius: 4px; }}
QProgressBar#boxUploadProgress, QProgressBar#eventProgress {{ min-height: {round(26 * zoom / 100)}px; max-height: 16777215px; color: {text}; text-align: center; }}
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



def style_cancel_button(button: QPushButton) -> QPushButton:
    """Use the same cancel treatment in the workspace and standalone dialogs."""
    button.setStyleSheet("""
        QPushButton { background-color: #a32935; color: white; border: 2px solid #a32935;
                      border-radius: 6px; padding: 7px 11px; }
        QPushButton:hover { background-color: #8b2230; border-color: #8b2230; }
        QPushButton:pressed { background-color: #711a26; border-color: #711a26; }
        QPushButton:focus { border-color: #ffbfc3; }
        QPushButton:disabled { background-color: palette(mid); color: palette(button-text);
                               border-color: palette(mid); }
    """)
    return button


def set_platform_status(label, state=""):
    if label.property("platformStatus") != state:
        label.setProperty("platformStatus", state)
        label.style().unpolish(label)
        label.style().polish(label)
        label.updateGeometry()
        label.update()


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
def platform_pixmap(platform, *, width, height, ratio=1, trim=False):
    """Keep the original aspect ratio and limit sampling to the source pixels."""
    source = QPixmap(str(ARTWORK / _PLATFORMS[platform][1]))
    if source.isNull():
        return source
    if trim and source.hasAlphaChannel():
        ink = QRegion(source.mask()).boundingRect()
        if not ink.isEmpty():
            source = source.copy(ink)
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


_PANEL_TITLES = {
    "box": "Upload event files to Box",
    "box_check": "Verify uploaded files against local copies",
    "soundhub": "Prepare and Stage Bird Audio Data for SoundHub Upload",
    "wildlife_insights": "Upload camera deployments and photos",
}


class _PanelActions(QLayout):
    """Wrap secondary controls while keeping the main action at the top right."""

    def __init__(self, frame):
        super().__init__(frame)
        self.items = []
        self.primary = None
        self.cancel = None
        self.setSpacing(12)

    def addItem(self, item):
        self.items.append(item)

    def count(self):
        return len(self.items)

    def itemAt(self, index):
        return self.items[index] if 0 <= index < len(self.items) else None

    def takeAt(self, index):
        return self.items.pop(index) if 0 <= index < len(self.items) else None

    def minimumSize(self):
        sizes = [item.minimumSize() for item in self.items if not item.isEmpty()]
        return QSize(max((s.width() + 28 for s in sizes), default=0), self.heightForWidth(10000))

    def sizeHint(self):
        return self.minimumSize()

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, True)

    def _arrange(self, rect, draw):
        visible = [item for item in self.items if not item.isEmpty() and item.sizeHint().width() > 0]
        if not visible:
            return 0
        left, right = rect.x() + 14, rect.x() + rect.width() - 14
        y = rect.y() + 10
        gap = self.spacing()
        right_items = [item for button in (self.cancel, self.primary)
                       for item in visible if item.widget() is button]
        left_items = [item for item in visible if item not in right_items]
        x, height = left, 0
        for item in left_items:
            size = item.sizeHint()
            if height and x + size.width() > right:
                y += height + gap
                x, height = left, 0
            if draw:
                item.setGeometry(QRect(x, y, size.width(), size.height()))
            x += size.width() + gap
            height = max(height, size.height())

        pair_width = sum(item.sizeHint().width() for item in right_items) + gap * max(0, len(right_items) - 1)
        if right_items and height and x > right - pair_width:
            y += height + gap
            height = 0
        x = right - pair_width
        stacked = pair_width > right - left
        for item in right_items:
            size = item.sizeHint()
            if stacked:
                x = right - size.width()
                if height:
                    y += height + gap
                    height = 0
            if draw:
                item.setGeometry(QRect(x, y, size.width(), size.height()))
            x += size.width() + gap
            height = max(height, size.height())
        return y - rect.y() + height + 10


class PlatformPanel(QGroupBox):
    """A platform header that expands to show its summary, progress, and actions."""

    def __init__(self, purpose):
        super().__init__()
        self.platform = "box" if purpose == "box_check" else purpose
        name = _PLATFORMS[self.platform][0]
        self.setProperty("role", "platformPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.header = QFrame()
        self.header.setObjectName("platformHeader")
        header = QGridLayout(self.header)
        header.setColumnStretch(1, 1)
        self._stacked = False
        header.setContentsMargins(14, 4, 14, 4)
        header.setSpacing(10)
        self.logo = QLabel()
        self.logo.setProperty("role", "platformLogo")
        self.logo.setAccessibleName(f"{name} logo")
        self.logo.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header.addWidget(self.logo, 0, 0, alignment=Qt.AlignVCenter | Qt.AlignLeft)
        self._heading = workspace_label(_PANEL_TITLES[purpose], "platformTitle", wrap=True)
        self._heading.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        header.addWidget(self._heading, 0, 1)
        self.disclosure = QToolButton()
        self.disclosure.setObjectName("progressDisclosure")
        self.disclosure.setCheckable(True)
        header.addWidget(self.disclosure, 0, 2)
        layout.addWidget(self.header)
        self._body_area = QWidget()
        body_policy = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        body_policy.setHeightForWidth(True)
        self._body_area.setSizePolicy(body_policy)
        self.body = QVBoxLayout(self._body_area)
        self.body.setContentsMargins(14, 10, 14, 12)
        layout.addWidget(self._body_area)
        self._detail_area = QWidget()
        self.details = QVBoxLayout(self._detail_area)
        self.details.setContentsMargins(14, 0, 14, 12)
        layout.addWidget(self._detail_area)
        self.footer = None
        if purpose in ("box", "soundhub", "wildlife_insights"):
            self.footer = QFrame()
            self.footer.setObjectName("platformActions")
            self.actions = _PanelActions(self.footer)
            layout.addWidget(self.footer)
            self.body.setSpacing(6)
            self.details.setSpacing(8)
        self._set_expanded(False)
        self.disclosure.toggled.connect(self._set_expanded)
        self.setAccessibleName(f"{name} {self.title()}")
        self._heading.installEventFilter(self)

    def add_action_button(self, label, action, *, primary=False, available=None):
        button = QPushButton(label)
        button.clicked.connect(action.trigger)

        def sync():
            button.setEnabled(action.isEnabled() and (available is None or available()))
            button.setToolTip(action.toolTip())

        action.changed.connect(sync)
        action.visibleChanged.connect(lambda: button.setVisible(action.isVisible()))
        button.setVisible(action.isVisible())
        sync()
        if primary:
            self.actions.primary = button
        self.add_footer_button(button)
        return button

    def add_cancel_button(self, button):
        self.actions.cancel = button
        self.add_footer_button(button)

    def add_footer_button(self, button):
        self.actions.addWidget(button)

    def title(self):
        return self._heading.text()

    def _set_expanded(self, expanded):
        self._body_area.setVisible(expanded)
        self._detail_area.setVisible(expanded)
        if self.footer is not None:
            self.footer.setVisible(expanded)
        self.disclosure.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        verb = "Hide" if expanded else "Show"
        label = f"{verb} {_PLATFORMS[self.platform][0]} progress details"
        self.disclosure.setAccessibleName(label)
        self.disclosure.setToolTip(label)

    def _size_header(self):
        # Size every band from the same set of titles so even wrapped headings align.
        metrics = self._heading.fontMetrics()
        scale = metrics.height() / 22
        slot = min(round(136 * scale), 160, max(85, self.width() // 3))
        logo_height = min(62, round(45 * scale))
        self.logo.setFixedSize(slot, logo_height + 4)
        ink_height = round(27 * scale) if self.platform == "box" else logo_height
        self.logo.setPixmap(platform_pixmap(self.platform, width=slot - 8, height=ink_height,
                                            ratio=self.devicePixelRatioF(), trim=True))
        inner = max(1, self.width() - 30)
        controls = self.disclosure.sizeHint()
        beside = inner - 20 - slot - controls.width()
        longest_word = max(metrics.horizontalAdvance(word) for title in _PANEL_TITLES.values() for word in title.split())
        stacked = beside < longest_word
        if stacked != self._stacked:
            self._stacked = stacked
            grid = self.header.layout()
            grid.removeWidget(self._heading)
            if stacked:
                grid.addWidget(self._heading, 1, 0, 1, 3)
            else:
                grid.addWidget(self._heading, 0, 1)
        text_width = inner if stacked else beside
        tallest = max(metrics.boundingRect(QRect(0, 0, text_width, 4096), Qt.TextWordWrap, title).height()
                      for title in _PANEL_TITLES.values())
        marks = max(self.logo.height(), controls.height())
        height = marks + 10 + tallest if stacked else max(marks, tallest)
        self.header.setFixedHeight(height + 10)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._size_header()

    def showEvent(self, event):
        super().showEvent(event)
        self._size_header()

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.FontChange, QEvent.DevicePixelRatioChange):
            self._size_header()
        return super().eventFilter(obj, event)
