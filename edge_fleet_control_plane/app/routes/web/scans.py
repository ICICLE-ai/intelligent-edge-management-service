"""Turntable scan pages: stations, the per-station scan list, and result files."""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from app.auth import current_username
from app.core.errors import DomainError
from app.core.templates import templates
from app.routes._helpers import form_error, http_error_from_domain, no_cache, redirect_with_notice
from app.services import scan_service

router = APIRouter(tags=["web"])


@router.get("/scans", response_class=HTMLResponse, name="scans_home")
def scans_home(request: Request):
    owner = current_username(request)
    return templates.TemplateResponse(
        "scans/index.html",
        {
            "request": request,
            "user": owner,
            "active_nav": "scans",
            "stations": scan_service.list_stations(owner),
        },
    )


@router.get("/scans/{device_uid}", response_class=HTMLResponse, name="scans_station")
def scans_station(request: Request, device_uid: str):
    owner = current_username(request)
    try:
        device = scan_service.get_station(device_uid, owner)
        data = scan_service.list_jobs(device_uid, owner)
    except DomainError as e:
        raise http_error_from_domain(e)
    return templates.TemplateResponse(
        "scans/station.html",
        {
            "request": request,
            "user": owner,
            "active_nav": "scans",
            "device": device,
            "data": data,
            "checklist": scan_service.CHECKLIST,
            "default_iterations": scan_service.DEFAULT_ITERATIONS,
        },
    )


@router.get("/scans/{device_uid}/jobs.json", name="scans_jobs_json")
def scans_jobs_json(request: Request, device_uid: str):
    owner = current_username(request)
    try:
        return no_cache(JSONResponse(scan_service.list_jobs(device_uid, owner)))
    except DomainError as e:
        raise http_error_from_domain(e)


@router.post("/scans/{device_uid}/submit", name="scans_submit")
async def scans_submit(request: Request, device_uid: str):
    owner = current_username(request)
    form = await request.form()
    back = f"/scans/{device_uid}"
    try:
        scan_service.submit(
            device_uid=device_uid,
            owner=owner,
            object_name=str(form.get("object_name") or ""),
            rotations=float(form.get("rotations") or 1),
            iterations=int(float(form.get("iterations") or scan_service.DEFAULT_ITERATIONS)),
            target_fps=float(form.get("target_fps") or 5),
            checklist=[str(k) for k in form.getlist("checklist")],
        )
    except DomainError as e:
        return form_error(e, back)
    except ValueError:
        return redirect_with_notice(back, notice="Check the scan settings: numbers only.", level="danger")
    return redirect_with_notice(back, notice="Scan sent to the station. It will start filming in a moment.")


def _job_action(request: Request, device_uid: str, job_id: str, fn, notice: str):
    owner = current_username(request)
    back = f"/scans/{device_uid}#job-{job_id}"
    try:
        fn(device_uid=device_uid, owner=owner, job_id=job_id)
    except DomainError as e:
        return form_error(e, back)
    return redirect_with_notice(back, notice=notice)


@router.post("/scans/{device_uid}/jobs/{job_id}/cancel", name="scans_cancel")
def scans_cancel(request: Request, device_uid: str, job_id: str):
    return _job_action(request, device_uid, job_id, scan_service.cancel, "Cancel sent to the station.")


@router.post("/scans/{device_uid}/jobs/{job_id}/retry", name="scans_retry")
def scans_retry(request: Request, device_uid: str, job_id: str):
    return _job_action(request, device_uid, job_id, scan_service.retry,
                       "Rebuild requested. The GPU will pick it up from the saved frames.")


@router.post("/scans/{device_uid}/jobs/{job_id}/fetch", name="scans_fetch")
def scans_fetch(request: Request, device_uid: str, job_id: str):
    return _job_action(request, device_uid, job_id, scan_service.fetch_bundle,
                       "Asked the station to send the results. Large scans take a few minutes.")


@router.post("/scans/{device_uid}/jobs/{job_id}/delete-bundle", name="scans_delete_bundle")
def scans_delete_bundle(request: Request, device_uid: str, job_id: str):
    return _job_action(request, device_uid, job_id, scan_service.delete_bundle,
                       "Removed the portal copy. The station still has the results.")


@router.get("/scans/{device_uid}/jobs/{job_id}/files/{name}", name="scans_file")
def scans_file(request: Request, device_uid: str, job_id: str, name: str):
    owner = current_username(request)
    try:
        scan_service.get_station(device_uid, owner)
        path = scan_service.preview_path(device_uid, job_id, name)
    except DomainError as e:
        raise http_error_from_domain(e)
    if not path:
        raise HTTPException(404, "Not uploaded yet")
    return FileResponse(path, media_type="video/mp4")


@router.get("/scans/{device_uid}/jobs/{job_id}/bundle", name="scans_bundle")
def scans_bundle(request: Request, device_uid: str, job_id: str):
    owner = current_username(request)
    try:
        scan_service.get_station(device_uid, owner)
        path = scan_service.bundle_path(device_uid, job_id)
    except DomainError as e:
        raise http_error_from_domain(e)
    if not path.is_file():
        raise HTTPException(404, "Results are not on the portal. Fetch them first.")
    return FileResponse(path, media_type="application/x-tar", filename=f"{job_id}.tar")
