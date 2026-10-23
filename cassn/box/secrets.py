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


def read(account):
    return backend().get_password(SERVICE, account)


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
