"""Presentation shell for the field workspace; workflow decisions stay in the wizard."""

from math import ceil
from PySide6.QtCore import QEvent, QPointF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QKeySequence, QPainter, QPalette, QPixmap, QTextLayout, QTextOption, QTransform
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QTabWidget,
    QVBoxLayout, QWidget, QProgressBar, QStyle, QStyleOptionProgressBar, QStylePainter,
)

from cassn.gui.appearance import ARTWORK, workspace_label, workspace_stylesheet
from cassn.profile import PROFILE


PAGE_NAMES = ("Deployments and devices", "Card ingest", "Review & upload")


class UploadMessage(QLabel):
    """Fit progress text to the panel, including filenames wider than a line."""

    def __init__(self):
        super().__init__()
        self.setTextFormat(Qt.PlainText)
        self.setWordWrap(True)
        # A long filename must not set the minimum width of the Review page.
        policy = QSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def _wrapped(self, width):
        text = self.text().replace("\n", "\u2028")
        layout = QTextLayout(text, self.font())
        options = QTextOption()
        options.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(options)
        height = 0.0
        layout.beginLayout()
        while (line := layout.createLine()).isValid():
            line.setLineWidth(max(1, width))
            line.setPosition(QPointF(0, height))
            height += line.height()
        layout.endLayout()
        return layout, ceil(height)

    def heightForWidth(self, width):
        return self._wrapped(width)[1]

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        layout, _ = self._wrapped(self.width())
        layout.draw(painter, QPointF(0, 0))


