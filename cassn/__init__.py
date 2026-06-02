"""
CA-SSN Field Data Manager.

A modular refactor of the original single-file application. The package is
layered so that dependencies flow strictly downward and Qt only appears at the
top (``cassn.gui``):

    config              constants, paths, schemas              (no deps)
    lookups             lookup-table loaders + container        (stdlib only)
    core/               domain logic: classification, hashing,  (stdlib + Pillow/ExifTool)
                        image_metadata, audio_metadata,
                        inventory, quality_control
    export/             metadata + Wildlife Insights CSV output (stdlib only)
    box/                Box auth, client helpers, QThreads       (box-sdk-gen [+ Qt for threads])
    gui/                the PySide6 wizard                       (Qt)
    __main__            entry point (``python -m cassn``)

Nothing in this package performs I/O at import time. All configuration and
lookup data is loaded explicitly by :func:`cassn.__main__.main`.
"""

from cassn.config import VERSION

__all__ = ["VERSION"]
