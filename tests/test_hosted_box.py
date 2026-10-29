"""Hosted credentials and SDK renewal without real accounts or Keychain."""
import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from urllib.parse import urlencode

import pytest

from cassn.box.auth import BoxConfig, csu_config, get_box_client
from cassn.box.hosted import (DeviceSession, HostedAuthentication, HostedError,
    SessionManager, SessionStore, _authorization_url)
from cassn.profile import HOSTED_CLIENT_ID, HOSTED_ORIGIN, CSU_DATA_ID, CSU_CONFIG_ID, session_matches


class Clock:
    def __init__(self):
        self.now = 1000.
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


class Store:
    def __init__(self, session=None):
        self.session = session
        self.fail = False
    def load(self):
        return self.session
    def save(self, session):
        if self.fail:
            raise HostedError("storage")
        self.session = replace(session)
    def clear(self):
        self.session = None


def result(clock, access="access", user="123"):
    return {"status": "connected", "access_token": access, "expires_in": 3600,
            "session_expires_at": (clock() + 604800) * 1000,
            "user": {"id": user, "name": "Test User"},
            "project": {"field_data_folder_id": CSU_DATA_ID, "app_config_folder_id": CSU_CONFIG_ID}}


def login_response():
    sid = "a" * 32
    return {"session_id": sid, "expires_in": 600, "poll_interval": 3,
        "authorization_url": "https://account.box.com/api/oauth2/authorize?" + urlencode({
            "response_type": "code", "client_id": HOSTED_CLIENT_ID,
            "redirect_uri": HOSTED_ORIGIN + "/oauth/callback", "state": sid + "." + "b" * 43})}


def fixture(*, old=False):
    clock = Clock()
    session = DeviceSession("c" * 32, "d" * 43, clock() + 604800, "123", "Test User") if old else None
    store = Store(session)
    calls = []
    responses = []
    def request(path, method="POST", payload=None, proof=None):
        calls.append((path, method, payload, proof))
        if method == "DELETE":
            return 200, {}
        if path == "/v1/sessions":
            return 201, login_response()
        if responses:
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return 200, result(clock)
    manager = SessionManager(store, request, clock=clock, wall=clock, sleep=clock.sleep)
    return manager, store, clock, calls, responses


def test_login_persists_device_proof_only_and_cleans_old_session():
    m, store, clock, calls, responses = fixture(old=True)
    responses[:] = [(202, {}), (429, {}), (200, result(clock))]
    urls = []
    assert m.login(urls.append, lambda: False) == "Test User"
    assert len(urls) == 1 and urls[0].startswith("https://account.box.com/")
    proof = store.session.proof
    challenge = base64.urlsafe_b64encode(hashlib.sha256(proof.encode()).digest()).decode().rstrip("=")
    assert calls[0][2] == {"code_challenge": challenge}
    assert proof not in urls[0]
    assert calls[-1][:2] == ("/v1/sessions/" + "c" * 32, "DELETE")
    assert "access" not in json.dumps(vars(store.session))
    assert "refresh" not in json.dumps(vars(store.session))


@pytest.mark.parametrize("mode", ["cancel", "storage", "denied", "bad_response", "different_user"])
def test_failed_login_preserves_prior_connection_and_deletes_candidate(mode):
    m, store, clock, calls, responses = fixture(old=True)
    previous = store.session
    if mode == "storage":
        store.fail = True
    elif mode == "denied":
        responses[:] = [(403, {})]
    elif mode == "bad_response":
        responses[:] = [(200, {"access_token": "secret"})]
    elif mode == "different_user":
        responses[:] = [(200, result(clock, user="456"))]
    with pytest.raises(HostedError):
        m.login(lambda _: None, lambda: mode == "cancel")
    assert store.session == previous
    assert calls[-1][:2] == ("/v1/sessions/" + "a" * 32, "DELETE")


