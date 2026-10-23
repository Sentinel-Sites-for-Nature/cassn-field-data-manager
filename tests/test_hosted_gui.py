"""Hosted first-run UI states; no Box, real Keychain, or personal files."""
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
from PySide6.QtWidgets import QApplication, QDialog
from PySide6.QtCore import QEventLoop, QTimer
from cassn.box.auth import csu_config
from cassn.gui import hosted_setup, box_connect
from cassn.lookup_sync import LookupBootstrapError


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def settle(app, predicate):
    loop = QEventLoop()
    poll = QTimer()
    poll.timeout.connect(lambda: loop.quit() if predicate() else None)
    poll.start(5)
    QTimer.singleShot(2000, loop.quit)
    loop.exec()
    assert predicate()


def test_hosted_dialog_has_no_administrator_controls(app):
    dialog = box_connect.BoxConnectDialog(csu_config())
    dialog.show(); app.processEvents()
    assert not dialog.settings.isVisible()
    assert not any(b.isVisible() and b.text() == "Box app settings…" for b in dialog.findChildren(box_connect.QPushButton))
    assert dialog.connect_button.isEnabled()
    dialog.close()


def test_missing_project_stays_open_without_repeating_login(app, monkeypatch, tmp_path):
    manager = SimpleNamespace(token=lambda: object(), session=object())
    monkeypatch.setattr(hosted_setup, "manager_for", lambda _: manager)
    monkeypatch.setattr(hosted_setup, "bootstrap_lookup_tables", lambda *a: (_ for _ in ()).throw(LookupBootstrapError("missing")))
    dialog = hosted_setup.HostedSetupDialog(csu_config(), cache_dir=tmp_path)
    dialog.show()
    settle(app, lambda: dialog.worker is None and "not ready" in dialog.status.text())
    assert dialog.connected and dialog.result() != QDialog.Accepted
    assert dialog.retry_button.isEnabled() and dialog.disconnect_button.isEnabled()
    assert dialog.retry_button.isVisible()
    assert dialog.retry_button.text() == "Reload project settings"
    # Rechecking access uses the saved connection, without another login dialog.
    monkeypatch.setattr(box_connect, "BoxConnectDialog", lambda *a: pytest.fail("unexpected login"))
    snapshot = SimpleNamespace(source="box", lookups=object())
    monkeypatch.setattr(hosted_setup, "bootstrap_lookup_tables", lambda *a: snapshot)
    dialog.retry_button.click()
    settle(app, lambda: dialog.bootstrap is snapshot)
    assert dialog.result() == QDialog.Accepted
    dialog.close()


def test_empty_project_error_scrolls_on_small_window_with_footer_reachable(app, monkeypatch, tmp_path):
    from cassn.lookup_sync import REQUIRED_RUNTIME_FILENAMES
    missing = sorted(REQUIRED_RUNTIME_FILENAMES | {"program_config.json"})
    monkeypatch.setattr(hosted_setup, "manager_for", lambda _: SimpleNamespace(token=lambda: object(), session=object()))
    def missing_config(*args):
        raise LookupBootstrapError("not for display", reason="missing_files", missing_files=missing)
    monkeypatch.setattr(hosted_setup, "bootstrap_lookup_tables", missing_config)
    dialog = hosted_setup.HostedSetupDialog(csu_config(), cache_dir=tmp_path)
    dialog.resize(440, 420)
    dialog.show()
    settle(app, lambda: dialog.worker is None and dialog.details.isVisible())
    assert "Box access confirmed" in dialog.status.text()
    assert "not for display" not in dialog.status.text() + dialog.details.text()
    assert all(name in dialog.details.text() for name in missing)
    assert "invitation" not in dialog.details.text()
    assert dialog.scroll.verticalScrollBar().maximum() > 0
    assert dialog.scroll.horizontalScrollBar().maximum() == 0
    assert dialog.rect().contains(dialog.close_button.geometry())
    dialog.scroll.ensureWidgetVisible(dialog.retry_button)
    app.processEvents()
    assert dialog.scroll.viewport().rect().intersects(dialog.retry_button.rect().translated(
        dialog.retry_button.mapTo(dialog.scroll.viewport(), dialog.retry_button.rect().topLeft())))
    assert not dialog.connect_button.isVisible()
    assert dialog.retry_button.isDefault()
    dialog.close()


