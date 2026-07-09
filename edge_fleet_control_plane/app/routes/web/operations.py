from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.auth import current_username
from app.core.templates import templates
from app.repositories import commands as commands_repo
from app.repositories import events as events_repo
from app.repositories import mqtt_audit as audit_repo

router = APIRouter(tags=["web"])


@router.get("/operations/commands", response_class=HTMLResponse, name="operations_commands")
def commands(request: Request, limit: int = 200):
    owner = current_username(request)
    return templates.TemplateResponse(
        "operations/commands.html",
        {
            "request": request,
            "user": owner,
            "active_nav": "ops_commands",
            "commands": commands_repo.list_for_owner(owner, limit=limit),
        },
    )


@router.get("/operations/events", response_class=HTMLResponse, name="operations_events")
def events(request: Request, limit: int = 200, severity: str = ""):
    owner = current_username(request)
    return templates.TemplateResponse(
        "operations/events.html",
        {
            "request": request,
            "user": owner,
            "active_nav": "ops_events",
            "events": events_repo.for_owner(owner, limit=limit, severity=severity or None),
            "severity": severity,
        },
    )


@router.get("/operations/mqtt", response_class=HTMLResponse, name="operations_mqtt")
def mqtt_audit_view(request: Request, limit: int = 200):
    owner = current_username(request)
    return templates.TemplateResponse(
        "operations/mqtt.html",
        {
            "request": request,
            "user": owner,
            "active_nav": "ops_mqtt",
            "events": audit_repo.list_recent(owner, limit=limit),
        },
    )