def test_transient_login_failure_retries_then_succeeds():
    m, store, clock, calls, responses = fixture()
    responses[:] = [HostedError("network"), (200, result(clock))]
    assert m.login(lambda _: None, lambda: False)


def test_timeout_is_bounded_and_cleans_candidate():
    m, store, clock, calls, responses = fixture()
    original = m.request
    def pending(path, method="POST", **kw):
        if path.endswith("/token"):
            return 202, {}
        return original(path, method, **kw)
    m.request = pending
    with pytest.raises(HostedError, match="timed out"):
        m.login(lambda _: None, lambda: False)
    assert calls[-1][1] == "DELETE"
    assert store.session is None


def test_cancellation_during_store_save_rolls_back():
    m, store, clock, calls, responses = fixture(old=True)
    previous = store.session
    saved = []
    original = store.save
    def save(session):
        saved.append(session)
        original(session)
    store.save = save
    with pytest.raises(HostedError, match="cancelled"):
        m.login(lambda _: None, lambda: bool(saved))
    assert store.session == previous


@pytest.mark.parametrize("field,value", [
    ("authorization_url", "https://evil.example/"),
    ("authorization_url", "https://account.box.com/api/oauth2/authorize?client_id=bad"),
    ("session_id", "e" * 32),
])
def test_untrusted_authorization_url_rejected(field, value):
    data = login_response()
    data[field] = value
    with pytest.raises(HostedError):
        _authorization_url(data)


def test_sdk_clients_share_cached_token_and_renew_once_across_threads():
    m, store, clock, calls, responses = fixture(old=True)
    auth = HostedAuthentication(m)
    assert auth.retrieve_authorization_header() == "Bearer access"
    clock.now += 3550
    responses[:] = [(200, result(clock, "renewed"))]
    with ThreadPoolExecutor(max_workers=6) as pool:
        tokens = list(pool.map(lambda _: auth.retrieve_token().access_token, range(6)))
    assert tokens == ["renewed"] * 6
    assert len(calls) == 2
    assert store.session.proof == "d" * 43


def test_early_401_cannot_loop_on_same_token():
    m, store, clock, calls, responses = fixture(old=True)
    auth = HostedAuthentication(m)
    auth.retrieve_token()
    with pytest.raises(HostedError, match="new sign-in"):
        auth.refresh_token()
    for _ in range(3):
        with pytest.raises(HostedError):
            auth.retrieve_token()
    assert len(calls) == 2


def test_network_failure_keeps_record_and_allows_retry():
    m, store, clock, calls, responses = fixture(old=True)
    responses[:] = [HostedError("network")]
    with pytest.raises(HostedError):
        m.token()
    assert store.session is not None
    assert m.token().access_token == "access"


def test_restart_reads_device_session_and_expiry_requires_login():
    m, store, clock, calls, responses = fixture(old=True)
    assert m.token().access_token == "access"
    clock.now = store.session.expires_at + 1
    with pytest.raises(HostedError):
        m.token()


def test_offline_disconnect_clears_local_and_reports_unconfirmed():
    m, store, clock, calls, responses = fixture(old=True)
    m.token()
    m.request = lambda *a, **kw: (_ for _ in ()).throw(HostedError("network"))
    assert m.disconnect() is False
    assert store.session is None and m.access is None
    with pytest.raises(HostedError):
        m.token()


def test_profiles_cannot_reuse_each_others_saved_deployments():
    hosted, legacy = csu_config(), BoxConfig("id", "secret", "uc", "config")
    assert session_matches({}, legacy)
    assert not session_matches({}, hosted)
    session = {"box_project": {"profile": "csu", "data_folder": CSU_DATA_ID}}
    assert session_matches(session, hosted)
    assert not session_matches(session, legacy)


