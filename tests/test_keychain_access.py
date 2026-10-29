"""Keychain approval is explicit; failures never expose backend details."""
from types import SimpleNamespace

import pytest

from cassn.box import secrets as vault
from cassn.box.hosted import DeviceSession, HostedError, SessionManager, SessionStore


@pytest.mark.parametrize('status', [0, -25300, -25308, -128, -25293])
@pytest.mark.skipif(vault.sys.platform != 'darwin', reason='macOS Security.framework adapter')
def test_silent_mac_read_disables_authentication_ui_and_releases_objects(monkeypatch, status):
    from keyring.backends import macOS
    captured, released = {}, []

    def query(**kwargs):
        captured.update(kwargs)
        return 42

    def copy_matching(query, data):
        if status == 0:
            data._obj.value = 99
        return status

    def release(value):
        released.append(getattr(value, 'value', value))

    class Error(Exception):
        @staticmethod
        def raise_for_status(code):
            if code:
                raise Error(code, 'must never be displayed')

    api = SimpleNamespace(
        create_query=query, k_=lambda key: key, SecItemCopyMatching=copy_matching,
        error=SimpleNamespace(item_not_found=-25300), Error=Error,
        cfstr_to_str=lambda _: 'synthetic-test-secret', _found=SimpleNamespace(CFRelease=release),
    )
    monkeypatch.setattr(macOS, 'api', api)
    if status not in (0, -25300):
        with pytest.raises(Error):
            vault._read_macos_without_prompt('test-account')
    else:
        assert vault._read_macos_without_prompt('test-account') == ('synthetic-test-secret' if status == 0 else None)
    assert captured['kSecUseAuthenticationUI'] == 'kSecUseAuthenticationUIFail'
    assert captured['kSecMatchLimit'] == 'kSecMatchLimitOne'
    assert captured['kSecAttrService'] == vault.SERVICE
    assert released == ([99, 42] if status == 0 else [42])


def test_denied_mac_access_is_classified_without_backend_message(monkeypatch):
    class Store:
        def get_password(self, *_):
            try:
                raise Exception(-128, 'private backend details')
            except Exception as exc:
                raise RuntimeError('private wrapper') from exc
    monkeypatch.setattr(vault, 'backend', Store)
    with pytest.raises(vault.CredentialAccessError) as caught:
        vault.read('test')
    assert caught.value.kind == 'keychain_approval'
    assert 'private' not in str(caught.value)


def test_startup_is_silent_and_explicit_connect_can_retry_approval(tmp_path, monkeypatch):
    from cassn import config
    monkeypatch.setattr(config, 'BASE_CONFIG_DIR', tmp_path)
    modes = []
    session = DeviceSession('a' * 32, 'b' * 43, 9999999999.)

    def read(account, *, interactive):
        import json
        modes.append(interactive)
        if not interactive:
            raise vault.CredentialAccessError('keychain_approval')
        return json.dumps(vars(session))

    monkeypatch.setattr(vault, 'read', read)
    manager = SessionManager(store=SessionStore('uc'), profile='uc')
    with pytest.raises(HostedError, match='Mac login password'):
        manager.token()
    assert not manager.loaded and manager.state == 'storage'
    assert manager._load(interactive=True) == session
    assert modes == [False, True]
