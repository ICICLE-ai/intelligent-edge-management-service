"""Admin web routes — hardware catalog and other operator settings."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import current_user, current_username, is_admin, require_admin
from app.core.errors import NotFoundError, ValidationError
from app.core.templates import templates
from app.repositories import generations as gen_repo
from app.routes._helpers import http_error_from_domain, redirect_with_notice

router = APIRouter(prefix="/admin", tags=["web"])


@router.get("/hardware", response_class=HTMLResponse, name="admin_hardware")
def admin_hardware(request: Request):
    require_admin(request)
    user = current_user(request)
    return templates.TemplateResponse(
        "admin/hardware.html",
        {
            "request": request,
            "user": user.get("tapis_username") or current_username(request),
            "user_role": user.get("role") or "operator",
            "active_nav": "admin_hardware",
            "generations": gen_repo.list_all(),
        },
    )


@router.post("/hardware", name="admin_hardware_create")
def admin_hardware_create(
    request: Request,
    generation_uid: str = Form(...),
    display_name: str = Form(...),
    hardware_type: str = Form(...),
    architecture: str = Form("aarch64"),
    vendor: str = Form(""),
    device_family: str = Form(""),
    default_runtime: str = Form("nvidia"),
    cpu_cores: Optional[int] = Form(None),
    memory_mb: Optional[int] = Form(None),
    storage_gb: Optional[int] = Form(None),
    description: str = Form(""),
    cuda_supported: str = Form("on"),
):
    require_admin(request)
    uid = generation_uid.strip().lower().replace(" ", "-")
    if not uid:
        raise http_error_from_domain(ValidationError("generation_uid is required"))
    try:
        gen_repo.upsert({
            "generation_uid": uid,
            "display_name": display_name.strip(),
            "vendor": vendor.strip() or None,
            "device_family": device_family.strip() or None,
            "hardware_type": hardware_type.strip(),
            "architecture": architecture.strip(),
            "cuda_supported": cuda_supported == "on",
            "default_runtime": default_runtime.strip() or None,
            "cpu_cores": cpu_cores,
            "memory_mb": memory_mb,
            "storage_gb": storage_gb,
            "description": description.strip() or None,
            "is_active": True,
        })
    except Exception as e:
        raise http_error_from_domain(ValidationError(str(e)))
    return redirect_with_notice("/admin/hardware", notice="Hardware generation saved")


@router.post("/hardware/{generation_uid}/deactivate", name="admin_hardware_deactivate")
def admin_hardware_deactivate(request: Request, generation_uid: str):
    require_admin(request)
    rec = gen_repo.get(generation_uid)
    if not rec:
        raise http_error_from_domain(NotFoundError("Generation not found"))
    rec["is_active"] = False
    gen_repo.upsert(rec)
    return redirect_with_notice("/admin/hardware", notice="Hardware generation deactivated")
