"""Typed domain errors mapped to HTTP responses by route handlers."""

from __future__ import annotations


class DomainError(Exception):
    """Base class for application/domain errors."""

    status_code = 400


class NotFoundError(DomainError):
    status_code = 404


class ConflictError(DomainError):
    status_code = 409


class UnauthorizedError(DomainError):
    status_code = 401


class ForbiddenError(DomainError):
    status_code = 403


class ValidationError(DomainError):
    status_code = 422


class GoneError(DomainError):
    status_code = 410
