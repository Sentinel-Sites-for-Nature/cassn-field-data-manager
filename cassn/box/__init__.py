"""
Box integration: authentication, a Qt-free storage façade, and the Qt upload
threads.

Importing this package pulls in only the Qt-free pieces (:mod:`cassn.box.auth`
and :mod:`cassn.box.client`). The background threads live in
:mod:`cassn.box.threads` and are imported explicitly by the GUI, so the data
and export layers can use Box authentication and the storage façade without a
hard dependency on PySide6.
"""

from cassn.box.auth import (
    BOX_AVAILABLE,
    BoxConfig,
    SimpleTokenStorage,
    get_box_client,
    load_box_config,
)
from cassn.box.client import BoxStorage

__all__ = [
    "BOX_AVAILABLE",
    "BoxConfig",
    "SimpleTokenStorage",
    "get_box_client",
    "load_box_config",
    "BoxStorage",
]
