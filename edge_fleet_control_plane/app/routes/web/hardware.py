from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.auth import current_username
from app.core.templates import templates
from app.repositories import generations as gen_repo

router = APIRouter(tags=["web"])


@router.get("/hardware", response_class=HTMLResponse, name="hardware")
def hardware(request: Request):
    return templates.TemplateResponse(
        "hardware.html",
        {
            "request": request,
            "user": current_username(request),
            "active_nav": "hardware",
            "generations": gen_repo.list_active(),
        },
    )
