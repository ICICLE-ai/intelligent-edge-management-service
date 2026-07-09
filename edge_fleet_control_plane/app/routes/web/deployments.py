from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import current_username
from app.core.errors import DomainError
from app.core.templates import templates
from app.routes._helpers import http_error_from_domain, redirect_with_notice
from app.services import deployment_service, stream_service

router = APIRouter(tags=["web"])


@router.get("/deployments", response_class=HTMLResponse, name="deployments_list")
def list_deployments(request: Request, scope: str = "all"):
    owner = current_username(request)
    active_only = scope == "active"
    deployments = deployment_service.list_for_owner(owner, active_only=active_only)
    return templates.TemplateResponse(
        "deployments/list.html",
        {
            "request": request,
            "user": owner,
            "active_nav": "deployments_active" if active_only else "deployments_history",
            "deployments": deployments,
            "scope": scope,
        },
    )


@router.get("/deployments/{deployment_uid}", response_class=HTMLResponse, name="deployment_detail")
def deployment_detail(request: Request, deployment_uid: str):
    owner = current_username(request)
    try:
        dep = deployment_service.get_full(deployment_uid, owner)
    except DomainError as e:
        raise http_error_from_domain(e)
    inference_streams = stream_service.inference_streams_context(
        dep.get("devices") or [],
        card=dep.get("card"),
    )
    return templates.TemplateResponse(
        "deployments/detail.html",
        {
            "request": request,
            "user": owner,
            "active_nav": "deployments_active",
            "deployment": dep,
            "inference_streams": inference_streams,
            "inference_stream_title": "Inference live stream",
            "inference_stream_subtitle": "Annotated model output from running containers on this deployment.",
        },
    )


@router.post("/deployments", name="deployments_create")
def create_deployment(
    request: Request,
    model_card_uid: str = Form(...),
    target_type: str = Form(...),
    target_uid: str = Form(...),
    notes: str = Form(""),
    redirect_to: str = Form(""),
):
    owner = current_username(request)
    try:
        dep = deployment_service.create(
            owner=owner,
            model_card_uid=model_card_uid,
            target_type=target_type,
            target_uid=target_uid,
            notes=notes or None,
        )
    except DomainError as e:
        raise http_error_from_domain(e)
    if redirect_to:
        return redirect_with_notice(redirect_to, notice="Deployment dispatched")
    return RedirectResponse(f"/deployments/{dep['deployment_uid']}", status_code=303)


@router.post("/deployments/{deployment_uid}/stop", name="deployment_stop")
def stop_deployment(request: Request, deployment_uid: str, purge: str = Form(""),
                    redirect_to: str = Form("")):
    owner = current_username(request)
    try:
        deployment_service.stop(owner=owner, deployment_uid=deployment_uid, purge=(purge == "on"))
    except DomainError as e:
        raise http_error_from_domain(e)
    target = redirect_to or f"/deployments/{deployment_uid}"
    notice = "Purge command dispatched" if purge == "on" else "Stop command dispatched"
    return redirect_with_notice(target, notice=notice)


@router.post("/deployments/{deployment_uid}/restart", name="deployment_restart")
def restart_deployment(request: Request, deployment_uid: str, redirect_to: str = Form("")):
    owner = current_username(request)
    try:
        deployment_service.restart(owner=owner, deployment_uid=deployment_uid)
    except DomainError as e:
        raise http_error_from_domain(e)
    target = redirect_to or f"/deployments/{deployment_uid}"
    return redirect_with_notice(target, notice="Restart command dispatched")


@router.post("/deployments/{deployment_uid}/dismiss", name="deployment_dismiss")
def dismiss_deployment(request: Request, deployment_uid: str, redirect_to: str = Form("")):
    owner = current_username(request)
    try:
        deployment_service.dismiss(owner=owner, deployment_uid=deployment_uid)
    except DomainError as e:
        raise http_error_from_domain(e)
    target = redirect_to or f"/deployments/{deployment_uid}"
    return redirect_with_notice(target, notice="Deployment dismissed")


@router.post("/deployments/{deployment_uid}/cancel", name="deployment_cancel")
def cancel_deployment(request: Request, deployment_uid: str, redirect_to: str = Form("")):
    owner = current_username(request)
    try:
        deployment_service.cancel(owner=owner, deployment_uid=deployment_uid)
    except DomainError as e:
        raise http_error_from_domain(e)
    target = redirect_to or "/deployments?scope=active"
    return redirect_with_notice(target, notice="Deployment deleted")


@router.post("/deployments/{deployment_uid}/delete", name="deployment_delete")
def delete_deployment(request: Request, deployment_uid: str, redirect_to: str = Form("")):
    return cancel_deployment(request, deployment_uid, redirect_to)


@router.post("/deployments/{deployment_uid}/redispatch", name="deployment_redispatch")
def redispatch_deployment(request: Request, deployment_uid: str, redirect_to: str = Form("")):
    owner = current_username(request)
    target = redirect_to or f"/deployments/{deployment_uid}"
    try:
        deployment_service.redispatch(owner=owner, deployment_uid=deployment_uid)
    except DomainError as e:
        return redirect_with_notice(target, notice=str(e), level="error")
    return redirect_with_notice(target, notice="Deployment re-dispatched")
