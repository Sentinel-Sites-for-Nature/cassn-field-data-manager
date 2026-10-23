"""Secret-free desktop client for the project authentication broker.

Only device credentials are persisted. HTTP errors and SDK-facing exceptions
never include response bodies or request headers. All calls run on workers.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import ssl
import threading
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import certifi
from box_sdk_gen import AccessToken
from box_sdk_gen.networking.auth import Authentication

from cassn.box import secrets as vault
from cassn.profile import HOSTED_ORIGIN, HOSTED_CLIENT_ID, PROJECTS

_ID = re.compile(r"[a-f0-9]{32}\Z")
_PROOF = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_MESSAGES = {
    "network": "Cannot reach Box sign-in. Check your internet connection and retry.",
    "reconnect": "Your Box connection needs a new sign-in. Click Connect to Box.",
    "denied": "This account cannot connect to this project. Contact John for access.",
    "invalid": "The sign-in service returned an unexpected response. Please retry.",
    "storage": "Could not save or read the connection securely. Check Keychain access and retry.",
    "cancelled": "Box sign-in cancelled.",
    "timeout": "Box sign-in timed out. Click Connect to Box to try again.",
    "busy": "The sign-in service is busy. Please retry shortly.",
    "declined": "Box sign-in was declined. Click Connect to Box and grant access to continue.",
    "account_not_approved": "This Box account is not approved for this project. Sign out of Box in your browser, then connect with your approved account. Contact John if you need access.",
    "folder_access_required": "Your Box account needs access to both project folders. Accept the Box invitations, then reconnect. Ask John to check data upload and configuration download permissions.",
    "sign_in_failed": "Box could not finish sign-in. Click Connect to Box to start a new attempt. If it continues, contact John.",
    "different_account": "This app already has a connection for a different Box account. Reconnect with the same account, or disconnect before changing accounts.",
}
_LOGIN_FAILURES = frozenset({"declined", "account_not_approved", "folder_access_required", "sign_in_failed"})


class HostedError(RuntimeError):
    def __init__(self, kind):
        self.kind = kind
        super().__init__(_MESSAGES.get(kind, _MESSAGES["invalid"]))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Transport:
    def __init__(self, origin=HOSTED_ORIGIN):
        if origin not in {p["origin"] for p in PROJECTS.values()}:
            raise HostedError("invalid")
        self.origin = origin
        self.opener = urllib.request.build_opener(
            NoRedirect, urllib.request.HTTPSHandler(
                context=ssl.create_default_context(cafile=certifi.where())))

    def __call__(self, path, method="POST", payload=None, proof=None):
        if method == "POST" and payload is None:
            payload = {}  # Cloudflare can represent an empty POST as a readable stream.
        headers = {"Content-Type": "application/json", "User-Agent": "CASSN-Field-Data-Manager/4.0"}
        if proof:
            headers["Authorization"] = "Bearer " + proof
        request = urllib.request.Request(self.origin + path, method=method, headers=headers,
            data=json.dumps(payload).encode() if payload is not None else None)
        try:
            try:
                response = self.opener.open(request, timeout=8)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                status = response.code
                if status not in (200, 201, 202):
                    return status, {}  # Never expose or parse arbitrary error bodies.
                raw = response.read(32_769)
                if len(raw) > 32_768:
                    raise HostedError("invalid")
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise HostedError("invalid")
                return status, data
        except HostedError:
            raise
        except (ValueError, UnicodeError):
            raise HostedError("invalid") from None
        except Exception:
            raise HostedError("network") from None


@dataclass(repr=False)
class DeviceSession:
    session_id: str
    proof: str = field(repr=False)
    expires_at: float
    user_id: str = ""
    user_name: str = ""

    @property
    def path(self):
        return "/v1/sessions/" + self.session_id

    @classmethod
    def parse(cls, data):
        try:
            value = cls(**data)
            if not _ID.fullmatch(value.session_id) or not _PROOF.fullmatch(value.proof):
                raise ValueError()
            if not isinstance(value.expires_at, (int, float)) or value.expires_at <= 0:
                raise ValueError()
            if not isinstance(value.user_id, str) or not isinstance(value.user_name, str):
                raise ValueError()
            return value
        except Exception:
            raise HostedError("storage") from None


class SessionStore:
    def __init__(self, profile="csu"):
        if profile not in PROJECTS:
            raise ValueError("Unknown hosted profile")
        self.profile = profile
        self.account = "hosted:" + hashlib.sha256((PROJECTS[profile]["origin"] + ":" + profile).encode()).hexdigest()

    def _local_copy(self):
        # Explicitly provisioned by John for unattended UC maintenance. Normal
        # installations still use Keychain; legacy OAuth JSON is not a device session.
        from cassn.config import BASE_CONFIG_DIR
        path = BASE_CONFIG_DIR / "box_tokens.json"
        if self.profile != "uc" or not path.exists():
            return None
        stat = path.lstat()
        if path.is_symlink() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            raise HostedError("storage")
        data = json.loads(path.read_text())
        if data.get("provider") != "hosted":
            return None
        if data.get("profile") != self.profile:
            raise HostedError("storage")
        return path, DeviceSession.parse(data["session"])

    def _save_local(self, path, session):
        fd, name = tempfile.mkstemp(prefix=".box-credentials-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"provider": "hosted", "profile": self.profile,
                           "session": vars(session)}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)

    def load(self):
        try:
            local = self._local_copy()
            if local is not None:
                return local[1]
            raw = vault.read(self.account)
            return DeviceSession.parse(json.loads(raw)) if raw else None
        except Exception:
            raise HostedError("storage") from None

    def save(self, session):
        try:
            local = self._local_copy()
            if local is not None:
                self._save_local(local[0], session)
                return
            store = vault.backend()
            old = store.get_password(vault.SERVICE, self.account)
            value = json.dumps(vars(session))
            try:
                store.set_password(vault.SERVICE, self.account, value)
                if store.get_password(vault.SERVICE, self.account) != value:
                    raise ValueError()
            except Exception:
                # Roll back a partially failed write; never select the new session.
                if old is not None:
                    store.set_password(vault.SERVICE, self.account, old)
                elif store.get_password(vault.SERVICE, self.account) is not None:
                    store.delete_password(vault.SERVICE, self.account)
                raise
        except Exception:
            raise HostedError("storage") from None

    def clear(self):
        try:
            local = self._local_copy()
            if local is not None:
                local[0].unlink()
            store = vault.backend()
            if store.get_password(vault.SERVICE, self.account) is not None:
                store.delete_password(vault.SERVICE, self.account)
            if store.get_password(vault.SERVICE, self.account) is not None:
                raise ValueError()
        except Exception:
            raise HostedError("storage") from None


def _status(status):
    if status == 401:
        raise HostedError("reconnect")
    if status == 403:
        raise HostedError("denied")
    if status == 429:
        raise HostedError("busy")
    raise HostedError("network" if status >= 500 else "invalid")


def _token(data, session, now, profile="csu"):
    project_config = PROJECTS[profile]
    try:
        token, lifetime = data["access_token"], data["expires_in"]
        user, project = data["user"], data["project"]
        expiry = data["session_expires_at"] / 1000
        if (data.get("status") != "connected" or not isinstance(token, str)
                or not token or len(token) > 8192 or any(c.isspace() for c in token)
                or not isinstance(lifetime, (int, float)) or not 0 < lifetime <= 86400
                or not now < expiry <= now + 8 * 86400
                or not isinstance(user["id"], str) or not user["id"].isdigit()
                or not isinstance(user["name"], str) or len(user["name"]) > 512
                or project["field_data_folder_id"] != project_config["data"]
                or project["app_config_folder_id"] != project_config["config"]
                or (session.user_id and session.user_id != user["id"])
                or "refresh_token" in data or "client_secret" in data):
            raise ValueError()
        session.expires_at = expiry
        session.user_id, session.user_name = user["id"], user["name"]
        return token, lifetime
    except Exception:
        raise HostedError("invalid") from None


def _authorization_url(data, profile="csu"):
    try:
        url = data["authorization_url"]
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query)
        state = query.get("state", [""])[0]
        if (parsed.scheme != "https" or parsed.netloc != "account.box.com"
                or parsed.path != "/api/oauth2/authorize" or parsed.fragment
                or query.get("client_id") != [HOSTED_CLIENT_ID]
                or query.get("redirect_uri") != [PROJECTS[profile]["origin"] + "/oauth/callback"]
                or query.get("response_type") != ["code"]
                or len(query.get("state", [])) != 1
                or not state.startswith(data["session_id"] + ".")
                or not _PROOF.fullmatch(state.split(".", 1)[1])):
            raise ValueError()
        return url
    except Exception:
        raise HostedError("invalid") from None


def _pause(seconds, cancelled, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + seconds
    while clock() < deadline:
        if cancelled():
            raise HostedError("cancelled")
        sleep(min(.1, deadline - clock()))
    if cancelled():
        raise HostedError("cancelled")


class SessionManager:
    def __init__(self, store=None, transport=None, *, profile="csu", clock=time.monotonic, wall=time.time, sleep=time.sleep):
        if profile not in PROJECTS:
            raise HostedError("invalid")
        self.profile = profile
        self.store = store if store is not None else SessionStore(profile)
        self.request = transport if transport is not None else Transport(PROJECTS[profile]["origin"])
        self.clock, self.wall, self.sleep = clock, wall, sleep
        self.lock = threading.RLock()
        self.session = None
        self.loaded = False
        self.access = None
        self.token_deadline = 0
        self.last_request = -100.
        self.state = "disconnected"

    def _load(self):
        if not self.loaded:
            self.session = self.store.load()
            self.loaded = True
        return self.session

    def has_session(self):
        with self.lock:
            session = self._load()
            return bool(session and session.expires_at > self.wall())

    def _delete(self, session):
        try:
            status, _ = self.request(session.path, "DELETE", proof=session.proof)
            return status in (200, 401)  # A missing/expired session is already unusable.
        except HostedError:
            return False

    def login(self, open_browser, cancelled, progress=lambda _: None):
        with self.lock:
            old = self._load()  # Check secure storage before opening the browser.
            proof = secrets.token_urlsafe(32)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(proof.encode()).digest()).decode().rstrip("=")
            progress("Opening your browser…")
            status, data = self.request("/v1/sessions", payload={"code_challenge": challenge})
            if status != 201:
                _status(status)
            try:
                sid = data["session_id"]
                if not isinstance(sid, str) or not _ID.fullmatch(sid):
                    raise ValueError()
                ttl = data["expires_in"]
                interval = data["poll_interval"]
                if not 0 < ttl <= 600 or not 2 <= interval <= 30:
                    raise ValueError()
            except Exception:
                raise HostedError("invalid") from None
            candidate = DeviceSession(sid, proof, self.wall() + ttl)
            committed = False
            try:
                url = _authorization_url(data, self.profile)
                if cancelled():
                    raise HostedError("cancelled")
                open_browser(url)
                progress("Waiting for Box sign-in in your browser…")
                deadline = self.clock() + ttl
                delay = interval
                while self.clock() < deadline:
                    _pause(min(delay, max(0, deadline - self.clock())), cancelled, self.clock, self.sleep)
                    if self.clock() >= deadline:
                        break
                    try:
                        status, result = self.request(candidate.path + "/token", proof=proof)
                    except HostedError as exc:
                        if exc.kind != "network":
                            raise
                        delay = min(15, delay * 2)
                        continue
                    if status in (202, 429, 502, 503, 504):
                        delay = interval if status == 202 else min(15, delay * 2)
                        if status != 202:
                            progress("Sign-in is temporarily unavailable. Retrying… You can cancel at any time.")
                        continue
                    if status != 200:
                        _status(status)
                    if result.get("status") == "failed":
                        reason = result.get("reason")
                        raise HostedError(reason if isinstance(reason, str) and reason in _LOGIN_FAILURES else "sign_in_failed")
                    access, lifetime = _token(result, candidate, self.wall(), self.profile)
                    if old and old.user_id and candidate.user_id != old.user_id:
                        raise HostedError("different_account")  # No account switching under active lookups.
                    if cancelled():
                        raise HostedError("cancelled")
                    progress("Saving your connection securely…")
                    self.store.save(candidate)
                    # Cancellation during a Keychain prompt must not commit late success.
                    if cancelled():
                        if old:
                            self.store.save(old)
                        else:
                            self.store.clear()
                        raise HostedError("cancelled")
                    self.session, self.access = candidate, access
                    self.token_deadline = self.clock() + lifetime
                    self.last_request = self.clock()
                    self.state = "connected"
                    committed = True
                    if old and old.session_id != sid:
                        self._delete(old)
                    return candidate.user_name
                raise HostedError("timeout")
            finally:
                if not committed:
                    self._delete(candidate)

    def token(self, *, force=False, rejected=None):
        with self.lock:
            session = self._load()
            if self.state in ("reconnect", "denied"):
                raise HostedError(self.state)
            if not session or session.expires_at <= self.wall():
                self.access = None
                self.state = "reconnect"
                raise HostedError("reconnect")
            if not force and self.access and self.token_deadline > self.clock() + 60:
                return AccessToken(access_token=self.access)
            if force and rejected is not None and self.access and self.access != rejected and self.token_deadline > self.clock() + 60:
                return AccessToken(access_token=self.access)
            previous = self.access
            gap = 2.1 - (self.clock() - self.last_request)
            if gap > 0:
                self.sleep(gap)
            try:
                self.last_request = self.clock()
                status, data = self.request(session.path + "/token", proof=session.proof,
                                            payload={"force_refresh": True} if force else None)
                if status != 200:
                    _status(status)
                access, lifetime = _token(data, session, self.wall(), self.profile)
                if force and access == previous:
                    raise HostedError("reconnect")
                self.access = access
                self.token_deadline = self.clock() + lifetime
                self.state = "connected"
                return AccessToken(access_token=access)
            except HostedError as exc:
                self.state = exc.kind
                if exc.kind in ("reconnect", "denied"):
                    self.access = None
                raise

    def disconnect(self):
        with self.lock:
            session = self._load()
            # Clear locally first. Failure must be visible; never claim disconnected.
            self.store.clear()
            self.session, self.access, self.loaded = None, None, True
            self.state = "disconnected"
            return self._delete(session) if session else True


_managers = {}
_registry_lock = threading.Lock()


def manager_for(config):
    if config.provider != "hosted" or not config.is_complete:
        raise HostedError("invalid")
    key = (config.service_url, config.profile)
    with _registry_lock:
        if key not in _managers:
            _managers[key] = SessionManager(profile=config.profile)
        return _managers[key]


class HostedAuthentication(Authentication):
    def __init__(self, manager):
        self.manager = manager
        self.last_token = None

    def retrieve_token(self, *, network_session=None):
        token = self.manager.token()
        self.last_token = token.access_token
        return token

    def retrieve_authorization_header(self, *, network_session=None):
        return "Bearer " + self.retrieve_token().access_token

    def refresh_token(self, *, network_session=None):
        token = self.manager.token(force=True, rejected=self.last_token)
        self.last_token = token.access_token
        return token

    def revoke_token(self, *, network_session=None):
        return self.manager.disconnect()

    def downscope_token(self, scopes, *, resource=None, shared_link=None, network_session=None):
        raise HostedError("invalid")  # Not part of the desktop protocol.
