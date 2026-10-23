"""Browser sign-in and credential migration without accessing real credentials."""
import io
import json
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from cassn.box import auth
from cassn.box import connect, secrets


@pytest.fixture
def vault(monkeypatch):
    values = {}
    class Store:
        def get_password(self, service, account):
            return values.get(account)
        def set_password(self, service, account, value):
            values[account] = value
        def delete_password(self, service, account):
            del values[account]
    monkeypatch.setattr(secrets, "backend", Store)
    return values


@pytest.fixture
def config():
    return auth.BoxConfig("client", "secret", "123", "456")


def test_save_migrates_only_secrets_preserving_settings(tmp_path, vault, config):
    path = tmp_path / "config.json"
    old = tmp_path / "box_tokens.json"
    path.write_text(json.dumps({"box": {"client_secret": "secret"}, "staging_root": "/data"}))
    old.write_text("old token")
    token = SimpleNamespace(access_token="access", refresh_token="refresh")
    connect.save_connection(config, token, path, old)
    data = json.loads(path.read_text())
    assert data["staging_root"] == "/data"
    assert "client_secret" not in data["box"]
    assert not old.exists()
    assert vault["client:client_secret"] == "secret"
    assert json.loads(vault["client:tokens"])["refresh_token"] == "refresh"
    assert auth.load_box_config(path) == config
    assert path.stat().st_mode & 0o777 == 0o600


def test_failed_secure_write_keeps_legacy_files(tmp_path, monkeypatch, config, vault):
    path = tmp_path / "config.json"
    path.write_text('{"box":{"client_secret":"secret"}}')
    old = tmp_path / "box_tokens.json"
    old.write_text("old token")
    before = path.read_bytes()
    monkeypatch.setattr(secrets.KeychainTokenStorage, "store", lambda *_: (_ for _ in ()).throw(RuntimeError("locked")))
    with pytest.raises(RuntimeError):
        connect.save_connection(config, SimpleNamespace(), path, old)
    assert path.read_bytes() == before
    assert old.read_text() == "old token"


def test_secure_write_must_read_back(monkeypatch):
    monkeypatch.setattr(secrets, "backend", lambda: SimpleNamespace(
        set_password=lambda *_: None, get_password=lambda *_: None,
    ))
    with pytest.raises(RuntimeError, match="verify"):
        secrets.write("test", "secret")


def test_secure_tokens_work_without_legacy_file_and_refresh(vault, config, monkeypatch, tmp_path):
    path = tmp_path / "missing.json"
    monkeypatch.setattr(auth, "BOX_TOKEN_FILE", path)
    store = secrets.KeychainTokenStorage(config.client_id)
    store.store(SimpleNamespace(access_token="initial", refresh_token="r1"))
    seen = {}
    def client(oauth):
        seen["storage"] = oauth.token_storage
        return oauth
    # Use actual OAuthConfig and BoxOAuth to exercise the SDK's storage contract.
    monkeypatch.setattr(auth, "BoxClient", client)
    assert auth.get_box_client(config, token_file=path) is not None
    seen["storage"].store(SimpleNamespace(access_token="renewed", refresh_token="r2"))
    assert store.get().refresh_token == "r2"
    assert not path.exists()


class Request:
    def __init__(self, path):
        self.stream = io.BytesIO(f"GET {path} HTTP/1.0\r\n\r\n".encode())
        self.output = b""
    def makefile(self, *_args):
        return self.stream
    def sendall(self, data):
        self.output += data


def fake_login(monkeypatch, *, denied=False, invalid_first=False):
    import box_sdk_gen
    shared = {"responses": [], "exchanges": []}
    class OAuth:
        def __init__(self, config):
            pass
        def get_authorize_url(self, *, options):
            shared["state"] = options.state
            assert options.redirect_uri == connect.REDIRECT_URI
            return "https://account.box.com/authorize"
        def get_tokens_authorization_code_grant(self, code):
            shared["exchanges"].append(code)
            return SimpleNamespace(access_token="token", refresh_token="refresh")
    class Server:
        def __init__(self, address, handler):
            assert address == ("127.0.0.1", 8765)
            self.handler = handler
            self.get_request = lambda: None
        def __enter__(self):
            return self
        def __exit__(self, *_):
            shared["closed"] = True
        def handle_request(self):
            if invalid_first and not shared["responses"]:
                query = {"state": "attacker", "code": "bad"}
            else:
                query = {"state": shared["state"]}
                query.update({"error": "access_denied"} if denied else {"code": "good"})
            request = Request("/callback?" + urlencode(query))
            self.handler(request, ("127.0.0.1", 9999), self)
            shared["responses"].append(request.output)
    monkeypatch.setattr(connect, "HTTPServer", Server)
    monkeypatch.setattr(box_sdk_gen, "BoxOAuth", OAuth)
    monkeypatch.setattr(box_sdk_gen, "BoxClient", lambda *_: SimpleNamespace(
        users=SimpleNamespace(get_user_me=lambda: SimpleNamespace(name="User"))))
    return shared


