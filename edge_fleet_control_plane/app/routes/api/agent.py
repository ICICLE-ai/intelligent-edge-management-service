"""HTTP API used by the on-device agent (enrollment, heartbeat, ack)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.auth import authenticate_device
from app.core.errors import DomainError
from app.repositories import commands as commands_repo
from app.repositories import deployments as dep_repo
from app.services import deployment_service, enrollment_service, event_service, heartbeat_service

router = APIRouter(prefix="/api/agent", tags=["agent"])


@router.post("/enroll")
def enroll(payload: dict):
    try:
        return enrollment_service.enroll(payload)
    except DomainError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.post("/heartbeat")
def heartbeat(request: Request, payload: dict):
    device_uid = payload.get("device_id") or payload.get("device_uid")
    if not device_uid:
        raise HTTPException(400, "Missing device_id")
    try:
        authenticate_device(request, device_uid)
        return heartbeat_service.record_heartbeat(payload)
    except DomainError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.post("/ack")
def ack(request: Request, payload: dict):
    """Agents call this when a command finishes — success or failure.

    Expected payload:
        {
            "device_id": "...",
            "request_id": "...",
            "deployment_uid": "...",   # optional, for deploy/stop ops
            "operation": "deploy_model" | "stop_deployment" | "restart_deployment",
            "status": "RUNNING" | "STOPPED" | "FAILED",
            "container_id": "...",
            "container_name": "...",
            "error": "..."
        }
    """
    request_id = payload.get("request_id")
    if not request_id:
        raise HTTPException(400, "Missing request_id")
    device_uid = payload.get("device_id")
    if not device_uid:
        raise HTTPException(400, "Missing device_id")
    try:
        authenticate_device(request, device_uid)
    except DomainError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    cmd = commands_repo.get_by_request_id(request_id)
    if not cmd:
        deployment_uid = payload.get("deployment_uid")
        operation = payload.get("operation")
        if deployment_uid and operation:
            cmd = commands_repo.get_latest_for_deployment(deployment_uid, operation)
            if cmd:
                request_id = cmd["request_id"]
                payload = {**payload, "request_id": request_id}
    if not cmd:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown request_id (no matching command for deployment={payload.get('deployment_uid')!r})",
        )
    commands_repo.mark_acked(request_id, payload)

    deployment_uid = payload.get("deployment_uid") or cmd.get("deployment_uid")
    device_uid = payload.get("device_id")
    op = payload.get("operation") or cmd.get("operation")
    status = (payload.get("status") or "").upper()
    error = payload.get("error")

    if deployment_uid and device_uid:
        if op in {"deploy_model", "restart_deployment"}:
            mapped = {
                "RUNNING": "RUNNING",
                "STARTED": "RUNNING",
                "FAILED": "FAILED",
                "DOWNLOADING": "DOWNLOADING",
                "PULLING": "PULLING",
                "STARTING": "STARTING",
            }.get(status, status or "RUNNING")
            dep_repo.update_device_deployment_status(
                deployment_uid,
                device_uid,
                status=mapped,
                container_id=payload.get("container_id"),
                container_name=payload.get("container_name"),
                error=error,
            )
            if mapped == "RUNNING":
                dep_repo.update_status(deployment_uid, "RUNNING")
            elif mapped == "FAILED":
                dep_repo.update_status(deployment_uid, "FAILED")
        elif op == "stop_deployment":
            mapped = "STOPPED" if status in {"STOPPED", "OK", "SUCCESS"} else "FAILED" if status == "FAILED" else status
            dep_repo.update_device_deployment_status(
                deployment_uid, device_uid, status=mapped, error=error
            )
            if mapped == "STOPPED":
                rows = dep_repo.list_device_deployments(deployment_uid)
                if rows and all(r["status"] in {"STOPPED", "FAILED"} for r in rows):
                    dep_repo.update_status(deployment_uid, "STOPPED")
                elif not rows:
                    dep_repo.update_status(deployment_uid, "STOPPED")
                else:
                    deployment_service.reconcile_deployment_status(deployment_uid)

    event_service.record(
        f"AGENT_ACK_{op or 'unknown'}_{status or 'unknown'}".upper(),
        f"Agent ack: {op} → {status} ({error or 'ok'})",
        severity="ERROR" if status == "FAILED" else "INFO",
        owner=cmd["owner_tapis_username"],
        device_uid=device_uid,
        deployment_uid=deployment_uid,
        payload=payload,
    )
    return {"ok": True}
