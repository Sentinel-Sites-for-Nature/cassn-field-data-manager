"""Shared presentation helpers, safe to import before choosing a project."""
from pathlib import Path
import re

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QLabel

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
