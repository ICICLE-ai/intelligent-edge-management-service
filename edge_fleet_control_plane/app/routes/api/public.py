"""Read-only JSON API used by the web UI and external integrations."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.config import get_settings
from app.auth import current_username
from app.repositories import commands as commands_repo
from app.repositories import deployments as dep_repo
from app.repositories import devices as devices_repo
from app.repositories import generations as gen_repo
from app.services import deployment_service, device_service, enrollment_service, group_service, model_service, stream_service

router = APIRouter(prefix="/api", tags=["public"])


class ParseCommandRequest(BaseModel):
    command: str
    model_container_path: Optional[str] = None
    model_env_var: Optional[str] = None


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/device-generations")
def device_generations():
    return gen_repo.list_active()


@router.get("/devices")
def devices(request: Request):
    return device_service.list_for_owner(current_username(request))


@router.get("/devices/{device_uid}")
def device(request: Request, device_uid: str):
    return device_service.get(device_uid, current_username(request))


@router.get("/apps")
def apps(request: Request):
    return model_service.list_published(current_username(request))


@router.get("/models")
def models(request: Request):
    return model_service.list_published(current_username(request))


@router.post("/apps/parse-command")
def parse_command_apps(request: Request, payload: ParseCommandRequest):
    current_username(request)
    return model_service.parse_command_for_form(
        payload.command,
        model_container_path=payload.model_container_path,
        model_env_var=payload.model_env_var,
    )


@router.post("/models/parse-command")
def parse_command(request: Request, payload: ParseCommandRequest):
    """Parse a raw `docker run` command into structured fields the app
    publish form understands, and return a canonical re-rendered preview."""
    current_username(request)
    return model_service.parse_command_for_form(
        payload.command,
        model_container_path=payload.model_container_path,
        model_env_var=payload.model_env_var,
    )


@router.get("/apps/{model_card_uid}")
def app_detail_api(request: Request, model_card_uid: str):
    return model_service.get_full_for_user(current_username(request), model_card_uid)


@router.get("/models/{model_card_uid}")
def model(request: Request, model_card_uid: str):
    return model_service.get_full_for_user(current_username(request), model_card_uid)


@router.get("/deployments")
def deployments(request: Request, scope: str = "all"):
    owner = current_username(request)
    return deployment_service.list_for_owner(owner, active_only=(scope == "active"))


@router.get("/deployments/{deployment_uid}")
def deployment(request: Request, deployment_uid: str):
    return deployment_service.get_full(deployment_uid, current_username(request))


@router.get("/deployments/{deployment_uid}/poll")
def deployment_poll(request: Request, deployment_uid: str):
    """Lightweight poll payload for the deployment detail page."""
    return deployment_service.poll_snapshot(deployment_uid, current_username(request))


@router.get("/groups/{group_uid}/status")
def group_status(request: Request, group_uid: str):
    """Lightweight poll payload for the group detail page."""
    owner = current_username(request)
    group_service.get(group_uid, owner)
    devices = devices_repo.list_in_group(owner, group_uid)
    deployments = deployment_service.list_for_group(group_uid, owner)
    active_dep = deployment_service.get_active_group_deployment(group_uid, owner)
    media = get_settings().media
    if active_dep:
        stream_rows = active_dep.get("devices") or []
    else:
        stream_rows = [
            {"device_uid": d["device_uid"], "status": "—"}
            for d in devices
        ]
    inference_devices = []
    for row in stream_rows:
        entry = {
            "device_uid": row["device_uid"],
            "status": row["status"],
        }
        if media.mode == "mediamtx":
            entry["hls_url"] = media.hls_url(row["device_uid"])
        inference_devices.append(entry)
    return {
        "devices": [
            {
                "device_uid": d["device_uid"],
                "status": d["status"],
                "last_heartbeat_at": d.get("last_heartbeat_at"),
            }
            for d in devices
        ],
        "deployments": [
            {"deployment_uid": d["deployment_uid"], "status": d["status"]}
            for d in deployments
        ],
        "inference_devices": inference_devices,
    }


@router.get("/devices/{device_uid}/activity")
def device_activity(request: Request, device_uid: str):
    """Device status plus deployment/command rows for the device detail page."""
    owner = current_username(request)
    device = device_service.get(device_uid, owner)
    deployments = deployment_service.list_for_device(device_uid, owner)
    commands = commands_repo.list_for_device(device_uid, owner, limit=30)
    deployment_rows = []
    for dep in deployments:
        deployment_service.reconcile_deployment_status(dep["deployment_uid"])
        fresh = dep_repo.get(dep["deployment_uid"], owner) or dep
        device_rows = dep_repo.list_device_deployments(dep["deployment_uid"])
        deployment_rows.append(
            {
                "deployment_uid": dep["deployment_uid"],
                "status": deployment_service.effective_status(fresh["status"], device_rows),
            }
        )
    inference_devices = []
    active_dep = deployment_service.get_active_device_deployment(device_uid, owner)
    if active_dep:
        inference_devices = stream_service.inference_poll_devices(active_dep.get("devices") or [])
    enrollment = enrollment_service.latest_enrollment_for_device(device_uid)
    deployable = model_service.deployable_for_device(owner, device)
    compat_count = len(deployable["mine"]) + len(deployable["others"])
    setup_readiness = device_service.setup_readiness(
        device,
        enrollment=enrollment,
        compatible_app_count=compat_count,
    )
    return {
        "status": device["status"],
        "last_heartbeat_at": device.get("last_heartbeat_at"),
        "deployments": deployment_rows,
        "inference_devices": inference_devices,
        "setup_readiness": setup_readiness,
        "enrollment": {
            "installer_downloaded_at": (enrollment or {}).get("installer_downloaded_at"),
            "used_at": (enrollment or {}).get("used_at"),
        },
        "commands": [
            {
                "request_id": c["request_id"],
                "status": c["status"],
                "acked_at": c.get("acked_at"),
            }
            for c in commands
        ],
    }


@router.get("/operations/commands")
def operations_commands(request: Request, limit: int = 100):
    rows = commands_repo.list_for_owner(current_username(request), limit=limit)
    return [
        {
            "request_id": r["request_id"],
            "status": r["status"],
            "operation": r["operation"],
            "acked_at": r.get("acked_at"),
        }
        for r in rows
    ]
