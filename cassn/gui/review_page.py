"""Keep deployment review and transfer progress reachable on one scrollable page."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGroupBox, QScrollArea, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from cassn.gui.appearance import workspace_label


class ReviewPage(QScrollArea):
    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("reviewContent")
        self.setWidget(content)
        # Put each title inside its card. Reserve space in the current font size.
        content.setStyleSheet("""
            QWidget#reviewContent QGroupBox {
                margin-top: 0; padding: 2em 6px 6px;
            }
            QWidget#reviewContent QGroupBox[role="platformPanel"],
            QWidget#reviewContent QGroupBox[role="reviewCard"] {
                padding: 0;
            }
            QToolButton[role="reviewDisclosure"] {
                text-align: left; border: none; padding: 14px;
                font-size: 18px; font-weight: 700;
            }
            QWidget#reviewContent QGroupBox::title {
                subcontrol-origin: border; subcontrol-position: top left;
                left: 12px; top: 6px; padding: 0;
            }
            QWidget#reviewContent QTreeWidget QProgressBar {
                min-height: 1.5em; max-height: 1.5em;
                color: palette(window-text);
            }
        """)
        self.progress_heading = workspace_label("Upload and Stage Data", "sectionTitle", wrap=True)


class ReviewCard(QGroupBox):
    """A compact review section whose body can be hidden without changing state."""

    def __init__(self, title):
        super().__init__()
        self.setProperty("role", "reviewCard")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.disclosure = QToolButton()
        self.disclosure.setProperty("role", "reviewDisclosure")
        self.disclosure.setText(title)
        self.disclosure.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.disclosure.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.disclosure.setCheckable(True)
        layout.addWidget(self.disclosure)
        self.content = QWidget()
        self.body = QVBoxLayout(self.content)
        self.body.setContentsMargins(14, 0, 14, 14)
        layout.addWidget(self.content)
        self.disclosure.toggled.connect(self._set_expanded)
        self.disclosure.setChecked(True)

    def _set_expanded(self, expanded):
        self.content.setVisible(expanded)
        self.disclosure.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        verb = "Hide" if expanded else "Show"
        label = f"{verb} {self.disclosure.text()}"
        self.disclosure.setAccessibleName(label)
        self.disclosure.setToolTip(label)
