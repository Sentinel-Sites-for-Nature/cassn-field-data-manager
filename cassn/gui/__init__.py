"""
The PySide6 GUI layer.

This package is the only part of :mod:`cassn` (besides :mod:`cassn.box.threads`)
that imports PySide6. :class:`~cassn.gui.wizard.FieldDataWizard` is the single
window; it receives its lookup tables and Box configuration by injection rather
than reading module globals, so the data, export, and Box-storage layers it
drives stay Qt-free and independently testable.
"""

from cassn.gui.wizard import FieldDataWizard

__all__ = ["FieldDataWizard"]
