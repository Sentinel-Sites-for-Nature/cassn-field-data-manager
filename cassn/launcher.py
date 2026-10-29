"""Choose an organization before importing project-specific configuration."""
import os
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from cassn.gui.appearance import ARTWORK, workspace_label, workspace_stylesheet
from cassn.gui.welcome_photos import WelcomePhotoBanner


class OrganizationDialog(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CA-SSN Data Manager")
        self.setObjectName("fieldWorkspace")
        self.setStyleSheet(workspace_stylesheet(QApplication.palette()) + """
            QFrame#welcomeBrand { background: #074510; border-radius: 10px; }
            QLabel#brandTitle { color: #e0ebde; font-size: 18px; font-weight: 700; }
            QLabel#brandSubtitle { color: #ffffff; font-size: 30px; font-weight: 700; }
            QLabel#brandMap { background: transparent; }
        """)
        self.setMinimumWidth(620)
        self.resize(760, 720)
        self.profile = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 32, 32, 24)
        layout.setSpacing(16)
        self.photo_banner = WelcomePhotoBanner(self)
        layout.addWidget(self.photo_banner)
        brand = QFrame()
        brand.setObjectName("welcomeBrand")
        brand_row = QHBoxLayout(brand)
        brand_row.setContentsMargins(20, 20, 20, 20)
        brand_row.setSpacing(20)
        self.brand_map = QLabel()
        self.brand_map.setObjectName("brandMap")
        self.brand_map.setAccessibleName("Sentinel Sites for Nature – California site map")
        ratio = self.devicePixelRatioF()
        map_image = QPixmap(str(ARTWORK.parent / "ssn-california-map-transparent.png"))
        map_image = map_image.scaled(round(128 * ratio), round(148 * ratio), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        map_image.setDevicePixelRatio(ratio)
        self.brand_map.setPixmap(map_image)
        brand_row.addWidget(self.brand_map, 0, Qt.AlignVCenter)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(10)
        brand_text.addStretch()
        self.brand_title = workspace_label("SENTINEL SITES FOR\nNATURE – CALIFORNIA", "brand", wrap=True)
        self.brand_title.setObjectName("brandTitle")
        title_font = self.brand_title.font()
        title_font.setLetterSpacing(QFont.AbsoluteSpacing, 1.4)
        self.brand_title.setFont(title_font)
        brand_text.addWidget(self.brand_title)
        self.brand_subtitle = workspace_label("Data Manager Application", "brand", wrap=True)
        self.brand_subtitle.setObjectName("brandSubtitle")
        brand_text.addWidget(self.brand_subtitle)
        brand_text.addStretch()
        brand_row.addLayout(brand_text, 1)
        layout.addWidget(brand)
        card = QFrame()
        card.setObjectName("setupCard")
        content = QVBoxLayout(card)
        content.setContentsMargins(20, 20, 20, 20)
        content.setSpacing(14)
        self.organization_label = workspace_label("Choose your organization to get started", "sectionTitle", wrap=True)
        content.addWidget(self.organization_label)
        self.organization = QComboBox()
        self.organization.setMinimumHeight(42)
        self.organization.setAccessibleName("Organization")
        self.organization.addItem("University of California (UC)", "uc")
        self.organization.addItem("California State University (CSU)", "csu")
        self.organization.setPlaceholderText("Select your organization…")
        self.organization.setCurrentIndex(-1)
        self.organization_label.setBuddy(self.organization)
        content.addWidget(self.organization)
        self.continue_button = QPushButton("Continue")
        self.continue_button.setMinimumHeight(44)
        self.continue_button.setProperty("role", "primary")
        self.continue_button.setDefault(True)
        self.continue_button.setEnabled(False)
        self.continue_button.clicked.connect(self.choose)
        self.organization.currentIndexChanged.connect(lambda index: self.continue_button.setEnabled(index >= 0))
        content.addWidget(self.continue_button)
        layout.addWidget(card)
        layout.addStretch()
        row = QHBoxLayout()
        row.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        row.addWidget(close)
        layout.addLayout(row)

    def choose(self):
        profile = self.organization.currentData()
        if profile in {"uc", "csu"}:
            self.profile = profile
            self.accept()


def select_profile(profile):
    """Apply routing before any paths, cached lookups, or credentials load."""
    if profile not in {"uc", "csu"}:
        raise ValueError("Choose UC or CSU")
    if "cassn.config" in sys.modules or "cassn.profile" in sys.modules:
        raise RuntimeError("Choose an organization before loading project configuration")
    os.environ["CASSN_PROFILE"] = profile
    os.environ["CASSN_UNIFIED"] = "1"


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName("CA-SSN Data Manager")
    app.setWindowIcon(QIcon(str(ARTWORK.parent / "ssn_data_manager_icon.png")))
    dialog = OrganizationDialog()
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return
    select_profile(dialog.profile)
    from cassn.__main__ import main as run_project
    run_project(application=app)


if __name__ == "__main__":
    main()