def test_callback_rejects_wrong_state_before_exchanging_code(monkeypatch, config):
    shared = fake_login(monkeypatch, invalid_first=True)
    token, name = connect.browser_login(config, lambda _: None, lambda: False)
    assert name == "User"
    assert token.access_token == "token"
    assert b"400" in shared["responses"][0]
    assert shared["exchanges"] == ["good"]
    assert shared["closed"]


def test_denied_login_does_not_exchange_or_store(monkeypatch, config):
    shared = fake_login(monkeypatch, denied=True)
    with pytest.raises(RuntimeError, match="declined"):
        connect.browser_login(config, lambda _: None, lambda: False)
    assert shared["exchanges"] == []
    assert shared["closed"]


@pytest.mark.parametrize("cancel,timeout,match", [(True, 180, "cancelled"), (False, 0, "timed out")])
def test_cancel_and_timeout_close_callback(monkeypatch, config, cancel, timeout, match):
    shared = fake_login(monkeypatch)
    with pytest.raises(RuntimeError, match=match):
        connect.browser_login(config, lambda _: None, lambda: cancel, timeout=timeout)
    assert shared["exchanges"] == []
    assert shared["closed"]


def test_dialog_success_persists_only_after_worker_finishes(monkeypatch, config):
    from PySide6.QtWidgets import QApplication, QDialog
    from cassn.gui import box_connect as gui
    app = QApplication.instance() or QApplication([])
    saved = []
    monkeypatch.setattr(gui, "save_connection", lambda *args: saved.append(args))
    dialog = gui.BoxConnectDialog(config)
    token = SimpleNamespace(access_token="access", refresh_token="refresh")
    worker = SimpleNamespace(result=(token, "User"), deleteLater=lambda: None)
    dialog.worker = worker
    dialog.login_finished()
    assert saved == [(config, token)]
    assert dialog.connected_config == config
    assert dialog.result() == QDialog.Accepted
    dialog.close()


def test_dialog_cancel_before_completion_never_persists(monkeypatch, config):
    from PySide6.QtWidgets import QApplication, QDialog
    from cassn.gui import box_connect as gui
    app = QApplication.instance() or QApplication([])
    saved = []
    monkeypatch.setattr(gui, "save_connection", lambda *args: saved.append(args))
    dialog = gui.BoxConnectDialog(config)
    dialog.worker = SimpleNamespace(
        result=(SimpleNamespace(), "User"), requestInterruption=lambda: None,
        deleteLater=lambda: None,
    )
    dialog.reject()
    dialog.login_finished()
    assert saved == []
    assert dialog.connected_config is None
    assert dialog.result() == QDialog.Rejected
    dialog.close()


def test_dialog_failed_storage_allows_retry(monkeypatch, config):
    from PySide6.QtWidgets import QApplication
    from cassn.gui import box_connect as gui
    app = QApplication.instance() or QApplication([])
    def fail(*_):
        raise RuntimeError("sensitive SDK details")
    monkeypatch.setattr(gui, "save_connection", fail)
    dialog = gui.BoxConnectDialog(config)
    dialog.worker = SimpleNamespace(result=(SimpleNamespace(), "User"), deleteLater=lambda: None)
    dialog.login_finished()
    assert dialog.connected_config is None
    assert dialog.connect_button.isEnabled()
    assert "sensitive" not in dialog.status.text()
    dialog.close()


def test_locked_store_preserves_settings_for_offline_startup(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"box": {"client_id": "client", "field_data_folder_id": "123", "app_config_folder_id": "456"}}))
    def locked(*_):
        raise RuntimeError("locked")
    monkeypatch.setattr(auth, "read", locked)
    config = auth.load_box_config(path)
    assert config.client_id == "client"
    assert config.field_data_folder_id == "123"
    assert config.app_config_folder_id == "456"
    assert config.client_secret is None
    assert auth.get_box_client(config) is None


def test_existing_legacy_connection_does_not_touch_keychain(monkeypatch, tmp_path, config):
    path = tmp_path / "box_tokens.json"
    path.write_text('{"access_token":"legacy","refresh_token":"refresh"}')
    monkeypatch.setattr(auth, "BOX_TOKEN_FILE", path)
    def forbidden(*_):
        pytest.fail("Unmigrated connections must not access Keychain")
    monkeypatch.setattr(secrets, "backend", forbidden)
    monkeypatch.setattr(auth, "BoxClient", lambda oauth: oauth)
    oauth = auth.get_box_client(config, token_file=path)
    assert oauth.token_storage.get().access_token == "legacy"
