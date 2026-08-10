"""
Application entry point — ``python -m cassn``.

This is the one place the layers are wired together. It loads the Box
credentials and the lookup tables once, then injects them into the single
:class:`~cassn.gui.wizard.FieldDataWizard` window and starts the Qt event
loop. The data, export, and Box-storage layers stay free of module globals and
import-time side effects; this module supplies their inputs explicitly.

Mirrors the original ``main()``: it warns (but does not abort) when the
optional EXIF or Box dependencies are missing, applies the Fusion style, and
exits with the Qt application's return code.
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from cassn.box.auth import BOX_AVAILABLE, load_box_config
from cassn.config import LOCAL_DATA_DIR
from cassn.core.image_metadata import EXIF_AVAILABLE
from cassn.gui import FieldDataWizard
from cassn.lookups import LookupSchemaError, LookupTables


def main() -> None:
    if not EXIF_AVAILABLE:
        print("Warning: PIL/piexif not available. EXIF extraction will be disabled.")
        print("Install with: pip install pillow piexif")

    if not BOX_AVAILABLE:
        print("Warning: box-sdk-gen not available. Box upload will be disabled.")
        print("Install with: pip install box-sdk-gen")
        print("Then run box_auth_setup.py to authenticate")

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # Load the injected dependencies once and hand them to the window.
    box_config = load_box_config()
    try:
        lookups = LookupTables.load(LOCAL_DATA_DIR)
    except LookupSchemaError as exc:
        QMessageBox.critical(
            None,
            "Survey123 Lookups Missing or Invalid",
            f"The new device lookup system could not be loaded:\n\n{exc}\n\n"
            "The app will not fall back to cameras.csv or ARUs.csv.",
        )
        raise SystemExit(1) from exc

    window = FieldDataWizard(lookups=lookups, box_config=box_config)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
