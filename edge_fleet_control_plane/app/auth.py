"""Authentication helpers.

Local dev: a fixed username is auto-created and returned.
Production: relies on a Tapis-issued session cookie (set by an upstream
proxy or a future login route). We keep the surface area small so wiring a
real OIDC/SAML flow later is straightforward.

Edge agents authenticate with a long-lived device API key (Bearer token)
issued at enrollment — not a Tapis user token.
"""

from __future__ import annotations

import hashlib

from fastapi import Request

from app.config import get_settings
from app.core.errors import ForbiddenError, UnauthorizedError
from app.repositories import device_credentials as creds_repo
from app.repositories import users as users_repo


def _hash_device_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _has_session(request: Request) -> bool:
    return "session" in request.scope


def is_logged_in(request: Request) -> bool:
    settings = get_settings()
    if settings.local_dev_auth:
        return True
    if not _has_session(request):
        return False
    return bool(request.session.get("tapis_username"))


def current_username(request: Request) -> str:
    settings = get_settings()
    if settings.local_dev_auth:
        username = settings.local_dev_username
    else:
        if not _has_session(request):
            raise UnauthorizedError("Not authenticated")
        username = request.session.get("tapis_username")
        if not username:
            raise UnauthorizedError("Not authenticated")
    users_repo.ensure(username)
    return username


def current_user(request: Request) -> dict:
    username = current_username(request)
    row = users_repo.get(username) or {}
    display = (
        row.get("display_name")
        or (_has_session(request) and request.session.get("tapis_display_name"))
        or username
    )
    return {
        "tapis_username": username,
        "display_name": display,
        "role": row.get("role") or "operator",
    }


def require_admin(request: Request) -> str:
    """Gate admin-only routes. Local dev mode grants admin to the dev user."""
    username = current_username(request)
    settings = get_settings()
    if settings.local_dev_auth and username == settings.local_dev_username:
        return username
    user = users_repo.get(username) or {}
    if user.get("role") != "admin":
        raise ForbiddenError("Admin access required")
    return username


def is_admin(request: Request) -> bool:
    try:
        require_admin(request)
        return True
    except (ForbiddenError, UnauthorizedError):
        return False


def authenticate_device(request: Request, device_uid: str) -> str:
    """Validate Bearer device API key and ensure it matches the claimed device_id."""
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        raise UnauthorizedError("Missing device API key")
    raw = auth[7:].strip()
    if not raw:
        raise UnauthorizedError("Missing device API key")
    rec = creds_repo.get_active_by_hash(_hash_device_key(raw))
    if not rec:
        raise UnauthorizedError("Invalid device API key")
    if rec["device_uid"] != device_uid:
        raise ForbiddenError("Device ID does not match API key")
    return rec["device_uid"]
