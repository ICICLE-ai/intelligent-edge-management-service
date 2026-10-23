"""Shared route helpers — flash notices and exception translation."""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import RedirectResponse, Response

from app.core.errors import DomainError


def http_error_from_domain(e: DomainError) -> HTTPException:
    return HTTPException(status_code=e.status_code, detail=str(e))


def redirect_with_notice(path: str, *, notice: Optional[str] = None,
                         level: str = "success") -> RedirectResponse:
    sep = "&" if "?" in path else "?"
    target = path
    if notice:
        if level == "error":
            level = "danger"
        target = f"{path}{sep}notice={quote(notice, safe='')}&level={quote(level, safe='')}"
    return RedirectResponse(url=target, status_code=303)


def form_error(e: DomainError, redirect_to: str = "") -> RedirectResponse:
    """Browser forms (with redirect_to) get a toast; clients without it get JSON."""
    if redirect_to:
        return redirect_with_notice(redirect_to, notice=str(e), level="danger")
    raise http_error_from_domain(e)


def no_cache(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response
