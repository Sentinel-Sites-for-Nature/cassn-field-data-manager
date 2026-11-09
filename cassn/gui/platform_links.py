"""Open destination websites without starting a transfer or creating folders."""
import json
import re
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

from cassn.box.publication import STATE_PATH
from cassn.gui.dialogs import WindowMessageBox


WI_PROJECT_NAMES = {"2010517": "UCNRS Small Animal", "2010527": "UCNRS Mammal"}


def wi_project_uploads_url(project=None):
    manage = "https://app.wildlifeinsights.org/manage"
    if str(project) in WI_PROJECT_NAMES:
        return f"{manage}/organizations/2001427/projects/{project}/uploads"
    return manage


def box_web_url(event_folder, data_folder_id):
    folder_id = None
    if event_folder:
        try:
            record = json.loads((Path(event_folder) / STATE_PATH).read_bytes())
            if isinstance(record, dict):
                folder_id = record.get("folder_id")
        except (OSError, ValueError):
            pass
    for candidate in (folder_id, data_folder_id):
        if re.fullmatch(r"[0-9]+", str(candidate)):
            return f"https://app.box.com/folder/{candidate}"
    return "https://app.box.com/"


def open_platform_url(parent, address):
    if sys.platform == "darwin":
        # Launch Services selects the installed Chrome application by bundle ID.
        try:
            result = subprocess.run(["/usr/bin/open", "-b", "com.google.Chrome", address],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
        else:
            if result.returncode == 0:
                return
    if not QDesktopServices.openUrl(QUrl(address)):
        WindowMessageBox.warning(parent, "Could not open browser", f"Open this address in your browser:\n{address}")
