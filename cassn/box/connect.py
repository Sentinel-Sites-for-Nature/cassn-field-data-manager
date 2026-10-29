"""Browser OAuth with a state-checked loopback callback and secure persistence."""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from cassn.box.auth import BoxConfig
from cassn.box.secrets import KeychainTokenStorage, write
from cassn.config import CONFIG_JSON, BOX_TOKEN_FILE

REDIRECT_URI = "http://127.0.0.1:8765/callback"


def save_connection(config, token, config_path=CONFIG_JSON, token_file=BOX_TOKEN_FILE):
    """Verify secure writes before atomically removing legacy plaintext secrets."""
    write(f"{config.client_id}:client_secret", config.client_secret)
    KeychainTokenStorage(config.client_id).store(token)
    data = json.loads(config_path.read_text()) if config_path.exists() else {}
    data.setdefault("box", {}).update({
        "client_id": config.client_id,
        "field_data_folder_id": config.field_data_folder_id,
        "app_config_folder_id": config.app_config_folder_id,
    })
    data["box"].pop("client_secret", None)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=config_path.parent, prefix=".box-config-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
        os.replace(name, config_path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    token_file.unlink(missing_ok=True)


def browser_login(config: BoxConfig, open_browser, cancelled, *, timeout=180):
    """Open Box only after binding the callback; return a validated login token."""
    from box_sdk_gen import BoxClient, BoxOAuth, OAuthConfig
    from box_sdk_gen.box.oauth import GetAuthorizeUrlOptions

    state = secrets.token_urlsafe(32)
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Callback URLs contain authorization codes.

        def do_GET(self):
            request = urlsplit(self.path)
            query = parse_qs(request.query)
            valid = (request.path == "/callback" and
                     len(query.get("state", [])) == 1 and
                     secrets.compare_digest(query["state"][0], state))
            if not valid:
                self.send_error(400, "Invalid sign-in callback")
                return
            if query.get("error"):
                result["error"] = "Box sign-in was declined. Try connecting again."
            elif len(query.get("code", [])) == 1:
                result["code"] = query["code"][0]
            else:
                self.send_error(400, "Missing authorization code")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"Sign-in received. Return to Field Data Manager to finish connecting.")

    with HTTPServer(("127.0.0.1", 8765), Callback) as server:
        server.timeout = 0.2
        # Limit a partial request so cancellation and timeout remain responsive.
        original_get_request = server.get_request
        def get_request():
            connection, address = original_get_request()
            connection.settimeout(1)
            return connection, address
        server.get_request = get_request
        auth = BoxOAuth(OAuthConfig(client_id=config.client_id, client_secret=config.client_secret))
        url = auth.get_authorize_url(options=GetAuthorizeUrlOptions(
            redirect_uri=REDIRECT_URI, state=state,
        ))
        open_browser(url)
        deadline = time.monotonic() + timeout
        while not result:
            if cancelled():
                raise RuntimeError("Box sign-in cancelled.")
            if time.monotonic() >= deadline:
                raise RuntimeError("Box sign-in timed out. Click Connect to Box to try again.")
            server.handle_request()
    if "error" in result:
        raise RuntimeError(result["error"])
    if cancelled():
        raise RuntimeError("Box sign-in cancelled.")
    token = auth.get_tokens_authorization_code_grant(result["code"])
    # Validate the OAuth client without writing tokens until the caller commits.
    from box_sdk_gen import BoxDeveloperTokenAuth
    user = BoxClient(BoxDeveloperTokenAuth(token=token.access_token)).users.get_user_me()
    if cancelled():
        raise RuntimeError("Box sign-in cancelled.")
    return token, user.name
