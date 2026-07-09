"""Jinja templates wired up with the design-system globals and filters."""

from __future__ import annotations

import json
from typing import Any

from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from app.config import TEMPLATE_DIR
from app.core.time import humanize_delta, parse_iso


def _format_dt(value: str | None) -> str:
    dt = parse_iso(value)
    if not dt:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _format_short(value: str | None) -> str:
    dt = parse_iso(value)
    if not dt:
        return "—"
    return dt.strftime("%b %d, %H:%M")


def _to_json(value: Any) -> Markup:
    return Markup(json.dumps(value, indent=2, default=str))


def _badge_class(value: str | None) -> str:
    if not value:
        return "badge--neutral"
    v = value.upper()
    GREEN = {"ONLINE", "RUNNING", "ACK", "SUCCEEDED", "PUBLISHED", "DELIVERED", "ACTIVE"}
    YELLOW = {"PENDING", "DELIVERING", "RECORDED", "MQTT_SENT", "STOPPING", "INSTALLER_READY", "REGISTERED", "ENROLLED", "DRAFT", "DOWNLOADING", "PULLING", "STARTING"}
    RED = {"OFFLINE", "FAILED", "ERROR", "MQTT_FAILED", "DEPRECATED", "CANCELLED"}
    GREY = {"STOPPED", "REGISTERED_NOT_INSTALLED", "INACTIVE"}
    if v in GREEN:
        return "badge--green"
    if v in YELLOW:
        return "badge--yellow"
    if v in RED:
        return "badge--red"
    if v in GREY:
        return "badge--grey"
    return "badge--neutral"


templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.filters["dt"] = _format_dt
templates.env.filters["dt_short"] = _format_short
templates.env.filters["relative"] = humanize_delta
templates.env.filters["tojson_pretty"] = _to_json
templates.env.globals["badge_class"] = _badge_class
