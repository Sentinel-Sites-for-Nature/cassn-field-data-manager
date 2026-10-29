"""OS-backed credential storage; never fall back to plaintext for new secrets."""
from __future__ import annotations

import json
import sys

SERVICE = "org.cassn.field-data-manager.box"


def backend():
    try:
        import keyring
        if sys.platform == "darwin":
            from keyring.backends.macOS import Keyring
            return Keyring()
        candidate = keyring.get_keyring()
        if not candidate.__class__.__module__.startswith((
            "keyring.backends.Windows", "keyring.backends.SecretService", "keyring.backends.kwallet",
        )):
            raise RuntimeError("No supported system credential store is available.")
        return candidate
    except ImportError as exc:
        raise RuntimeError("Install the app dependencies to enable secure storage (keyring).") from exc


class CredentialAccessError(RuntimeError):
    """A safe classification, without Keychain contents or backend exception text."""

    def __init__(self, kind):
        self.kind = kind
        super().__init__(kind)


def _access_error(exc):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        status = exc.args[0] if exc.args else None
        if status in (-128, -25293, -25308, -67030):
            return CredentialAccessError("keychain_approval")
        exc = exc.__cause__ or exc.__context__
    return CredentialAccessError("storage")


def _read_macos_without_prompt(account):
    """Ask Security.framework to fail rather than display an authorization dialog."""
    from ctypes import byref, c_void_p
    from keyring.backends.macOS import api

    query = api.create_query(
        kSecClass=api.k_("kSecClassGenericPassword"),
        kSecMatchLimit=api.k_("kSecMatchLimitOne"),
        kSecAttrService=SERVICE,
        kSecAttrAccount=account,
        kSecReturnData=True,
        kSecUseAuthenticationUI=api.k_("kSecUseAuthenticationUIFail"),
    )
    data = c_void_p()
    release = api._found.CFRelease
    release.argtypes = (c_void_p,)
    release.restype = None
    try:
        status = api.SecItemCopyMatching(query, byref(data))
        if status == api.error.item_not_found:
            return None
        api.Error.raise_for_status(status)
        return api.cfstr_to_str(data)
    finally:
        if data.value:
            release(data)
        release(query)


def read(account, *, interactive=True):
    try:
        store = backend()
        if not interactive and sys.platform == "darwin" and type(store).__module__ == "keyring.backends.macOS":
            return _read_macos_without_prompt(account)
        return store.get_password(SERVICE, account)
    except Exception as exc:
        raise _access_error(exc) from None


def write(account, value):
    store = backend()
    store.set_password(SERVICE, account, value)
    if store.get_password(SERVICE, account) != value:
        raise RuntimeError("Could not verify the saved system credential.")


class KeychainTokenStorage:
    def __init__(self, client_id):
        self.account = f"{client_id}:tokens"

    def store(self, token):
        write(self.account, json.dumps({
            "access_token": token.access_token, "refresh_token": token.refresh_token,
        }))

    def get(self):
        from box_sdk_gen import AccessToken
        value = read(self.account)
        if not value:
            return None
        data = json.loads(value)
        return AccessToken(access_token=data["access_token"], refresh_token=data.get("refresh_token"))

    def clear(self):
        store = backend()
        if store.get_password(SERVICE, self.account) is not None:
            store.delete_password(SERVICE, self.account)
