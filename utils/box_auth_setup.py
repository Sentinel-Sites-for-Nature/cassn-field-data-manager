"""Compatibility launcher for the app's secure browser sign-in dialog."""
import sys
from pathlib import Path

# Support invoking this script directly from any working directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    from PySide6.QtWidgets import QApplication, QDialog
    from cassn.gui.box_connect import BoxConnectDialog

    app = QApplication.instance() or QApplication(sys.argv)
    dialog = BoxConnectDialog()
    return 0 if dialog.exec() == QDialog.Accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
