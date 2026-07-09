"""FastAPI application bootstrap.

Wires up middleware, routes, error handlers, and background tasks.
"""

from __future__ import annotations

import threading
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.config import STATIC_DIR, get_settings
from app.core.errors import DomainError, UnauthorizedError
from app.core.logging import configure_logging, get_logger
from app.core.templates import templates
from app.db.migrations import run_migrations
from app.routes.api import agent as agent_api
from app.routes.api import public as public_api
from app.routes.api import stream as stream_api
from app.routes.web import admin as admin_web
from app.routes.web import auth as auth_web
from app.routes.web import dashboard as dashboard_web
from app.routes.web import deployments as deployments_web
from app.routes.web import devices as devices_web
from app.routes.web import groups as groups_web
from app.routes.web import hardware as hardware_web
from app.routes.web import models as models_web
from app.routes.web import operations as operations_web
from app.services import heartbeat_service
from app.services.seed_service import seed_everything
from app.services import tapis_auth_service


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging("DEBUG" if settings.debug else "INFO")
    log = get_logger("app")

    app = FastAPI(
        title="ICICLE Edge Control Plane",
        version="2.0.0",
        description="Fleet management and model deployment for edge inference.",
        docs_url="/api/docs",
        redoc_url=None,
    )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    PUBLIC_PATH_PREFIXES = (
        "/auth/",
        "/api/agent/",
        "/api/stream/",
        "/api/health",
        "/static/",
        "/api/docs",
        "/docs",
        "/openapi.json",
    )

    @app.middleware("http")
    async def require_human_login(request: Request, call_next):
        """When Tapis auth is enabled, redirect unauthenticated browser users to login."""
        settings = get_settings()
        if settings.local_dev_auth:
            return await call_next(request)
        path = request.url.path
        if any(path.startswith(prefix) for prefix in PUBLIC_PATH_PREFIXES):
            return await call_next(request)
        if "session" in request.scope and request.session.get("tapis_username"):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse(
                {"error": "Not authenticated", "type": "UnauthorizedError"},
                status_code=401,
            )
        next_url = path
        if request.url.query:
            next_url = f"{path}?{request.url.query}"
        if tapis_auth_service.is_iframe_request(request):
            return RedirectResponse(
                f"/auth/embed?next={quote(next_url)}",
                status_code=303,
            )
        return RedirectResponse(f"/auth/start?next={quote(next_url)}", status_code=303)

    @app.middleware("http")
    async def attach_template_flags(request: Request, call_next):
        from app.auth import current_user, is_admin, is_logged_in
        request.state.is_admin = False
        request.state.user_role = "Guest"
        request.state.local_dev_auth = settings.local_dev_auth
        if is_logged_in(request):
            try:
                request.state.is_admin = is_admin(request)
                request.state.user_role = (current_user(request).get("role") or "operator").title()
            except Exception:
                pass
        return await call_next(request)

    # Session must load before auth middleware runs (Starlette runs last-added middleware first).
    session_same_site = settings.session_same_site
    if session_same_site not in {"lax", "strict", "none"}:
        session_same_site = "lax"
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        https_only=settings.base_url_clean.startswith("https://"),
        same_site=session_same_site,
        max_age=14 * 24 * 3600,
    )
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Auth routes (login/callback/logout) before protected web routes
    app.include_router(auth_web.router)
    app.include_router(dashboard_web.router)
    app.include_router(admin_web.router)
    app.include_router(devices_web.router)
    app.include_router(groups_web.router)
    app.include_router(hardware_web.router)
    app.include_router(models_web.router)
    app.include_router(deployments_web.router)
    app.include_router(operations_web.router)

    # JSON API routes
    app.include_router(public_api.router)
    app.include_router(agent_api.router)
    app.include_router(stream_api.router)

    @app.exception_handler(UnauthorizedError)
    async def _unauthorized_handler(request: Request, exc: UnauthorizedError):
        accepts_json = "application/json" in (request.headers.get("accept") or "")
        if request.url.path.startswith("/api") or accepts_json:
            return JSONResponse(
                {"error": str(exc), "type": exc.__class__.__name__},
                status_code=exc.status_code,
            )
        next_url = request.url.path
        if request.url.query:
            next_url = f"{next_url}?{request.url.query}"
        if tapis_auth_service.is_iframe_request(request):
            return RedirectResponse(f"/auth/embed?next={quote(next_url)}", status_code=303)
        return RedirectResponse(f"/auth/start?next={quote(next_url)}", status_code=303)

    @app.exception_handler(DomainError)
    async def _domain_error_handler(request: Request, exc: DomainError):
        accepts_json = "application/json" in (request.headers.get("accept") or "")
        if request.url.path.startswith("/api") or accepts_json:
            return JSONResponse({"error": str(exc), "type": exc.__class__.__name__},
                                status_code=exc.status_code)
        return templates.TemplateResponse(
            "errors/error.html",
            {"request": request, "user": "", "active_nav": "",
             "status_code": exc.status_code, "title": exc.__class__.__name__, "message": str(exc)},
            status_code=exc.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        if request.url.path.startswith("/api"):
            return JSONResponse({"error": "ValidationError", "details": exc.errors()}, status_code=422)
        return templates.TemplateResponse(
            "errors/error.html",
            {"request": request, "user": "", "active_nav": "",
             "status_code": 422, "title": "Invalid request",
             "message": "Some fields are missing or invalid."},
            status_code=422,
        )

    @app.exception_handler(404)
    async def _not_found(request: Request, exc):
        if request.url.path.startswith("/api"):
            detail = getattr(exc, "detail", None)
            if isinstance(detail, str) and detail:
                return JSONResponse({"error": detail}, status_code=404)
            return JSONResponse({"error": "Not found"}, status_code=404)
        return templates.TemplateResponse(
            "errors/error.html",
            {"request": request, "user": "", "active_nav": "",
             "status_code": 404, "title": "Page not found",
             "message": "We couldn't find what you were looking for."},
            status_code=404,
        )

    @app.on_event("startup")
    def _on_startup() -> None:
        try:
            log.info("running migrations…")
            run_migrations()
            log.info("seeding reference data…")
            seed_everything()
            log.info("starting offline watchdog…")
            threading.Thread(
                target=heartbeat_service.run_offline_watchdog_forever,
                daemon=True,
                name="offline-watchdog",
            ).start()
            log.info("ICICLE Edge Control Plane ready on %s", settings.base_url_clean)
        except Exception:
            log.exception("startup failed — see traceback above")
            raise

    return app


app = create_app()
