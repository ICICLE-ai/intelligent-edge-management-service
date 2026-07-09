from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import current_username
from app.core.errors import DomainError
from app.core.templates import templates
from app.repositories import devices as devices_repo
from app.routes._helpers import http_error_from_domain, redirect_with_notice
from app.services import deployment_service, group_service, model_service, stream_service

router = APIRouter(tags=["web"])


@router.get("/groups", response_class=HTMLResponse, name="groups_list")
def list_groups(request: Request):
    owner = current_username(request)
    groups = group_service.list_for_owner(owner)
    return templates.TemplateResponse(
        "groups/list.html",
        {
            "request": request,
            "user": owner,
            "active_nav": "groups",
            "groups": groups,
        },
    )


@router.post("/groups", name="groups_create")
def create_group(
    request: Request,
    group_name: str = Form(...),
    site_name: str = Form(""),
    description: str = Form(""),
    color_tag: str = Form("indigo"),
):
    owner = current_username(request)
    try:
        group = group_service.create(
            owner=owner,
            group_name=group_name,
            description=description or None,
            site_name=site_name or None,
            color_tag=color_tag,
        )
    except DomainError as e:
        raise http_error_from_domain(e)
    return RedirectResponse(f"/groups/{group['group_uid']}", status_code=303)


@router.get("/groups/{group_uid}", response_class=HTMLResponse, name="group_detail")
def group_detail(request: Request, group_uid: str):
    owner = current_username(request)
    try:
        group = group_service.get(group_uid, owner)
    except DomainError as e:
        raise http_error_from_domain(e)
    devices = devices_repo.list_in_group(owner, group_uid)
    deployments = deployment_service.list_for_group(group_uid, owner)
    deployable = model_service.deployable_for_group_devices(owner, devices)
    active_dep = deployment_service.get_active_group_deployment(group_uid, owner)
    if active_dep:
        stream_devices = active_dep.get("devices") or []
    else:
        stream_devices = [
            {
                "device_uid": d["device_uid"],
                "device_name": d["device_name"],
                "status": "—",
            }
            for d in devices
        ]
    inference_streams = stream_service.inference_streams_context(
        stream_devices,
        card=(active_dep or {}).get("card"),
    )
    return templates.TemplateResponse(
        "groups/detail.html",
        {
            "request": request,
            "user": owner,
            "active_nav": "groups",
            "group": group,
            "devices": devices,
            "deployments": deployments,
            "my_apps": deployable["mine"],
            "other_apps": deployable["others"],
            "active_deployment": active_dep,
            "inference_streams": inference_streams,
            "inference_stream_title": (
                f"Live feeds · {active_dep['model_display_name']}"
                if active_dep else "Group live feeds"
            ),
            "inference_stream_subtitle": (
                "CCTV view of annotated inference from the active group deployment."
                if active_dep else "Deploy an app to this group to see live inference feeds."
            ),
        },
    )


@router.post("/groups/{group_uid}/edit", name="group_update")
def update_group(
    request: Request,
    group_uid: str,
    group_name: str = Form(...),
    site_name: str = Form(""),
    description: str = Form(""),
    color_tag: str = Form("indigo"),
):
    owner = current_username(request)
    try:
        group_service.update(
            group_uid,
            owner,
            group_name=group_name,
            description=description or None,
            site_name=site_name or None,
            color_tag=color_tag,
        )
    except DomainError as e:
        raise http_error_from_domain(e)
    return redirect_with_notice(f"/groups/{group_uid}", notice="Group updated")


@router.post("/groups/{group_uid}/delete", name="group_delete")
def delete_group(request: Request, group_uid: str):
    owner = current_username(request)
    try:
        group_service.delete(group_uid, owner)
    except DomainError as e:
        raise http_error_from_domain(e)
    return RedirectResponse("/groups", status_code=303)
