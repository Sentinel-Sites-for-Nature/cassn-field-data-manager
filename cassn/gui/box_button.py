"""A single accessible action with a baseline-aligned Box wordmark."""
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QPushButton, QStyle, QStyleOptionButton, QStylePainter

from cassn.gui.appearance import ARTWORK


class BoxConnectButton(QPushButton):
    def __init__(self, text="Connect to Box", parent=None):
        super().__init__(text, parent)
        self.setObjectName("boxConnectButton")
        self.setIcon(QIcon(str(ARTWORK / "box.webp")))
        self.setIconSize(QSize(38, 20))
        self.setMinimumHeight(52)
        self.setCursor(Qt.PointingHandCursor)
        self._logo = QPixmap(str(ARTWORK / "box.webp"))
        self._scaled_logo = None
        self._logo_size = None

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
        logo_width = round(logo_height * self._logo.width() / self._logo.height())
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
        ratio = self.devicePixelRatioF()
        logo_size = (logo_width, logo_height, ratio)
        if self._logo_size != logo_size:
            self._scaled_logo = self._logo.scaled(
                round(logo_width * ratio), round(logo_height * ratio),
                Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )
            self._scaled_logo.setDevicePixelRatio(ratio)
            self._logo_size = logo_size
        painter.drawPixmap(x + text_width + gap, baseline - logo_height + 1, self._scaled_logo)
