"""Tapis OAuth login, callback, and logout routes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.config import get_settings
from app.core.errors import DomainError
from app.core.templates import templates
from app.routes._helpers import redirect_with_notice
from app.services import tapis_auth_service

router = APIRouter(tags=["auth"])


class PortalSessionBody(BaseModel):
    access_token: str = Field(min_length=32, max_length=8192)
    next: str = "/"


def _login_page(request: Request, *, error: str = "", next_path: str = "/"):
    return templates.TemplateResponse(
        "auth/login.html",
        {
            "request": request,
            "user": "",
            "active_nav": "",
            "error": error,
            "next": tapis_auth_service.safe_next_path(next_path),
        },
    )


@router.get("/auth/login", response_class=HTMLResponse, name="auth_login")
def login(request: Request, next: str = "/"):
    """Landing page — never auto-redirects to Tapis (avoids redirect loops)."""
    settings = get_settings()
    if settings.local_dev_auth:
        return RedirectResponse(tapis_auth_service.safe_next_path(next), status_code=303)
    notice = request.query_params.get("notice")
    return _login_page(request, error=notice or "", next_path=next)


@router.get("/auth/start", name="auth_start")
def start_oauth(request: Request, next: str = "/"):
    """Begin the Tapis OAuth redirect chain."""
    settings = get_settings()
    if settings.local_dev_auth:
        return RedirectResponse(tapis_auth_service.safe_next_path(next), status_code=303)
    if tapis_auth_service.is_iframe_request(request):
        target = tapis_auth_service.safe_next_path(next)
        return RedirectResponse(f"/auth/embed?next={target}", status_code=303)
    if not settings.tapis.configured:
        return _login_page(
            request,
            error="Tapis OAuth is not configured. Set TAPIS_CLIENT_ID and TAPIS_CLIENT_KEY.",
            next_path=next,
        )
    state = tapis_auth_service.build_oauth_state(next)
    try:
        url = tapis_auth_service.authorize_url(state=state)
    except DomainError as e:
        return _login_page(request, error=str(e), next_path=next)
    return RedirectResponse(url, status_code=302)


@router.get("/auth/embed", response_class=HTMLResponse, name="auth_embed")
def embed_auth(request: Request, next: str = "/"):
    """Iframe bootstrap — receives a Tapis token from the parent Tapis UI via postMessage."""
    settings = get_settings()
    if settings.local_dev_auth:
        return RedirectResponse(tapis_auth_service.safe_next_path(next), status_code=303)
    if request.session.get("tapis_username"):
        return RedirectResponse(tapis_auth_service.safe_next_path(next), status_code=303)
    return templates.TemplateResponse(
        "auth/embed.html",
        {
            "request": request,
            "user": "",
            "active_nav": "",
            "next": tapis_auth_service.safe_next_path(next),
            "portal_origins": settings.tapis_portal_origins,
        },
    )


@router.post("/auth/portal-session", name="auth_portal_session")
def portal_session(request: Request, body: PortalSessionBody):
    """Create a session from a Tapis UI portal handoff token."""
    settings = get_settings()
    if settings.local_dev_auth:
        return JSONResponse({"ok": True, "next": tapis_auth_service.safe_next_path(body.next)})
    try:
        tapis_auth_service.establish_user_session(request, body.access_token.strip())
    except DomainError as e:
        return JSONResponse({"error": str(e)}, status_code=e.status_code)
    except Exception as e:
        return JSONResponse({"error": f"Login failed: {e}"}, status_code=401)
    return JSONResponse({"ok": True, "next": tapis_auth_service.safe_next_path(body.next)})


@router.get("/auth/callback", name="auth_callback")
def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    settings = get_settings()
    if settings.local_dev_auth:
        return RedirectResponse("/", status_code=303)
    if error:
        return redirect_with_notice("/auth/login", notice=f"Tapis login failed: {error}", level="error")
    if not code:
        return redirect_with_notice("/auth/login", notice="Missing authorization code from Tapis.", level="error")
    try:
        target = tapis_auth_service.parse_oauth_state(state)
        token_data = tapis_auth_service.exchange_code_for_token(code)
        access_token = tapis_auth_service.extract_access_token(token_data)
        tapis_auth_service.establish_user_session(request, access_token)
        refresh = tapis_auth_service.extract_refresh_token(token_data)
        if refresh:
            request.session["tapis_refresh_token"] = refresh
    except DomainError as e:
        return redirect_with_notice("/auth/login", notice=str(e), level="error")
    except Exception as e:
        return redirect_with_notice("/auth/login", notice=f"Login failed: {e}", level="error")
    return RedirectResponse(target, status_code=303)


@router.get("/auth/logout", name="auth_logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/auth/login", status_code=303)
