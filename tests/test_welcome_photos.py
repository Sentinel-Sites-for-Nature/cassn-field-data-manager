"""Welcome artwork ships with the app and never reads a user's photo folder."""
from pathlib import Path

from PySide6.QtWidgets import QApplication

from cassn.gui.appearance import ARTWORK
from cassn.gui.welcome_photos import DEFAULT_PHOTOS, WelcomePhotoBanner


def test_banner_uses_bundled_photos_without_reading_desktop(monkeypatch):
    app = QApplication.instance() or QApplication([])

    def forbidden(*args, **kwargs):
        raise AssertionError("Welcome photos must not read local user folders")

    monkeypatch.setattr(Path, "home", forbidden)
    monkeypatch.setattr(Path, "iterdir", forbidden)
    banner = WelcomePhotoBanner()
    assert [photo[0] for photo in banner.photos] == [
        ARTWORK.parent / "welcome" / photo[0] for photo in DEFAULT_PHOTOS
    ]
    assert all(not image.isNull() for image in banner._images)
    banner.close()
