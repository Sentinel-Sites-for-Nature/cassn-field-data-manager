#!/usr/bin/env python3
"""Authenticate to ArcGIS Online for Survey123 lookup synchronization.

This utility is deliberately standalone and does not run when the field data
manager starts. Snapshot and transform operations are non-destructive; the
explicit ``refresh`` command installs only the local Survey123-managed lookup
pair after validation, backup, atomic replacement, and hash verification. It
never modifies ArcGIS records or Box-hosted lookup tables.

The OAuth implementation uses authorization-code flow with PKCE.  A client
secret is neither read nor sent: this is a native/public client, so the
per-login PKCE verifier proves possession when the authorization code is
exchanged for tokens.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import re
import secrets
import shutil
import ssl
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping

try:
    from utils.survey123_legacy_transform import TransformError, transform_legacy_snapshot
except ModuleNotFoundError:  # Direct execution: python utils/sync_survey123_lookups.py
    from survey123_legacy_transform import TransformError, transform_legacy_snapshot

try:
    from cassn.lookups import (
        LookupSchemaError,
        build_deployment_rounds,
        load_device_deployments,
        load_devices,
    )
except ModuleNotFoundError:  # Direct execution from outside the repository root.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from cassn.lookups import (  # type: ignore[no-redef]
        LookupSchemaError,
        build_deployment_rounds,
        load_device_deployments,
        load_devices,
    )


DEFAULT_CONFIG_PATH = Path.home() / ".cassn_config" / "survey123" / "sources.json"
DEFAULT_SNAPSHOT_ROOT = Path(__file__).resolve().parents[1] / "local_data" / "survey123_sync"
DEFAULT_LOOKUP_DIR = Path.home() / ".cassn_config" / "lookup_tables"
DEFAULT_CANDIDATE_ROOT = DEFAULT_SNAPSHOT_ROOT / "candidates"
REFRESH_FILENAMES = ("devices.csv", "deployments.csv")
REFRESH_LOCK_FILENAME = ".survey123_refresh.lock"
REFRESH_RECEIPT_FILENAME = ".survey123_last_refresh.json"
TOKEN_FILENAME = "tokens.json"
DEFAULT_CALLBACK_TIMEOUT_SECONDS = 300
DEFAULT_REFRESH_TOKEN_EXPIRATION_MINUTES = 20_160  # 14 days
TOKEN_EXPIRY_SKEW_SECONDS = 60
COMMON_CA_BUNDLE_PATHS = (
    Path("/etc/ssl/cert.pem"),
    Path("/etc/ssl/certs/ca-certificates.crt"),
    Path("/etc/pki/tls/certs/ca-bundle.crt"),
)
SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "authorization_code",
        "client_secret",
        "code",
        "code_verifier",
        "refresh_token",
        "token",
    }
)


class SurveySyncError(RuntimeError):
    """Base class for expected, user-facing utility errors."""


class ConfigError(SurveySyncError):
    """Raised when the private Survey123 configuration is invalid."""


class AuthenticationError(SurveySyncError):
    """Raised when OAuth authentication cannot be completed safely."""


class ArcGISError(SurveySyncError):
    """Raised when an ArcGIS REST operation fails."""


class SnapshotError(SurveySyncError):
    """Raised when a read-only source snapshot cannot be completed safely."""


class RefreshError(SurveySyncError):
    """Raised when candidate validation or live installation cannot finish safely."""


@dataclass(frozen=True)
class OAuthSettings:
    client_id: str
    redirect_uri: str
    credential_item_id: str | None = None


@dataclass(frozen=True)
class SurveySource:
    role: str
    label: str
    item_id: str
    enabled: bool = True


@dataclass(frozen=True)
class SurveySyncConfig:
    portal_url: str
    oauth: OAuthSettings
    surveys: tuple[SurveySource, ...]
    config_path: Path

    @property
    def token_path(self) -> Path:
        return self.config_path.parent / TOKEN_FILENAME

    @property
    def authorize_url(self) -> str:
        return f"{self.portal_url}/sharing/rest/oauth2/authorize/"

    @property
    def token_url(self) -> str:
        return f"{self.portal_url}/sharing/rest/oauth2/token/"


@dataclass(frozen=True)
class ResolvedSurveyService:
    survey: SurveySource
    form_item: dict[str, Any]
    service_item: dict[str, Any]
    service_url: str
    service_schema: dict[str, Any]


def _required_string(data: Mapping[str, Any], key: str, context: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context}.{key} must be a non-empty string")
    return value.strip()


def _validate_redirect_uri(redirect_uri: str) -> None:
    parsed = urllib.parse.urlparse(redirect_uri)
    if parsed.scheme != "http":
        raise ConfigError("oauth.redirect_uri must use http for the localhost callback")
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ConfigError("oauth.redirect_uri must use host 127.0.0.1 or localhost")
    if parsed.port is None:
        raise ConfigError("oauth.redirect_uri must include a callback port")
    if not parsed.path or parsed.path == "/":
        raise ConfigError("oauth.redirect_uri must include a callback path")
    if parsed.params or parsed.query or parsed.fragment:
        raise ConfigError("oauth.redirect_uri cannot contain params, a query, or a fragment")


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> SurveySyncConfig:
    """Load and validate the private Survey123 source/OAuth configuration."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Survey123 configuration not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in Survey123 configuration: {path}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("Survey123 configuration must be a JSON object")

    portal_url = _required_string(raw, "portal_url", "configuration").rstrip("/")
    portal = urllib.parse.urlparse(portal_url)
    if portal.scheme != "https" or not portal.netloc or portal.path not in {"", "/"}:
        raise ConfigError("portal_url must be an HTTPS portal origin without a path")

    oauth_raw = raw.get("oauth")
    if not isinstance(oauth_raw, dict):
        raise ConfigError("configuration.oauth must be an object")
    if "client_secret" in oauth_raw:
        raise ConfigError(
            "oauth.client_secret must not be configured for the PKCE public-client flow"
        )

    client_id = _required_string(oauth_raw, "client_id", "oauth")
    redirect_uri = _required_string(oauth_raw, "redirect_uri", "oauth")
    _validate_redirect_uri(redirect_uri)
    credential_item_id_value = oauth_raw.get("credential_item_id")
    credential_item_id = None
    if credential_item_id_value is not None:
        if not isinstance(credential_item_id_value, str) or not credential_item_id_value.strip():
            raise ConfigError("oauth.credential_item_id must be a non-empty string")
        credential_item_id = credential_item_id_value.strip()

    surveys_raw = raw.get("surveys")
    if not isinstance(surveys_raw, list) or not surveys_raw:
        raise ConfigError("configuration.surveys must be a non-empty array")

    surveys: list[SurveySource] = []
    seen_roles: set[str] = set()
    for index, survey_raw in enumerate(surveys_raw):
        context = f"surveys[{index}]"
        if not isinstance(survey_raw, dict):
            raise ConfigError(f"{context} must be an object")
        role = _required_string(survey_raw, "role", context)
        if role in seen_roles:
            raise ConfigError(f"Duplicate survey role: {role}")
        seen_roles.add(role)
        enabled = survey_raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(f"{context}.enabled must be true or false")
        surveys.append(
            SurveySource(
                role=role,
                label=_required_string(survey_raw, "label", context),
                item_id=_required_string(survey_raw, "item_id", context),
                enabled=enabled,
            )
        )

    if not any(survey.enabled for survey in surveys):
        raise ConfigError("At least one Survey123 source must be enabled")

    return SurveySyncConfig(
        portal_url=portal_url,
        oauth=OAuthSettings(
            client_id=client_id,
            redirect_uri=redirect_uri,
            credential_item_id=credential_item_id,
        ),
        surveys=tuple(surveys),
        config_path=path,
    )


