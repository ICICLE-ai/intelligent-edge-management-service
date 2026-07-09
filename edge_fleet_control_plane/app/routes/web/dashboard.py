from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.auth import current_username
from app.core.templates import templates
from app.repositories import events as events_repo
from app.services import deployment_service, device_service, model_service

router = APIRouter(tags=["web"])


@router.get("/", response_class=HTMLResponse, name="dashboard")
def dashboard(request: Request):
    owner = current_username(request)
    devices = device_service.list_for_owner(owner)
    deployments = deployment_service.list_for_owner(owner)
    recent_events = events_repo.for_owner(owner, limit=12)
    counts = {
        "devices": device_service.counts(owner),
        "deployments": deployment_service.counts(owner),
        "models": len(model_service.list_published(owner)),
    }
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": owner,
            "active_nav": "dashboard",
            "devices": devices[:6],
            "deployments": deployments[:6],
            "events": recent_events,
            "counts": counts,
        },
    )