@pytest.mark.parametrize("profile", ["uc", "csu"])
def test_fresh_welcome_uses_shared_brand_and_one_primary_action(app, monkeypatch, tmp_path, profile):
    from dataclasses import replace
    from PySide6.QtWidgets import QLabel
    monkeypatch.setattr(hosted_setup.QTimer, "singleShot", lambda *_: None)
    dialog = hosted_setup.HostedSetupDialog(replace(csu_config(), profile=profile), cache_dir=tmp_path)
    dialog.show()
    app.processEvents()
    assert dialog.title.text() == "Welcome to the CA-SSN Data Manager"
    assert not dialog.connect_button.icon().isNull()
    assert dialog.connect_button.isDefault()
    assert not dialog.retry_button.isVisible()
    assert not dialog.disconnect_button.isVisible()
    labels = dialog.findChildren(QLabel)
    assert any("Click Connect to Box below to begin." in label.text() for label in labels)
    assert any("approved to access your project folder" in label.text() for label in labels)
    assert any('href="mailto:jtimperato@ucdavis.edu"' in label.text() and label.openExternalLinks() for label in labels)
    dialog.close()


def test_valid_config_accepts_startup_and_offline_stays_disconnected(app, monkeypatch, tmp_path):
    from cassn.box.hosted import HostedError
    monkeypatch.setattr(hosted_setup, "manager_for", lambda _: SimpleNamespace(
        token=lambda: (_ for _ in ()).throw(HostedError("network")), session=object()))
    snapshot = SimpleNamespace(source="offline-cache", lookups=object())
    monkeypatch.setattr(hosted_setup, "bootstrap_lookup_tables", lambda *a: snapshot)
    dialog = hosted_setup.HostedSetupDialog(csu_config(), cache_dir=tmp_path)
    dialog.show()
    settle(app, lambda: dialog.bootstrap is snapshot)
    assert dialog.result() == QDialog.Accepted
    assert not dialog.connected
    dialog.close()


def test_failed_reconnect_does_not_publish_old_connected_flag(app, monkeypatch, tmp_path):
    from cassn.gui.windows import DeploymentWindows
    from cassn.gui.wizard import FieldDataWizard
    from cassn.gui.preview import sample_lookups
    monkeypatch.setattr(FieldDataWizard, "load_config", lambda _: None)
    monkeypatch.setattr(FieldDataWizard, "find_all_sessions", lambda _: [])
    windows = DeploymentWindows(lookups=sample_lookups(), box_config=csu_config(), lock_directory=tmp_path)
    windows.box_authenticated = True
    window = windows.new_window()
    monkeypatch.setattr(box_connect.BoxConnectDialog, "exec", lambda _: QDialog.Rejected)
    publish = MagicMock()
    monkeypatch.setattr(window, "_publish_connection", publish)
    window.connect_to_box()
    publish.assert_not_called()
    window.current_deployment_folder = None
    window.close()


def test_connection_changes_update_every_window_sidebar(app, monkeypatch, tmp_path):
    from cassn.gui.windows import DeploymentWindows
    from cassn.gui.wizard import FieldDataWizard
    from cassn.gui.preview import sample_lookups
    from cassn.box import hosted
    monkeypatch.setattr(FieldDataWizard, "load_config", lambda _: None)
    monkeypatch.setattr(FieldDataWizard, "find_all_sessions", lambda _: [])
    monkeypatch.setattr(hosted, "manager_for", lambda _: SimpleNamespace(session=None))
    windows = DeploymentWindows(lookups=sample_lookups(), box_config=csu_config(), lock_directory=tmp_path)
    windows.box_authenticated = False
    first, second = windows.new_window(), windows.new_window()
    for connected in (True, False):
        first._publish_connection(connected)
        for window in (first, second):
            assert window.box_authenticated == connected
            assert window.workspace.box_status.text() == (
                "● Box connected" if connected else "○ Box not connected")
    for window in (first, second):
        window.current_deployment_folder = None
        window.close()


@pytest.mark.parametrize("profile", ["uc", "csu"])
def test_profile_paths_selected_before_imports(tmp_path, profile):
    import os, subprocess, sys, json
    code = 'import json; from cassn.config import CONFIG_DIR,LOCAL_DATA_DIR,LOCK_DIR,DEFAULT_STAGING_ROOT; from cassn.box.auth import load_box_config; print(json.dumps([str(CONFIG_DIR),str(LOCAL_DATA_DIR),str(LOCK_DIR),str(DEFAULT_STAGING_ROOT),load_box_config().provider]))'
    env = dict(os.environ, CASSN_PROFILE=profile, CASSN_CONFIG_ROOT=str(tmp_path))
    paths = json.loads(subprocess.check_output([sys.executable, "-c", code], env=env, text=True))
    assert paths[0] == str(tmp_path / 'profiles' / profile)
    assert paths[1].startswith(paths[0])
    assert paths[2] == str(tmp_path / 'window-locks')
    assert paths[3].endswith(profile.upper() + '_CASSN_field_data_staging')
    assert paths[4] == 'hosted'
    assert not (tmp_path / 'config.json').exists()


