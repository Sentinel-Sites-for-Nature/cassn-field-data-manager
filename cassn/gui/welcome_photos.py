"""A still photo banner drawn from John's curated camera-trap photographs."""
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QWidget

from cassn.gui.appearance import ARTWORK


# Original files are preserved. Focal points guide display-only crops, and the
# camera's timestamp strip is kept outside the photo area where present.
DEFAULT_PHOTOS = (
    ("RCNX0068.JPG", .53, .37, .965),
    ("RCNX2107.JPG", .72, .62, 1.0),
    ("47647AF5-90AD-459C-989D-1092C83B1099.JPG", .88, .64, .965),
)


class WelcomePhotoBanner(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(168)
        self.setMaximumHeight(200)
        self.setAccessibleName("Camera-trap wildlife photographs from the CA-SSN field collection")
        self.photos = [(ARTWORK.parent / "welcome" / name, x, y, height)
                       for name, x, y, height in DEFAULT_PHOTOS]
        self._images = [QPixmap(str(photo[0])) for photo in self.photos]
        self._tile_size = None
        self._tiles = {}

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        outline = QPainterPath()
        outline.addRoundedRect(QRectF(self.rect()), 10, 10)
        painter.setClipPath(outline)
        gap = 4
        available = self.width() - gap * 2
        left = round(available * .28)
        center = available - left * 2
        ratio = self.devicePixelRatioF()
        tile_size = (self.width(), self.height(), ratio)
        if tile_size != self._tile_size:
            self._tiles.clear()
            self._tile_size = tile_size
        x = 0
        for photo, image, width in zip(self.photos, self._images, (left, center, left)):
            if not image.isNull():
                key = (photo[0], width)
                if key not in self._tiles:
                    _, focal_x, focal_y, usable_height = photo
                    bounds_height = image.height() * usable_height
                    crop_width = min(image.width(), bounds_height * width / self.height())
                    crop_height = crop_width * self.height() / width
                    crop_x = max(0, min(image.width() - crop_width, image.width() * focal_x - crop_width / 2))
                    crop_y = max(0, min(bounds_height - crop_height, image.height() * focal_y - crop_height / 2))
                    tile = image.copy(round(crop_x), round(crop_y), round(crop_width), round(crop_height))
                    tile = tile.scaled(round(width * ratio), round(self.height() * ratio),
                                       Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                    tile.setDevicePixelRatio(ratio)
                    self._tiles[key] = tile
                painter.drawPixmap(x, 0, self._tiles[key])
            x += width + gap