def test_sdk_factory_hosted_never_reads_legacy_tokens(monkeypatch):
    import cassn.box.auth as auth
    import cassn.box.hosted as hosted
    m, *_ = fixture(old=True)
    monkeypatch.setattr(hosted, "manager_for", lambda _: m)
    monkeypatch.setattr(auth, "SimpleTokenStorage", lambda *_: pytest.fail("legacy storage used"))
    monkeypatch.setattr(auth, "BoxClient", lambda x: x)
    client = get_box_client(csu_config())
    assert isinstance(client, HostedAuthentication)
    assert client.retrieve_token().access_token == "access"


def test_two_sdk_clients_coalesce_401_refresh_for_same_rejected_token():
    m, store, clock, calls, responses = fixture(old=True)
    first, second = HostedAuthentication(m), HostedAuthentication(m)
    first.retrieve_token(); second.retrieve_token()
    responses[:] = [(200, result(clock, "replacement"))]
    assert first.refresh_token().access_token == "replacement"
    assert second.refresh_token().access_token == "replacement"
    assert len(calls) == 2
    assert calls[-1][2] == {"force_refresh": True}


def test_os_store_keeps_legacy_records_and_rolls_back_bad_write(monkeypatch):
    from cassn.box import secrets as vault
    values = {"legacy:tokens": "existing"}
    class Backend:
        bad = False
        def get_password(self, service, account):
            return values.get(account)
        def set_password(self, service, account, value):
            values[account] = "damaged" if self.bad else value
            self.bad = False
        def delete_password(self, service, account):
            del values[account]
    backend = Backend()
    monkeypatch.setattr(vault, "backend", lambda: backend)
    store = SessionStore()
    old = DeviceSession("a" * 32, "b" * 43, 9999999999)
    store.save(old)
    backend.bad = True
    with pytest.raises(HostedError):
        store.save(DeviceSession("c" * 32, "d" * 43, 9999999999))
    assert store.load() == old
    store.clear()
    assert values == {"legacy:tokens": "existing"}


def test_uc_login_is_bound_to_uc_service_and_folders():
    from cassn.box.auth import hosted_config
    from cassn.box.hosted import _token, Transport
    from cassn.profile import PROJECTS
    uc = hosted_config('uc')
    assert uc.is_complete and uc.client_secret is None
    assert not replace(uc, service_url=HOSTED_ORIGIN).is_complete
    assert not replace(uc, field_data_folder_id=CSU_DATA_ID).is_complete
    assert SessionStore('uc').account != SessionStore('csu').account
    assert Transport(uc.service_url).origin == uc.service_url
    clock, store = Clock(), Store()
    response = result(clock)
    response['project'] = {'field_data_folder_id': uc.field_data_folder_id,
                           'app_config_folder_id': uc.app_config_folder_id}
    start = login_response()
    start['authorization_url'] = start['authorization_url'].replace(
        'cassn-csu-box-auth', 'cassn-uc-box-auth')
    def request(path, method='POST', **kwargs):
        return (201, start) if path == '/v1/sessions' else (200, response)
    m = SessionManager(store, request, profile='uc', clock=clock, wall=clock, sleep=clock.sleep)
    assert m.login(lambda _: None, lambda: False) == 'Test User'
    assert m.token().access_token == 'access'
    with pytest.raises(HostedError):
        _authorization_url(start, 'csu')
    with pytest.raises(HostedError):
        _token(response, store.session, clock(), 'csu')
    with pytest.raises(HostedError):
        Transport('https://untrusted.example')


def test_explicit_uc_local_credential_works_without_keychain(tmp_path, monkeypatch):
    from cassn import config
    from cassn.box import secrets as vault
    monkeypatch.setattr(config, 'BASE_CONFIG_DIR', tmp_path)
    monkeypatch.setattr(vault, 'read', lambda *a: pytest.fail('local credential must avoid Keychain'))
    monkeypatch.setattr(vault, 'backend', lambda: pytest.fail('local renewal must avoid Keychain'))
    session = DeviceSession('c' * 32, 'd' * 43, 9999999999., '123', 'Test')
    path = tmp_path / 'box_tokens.json'
    store = SessionStore('uc')
    store._save_local(path, session)
    assert path.stat().st_mode & 0o777 == 0o600
    assert SessionStore('uc').load() == session
    renewed = replace(session, expires_at=99999999999.)
    store.save(renewed)
    assert SessionStore('uc').load() == renewed
    assert path.stat().st_mode & 0o777 == 0o600
    assert SessionStore('csu')._local_copy() is None