def test_reopen_browser_reuses_pending_login_and_cancellation_disables_it(app, monkeypatch):
    from PySide6.QtGui import QDesktopServices
    opened = []
    monkeypatch.setattr(QDesktopServices, 'openUrl', lambda url: opened.append(url.toString()) or True)
    dialog = box_connect.BoxConnectDialog(csu_config())
    dialog.show(); app.processEvents()
    assert not dialog.reopen_browser.isEnabled()
    assert not dialog.reopen_browser.isVisible()
    worker = SimpleNamespace(requestInterruption=MagicMock())
    dialog.worker = worker
    dialog.open_browser('https://account.box.com/api/oauth2/authorize?state=test')
    assert dialog.reopen_browser.isVisible()
    dialog.reopen_browser.click()
    assert len(opened) == 2 and opened[0] == opened[1]
    assert dialog.worker is worker and dialog.isVisible()
    dialog.reject()
    worker.requestInterruption.assert_called_once()
    assert not dialog.reopen_browser.isEnabled()
    dialog.worker = None
    dialog.close()


@pytest.fixture
def fake_login_worker(monkeypatch):
    from PySide6.QtCore import QObject, Signal
    workers = []
    class Worker(QObject):
        open_url = Signal(str)
        progress = Signal(str)
        finished = Signal()

        def __init__(self, config, parent):
            super().__init__(parent)
            self.result = None
            self.error = None
            self.interrupted = False
            workers.append(self)

        def start(self):
            self.open_url.emit("https://account.box.com/api/oauth2/authorize?state=test")

        def requestInterruption(self):
            self.interrupted = True

    monkeypatch.setattr(box_connect, "LoginWorker", Worker)
    return workers


