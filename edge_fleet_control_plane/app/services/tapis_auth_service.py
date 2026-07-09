"""Tapis OAuth2 authorization-code flow helpers."""

from __future__ import annotations

import base64
import json
import secrets
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx
from fastapi import Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import get_settings
from app.core.errors import UnauthorizedError, ValidationError
from app.repositories import users as users_repo

_STATE_SALT = "icicle-edge-oauth-state"
_STATE_MAX_AGE = 600


def _decode_jwt_payload(token: str) -> Dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + padding)
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def extract_access_token(data: Any) -> str:
    """Pull the JWT string from Tapis /tokens responses (shape varies by tenant)."""
    if isinstance(data, str):
        if data.count(".") >= 2:
            return data
        raise UnauthorizedError("Unexpected access_token string")
    if not isinstance(data, dict):
        raise UnauthorizedError("Unexpected Tapis token response")
    for key in ("access_token", "accessToken"):
        val = data.get(key)
        if isinstance(val, str) and val.count(".") >= 2:
            return val
        if isinstance(val, dict):
            nested = val.get("access_token") or val.get("accessToken")
            if isinstance(nested, str) and nested.count(".") >= 2:
                return nested
    raise UnauthorizedError("Tapis token response missing access_token")


def extract_refresh_token(data: dict) -> Optional[str]:
    for key in ("refresh_token", "refreshToken"):
        val = data.get(key)
        if isinstance(val, str):
            return val
        if isinstance(val, dict):
            nested = val.get("refresh_token") or val.get("refreshToken")
            if isinstance(nested, str):
                return nested
    return None


def username_from_token(access_token: str) -> str:
    claims = _decode_jwt_payload(access_token)
    username = claims.get("tapis/username")
    if username:
        return str(username)
    sub = claims.get("sub") or ""
    if "@" in sub:
        return sub.split("@", 1)[0]
    if sub:
        return str(sub)
    raise UnauthorizedError("Tapis token is missing a username claim")


def display_name_from_token(access_token: str, username: str) -> str:
    claims = _decode_jwt_payload(access_token)
    for key in ("name", "tapis/display_name", "preferred_username"):
        value = claims.get(key)
        if value:
            return str(value)
    return username


def _state_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt=_STATE_SALT)


def build_oauth_state(next_path: str) -> str:
    """Signed state embeds the post-login redirect — no session cookie required."""
    return _state_serializer().dumps({
        "n": secrets.token_urlsafe(12),
        "next": safe_next_path(next_path),
    })


def parse_oauth_state(state: str) -> str:
    """Validate OAuth state and return the post-login redirect path."""
    if not state:
        raise UnauthorizedError("Invalid OAuth state — please try again.")
    try:
        data = _state_serializer().loads(state, max_age=_STATE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        raise UnauthorizedError("Invalid OAuth state — please try again.")
    if not isinstance(data, dict):
        raise UnauthorizedError("Invalid OAuth state — please try again.")
    return safe_next_path(data.get("next"))


def authorize_url(*, state: str) -> str:
    settings = get_settings()
    tapis = settings.tapis
    if not tapis.configured:
        raise ValidationError("Tapis OAuth is not configured (set TAPIS_CLIENT_ID and TAPIS_CLIENT_KEY).")
    params = {
        "client_id": tapis.client_id,
        "redirect_uri": tapis.callback_url,
        "response_type": "code",
        "state": state,
    }
    return f"{tapis.oauth_base}/authorize?{urlencode(params)}"


def exchange_code_for_token(code: str) -> Dict[str, Any]:
    settings = get_settings()
    tapis = settings.tapis
    if not tapis.configured:
        raise ValidationError("Tapis OAuth is not configured.")
    body = {
        "code": code,
        "redirect_uri": tapis.callback_url,
        "grant_type": "authorization_code",
    }
    auth = base64.b64encode(f"{tapis.client_id}:{tapis.client_key}".encode()).decode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {auth}",
    }
    with httpx.Client(timeout=20.0) as client:
        resp = client.post(f"{tapis.oauth_base}/tokens", json=body, headers=headers)
    if resp.status_code >= 400:
        detail = resp.text[:500]
        raise UnauthorizedError(f"Tapis token exchange failed ({resp.status_code}): {detail}")
    data = resp.json()
    if isinstance(data, dict) and "result" in data:
        data = data["result"]
    if not isinstance(data, dict):
        raise UnauthorizedError("Unexpected Tapis token response")
    extract_access_token(data)  # validate before returning
    return data


def role_for_username(username: str) -> str:
    settings = get_settings()
    if username in settings.tapis.admin_usernames:
        return "admin"
    return "operator"


def safe_next_path(next_path: Optional[str]) -> str:
    if not next_path:
        return "/"
    if not next_path.startswith("/") or next_path.startswith("//"):
        return "/"
    return next_path


def is_iframe_request(request: Request) -> bool:
    """Detect iframe navigations (Sec-Fetch-Dest is set by modern browsers)."""
    dest = (request.headers.get("sec-fetch-dest") or "").strip().lower()
    if dest in {"iframe", "embed", "object", "frame"}:
        return True
    # Legacy fallback when embedding via older browsers / proxies strip Sec-Fetch-*.
    return request.query_params.get("embed") == "1"


def validate_portal_access_token(access_token: str) -> None:
    """Basic JWT checks before trusting a portal handoff token."""
    token = (access_token or "").strip()
    if token.count(".") < 2:
        raise UnauthorizedError("Invalid portal access token")
    claims = _decode_jwt_payload(token)
    if not claims:
        raise UnauthorizedError("Invalid portal access token")
    exp = claims.get("exp")
    if isinstance(exp, (int, float)) and exp <= time.time():
        raise UnauthorizedError("Portal access token expired — refresh Tapis UI and retry.")
    username_from_token(token)


def establish_user_session(request: Request, access_token: str) -> str:
    """Persist Tapis identity in the signed session cookie."""
    validate_portal_access_token(access_token)
    username = username_from_token(access_token)
    display_name = display_name_from_token(access_token, username)
    role = role_for_username(username)
    users_repo.ensure(username, display_name=display_name, role=role)
    if role == "admin":
        users_repo.set_role(username, "admin")
    request.session["tapis_username"] = username
    request.session["tapis_display_name"] = display_name
    request.session["tapis_access_token"] = access_token
    return username
