import asyncio
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from app.auth import current_username
from app.core.errors import DomainError
from app.core.templates import templates
from app.repositories import commands as commands_repo
from app.repositories import deployments as dep_repo
from app.repositories import enrollments as enrollments_repo
from app.repositories import events as events_repo
from app.repositories import generations as gen_repo
from app.repositories import groups as groups_repo
from app.repositories import heartbeats as heartbeats_repo
from app.routes._helpers import http_error_from_domain, redirect_with_notice
from app.services import (
    deployment_service,
    device_service,
    enrollment_service,
    model_service,
    stream_relay,
    stream_service,
)

router = APIRouter(tags=["web"])


@router.get("/devices", response_class=HTMLResponse, name="devices_list")
def list_devices(request: Request):
    owner = current_username(request)
    devices = device_service.list_for_owner(owner)
    return templates.TemplateResponse(
        "devices/list.html",
        {
            "request": request,
            "user": owner,
            "active_nav": "devices",
            "devices": devices,
        },
    )


@router.get("/devices/register", name="devices_register")
def register_form_redirect():
    return RedirectResponse("/devices/setup", status_code=302)


@router.get("/devices/setup", response_class=HTMLResponse, name="devices_setup")
def setup_form(request: Request):
    owner = current_username(request)
    return templates.TemplateResponse(
        "devices/setup.html",
        {
            "request": request,
            "user": owner,
            "active_nav": "devices_setup",
            "generations": gen_repo.list_active(),
            "groups": groups_repo.list_for_owner(owner),
        },
    )


@router.post("/devices", name="devices_create")
def create_device(
    request: Request,
    device_name: str = Form(...),
    device_alias: str = Form(""),
    generation_uid: str = Form(...),
    group_uid: str = Form(""),
    site_name: str = Form(""),
    camera_bus: str = Form("csi"),
    camera_count: int = Form(1),
    camera_indices: str = Form(""),
):
    owner = current_username(request)
    try:
        device = device_service.register(
            owner=owner,
            device_name=device_name,
            device_alias=device_alias or None,
            generation_uid=generation_uid,
            group_uid=group_uid or None,
            site_name=site_name or None,
            camera_bus=camera_bus or None,
            camera_count=camera_count,
            camera_indices=camera_indices or None,
        )
    except DomainError as e:
        raise http_error_from_domain(e)
    return RedirectResponse(f"/devices/{device['device_uid']}?setup=1", status_code=303)


@router.get("/devices/{device_uid}", response_class=HTMLResponse, name="device_detail")
def device_detail(request: Request, device_uid: str, setup: Optional[str] = None):
    owner = current_username(request)
    try:
        device = device_service.get(device_uid, owner)
    except DomainError as e:
        raise http_error_from_domain(e)
    enrollment = enrollment_service.latest_enrollment_for_device(device_uid)
    heartbeats = heartbeats_repo.latest_for_device(device_uid, limit=8)
    latest_hb = heartbeats[0] if heartbeats else None
    deployments = deployment_service.list_for_device(device_uid, owner)
    for dep in deployments:
        deployment_service.reconcile_deployment_status(dep["deployment_uid"])
        fresh = dep_repo.get(dep["deployment_uid"], owner) or dep
        device_rows = dep_repo.list_device_deployments(dep["deployment_uid"])
        dep["effective_status"] = deployment_service.effective_status(fresh["status"], device_rows)
    commands = commands_repo.list_for_device(device_uid, owner, limit=20)
    device_events = events_repo.for_device(device_uid, limit=20)
    deployable = model_service.deployable_for_device(owner, device)
    groups = groups_repo.list_for_owner(owner)
    stream = stream_service.stream_context(device_uid, owner)
    active_deployment = deployment_service.get_active_device_deployment(device_uid, owner)
    inference_streams = stream_service.inference_streams_context(
        (active_deployment or {}).get("devices") or [],
        card=(active_deployment or {}).get("card"),
    )
    compat_count = len(deployable["mine"]) + len(deployable["others"])
    setup_readiness = device_service.setup_readiness(
        device,
        enrollment=enrollment,
        compatible_app_count=compat_count,
    )
    show_setup_panel = setup == "1" or not setup_readiness["core_complete"]
    return templates.TemplateResponse(
        "devices/detail.html",
        {
            "request": request,
            "user": owner,
            "active_nav": "devices",
            "device": device,
            "enrollment": enrollment,
            "heartbeats": heartbeats,
            "latest_hb": latest_hb,
            "deployments": deployments,
            "commands": commands,
            "events": device_events,
            "my_apps": deployable["mine"],
            "other_apps": deployable["others"],
            "groups": groups,
            "generations": gen_repo.list_active(),
            "stream": stream,
            "active_deployment": active_deployment,
            "inference_streams": inference_streams,
            "setup_readiness": setup_readiness,
            "show_setup_panel": show_setup_panel,
            "inference_stream_title": (
                "Live feeds · %s" % active_deployment["model_display_name"]
                if active_deployment else "Inference live stream"
            ),
            "inference_stream_subtitle": (
                "Annotated model output from the running container on this device."
                if active_deployment else ""
            ),
        },
    )