def test_setup_one_click_opens_browser_and_loads_project_without_another_dialog(app, monkeypatch, tmp_path, fake_login_worker):
    monkeypatch.setattr(hosted_setup.QTimer, "singleShot", lambda *_: None)
    monkeypatch.setattr(box_connect, "BoxConnectDialog", lambda *a: pytest.fail("unexpected confirmation dialog"))
    opened = []
    monkeypatch.setattr(hosted_setup.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    dialog = hosted_setup.HostedSetupDialog(csu_config(), cache_dir=tmp_path)
    load = MagicMock()
    monkeypatch.setattr(dialog, "load_project", load)
    dialog.connect_button.click()
    assert len(opened) == len(fake_login_worker) == 1
    assert dialog.cancel_login_button.isHidden() is False
    dialog.connect_box()
    assert len(fake_login_worker) == 1
    fake_login_worker[0].result = True
    fake_login_worker[0].finished.emit()
    load.assert_called_once_with(require_live=True)
    assert dialog.worker is None
    dialog.close()


def test_setup_cancel_signin_stays_open_and_allows_retry(app, monkeypatch, tmp_path, fake_login_worker):
    monkeypatch.setattr(hosted_setup.QTimer, "singleShot", lambda *_: None)
    monkeypatch.setattr(hosted_setup.QDesktopServices, "openUrl", lambda _: True)
    dialog = hosted_setup.HostedSetupDialog(csu_config(), cache_dir=tmp_path)
    dialog.show()
    dialog.connect_button.click()
    worker = fake_login_worker[0]
    dialog.cancel_login_button.click()
    assert worker.interrupted
    assert not dialog.reopen_browser.isVisible()
    assert not dialog.cancel_login_button.isEnabled()
    worker.finished.emit()
    assert dialog.isVisible() and dialog.connect_button.isEnabled()
    dialog.connect_button.click()
    assert len(fake_login_worker) == 2
    dialog.reject()
    assert fake_login_worker[1].interrupted
    fake_login_worker[1].finished.emit()
    assert not dialog.isVisible()
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_workspace_login_auto_starts_with_no_second_connect_click(app, monkeypatch, fake_login_worker):
    monkeypatch.setattr(box_connect.QDesktopServices, "openUrl", lambda _: True)
    dialog = box_connect.BoxConnectDialog(csu_config(), auto_start=True)
    assert dialog.connect_button.isHidden()
    def finish():
        if fake_login_worker:
            fake_login_worker[0].result = True
            fake_login_worker[0].finished.emit()
    timer = QTimer()
    timer.timeout.connect(finish)
    timer.start(5)
    assert dialog.exec() == QDialog.DialogCode.Accepted
    timer.stop()
    assert len(fake_login_worker) == 1
    dialog.close()


def test_disconnect_dialog_closes_after_remote_cleanup(app, monkeypatch):
    monkeypatch.setattr(hosted_setup, 'manager_for', lambda _: SimpleNamespace(disconnect=lambda: True))
    dialog = hosted_setup.HostedDisconnectDialog(csu_config())
    dialog.show()
    settle(app, lambda: dialog.worker is None)
    assert dialog.disconnected and dialog.remote_removed
    dialog.close_button.click()
    app.processEvents()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert not dialog.isVisible()


def test_double_connect_does_not_start_a_second_worker(app, monkeypatch):
    dialog = box_connect.BoxConnectDialog(csu_config())
    worker = object()
    dialog.worker = worker
    monkeypatch.setattr(box_connect, 'LoginWorker', lambda *args: pytest.fail('duplicate login worker'))
    dialog.start_login()
    assert dialog.worker is worker
    dialog.worker = None
    dialog.close()


@pytest.mark.parametrize('kind', ['keychain_approval', 'storage', 'reconnect', 'denied'])
def test_authentication_failure_stays_recoverable_without_cache_retry(app, monkeypatch, tmp_path, kind):
    from cassn.box.hosted import HostedError
    calls = []
    def token():
        calls.append('token')
        raise HostedError(kind)
    monkeypatch.setattr(hosted_setup, 'manager_for', lambda _: SimpleNamespace(token=token, session=None))
    monkeypatch.setattr(hosted_setup, 'bootstrap_lookup_tables', lambda *a: pytest.fail('must not retry blocked credentials'))
    dialog = hosted_setup.HostedSetupDialog(csu_config(), cache_dir=tmp_path)
    dialog.show()
    settle(app, lambda: dialog.worker is None)
    assert calls == ['token']
    assert dialog.result() != QDialog.Accepted
    assert dialog.connect_button.isVisible() and dialog.connect_button.isEnabled()
    assert str(HostedError(kind)) in dialog.status.text()
    assert not dialog.connected
    dialog.close()


def test_successful_login_keeps_settings_failure_on_welcome_until_reload(app, monkeypatch, tmp_path):
    # The broker authenticates successfully, but configuration sync falls back
    # to cache. Do not claim sign-in failed or send the user into a disconnected
    # workspace. Reuse that connection when the operator reloads settings.
    manager = SimpleNamespace(token=lambda: object(), session=object())
    monkeypatch.setattr(hosted_setup, 'manager_for', lambda _: manager)
    cached = SimpleNamespace(source='offline-cache', lookups=object())
    live = SimpleNamespace(source='box', lookups=object())
    responses = [cached, cached, live]
    monkeypatch.setattr(hosted_setup, 'bootstrap_lookup_tables', lambda *a: responses.pop(0))
    dialog = hosted_setup.HostedSetupDialog(csu_config(), cache_dir=tmp_path)
    dialog.show()
    settle(app, lambda: dialog.worker is None)
    assert dialog.connected and dialog.result() != QDialog.Accepted
    assert dialog.retry_button.isVisible() and not dialog.connect_button.isVisible()
    assert 'signed in' in dialog.status.text()
    dialog.retry_button.click()
    settle(app, lambda: dialog.worker is None)
    assert dialog.connected and dialog.result() != QDialog.Accepted
    dialog.retry_button.click()
    settle(app, lambda: dialog.bootstrap is live)
    assert dialog.result() == QDialog.Accepted and dialog.connected
    dialog.close()


def test_reload_after_explicit_login_does_not_silently_accept_offline_cache(app, monkeypatch, tmp_path):
    from cassn.box.hosted import HostedError
    monkeypatch.setattr(hosted_setup.QTimer, 'singleShot', lambda *_: None)
    def offline():
        raise HostedError('network')
    monkeypatch.setattr(hosted_setup, 'manager_for', lambda _: SimpleNamespace(token=offline, session=object()))
    monkeypatch.setattr(hosted_setup, 'bootstrap_lookup_tables', lambda *a: SimpleNamespace(source='offline-cache', lookups=object()))
    dialog = hosted_setup.HostedSetupDialog(csu_config(), cache_dir=tmp_path)
    dialog.show()
    dialog.load_project(require_live=True)
    settle(app, lambda: dialog.worker is None)
    assert dialog.result() != QDialog.Accepted
    dialog.retry_button.click()
    settle(app, lambda: dialog.worker is None)
    assert dialog.result() != QDialog.Accepted
    dialog.close()
