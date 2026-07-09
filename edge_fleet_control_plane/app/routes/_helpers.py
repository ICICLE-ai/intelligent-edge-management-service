"""Shared route helpers — flash notices and exception translation."""

from __future__ import annotations

from typing import Optional

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
        target = f"{path}{sep}notice={notice}&level={level}"
    return RedirectResponse(url=target, status_code=303)


def no_cache(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response