def test_uc_local_credential_rejects_shared_permissions_and_symlinks(tmp_path, monkeypatch):
    from cassn import config
    monkeypatch.setattr(config, 'BASE_CONFIG_DIR', tmp_path)
    session = DeviceSession('c' * 32, 'd' * 43, 9999999999., '123', 'Test')
    path = tmp_path / 'box_tokens.json'
    store = SessionStore('uc')
    store._save_local(path, session)
    path.chmod(0o644)
    with pytest.raises(HostedError): store.load()
    path.chmod(0o600)
    path.rename(tmp_path / 'target')
    path.symlink_to(tmp_path / 'target')
    with pytest.raises(HostedError): store.load()


def test_uc_without_local_copy_still_uses_keychain(tmp_path, monkeypatch):
    from cassn import config
    from cassn.box import secrets as vault
    monkeypatch.setattr(config, 'BASE_CONFIG_DIR', tmp_path)
    session = DeviceSession('c' * 32, 'd' * 43, 9999999999., '123', 'Test')
    monkeypatch.setattr(vault, 'read', lambda *a, **kw: json.dumps(vars(session)))
    assert SessionStore('uc').load() == session


@pytest.mark.parametrize("reason", ["declined", "account_not_approved", "folder_access_required", "sign_in_failed"])
def test_callback_failure_is_actionable_and_preserves_previous_connection(reason):
    m, store, clock, calls, responses = fixture(old=True)
    previous = store.session
    responses[:] = [(200, {"status": "failed", "reason": reason})]
    with pytest.raises(HostedError) as error:
        m.login(lambda _: None, lambda: False)
    assert error.value.kind == reason
    assert store.session == previous
    assert calls[-1][:2] == ("/v1/sessions/" + "a" * 32, "DELETE")


@pytest.mark.parametrize("reason", ["private-upstream-detail", ["declined"], None])
def test_unknown_callback_failure_never_exposes_upstream_text(reason):
    m, store, clock, calls, responses = fixture()
    responses[:] = [(200, {"status": "failed", "reason": reason})]
    with pytest.raises(HostedError) as error:
        m.login(lambda _: None, lambda: False)
    assert error.value.kind == "sign_in_failed"
    assert "private-upstream-detail" not in str(error.value)


def test_temporary_service_outage_during_login_recovers_with_bounded_backoff():
    m, store, clock, calls, responses = fixture()
    responses[:] = [(503, {}), (502, {}), (504, {}), (200, result(clock))]
    progress = []
    assert m.login(lambda _: None, lambda: False, progress.append) == "Test User"
    assert any("Retrying" in text for text in progress)
    assert 0 < clock.now - 1000 < 600


def test_pending_login_does_not_poll_after_deadline():
    m, store, clock, calls, responses = fixture()
    original = m.request
    polls = []
    def pending(path, method="POST", **kwargs):
        if path.endswith("/token"):
            polls.append(clock())
            return 429, {}
        return original(path, method, **kwargs)
    m.request = pending
    with pytest.raises(HostedError) as error:
        m.login(lambda _: None, lambda: False)
    assert error.value.kind == "timeout"
    assert clock.now == 1600
    assert max(polls) < 1600


def test_account_switch_explains_how_to_recover():
    m, store, clock, calls, responses = fixture(old=True)
    previous = store.session
    responses[:] = [(200, result(clock, user="456"))]
    with pytest.raises(HostedError, match="disconnect before changing accounts"):
        m.login(lambda _: None, lambda: False)
    assert store.session == previous
