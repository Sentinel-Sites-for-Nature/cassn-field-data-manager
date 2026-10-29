"""Unified routing must happen before profile-dependent modules are imported."""
import json
import os
import subprocess
import sys

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QPushButton

from cassn.launcher import OrganizationDialog


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("profile", ["uc", "csu"])
def test_organization_selection_requires_explicit_choice(app, profile):
    dialog = OrganizationDialog()
    assert not any("welcome photos" in button.text().lower() for button in dialog.findChildren(QPushButton))
    assert dialog.organization.currentIndex() == -1
    assert not dialog.continue_button.isEnabled()
    dialog.choose()
    assert dialog.profile is None
    dialog.organization.setCurrentIndex(dialog.organization.findData(profile))
    assert dialog.continue_button.isEnabled()
    dialog.continue_button.click()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.profile == profile
    dialog.close()


@pytest.mark.parametrize("profile", ["uc", "csu"])
def test_selection_routes_config_credentials_and_staging_in_fresh_process(tmp_path, profile):
    code = '''
import json, sys
from pathlib import Path
from cassn.launcher import select_profile
assert "cassn.config" not in sys.modules
assert "cassn.profile" not in sys.modules
import os
assert not Path(os.environ["CASSN_CONFIG_ROOT"]).exists()
select_profile(sys.argv[1])
from cassn.config import CONFIG_DIR, LOCAL_DATA_DIR, DEFAULT_STAGING_ROOT, APP_TITLE
from cassn.box.auth import load_box_config
from cassn.box.hosted import manager_for
config = load_box_config()
manager = manager_for(config)
print(json.dumps({"profile": config.profile, "config": str(CONFIG_DIR),
"cache": str(LOCAL_DATA_DIR), "staging": str(DEFAULT_STAGING_ROOT),
"title": APP_TITLE, "origin": config.service_url, "data": config.field_data_folder_id,
"lookup": config.app_config_folder_id, "complete": config.is_complete}))
'''
    root = tmp_path / "settings"
    env = dict(os.environ, CASSN_CONFIG_ROOT=str(root), CASSN_PROFILE="legacy")
    result = json.loads(subprocess.check_output([sys.executable, "-c", code, profile], env=env, text=True))
    assert result["profile"] == profile
    assert result["config"] == str(root / "profiles" / profile)
    assert result["cache"] == str(root / "profiles" / profile / "lookup_tables")
    assert result["staging"].endswith(profile.upper() + "_CASSN_field_data_staging")
    assert result["title"] == "CA-SSN Data Manager — " + profile.upper()
    assert result["origin"].startswith("https://cassn-" + profile + "-box-auth.")
    expected = {"uc": ("377166707326", "377164694031"), "csu": ("430058250915", "430057609739")}
    assert (result["data"], result["lookup"]) == expected[profile]
    assert result["complete"]
    assert not (root / "profiles" / ("csu" if profile == "uc" else "uc")).exists()


def test_cancel_launcher_does_not_load_project_or_touch_settings(tmp_path):
    code = '''
import sys
from unittest.mock import MagicMock
from cassn import launcher
launcher.QApplication = MagicMock()
launcher.QIcon = MagicMock()
launcher.OrganizationDialog = MagicMock()
launcher.OrganizationDialog.return_value.exec.return_value = launcher.QDialog.DialogCode.Rejected
launcher.main()
assert "cassn.config" not in sys.modules
assert "cassn.profile" not in sys.modules
assert "cassn.__main__" not in sys.modules
'''
    root = tmp_path / "settings"
    subprocess.run([sys.executable, "-c", code], check=True, env=dict(os.environ, CASSN_CONFIG_ROOT=str(root)))
    assert not root.exists()


@pytest.mark.parametrize("profile", ["uc", "csu"])
def test_launcher_hands_selected_project_and_existing_application_to_startup(tmp_path, profile):
    code = '''
import sys, types
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from cassn import launcher
original = launcher.OrganizationDialog
def dialog():
    instance = original()
    def choose():
        instance.organization.setCurrentIndex(instance.organization.findData(sys.argv[1]))
        instance.continue_button.click()
    QTimer.singleShot(0, choose)
    return instance
launcher.OrganizationDialog = dialog
called = []
def run_project(*, application):
    from cassn.profile import PROFILE
    from cassn.box.auth import load_box_config
    assert PROFILE == sys.argv[1] == load_box_config().profile
    assert application is QApplication.instance()
    called.append(PROFILE)
module = types.ModuleType("cassn.__main__")
module.main = run_project
sys.modules["cassn.__main__"] = module
launcher.main()
assert called == [sys.argv[1]]
'''
    subprocess.run([sys.executable, "-c", code, profile], check=True,
                   env=dict(os.environ, CASSN_CONFIG_ROOT=str(tmp_path)))
