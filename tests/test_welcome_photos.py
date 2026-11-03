"""Welcome artwork ships with the app and never reads a user's photo folder."""
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QPushButton

from cassn.gui.appearance import ARTWORK
from cassn.gui.welcome_photos import DEFAULT_PHOTOS, WelcomePhotoBanner
from cassn.launcher import OrganizationDialog


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


def test_dialog_preserves_photo_framing_and_leaves_room_for_controls():
    app = QApplication.instance() or QApplication([])
    dialog = OrganizationDialog()
    banner = dialog.photo_banner
    dialog.show()
    try:
        app.processEvents()
        normal = banner.size()
        assert normal.toTuple() == (696, 168)

        dialog.resize(1440, 900)
        app.processEvents()
        wide = banner.size()
        assert wide.height() > normal.height()
        assert wide.width() / wide.height() == pytest.approx(normal.width() / normal.height(), rel=.02)

        dialog.resize(1440, 620)
        app.processEvents()
        assert dialog.height() == 620
        assert 0 < banner.height() < wide.height()
        controls = [dialog.organization, *dialog.findChildren(QPushButton)]
        for control in controls:
            assert control.visibleRegion().contains(control.rect())
            assert control.height() >= control.minimumSizeHint().height()

        dialog.resize(760, 720)
        app.processEvents()
        assert banner.size() == normal
    finally:
        dialog.close()