def generate_code_verifier() -> str:
    """Return an RFC 7636-compatible, high-entropy PKCE code verifier."""

    return secrets.token_urlsafe(64)


def pkce_challenge(code_verifier: str) -> str:
    """Return the unpadded base64url SHA-256 challenge for a verifier."""

    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_authorization_url(
    config: SurveySyncConfig,
    *,
    state_value: str,
    code_challenge: str,
) -> str:
    params = {
        "client_id": config.oauth.client_id,
        "response_type": "code",
        "redirect_uri": config.oauth.redirect_uri,
        "state": state_value,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "expiration": str(DEFAULT_REFRESH_TOKEN_EXPIRATION_MINUTES),
    }
    return f"{config.authorize_url}?{urllib.parse.urlencode(params)}"


def parse_callback_url(
    callback_url: str,
    *,
    expected_path: str,
    expected_state: str,
) -> str:
    """Validate an OAuth callback URL and return its authorization code."""

    parsed = urllib.parse.urlparse(callback_url)
    if parsed.path != expected_path:
        raise AuthenticationError("OAuth callback path did not match the configured path")

    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    returned_state = query.get("state", [None])[0]
    if not returned_state or not secrets.compare_digest(returned_state, expected_state):
        raise AuthenticationError("OAuth callback state validation failed")

    if "error" in query:
        error = query.get("error_description", query["error"])[0]
        raise AuthenticationError(f"ArcGIS authorization failed: {error}")

    code = query.get("code", [None])[0]
    if not code:
        raise AuthenticationError("OAuth callback did not contain an authorization code")
    return code


class _CallbackHandler(BaseHTTPRequestHandler):
    expected_path = "/"
    expected_state = ""
    result: dict[str, str | SurveySyncError] = {}

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != self.expected_path:
            self.send_response(404)
            self.end_headers()
            return

        try:
            code = parse_callback_url(
                self.path,
                expected_path=self.expected_path,
                expected_state=self.expected_state,
            )
            self.result["code"] = code
            self._write_response(
                200,
                "Authentication received",
                "You can close this browser tab and return to the terminal.",
            )
        except SurveySyncError as exc:
            self.result["error"] = exc
            self._write_response(
                400,
                "Authentication failed",
                "Return to the terminal for details. No credentials were saved.",
            )

    def _write_response(self, status_code: int, title: str, message: str) -> None:
        body = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{title}</title></head><body><h1>{title}</h1>"
            f"<p>{message}</p></body></html>"
        ).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def wait_for_callback(
    redirect_uri: str,
    *,
    expected_state: str,
    timeout_seconds: int,
    on_listening: Callable[[], None] | None = None,
) -> str:
    parsed = urllib.parse.urlparse(redirect_uri)
    handler = type(
        "OAuthCallbackHandler",
        (_CallbackHandler,),
        {
            "expected_path": parsed.path,
            "expected_state": expected_state,
            "result": {},
        },
    )

    try:
        server = HTTPServer((parsed.hostname or "127.0.0.1", parsed.port or 0), handler)
    except OSError as exc:
        raise AuthenticationError(
            f"Could not listen for the OAuth callback at {parsed.hostname}:{parsed.port}: {exc}"
        ) from exc

    if on_listening is not None:
        try:
            on_listening()
        except Exception:
            server.server_close()
            raise

    deadline = time.monotonic() + timeout_seconds
    try:
        while time.monotonic() < deadline and not handler.result:
            server.timeout = min(1.0, max(0.0, deadline - time.monotonic()))
            server.handle_request()
    finally:
        server.server_close()

    if not handler.result:
        raise AuthenticationError("Timed out waiting for the ArcGIS OAuth callback")
    error = handler.result.get("error")
    if isinstance(error, SurveySyncError):
        raise error
    code = handler.result.get("code")
    if not isinstance(code, str):
        raise AuthenticationError("OAuth callback completed without an authorization code")
    return code