@router.post("/devices/{device_uid}/stream/start", name="device_stream_start")
def stream_start(request: Request, device_uid: str, camera_type: str = Form("")):
    owner = current_username(request)
    try:
        stream_service.start(owner=owner, device_uid=device_uid, camera_type=camera_type or None)
    except DomainError as e:
        raise http_error_from_domain(e)
    return redirect_with_notice(f"/devices/{device_uid}", notice="Live stream requested")


@router.post("/devices/{device_uid}/stream/stop", name="device_stream_stop")
def stream_stop(request: Request, device_uid: str):
    owner = current_username(request)
    try:
        stream_service.stop(owner=owner, device_uid=device_uid)
    except DomainError as e:
        raise http_error_from_domain(e)
    return redirect_with_notice(f"/devices/{device_uid}", notice="Live stream stop sent")


@router.get("/devices/{device_uid}/stream.mjpg", name="device_stream_mjpeg")
async def stream_mjpeg(request: Request, device_uid: str):
    """Serve the device's live MJPEG stream as multipart/x-mixed-replace.

    Long-poll friendly: if the device hasn't started pushing yet, we hold the
    connection open (the browser <img> shows nothing) until frames arrive or the
    client gives up.
    """
    owner = current_username(request)
    try:
        device_service.get(device_uid, owner)  # ownership / existence guard
    except DomainError as e:
        raise http_error_from_domain(e)

    boundary = "icicleframe"

    async def gen():
        last_seq = -1
        idle_ticks = 0
        # ~25s of grace before a frame arrives; then keep going while frames flow.
        max_idle_ticks = 500
        while True:
            if await request.is_disconnected():
                break
            seq, frame, _ = stream_relay.latest(device_uid)
            if frame is not None and seq != last_seq:
                last_seq = seq
                idle_ticks = 0
                yield (
                    f"--{boundary}\r\n"
                    f"Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(frame)}\r\n\r\n"
                ).encode() + frame + b"\r\n"
            else:
                idle_ticks += 1
                if idle_ticks > max_idle_ticks:
                    break
            await asyncio.sleep(0.05)

    return StreamingResponse(
        gen(),
        media_type=f"multipart/x-mixed-replace; boundary={boundary}",
        headers={"Cache-Control": "no-cache, no-store", "Pragma": "no-cache"},
    )


@router.post("/devices/{device_uid}/move", name="device_move")
def move_device(request: Request, device_uid: str, group_uid: str = Form("")):
    owner = current_username(request)
    try:
        device_service.move_to_group(device_uid, owner, group_uid or None)
    except DomainError as e:
        raise http_error_from_domain(e)
    return redirect_with_notice(f"/devices/{device_uid}", notice="Device moved")


@router.post("/devices/{device_uid}/capabilities", name="device_capabilities_update")
def update_device_capabilities(
    request: Request,
    device_uid: str,
    camera_bus: str = Form(...),
    camera_count: int = Form(...),
    camera_indices: str = Form(""),
):
    owner = current_username(request)
    try:
        device_service.update_capabilities(
            device_uid,
            owner,
            camera_bus=camera_bus,
            camera_count=camera_count,
            camera_indices=camera_indices,
        )
    except DomainError as e:
        raise http_error_from_domain(e)
    return redirect_with_notice(f"/devices/{device_uid}", notice="Hardware setup saved")


@router.post("/devices/{device_uid}/generation", name="device_generation_update")
def update_device_generation(
    request: Request,
    device_uid: str,
    generation_uid: str = Form(...),
):
    owner = current_username(request)
    try:
        device_service.update_generation(device_uid, owner, generation_uid)
    except DomainError as e:
        raise http_error_from_domain(e)
    return redirect_with_notice(
        f"/devices/{device_uid}",
        notice="Device generation updated — compatible apps list refreshed.",
    )


@router.post("/devices/{device_uid}/edit", name="device_update")
def update_device(
    request: Request,
    device_uid: str,
    device_name: str = Form(...),
    device_alias: str = Form(""),
    group_uid: str = Form(""),
    site_name: str = Form(""),
):
    owner = current_username(request)
    try:
        device_service.update(
            device_uid,
            owner,
            device_name=device_name,
            device_alias=device_alias or None,
            group_uid=group_uid or None,
            site_name=site_name or None,
        )
    except DomainError as e:
        raise http_error_from_domain(e)
    return redirect_with_notice(f"/devices/{device_uid}", notice="Device updated")


@router.post("/devices/{device_uid}/delete", name="device_delete")
def delete_device(request: Request, device_uid: str):
    owner = current_username(request)
    try:
        device_service.delete(device_uid, owner)
    except DomainError as e:
        raise http_error_from_domain(e)
    return RedirectResponse("/devices", status_code=303)


@router.get("/devices/{device_uid}/installer", name="device_installer")
def download_installer(request: Request, device_uid: str):
    owner = current_username(request)
    try:
        data = enrollment_service.build_installer(device_uid, owner)
    except DomainError as e:
        raise http_error_from_domain(e)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return StreamingResponse(
        iter([data]),
        media_type="application/gzip",
        headers={
            "Content-Disposition": f"attachment; filename=icicle-edge-agent-{device_uid}.tar.gz"
        },
    )
