"""A single accessible action with a baseline-aligned Box wordmark."""
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QIcon, QPainter, QPalette
from PySide6.QtWidgets import QPushButton, QStyle, QStyleOptionButton, QStylePainter

from cassn.gui.appearance import platform_pixmap


class BoxConnectButton(QPushButton):
    def __init__(self, text="Connect to Box", parent=None):
        super().__init__(text, parent)
        self.setObjectName("boxConnectButton")
        self.setIcon(QIcon(platform_pixmap("box", width=38, height=20, ratio=self.devicePixelRatioF())))
        self.setIconSize(QSize(38, 20))
        self.setMinimumHeight(52)
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, event):
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = ""
        option.icon = QIcon()
        painter = QStylePainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.drawControl(QStyle.CE_PushButton, option)
        painter.setFont(self.font())
        painter.setPen(option.palette.color(QPalette.ButtonText))
        prefix = self.text().removesuffix(" Box")
        metrics = painter.fontMetrics()
        logo_height = metrics.capHeight() + 6
        logo = platform_pixmap("box", width=logo_height * 5, height=logo_height,
                               ratio=self.devicePixelRatioF())
        size = logo.deviceIndependentSize()
        logo_width, logo_height = round(size.width()), round(size.height())
        gap = 8
        text_width = metrics.horizontalAdvance(prefix)
        x = (self.width() - text_width - gap - logo_width) // 2
        baseline = (self.height() + metrics.ascent() - metrics.descent()) // 2
        if self.isDown():
            x += 1
            baseline += 1
        painter.drawText(x, baseline, prefix)
        if not self.isEnabled():
            painter.setOpacity(0.45)
        painter.drawPixmap(x + text_width + gap, baseline - logo_height + 1, logo)
