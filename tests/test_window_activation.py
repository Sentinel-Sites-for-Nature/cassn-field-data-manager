"""A second launch forwards to the existing GUI instead of silently disappearing."""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from cassn import __main__ as startup
from cassn.gui.activation import ActivationServer, activate_running_instance
from cassn.gui.windows import ResourceBusy


@pytest.fixture(scope="session")
def activation_app():
    return QApplication.instance() or QApplication([])


def test_second_launch_sends_window_activation_and_receives_acknowledgment(activation_app):
    app = activation_app
    activated = MagicMock()
    with tempfile.TemporaryDirectory(prefix="ipc-") as directory:
        name = str(Path(directory) / "window.sock")
        server = ActivationServer(name, activated)
        script = """
import sys
from PySide6.QtCore import QCoreApplication
from cassn.gui.activation import activate_running_instance
app = QCoreApplication([])
print(activate_running_instance(sys.argv[1], timeout_ms=3000), flush=True)
"""
        client = subprocess.Popen([sys.executable, "-c", script, name], stdout=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while client.poll() is None and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.005)
            output, _ = client.communicate(timeout=1)
            assert client.returncode == 0
            assert output.strip() == "True"
            activated.assert_called_once()
        finally:
            if client.poll() is None:
                client.terminate()
                client.communicate(timeout=5)
            server.close()


@pytest.mark.parametrize("reachable", [True, False])
def test_running_instance_branch_never_bootstraps_or_opens_another_session(monkeypatch, reachable, activation_app):
    app = activation_app
    monkeypatch.setattr(startup, "QApplication", lambda args: app)

    def busy(*args):
        raise ResourceBusy("application")

    monkeypatch.setattr(startup, "ResourceLease", busy)
    activate = MagicMock(return_value=reachable)
    monkeypatch.setattr(startup, "activate_running_instance", activate)
    config = MagicMock()
    monkeypatch.setattr(startup, "load_box_config", config)
    message = MagicMock()
    monkeypatch.setattr(startup.QMessageBox, "information", message)
    startup.main()
    activate.assert_called_once()
    config.assert_not_called()
    assert message.call_count == (0 if reachable else 1)
