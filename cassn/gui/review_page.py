"""Keep deployment review and transfer progress reachable on one scrollable page."""

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QScrollArea, QWidget

from cassn.gui.appearance import workspace_label


class ReviewPage(QScrollArea):
    def __init__(self):
        super().__init__()
        self.groups = []
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
            QWidget#reviewContent QGroupBox[role="platformPanel"] {
                padding: 0;
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
        self.progress_link = QPushButton("Progress ↓")
        self.progress_link.clicked.connect(self._scroll_to_progress)
        self.progress_heading = QWidget()
        heading = QHBoxLayout(self.progress_heading)
        heading.setContentsMargins(0, 0, 0, 0)
        heading.addWidget(workspace_label("Upload and staging progress", "sectionTitle", wrap=True), 1)
        back = QPushButton("Summary ↑")
        back.clicked.connect(lambda: self.verticalScrollBar().setValue(0))
        heading.addWidget(back)

    def watch_progress(self, groups):
        self.groups = groups
        for group in groups:
            group.installEventFilter(self)
        self._update_links()

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.ShowToParent, QEvent.HideToParent) and obj in self.groups:
            self._update_links()
        return super().eventFilter(obj, event)

    def _update_links(self):
        visible = any(not group.isHidden() for group in self.groups)
        self.progress_link.setVisible(visible)
        self.progress_heading.setVisible(visible)

    def _scroll_to_progress(self):
        self.verticalScrollBar().setValue(self.progress_heading.y())