def redact_auth_data(value: Any) -> Any:
    """Return a copy with OAuth/token values replaced by a fixed marker."""

    if isinstance(value, Mapping):
        return {
            key: "<redacted>" if str(key).lower() in SENSITIVE_KEYS else redact_auth_data(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_auth_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_auth_data(item) for item in value)
    return value


def create_verified_ssl_context() -> ssl.SSLContext:
    """Create a verified TLS context, with a macOS framework-Python fallback."""

    context = ssl.create_default_context()
    if context.get_ca_certs():
        return context

    default_paths = ssl.get_default_verify_paths()
    candidates = []
    if default_paths.cafile:
        candidates.append(Path(default_paths.cafile))
    candidates.extend(COMMON_CA_BUNDLE_PATHS)
    for candidate in candidates:
        if candidate.is_file():
            context.load_verify_locations(cafile=str(candidate))
            if context.get_ca_certs():
                return context

    raise ArcGISError(
        "No trusted CA certificate bundle is available; TLS verification cannot continue"
    )


def request_json(
    url: str,
    data: Mapping[str, Any],
    *,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=encoded,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "CA-SSN-Survey123-Lookup-Sync/1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout_seconds,
            context=create_verified_ssl_context(),
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ArcGISError(f"ArcGIS request failed with HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ArcGISError(f"Could not connect to ArcGIS: {exc.reason}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArcGISError("ArcGIS returned a response that was not valid JSON") from exc

    if not isinstance(payload, dict):
        raise ArcGISError("ArcGIS returned an unexpected response shape")
    if "error" in payload:
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message", "Unknown ArcGIS error")
            details = error.get("details")
            detail_text = ""
            if isinstance(details, list) and details:
                detail_text = f" ({'; '.join(str(item) for item in details)})"
            raise ArcGISError(f"ArcGIS error: {message}{detail_text}")
        raise ArcGISError("ArcGIS returned an authentication error")
    return payload


def exchange_authorization_code(
    config: SurveySyncConfig,
    *,
    authorization_code: str,
    code_verifier: str,
    post_json: Callable[[str, Mapping[str, Any]], dict[str, Any]] = request_json,
) -> dict[str, Any]:
    payload = post_json(
        config.token_url,
        {
            "f": "json",
            "grant_type": "authorization_code",
            "client_id": config.oauth.client_id,
            "redirect_uri": config.oauth.redirect_uri,
            "code": authorization_code,
            "code_verifier": code_verifier,
        },
    )
    if not payload.get("access_token"):
        raise AuthenticationError("ArcGIS token response did not contain an access token")
    if not payload.get("refresh_token"):
        raise AuthenticationError("ArcGIS token response did not contain a refresh token")
    return payload


def _token_record_from_response(
    response: Mapping[str, Any],
    *,
    previous_refresh_token: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    now_value = time.time() if now is None else now
    access_token = response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise AuthenticationError("ArcGIS token response did not contain an access token")

    refresh_token = response.get("refresh_token", previous_refresh_token)
    if not isinstance(refresh_token, str) or not refresh_token:
        raise AuthenticationError("No renewable ArcGIS refresh token is available")

    try:
        expires_in = int(response.get("expires_in", 0))
    except (TypeError, ValueError) as exc:
        raise AuthenticationError("ArcGIS returned an invalid access-token lifetime") from exc
    if expires_in <= 0:
        raise AuthenticationError("ArcGIS did not return a positive access-token lifetime")

    record: dict[str, Any] = {
        "schema_version": 1,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": now_value + expires_in,
        "updated_at": datetime.fromtimestamp(now_value, timezone.utc).isoformat(),
    }
    username = response.get("username")
    if isinstance(username, str) and username:
        record["username"] = username
    refresh_expires_in = response.get("refresh_token_expires_in")
    if refresh_expires_in is not None:
        try:
            record["refresh_expires_at"] = now_value + int(refresh_expires_in)
        except (TypeError, ValueError):
            pass
    return record


def ensure_private_directory(path: Path) -> None:
    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        if os.name != "nt":
            os.chmod(directory, 0o700)
    if os.name != "nt":
        os.chmod(path, 0o700)


def save_token_record(path: Path, record: Mapping[str, Any]) -> None:
    """Atomically store renewable OAuth data with owner-only permissions."""

    ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".tokens-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        if os.name != "nt":
            os.chmod(temporary_path, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(record), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def load_token_record(path: Path) -> dict[str, Any]:
    try:
        if os.name != "nt":
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o077:
                raise AuthenticationError(
                    f"OAuth token file permissions are too broad ({mode:04o}); expected 0600"
                )
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AuthenticationError("Not authenticated; run the auth command first") from exc
    except json.JSONDecodeError as exc:
        raise AuthenticationError("OAuth token file contains invalid JSON") from exc
    if not isinstance(payload, dict):
        raise AuthenticationError("OAuth token file has an invalid structure")
    return payload


def refresh_access_token(
    config: SurveySyncConfig,
    token_record: Mapping[str, Any],
    *,
    post_json: Callable[[str, Mapping[str, Any]], dict[str, Any]] = request_json,
    now: float | None = None,
) -> dict[str, Any]:
    refresh_token = token_record.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise AuthenticationError("Stored OAuth data does not contain a refresh token")

    response = post_json(
        config.token_url,
        {
            "f": "json",
            "grant_type": "refresh_token",
            "client_id": config.oauth.client_id,
            "refresh_token": refresh_token,
        },
    )
    return _token_record_from_response(
        response,
        previous_refresh_token=refresh_token,
        now=now,
    )


def usable_token_record(
    config: SurveySyncConfig,
    *,
    now: float | None = None,
) -> tuple[dict[str, Any], bool]:
    now_value = time.time() if now is None else now
    record = load_token_record(config.token_path)
    access_token = record.get("access_token")
    expires_at = record.get("expires_at", 0)
    try:
        still_valid = (
            isinstance(access_token, str)
            and bool(access_token)
            and float(expires_at) > now_value + TOKEN_EXPIRY_SKEW_SECONDS
        )
    except (TypeError, ValueError):
        still_valid = False
    if still_valid:
        return record, False

    refreshed = refresh_access_token(config, record, now=now_value)
    save_token_record(config.token_path, refreshed)
    return refreshed, True


def arcgis_post(
    config: SurveySyncConfig,
    path: str,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    url = f"{config.portal_url}/sharing/rest/{path.lstrip('/')}"
    return request_json(url, {"f": "json", **params})


def authenticated_user(config: SurveySyncConfig, access_token: str) -> dict[str, Any]:
    user = arcgis_post(config, "community/self", {"token": access_token})
    username = user.get("username")
    if not isinstance(username, str) or not username:
        raise ArcGISError("ArcGIS did not return an authenticated username")
    return user


def verify_survey_access(
    config: SurveySyncConfig,
    access_token: str,
) -> list[tuple[SurveySource, dict[str, Any]]]:
    results: list[tuple[SurveySource, dict[str, Any]]] = []
    for survey in config.surveys:
        if not survey.enabled:
            continue
        item = arcgis_post(
            config,
            f"content/items/{survey.item_id}",
            {"token": access_token},
        )
        returned_id = item.get("id")
        if returned_id != survey.item_id:
            raise ArcGISError(f"Could not verify configured survey: {survey.label}")
        results.append((survey, item))
    return results


def validate_feature_service_url(url: str) -> str:
    """Validate an ArcGIS Online FeatureServer URL before sending it a token."""

    parsed = urllib.parse.urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not hostname.endswith(".arcgis.com"):
        raise SnapshotError("Related feature service URL is not hosted on ArcGIS Online")
    if parsed.port not in {None, 443}:
        raise SnapshotError("Related feature service URL uses an unexpected port")
    if parsed.params or parsed.query or parsed.fragment:
        raise SnapshotError("Related feature service URL cannot contain extra URL components")
    canonical = url.rstrip("/")
    if not canonical.endswith("/FeatureServer"):
        raise SnapshotError("Related item URL is not a FeatureServer endpoint")
    return canonical


def select_related_service(
    survey: SurveySource,
    form_item: Mapping[str, Any],
    related_payload: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    """Select the unique feature service related to a Survey123 Form item."""

    if form_item.get("id") != survey.item_id or form_item.get("type") != "Form":
        raise SnapshotError(f"Configured source is not the expected Form item: {survey.label}")
    related = related_payload.get("relatedItems")
    if not isinstance(related, list):
        raise SnapshotError(f"ArcGIS returned invalid related-item data for: {survey.label}")
    candidates = [
        item
        for item in related
        if isinstance(item, dict)
        and item.get("type") == "Feature Service"
        and isinstance(item.get("url"), str)
    ]
    if len(candidates) != 1:
        raise SnapshotError(
            f"Expected one Survey2Service feature service for {survey.label}; "
            f"found {len(candidates)}"
        )
    service_item = dict(candidates[0])
    return service_item, validate_feature_service_url(str(service_item["url"]))


def resolve_survey_service(
    config: SurveySyncConfig,
    survey: SurveySource,
    access_token: str,
) -> ResolvedSurveyService:
    """Resolve a configured Form to its Survey2Service feature service."""

    form_item = arcgis_post(
        config,
        f"content/items/{survey.item_id}",
        {"token": access_token},
    )
    related = arcgis_post(
        config,
        f"content/items/{survey.item_id}/relatedItems",
        {
            "relationshipType": "Survey2Service",
            "direction": "forward",
            "token": access_token,
        },
    )
    service_item, service_url = select_related_service(survey, form_item, related)
    service_schema = request_json(service_url, {"f": "json", "token": access_token})
    if not isinstance(service_schema.get("layers", []), list) or not isinstance(
        service_schema.get("tables", []), list
    ):
        raise SnapshotError(f"Feature service schema is invalid for: {survey.label}")
    return ResolvedSurveyService(
        survey=survey,
        form_item=dict(form_item),
        service_item=service_item,
        service_url=service_url,
        service_schema=service_schema,
    )


def resolve_all_survey_services(
    config: SurveySyncConfig,
    access_token: str,
) -> list[ResolvedSurveyService]:
    return [
        resolve_survey_service(config, survey, access_token)
        for survey in config.surveys
        if survey.enabled
    ]


def _private_json_write(path: Path, payload: Mapping[str, Any]) -> None:
    """Write JSON atomically with owner-only permissions."""

    ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        if os.name != "nt":
            os.chmod(temporary_path, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def fetch_dataset_pages(
    dataset_url: str,
    schema: Mapping[str, Any],
    access_token: str,
    *,
    page_size: int,
    post_json: Callable[[str, Mapping[str, Any]], dict[str, Any]] = request_json,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fetch all dataset records in verified object-ID chunks."""

    if page_size <= 0:
        raise SnapshotError("Snapshot page size must be positive")
    object_id_field = schema.get("objectIdField") or schema.get("objectIdFieldName")
    if not isinstance(object_id_field, str) or not object_id_field:
        raise SnapshotError(f"Dataset does not declare an object ID field: {dataset_url}")

    id_payload = post_json(
        f"{dataset_url}/query",
        {
            "f": "json",
            "token": access_token,
            "where": "1=1",
            "returnIdsOnly": "true",
        },
    )
    object_ids = id_payload.get("objectIds")
    if not isinstance(object_ids, list):
        raise SnapshotError(f"Dataset object-ID query was invalid: {dataset_url}")
    expected = [str(value) for value in object_ids]
    if len(set(expected)) != len(expected):
        raise SnapshotError(f"Dataset object-ID query returned duplicates: {dataset_url}")

    pages: list[dict[str, Any]] = []
    returned: list[str] = []
    geometry = str(schema.get("type", "")).lower() != "table"
    for start in range(0, len(object_ids), page_size):
        chunk = object_ids[start : start + page_size]
        page = post_json(
            f"{dataset_url}/query",
            {
                "f": "json",
                "token": access_token,
                "objectIds": ",".join(str(value) for value in chunk),
                "outFields": "*",
                "returnGeometry": "true" if geometry else "false",
            },
        )
        features = page.get("features")
        if not isinstance(features, list):
            raise SnapshotError(f"Dataset feature query was invalid: {dataset_url}")
        for feature in features:
            if not isinstance(feature, dict) or not isinstance(feature.get("attributes"), dict):
                raise SnapshotError(f"Dataset returned an invalid feature: {dataset_url}")
            value = feature["attributes"].get(object_id_field)
            if value is None:
                raise SnapshotError(
                    f"Dataset feature omitted object ID field {object_id_field}: {dataset_url}"
                )
            returned.append(str(value))
        pages.append(page)

    if len(returned) != len(set(returned)):
        raise SnapshotError(f"Dataset feature pages contained duplicate records: {dataset_url}")
    if set(returned) != set(expected):
        missing = len(set(expected) - set(returned))
        unexpected = len(set(returned) - set(expected))
        raise SnapshotError(
            f"Dataset changed or returned incomplete pages ({missing} missing, "
            f"{unexpected} unexpected): {dataset_url}"
        )
    return id_payload, pages


def _safe_snapshot_name(value: str, description: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value):
        raise SnapshotError(f"Unsafe {description}: {value!r}")
    return value


def create_snapshot(
    config: SurveySyncConfig,
    access_token: str,
    *,
    output_root: Path = DEFAULT_SNAPSHOT_ROOT,
    page_size: int = 500,
    snapshot_id: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Create an atomic, read-only raw snapshot of all configured surveys."""

    identifier = snapshot_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    _safe_snapshot_name(identifier, "snapshot ID")
    if page_size <= 0:
        raise SnapshotError("Snapshot page size must be positive")
    output_root = output_root.resolve()
    ensure_private_directory(output_root)
    final_path = output_root / identifier
    if final_path.exists():
        raise SnapshotError(f"Snapshot already exists: {final_path}")

    temporary_path = Path(tempfile.mkdtemp(prefix=f".{identifier}-", dir=output_root))
    if os.name != "nt":
        os.chmod(temporary_path, 0o700)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "snapshot_id": identifier,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "portal_url": config.portal_url,
        "sources": [],
    }
    try:
        user = authenticated_user(config, access_token)
        manifest["username"] = user["username"]
        for resolved in resolve_all_survey_services(config, access_token):
            role = _safe_snapshot_name(resolved.survey.role, "survey role")
            role_path = temporary_path / role
            ensure_private_directory(role_path)
            _private_json_write(role_path / "form_item.json", resolved.form_item)
            _private_json_write(role_path / "service_item.json", resolved.service_item)
            _private_json_write(role_path / "service.json", resolved.service_schema)

            source_manifest: dict[str, Any] = {
                "role": role,
                "label": resolved.survey.label,
                "form_item_id": resolved.survey.item_id,
                "service_item_id": resolved.service_item.get("id"),
                "service_url": resolved.service_url,
                "datasets": [],
            }
            for collection_name in ("layers", "tables"):
                datasets = resolved.service_schema.get(collection_name, [])
                for dataset_summary in datasets:
                    if not isinstance(dataset_summary, dict) or not isinstance(
                        dataset_summary.get("id"), int
                    ):
                        raise SnapshotError(
                            f"Feature service contains an invalid {collection_name} entry: "
                            f"{resolved.survey.label}"
                        )
                    dataset_id = dataset_summary["id"]
                    dataset_url = f"{resolved.service_url}/{dataset_id}"
                    schema = request_json(
                        dataset_url,
                        {"f": "json", "token": access_token},
                    )
                    dataset_path = role_path / collection_name / str(dataset_id)
                    _private_json_write(dataset_path / "schema.json", schema)
                    max_records = schema.get("maxRecordCount", page_size)
                    try:
                        effective_page_size = min(page_size, max(1, int(max_records)))
                    except (TypeError, ValueError):
                        effective_page_size = page_size
                    id_payload, pages = fetch_dataset_pages(
                        dataset_url,
                        schema,
                        access_token,
                        page_size=effective_page_size,
                    )
                    _private_json_write(dataset_path / "object_ids.json", id_payload)
                    page_files: list[str] = []
                    for page_index, page in enumerate(pages, start=1):
                        relative_page = Path(collection_name) / str(dataset_id) / "pages" / (
                            f"page-{page_index:04d}.json"
                        )
                        _private_json_write(role_path / relative_page, page)
                        page_files.append(str(relative_page))
                    source_manifest["datasets"].append(
                        {
                            "collection": collection_name,
                            "id": dataset_id,
                            "name": schema.get("name", dataset_summary.get("name")),
                            "object_id_field": schema.get("objectIdField")
                            or schema.get("objectIdFieldName"),
                            "record_count": len(id_payload.get("objectIds", [])),
                            "page_count": len(pages),
                            "page_files": page_files,
                        }
                    )
            manifest["sources"].append(source_manifest)

        _private_json_write(temporary_path / "manifest.json", manifest)
        os.replace(temporary_path, final_path)
        if os.name != "nt":
            os.chmod(final_path, 0o700)
        return final_path, manifest
    except Exception:
        shutil.rmtree(temporary_path, ignore_errors=True)
        raise


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_csv_dicts(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [
                {key: (value or "").strip() for key, value in row.items()}
                for row in csv.DictReader(handle)
            ]
    except (OSError, csv.Error) as exc:
        raise RefreshError(f"Could not read required CSV: {path}") from exc


def validate_candidate_lookup_set(candidate_path: Path, lookup_dir: Path) -> dict[str, Any]:
    """Validate a transformed candidate before any live lookup is changed."""
    candidate_path = Path(candidate_path)
    if candidate_path.is_symlink():
        raise RefreshError(f"Candidate directory must not be a symlink: {candidate_path}")
    candidate_path = candidate_path.resolve()
    lookup_dir = lookup_dir.resolve()
    if not candidate_path.is_dir():
        raise RefreshError(f"Candidate directory is missing or unsafe: {candidate_path}")

    required = [candidate_path / name for name in (*REFRESH_FILENAMES, "manifest.json", "issues.csv")]
    for path in required:
        if not path.is_file() or path.is_symlink():
            raise RefreshError(f"Candidate file is missing or unsafe: {path}")

    try:
        manifest = json.loads((candidate_path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RefreshError("Candidate manifest is unreadable") from exc

    try:
        devices = load_devices(candidate_path / "devices.csv")
        deployments = load_device_deployments(candidate_path / "deployments.csv")
        events, _rows_by_round = build_deployment_rounds(deployments)
    except LookupSchemaError as exc:
        raise RefreshError(str(exc)) from exc

    device_record_ids = [row.get("device_record_id", "") for row in devices]
    if any(not value for value in device_record_ids):
        raise RefreshError("devices.csv contains a blank device_record_id")
    if len(device_record_ids) != len(set(device_record_ids)):
        raise RefreshError("devices.csv contains duplicate device_record_id values")

    deployment_ids = [row.get("deployment_id", "") for row in deployments]
    if any(not value for value in deployment_ids):
        raise RefreshError("deployments.csv contains a blank deployment_id")
    if len(deployment_ids) != len(set(deployment_ids)):
        raise RefreshError("deployments.csv contains duplicate deployment_id values")

    known_device_records = set(device_record_ids)
    missing_device_records = sorted(
        {
            row.get("device_record_id", "")
            for row in deployments
            if row.get("device_record_id", "") not in known_device_records
        }
    )
    if missing_device_records:
        raise RefreshError(
            f"deployments.csv references {len(missing_device_records)} unknown device record(s)"
        )

    plot_rows = _read_csv_dicts(lookup_dir / "plots.csv")
    valid_plots = {
        (row.get("site_code", ""), row.get("plot_number", ""))
        for row in plot_rows
    }
    unknown_plots = sorted(
        {
            (row.get("site_code", ""), row.get("plot_number", ""))
            for row in deployments
            if (row.get("site_code", ""), row.get("plot_number", "")) not in valid_plots
        }
    )
    if unknown_plots:
        raise RefreshError(
            f"deployments.csv references {len(unknown_plots)} non-authoritative plot(s)"
        )

    expected_counts = manifest.get("counts", {})
    if expected_counts.get("devices") != len(devices):
        raise RefreshError("Candidate device count does not match its manifest")
    if expected_counts.get("deployments") != len(deployments):
        raise RefreshError("Candidate deployment count does not match its manifest")

    issues = _read_csv_dicts(candidate_path / "issues.csv")
    blocking_issues = [row for row in issues if row.get("severity") == "blocking"]
    return {
        "snapshot_id": manifest.get("source_snapshot_id") or candidate_path.name,
        "devices": len(devices),
        "deployments": len(deployments),
        "sites": len(events),
        "rounds": sum(len(site_events) for site_events in events.values()),
        "blocking_issues": len(blocking_issues),
        "warnings": sum(row.get("severity") == "warning" for row in issues),
        "hashes": {
            name: _sha256_file(candidate_path / name) for name in REFRESH_FILENAMES
        },
    }


def _stage_private_copy(source: Path, destination_dir: Path) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{source.name}.refresh-", dir=destination_dir
    )
    temporary_path = Path(temporary_name)
    try:
        if os.name != "nt":
            os.chmod(temporary_path, 0o600)
        with source.open("rb") as src, os.fdopen(descriptor, "wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        if _sha256_file(temporary_path) != _sha256_file(source):
            raise RefreshError(f"Staged copy failed hash verification: {source.name}")
        return temporary_path
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def _sync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def install_candidate_lookup_set(
    candidate_path: Path,
    lookup_dir: Path,
    validation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Back up and atomically install a validated candidate lookup pair.

    Both destination files are staged and hash-checked before replacement. If
    either replacement or final verification fails, the previous pair is
    restored from the just-created backup.
    """
    candidate_path = candidate_path.resolve()
    lookup_dir = lookup_dir.resolve()
    ensure_private_directory(lookup_dir)
    checked = dict(validation or validate_candidate_lookup_set(candidate_path, lookup_dir))
    snapshot_id = _safe_snapshot_name(str(checked["snapshot_id"]), "snapshot ID")

    lock_path = lookup_dir / REFRESH_LOCK_FILENAME
    try:
        lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RefreshError(
            f"Another Survey123 refresh appears to be running: {lock_path}"
        ) from exc

    backup_dir = lookup_dir / "survey123_backups" / snapshot_id
    staged: dict[str, Path] = {}
    previous_exists: dict[str, bool] = {}
    installed = False
    try:
        os.write(lock_descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(lock_descriptor)
        ensure_private_directory(backup_dir.parent)
        if backup_dir.exists():
            raise RefreshError(f"Refresh backup already exists: {backup_dir}")
        ensure_private_directory(backup_dir)

        for name in REFRESH_FILENAMES:
            destination = lookup_dir / name
            previous_exists[name] = destination.is_file()
            if destination.is_symlink():
                raise RefreshError(f"Live lookup target must not be a symlink: {destination}")
            if destination.exists() and not destination.is_file():
                raise RefreshError(f"Live lookup target is not a regular file: {destination}")
            if destination.is_file():
                backup = backup_dir / name
                shutil.copy2(destination, backup)
                if os.name != "nt":
                    os.chmod(backup, 0o600)
                if _sha256_file(backup) != _sha256_file(destination):
                    raise RefreshError(f"Backup failed hash verification: {name}")
            staged[name] = _stage_private_copy(candidate_path / name, lookup_dir)

        def restore_previous_pair() -> None:
            for restore_name in REFRESH_FILENAMES:
                restore_destination = lookup_dir / restore_name
                restore_backup = backup_dir / restore_name
                if previous_exists.get(restore_name) and restore_backup.is_file():
                    rollback = _stage_private_copy(restore_backup, lookup_dir)
                    os.replace(rollback, restore_destination)
                elif not previous_exists.get(restore_name):
                    restore_destination.unlink(missing_ok=True)
            _sync_directory(lookup_dir)

        try:
            for name in REFRESH_FILENAMES:
                os.replace(staged[name], lookup_dir / name)
            _sync_directory(lookup_dir)
            for name, expected_hash in checked["hashes"].items():
                installed_hash = _sha256_file(lookup_dir / name)
                if installed_hash != expected_hash:
                    raise RefreshError(f"Installed lookup failed verification: {name}")
        except Exception as install_exc:
            restore_previous_pair()
            if isinstance(install_exc, RefreshError):
                raise
            raise RefreshError(f"Could not install refreshed lookups: {install_exc}") from install_exc

        receipt = {
            "schema_version": 1,
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "snapshot_id": snapshot_id,
            "candidate_path": str(candidate_path),
            "backup_path": str(backup_dir),
            "counts": {
                key: checked[key]
                for key in ("devices", "deployments", "sites", "rounds", "blocking_issues", "warnings")
            },
            "hashes": dict(checked["hashes"]),
        }
        try:
            _private_json_write(lookup_dir / REFRESH_RECEIPT_FILENAME, receipt)
        except Exception as receipt_exc:
            restore_previous_pair()
            raise RefreshError(
                f"Installed files were rolled back because the refresh receipt "
                f"could not be written: {receipt_exc}"
            ) from receipt_exc
        installed = True
        return receipt
    finally:
        os.close(lock_descriptor)
        lock_path.unlink(missing_ok=True)
        for path in staged.values():
            path.unlink(missing_ok=True)
        if not installed and backup_dir.exists() and not any(backup_dir.iterdir()):
            backup_dir.rmdir()


def _format_expiration(epoch_seconds: Any) -> str:
    try:
        value = float(epoch_seconds)
    except (TypeError, ValueError):
        return "unknown"
    return datetime.fromtimestamp(value, timezone.utc).astimezone().isoformat(timespec="seconds")


def command_auth(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    verifier = generate_code_verifier()
    state_value = secrets.token_urlsafe(32)
    authorization_url = build_authorization_url(
        config,
        state_value=state_value,
        code_challenge=pkce_challenge(verifier),
    )

    print("Opening UC Davis ArcGIS sign-in for Survey123 lookup access.")
    print("No ArcGIS password or client secret is requested or stored.")
    print(f"Authorization URL:\n{authorization_url}\n")

    def open_authorization_page() -> None:
        if args.no_browser:
            return
        try:
            opened = webbrowser.open(authorization_url, new=1, autoraise=True)
        except webbrowser.Error:
            opened = False
        if not opened:
            print("The browser did not open automatically; use the URL above.")

    authorization_code = wait_for_callback(
        config.oauth.redirect_uri,
        expected_state=state_value,
        timeout_seconds=args.timeout,
        on_listening=open_authorization_page,
    )
    token_response = exchange_authorization_code(
        config,
        authorization_code=authorization_code,
        code_verifier=verifier,
    )
    token_record = _token_record_from_response(token_response)
    access_token = str(token_record["access_token"])

    user = authenticated_user(config, access_token)
    surveys = verify_survey_access(config, access_token)
    token_record["username"] = user["username"]
    save_token_record(config.token_path, token_record)

    print(f"Authenticated as: {user['username']}")
    for survey, item in surveys:
        print(f"  OK  {survey.role}: {item.get('title', survey.label)} ({survey.item_id})")
    print(f"Renewable OAuth data saved privately to: {config.token_path}")
    return 0


def command_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    record, refreshed = usable_token_record(config)
    access_token = str(record["access_token"])
    user = authenticated_user(config, access_token)
    surveys = verify_survey_access(config, access_token)

    print(f"Authenticated as: {user['username']}")
    print(f"Access token expires: {_format_expiration(record.get('expires_at'))}")
    if refreshed:
        print("Access token was refreshed successfully.")
    for survey, item in surveys:
        print(f"  OK  {survey.role}: {item.get('title', survey.label)} ({survey.item_id})")
    return 0


def command_discover(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    record, refreshed = usable_token_record(config)
    access_token = str(record["access_token"])
    user = authenticated_user(config, access_token)
    resolved_sources = resolve_all_survey_services(config, access_token)

    print(f"Authenticated as: {user['username']}")
    if refreshed:
        print("Access token was refreshed successfully.")
    for resolved in resolved_sources:
        print(
            f"\n{resolved.survey.role}: {resolved.form_item.get('title', resolved.survey.label)}"
        )
        print(f"  Form item:    {resolved.survey.item_id}")
        print(f"  Service item: {resolved.service_item.get('id')}")
        print(f"  Service URL:  {resolved.service_url}")
        for collection_name in ("layers", "tables"):
            for dataset in resolved.service_schema.get(collection_name, []):
                if not isinstance(dataset, dict) or not isinstance(dataset.get("id"), int):
                    continue
                schema = request_json(
                    f"{resolved.service_url}/{dataset['id']}",
                    {"f": "json", "token": access_token},
                )
                fields = schema.get("fields", [])
                print(
                    f"  {collection_name[:-1].title()} {dataset['id']}: "
                    f"{schema.get('name', dataset.get('name', 'unnamed'))} "
                    f"({len(fields) if isinstance(fields, list) else 0} fields)"
                )
                if isinstance(fields, list):
                    for field in fields:
                        if isinstance(field, dict):
                            print(
                                f"    {field.get('name', '?')} | "
                                f"{field.get('alias', '')} | {field.get('type', '')}"
                            )
    return 0


def command_snapshot(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    record, refreshed = usable_token_record(config)
    if refreshed:
        print("Access token was refreshed successfully.")
    snapshot_path, manifest = create_snapshot(
        config,
        str(record["access_token"]),
        output_root=args.output_root,
        page_size=args.page_size,
    )
    print(f"Created verified raw snapshot: {snapshot_path}")
    for source in manifest["sources"]:
        counts = ", ".join(
            f"{dataset['collection']}/{dataset['id']}={dataset['record_count']} records"
            for dataset in source["datasets"]
        )
        print(f"  {source['role']}: {counts or 'no datasets'}")
    print("ArcGIS source data was read only; no remote items or records were modified.")
    return 0


def command_transform(args: argparse.Namespace) -> int:
    snapshot_path = args.snapshot
    if snapshot_path is None:
        candidates = sorted(
            path
            for path in DEFAULT_SNAPSHOT_ROOT.iterdir()
            if path.is_dir() and (path / "manifest.json").is_file()
        )
        if not candidates:
            raise SnapshotError(f"No verified snapshots found under: {DEFAULT_SNAPSHOT_ROOT}")
        snapshot_path = candidates[-1]
    try:
        output_path, manifest = transform_legacy_snapshot(
            snapshot_path,
            args.lookup_dir,
            args.output_root,
        )
    except TransformError as exc:
        raise SnapshotError(str(exc)) from exc
    counts = manifest["counts"]
    print(f"Created private candidate lookup set: {output_path}")
    print(f"  devices: {counts['devices']}")
    print(f"  device deployments: {counts['deployments']}")
    print(f"  blocking issues: {counts['blocking_issues']}")
    print(f"  warnings: {counts['warnings']}")
    print("Active lookup files and ArcGIS records were not modified.")
    return 0


def command_refresh(args: argparse.Namespace) -> int:
    """Run the complete read/transform/validate/backup/install workflow."""
    config = load_config(args.config)
    record, refreshed = usable_token_record(config)
    if refreshed:
        print("Access token was refreshed successfully.")

    snapshot_path, snapshot_manifest = create_snapshot(
        config,
        str(record["access_token"]),
        output_root=args.snapshot_root,
        page_size=args.page_size,
    )
    print(f"1/4 Snapshot verified: {snapshot_path}")
    for source in snapshot_manifest["sources"]:
        count = sum(dataset["record_count"] for dataset in source["datasets"])
        print(f"    {source['role']}: {count} records")

    try:
        candidate_path, _transform_manifest = transform_legacy_snapshot(
            snapshot_path,
            args.lookup_dir,
            args.candidate_root,
        )
    except TransformError as exc:
        raise RefreshError(str(exc)) from exc
    print(f"2/4 Candidate generated: {candidate_path}")

    validation = validate_candidate_lookup_set(candidate_path, args.lookup_dir)
    print(
        "3/4 Candidate validated: "
        f"{validation['devices']} devices, {validation['deployments']} placements, "
        f"{validation['rounds']} rounds"
    )
    if validation["blocking_issues"]:
        print(
            f"    Note: {validation['blocking_issues']} unresolved source record(s) "
            "were excluded and remain listed in issues.csv."
        )
    if validation["warnings"]:
        print(f"    Note: {validation['warnings']} non-blocking warning(s) are listed in issues.csv.")

    receipt = install_candidate_lookup_set(candidate_path, args.lookup_dir, validation)
    print(f"4/4 Installed and hash-verified: {args.lookup_dir.resolve()}")
    print(f"    Previous lookup pair backed up to: {receipt['backup_path']}")
    print("Refresh complete. Restart the app to load the new Survey123 data.")
    return 0


def command_logout(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if config.token_path.exists():
        config.token_path.unlink()
        print(f"Removed local ArcGIS authentication data: {config.token_path}")
    else:
        print("No local ArcGIS authentication data was present.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Authenticate, snapshot, validate, and refresh Survey123 lookups."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"private source configuration (default: {DEFAULT_CONFIG_PATH})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth_parser = subparsers.add_parser("auth", help="authenticate with ArcGIS using PKCE")
    auth_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="print the authorization URL without opening a browser",
    )
    auth_parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_CALLBACK_TIMEOUT_SECONDS,
        help="seconds to wait for the localhost callback",
    )
    auth_parser.set_defaults(func=command_auth)

    status_parser = subparsers.add_parser(
        "status", help="verify the current login and configured surveys"
    )
    status_parser.set_defaults(func=command_status)

    discover_parser = subparsers.add_parser(
        "discover", help="resolve Survey123 forms and print their feature-service schemas"
    )
    discover_parser.set_defaults(func=command_discover)

    snapshot_parser = subparsers.add_parser(
        "snapshot", help="capture complete raw Survey123 source data without modifying ArcGIS"
    )
    snapshot_parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_SNAPSHOT_ROOT,
        help=f"private snapshot directory (default: {DEFAULT_SNAPSHOT_ROOT})",
    )
    snapshot_parser.add_argument(
        "--page-size",
        type=int,
        default=500,
        help="maximum object IDs requested per feature page (default: 500)",
    )
    snapshot_parser.set_defaults(func=command_snapshot)

    transform_parser = subparsers.add_parser(
        "transform",
        help="create candidate devices/deployments from a verified snapshot",
    )
    transform_parser.add_argument(
        "--snapshot",
        type=Path,
        help="verified snapshot directory (default: newest local snapshot)",
    )
    transform_parser.add_argument(
        "--lookup-dir",
        type=Path,
        default=DEFAULT_LOOKUP_DIR,
        help=f"current lookup contract (default: {DEFAULT_LOOKUP_DIR})",
    )
    transform_parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_CANDIDATE_ROOT,
        help=f"private candidate directory (default: {DEFAULT_CANDIDATE_ROOT})",
    )
    transform_parser.set_defaults(func=command_transform)

    refresh_parser = subparsers.add_parser(
        "refresh",
        help="snapshot, transform, validate, back up, and install Survey123 lookups",
    )
    refresh_parser.add_argument(
        "--snapshot-root",
        type=Path,
        default=DEFAULT_SNAPSHOT_ROOT,
        help=f"private snapshot directory (default: {DEFAULT_SNAPSHOT_ROOT})",
    )
    refresh_parser.add_argument(
        "--candidate-root",
        type=Path,
        default=DEFAULT_CANDIDATE_ROOT,
        help=f"private candidate directory (default: {DEFAULT_CANDIDATE_ROOT})",
    )
    refresh_parser.add_argument(
        "--lookup-dir",
        type=Path,
        default=DEFAULT_LOOKUP_DIR,
        help=f"live lookup directory (default: {DEFAULT_LOOKUP_DIR})",
    )
    refresh_parser.add_argument(
        "--page-size",
        type=int,
        default=500,
        help="maximum object IDs requested per feature page (default: 500)",
    )
    refresh_parser.set_defaults(func=command_refresh)

    logout_parser = subparsers.add_parser("logout", help="remove local ArcGIS OAuth data")
    logout_parser.set_defaults(func=command_logout)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "timeout", 1) <= 0:
        parser.error("--timeout must be a positive number")
    if getattr(args, "page_size", 1) <= 0:
        parser.error("--page-size must be a positive number")
    try:
        return int(args.func(args))
    except SurveySyncError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