class UploadProgressBar(QProgressBar):
    """Keep the percentage legible over both the green fill and empty track."""

    def paintEvent(self, event):
        option = QStyleOptionProgressBar()
        self.initStyleOption(option)
        option.textVisible = False
        painter = QStylePainter(self)
        painter.drawControl(QStyle.CE_ProgressBar, option)
        if not self.isTextVisible() or self.maximum() <= self.minimum():
            return
        painter.setPen(self.palette().color(QPalette.Text))
        text_flags = Qt.AlignCenter | Qt.TextWordWrap if self.property("wrapProgressText") else Qt.AlignCenter
        painter.drawText(option.rect, text_flags, self.text())
        filled = self.style().subElementRect(QStyle.SE_ProgressBarContents, option, self)
        fraction = max(0, self.value() - self.minimum()) / (self.maximum() - self.minimum())
        filled.setWidth(round(filled.width() * fraction))
        if (self.layoutDirection() == Qt.RightToLeft) != self.invertedAppearance():
            filled.moveRight(option.rect.right())
        painter.setClipRect(filled)
        dark = QApplication.palette().color(QPalette.Window).lightness() < 128
        painter.setPen(QColor("#152d25" if dark else "#ffffff"))
        painter.drawText(option.rect, text_flags, self.text())




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
        shell = QHBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("workspaceSidebar")
        self.sidebar = sidebar
        sidebar.setFixedWidth(280)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setObjectName("sidebarScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sidebar_content = QWidget()
        sidebar_content.setObjectName("sidebarContent")
        scroll.setWidget(sidebar_content)
        sidebar_layout.addWidget(scroll)
        side = QVBoxLayout(sidebar_content)
        side.setContentsMargins(16, 16, 16, 16)
        side.setSpacing(20)

        brand = QFrame()
        brand.setObjectName("sidebarBrandBlock")
        brand_layout = QVBoxLayout(brand)
        brand_layout.setContentsMargins(20, 20, 20, 22)
        brand_layout.setSpacing(4)
        brand_layout.addWidget(workspace_label("CA-SSN", "sidebarBrand"))
        brand_layout.addWidget(workspace_label("Data Manager", "sidebarBrand"))
        side.addWidget(brand)

        organization = QFrame()
        organization.setObjectName("sidebarOrganization")
        organization_layout = QVBoxLayout(organization)
        organization_layout.setContentsMargins(16, 14, 16, 18)
        organization_layout.setSpacing(12)
        organization_layout.addWidget(workspace_label("Organization", "organizationCaption"))
        self.nature_logo = QLabel()
        self.nature_logo.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.nature_logo.setAccessibleName("University of California Nature")
        self._refresh_branding()
        organization_layout.addWidget(self.nature_logo)
        side.addWidget(organization)

        workflow = QWidget()
        workflow_layout = QVBoxLayout(workflow)
        workflow_layout.setContentsMargins(0, 0, 0, 0)
        workflow_layout.setSpacing(8)
        heading = workspace_label("Ingest data", "sidebarSection")
        heading.setContentsMargins(10, 0, 0, 8)
        workflow_layout.addWidget(heading)
        self.navigation = []
        for index, name in enumerate(PAGE_NAMES):
            button = QPushButton()
            button.setMinimumHeight(64)
            button.setToolTip(f"Step {index + 1} of 3: {name}")
            button.setProperty("role", "navigation")
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setAccessibleName(name)
            row = QHBoxLayout(button)
            row.setContentsMargins(12, 12, 12, 12)
            row.setSpacing(12)
            number = workspace_label(str(index + 1), "stepNumber")
            number.setAlignment(Qt.AlignCenter)
            title = workspace_label(name, "stepTitle", wrap=True)
            for label in (number, title):
                label.setAttribute(Qt.WA_TransparentForMouseEvents)
            row.addWidget(number)
            row.addWidget(title, 1)
            button.clicked.connect(lambda _checked=False, i=index: self.page_requested.emit(i))
            workflow_layout.addWidget(button)
            self.navigation.append(button)
        side.addWidget(workflow)
        side.addStretch()

        shell.addWidget(sidebar)

        content = QWidget()
        content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
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
        self.sidebar.setFixedWidth(round(280 * self.zoom / 100))
        self._refresh_branding()
        for button in self.navigation:
            button.setMinimumHeight(round(64 * self.zoom / 100))
        self.zoom_in_action.setEnabled(self.zoom < 200)
        self.zoom_out_action.setEnabled(self.zoom > 80)
        self.zoom_reset_action.setText(f"Actual Size (zoom {self.zoom}%)")

    def _refresh_branding(self):
        self.nature_logo.setVisible(True)
        variant = "black"  # Organization logos sit on a white brand surface.
        if PROFILE == "csu":
            from PySide6.QtSvg import QSvgRenderer
            from PySide6.QtGui import QPainter
            renderer = QSvgRenderer(str(ARTWORK / "csu-wordmark-stacked.svg"))
            width = round(200 * self.zoom / 100)
            ratio = self.devicePixelRatioF()
            size = renderer.defaultSize()
            logo = QPixmap(round(width * ratio), round(width * ratio * size.height() / size.width()))
            logo.fill(Qt.transparent)
            painter = QPainter(logo)
            renderer.render(painter)
            painter.end()
            logo.setDevicePixelRatio(ratio)
            self.nature_logo.setAccessibleName("California State University")
            self.nature_logo.setPixmap(logo)
            return
        self.nature_logo.setAccessibleName("University of California Nature")
        logo = QPixmap(str(ARTWORK / f"uc-nature-lockup-hori-{variant}.png"))
        width = round(196 * self.zoom / 100)
        self.nature_logo.setPixmap(logo.scaledToWidth(width, Qt.SmoothTransformation))

    def _sync_page(self, index):
        for i, button in enumerate(self.navigation):
            button.setChecked(i == index)
            for label in button.findChildren(QLabel):
                if label.property("current") != (i == index):
                    label.setProperty("current", i == index)
                    label.style().unpolish(label)
                    label.style().polish(label)

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
            self._refresh_branding()


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
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setWidget(content)
        content.installEventFilter(self)

    def eventFilter(self, watched, event):
        if watched is self.widget() and event.type() == QEvent.LayoutRequest:
            height = self.widget().sizeHint().height() + self.horizontalScrollBar().sizeHint().height() + 4
            if self.minimumHeight() != height:
                self.setMinimumHeight(height)
        return super().eventFilter(watched, event)
