"""Box configuration and OAuth storage shared by the GUI and maintenance tools.

Non-secret settings live in config.json. New connections store secrets in the
OS credential store; legacy JSON credentials remain readable until sign-in
migrates them. Importing this module does not access the credential store.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from cassn.config import BOX_TOKEN_FILE, CONFIG_JSON
from cassn.profile import HOSTED, PROFILE, PROJECTS, HOSTED_CLIENT_ID
from cassn.box.secrets import KeychainTokenStorage, read

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

    provider: str = "local"
    service_url: str | None = None
    profile: str = "legacy"

    @property
    def is_complete(self) -> bool:
        """True when the three credentials required to reach Box are present."""
        if self.provider == "hosted":
            project = PROJECTS.get(self.profile)
            return bool(project and self.service_url == project["origin"]
                        and self.client_id == HOSTED_CLIENT_ID and self.client_secret is None
                        and self.field_data_folder_id == project["data"]
                        and self.app_config_folder_id == project["config"])
        return bool(self.client_id and self.client_secret and self.field_data_folder_id)


def hosted_config(profile: str) -> BoxConfig:
    project = PROJECTS[profile]
    return BoxConfig(HOSTED_CLIENT_ID, None, project["data"], project["config"],
                     provider="hosted", service_url=project["origin"], profile=profile)


def csu_config() -> BoxConfig:
    return hosted_config("csu")


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
    if HOSTED:
        return hosted_config(PROFILE)
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}\n"
            "Please copy config.json.example to config.json and add your Box credentials."
        )
    with open(config_path, "r") as f:
        config = json.load(f)
    box = config.get("box", {})
    secret = box.get("client_secret")
    if not secret and box.get("client_id"):
        try:
            secret = read(f"{box['client_id']}:client_secret")
        except Exception:
            # Keep ordinary settings available for validated offline operation
            # when the system store is locked or temporarily unavailable.
            secret = None
    return BoxConfig(
        client_id=box.get("client_id"),
        client_secret=secret,
        field_data_folder_id=box.get("field_data_folder_id"),
        app_config_folder_id=box.get("app_config_folder_id"),
    )


def get_box_client(box_config: BoxConfig, *, token_file: Path = BOX_TOKEN_FILE):
    """Return an authenticated :class:`BoxClient`, or ``None``.

    Returns ``None`` when no saved tokens exist. Any failure constructing
    the OAuth client (including ``box_sdk_gen`` being unavailable) is caught,
    reported as ``None``. The SDK refreshes tokens through the selected storage;
    legacy files remain in use until the connection dialog migrates them.
    """
    if not BOX_AVAILABLE or not box_config.is_complete:
        return None
    if box_config.provider == "hosted":
        from cassn.box.hosted import HostedAuthentication, manager_for
        return BoxClient(HostedAuthentication(manager_for(box_config)))
    try:
        storage = SimpleTokenStorage(token_file)
        if token_file == BOX_TOKEN_FILE and not token_file.exists():
            storage = KeychainTokenStorage(box_config.client_id)
        if storage.get() is None:
            return None
        config = OAuthConfig(
            client_id=box_config.client_id,
            client_secret=box_config.client_secret,
            token_storage=storage,
        )
        return BoxClient(BoxOAuth(config))
    except Exception:
        print("Box connection unavailable. Use Connect to Box to reconnect.")
        return None
