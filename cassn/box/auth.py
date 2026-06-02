"""
Box authentication and credential loading.

Mirrors the original exactly:

* credentials come from ``~/.cassn_config/config.json`` under the ``box`` key
  (``client_id``, ``client_secret``, ``field_data_folder_id``, optional
  ``app_config_folder_id``);
* OAuth tokens are persisted to ``~/.cassn_config/box_tokens.json`` through a
  minimal token-storage shim the Box SDK calls on every refresh.

``BOX_AVAILABLE`` reflects whether ``box_sdk_gen`` imported; when False every
Box code path degrades exactly as the original did. Unlike the original, this
module performs no work at import time beyond the optional-dependency probe —
:func:`load_box_config` is called explicitly from the entry point.
"""

from __future__ import annotations

import json
import traceback
from dataclasses import dataclass
from pathlib import Path

from cassn.config import BOX_TOKEN_FILE, CONFIG_JSON

try:
    from box_sdk_gen import AccessToken, BoxClient, BoxOAuth, OAuthConfig

    BOX_AVAILABLE = True
except ImportError:
    BOX_AVAILABLE = False
    print("Warning: box-sdk-gen not available. Install with: pip install box-sdk-gen")


@dataclass(frozen=True)
class BoxConfig:
    """Box credentials and target folder ids, loaded from config.json.

    All fields default to ``None`` so a missing config file produces a
    well-formed but empty config rather than raising on attribute access
    downstream.
    """

    client_id: str | None = None
    client_secret: str | None = None
    field_data_folder_id: str | None = None
    app_config_folder_id: str | None = None

    @property
    def is_complete(self) -> bool:
        """True when the three credentials required to reach Box are present."""
        return bool(self.client_id and self.client_secret and self.field_data_folder_id)


class SimpleTokenStorage:
    """Token storage backed by a JSON file the Box SDK reads and rewrites.

    The SDK calls :meth:`store` automatically whenever it refreshes the access
    token, so the on-disk file always holds the latest credentials. The JSON
    shape (``access_token`` + ``refresh_token``) matches the original app's
    ``box_tokens.json`` so existing saved tokens keep working.
    """

    def __init__(self, token_file: Path):
        self.token_file = token_file

    def store(self, token) -> None:
        with open(self.token_file, "w") as f:
            json.dump(
                {"access_token": token.access_token, "refresh_token": token.refresh_token},
                f,
                indent=2,
            )

    def get(self):
        try:
            with open(self.token_file, "r") as f:
                data = json.load(f)
            return AccessToken(
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token"),
            )
        except Exception:
            return None

    def clear(self) -> None:
        if self.token_file.exists():
            self.token_file.unlink()


def load_box_config(config_path: Path = CONFIG_JSON) -> BoxConfig:
    """Load Box credentials from config.json.

    Raises ``FileNotFoundError`` when config.json is absent, with the same
    guidance message as the original startup path.
    """
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}\n"
            "Please copy config.json.example to config.json and add your Box credentials."
        )
    with open(config_path, "r") as f:
        config = json.load(f)
    box = config["box"]
    return BoxConfig(
        client_id=box["client_id"],
        client_secret=box["client_secret"],
        field_data_folder_id=box["field_data_folder_id"],
        app_config_folder_id=box.get("app_config_folder_id"),
    )


def get_box_client(box_config: BoxConfig, *, token_file: Path = BOX_TOKEN_FILE):
    """Return an authenticated :class:`BoxClient`, or ``None``.

    Returns ``None`` when no saved token file exists. Any failure constructing
    the OAuth client (including ``box_sdk_gen`` being unavailable) is caught,
    logged with a traceback, and reported as ``None`` — exactly the original's
    "not connected to Box" behavior. The SDK auto-refreshes the access token
    via :class:`SimpleTokenStorage`.
    """
    if not token_file.exists():
        return None
    try:
        config = OAuthConfig(
            client_id=box_config.client_id,
            client_secret=box_config.client_secret,
            token_storage=SimpleTokenStorage(token_file),
        )
        return BoxClient(BoxOAuth(config))
    except Exception as e:
        print(f"Box authentication error: {e}")
        traceback.print_exc()
        return None
