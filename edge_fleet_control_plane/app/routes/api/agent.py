"""HTTP API used by the on-device agent (enrollment, heartbeat, ack)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.auth import authenticate_device
from app.core.errors import DomainError
from app.repositories import commands as commands_repo
from app.repositories import deployments as dep_repo
from app.services import (
    deployment_service,
    enrollment_service,
    event_service,
    heartbeat_service,
    provenance_service,
)

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
            "operation": "deploy_model" | "stop_deployment" | "restart_deployment" | "recover_deployment",
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
    skip_mark_acked = False
    if not cmd:
        deployment_uid = payload.get("deployment_uid")
        operation = payload.get("operation")
        if operation == "recover_deployment" and deployment_uid:
            cmd = commands_repo.get_latest_for_deployment(deployment_uid, "deploy_model")
            skip_mark_acked = True
        elif deployment_uid and operation:
            cmd = commands_repo.get_latest_for_deployment(deployment_uid, operation)
            if cmd:
                request_id = cmd["request_id"]
                payload = {**payload, "request_id": request_id}
    if not cmd and payload.get("operation") != "recover_deployment":
        raise HTTPException(
            status_code=404,
            detail=f"Unknown request_id (no matching command for deployment={payload.get('deployment_uid')!r})",
        )
    if cmd and not skip_mark_acked:
        commands_repo.mark_acked(request_id, payload)

    deployment_uid = payload.get("deployment_uid") or (cmd or {}).get("deployment_uid")
    device_uid = payload.get("device_id")
    op = payload.get("operation") or (cmd or {}).get("operation")
    status = (payload.get("status") or "").upper()
    error = payload.get("error")

    superseded_by = None
    if cmd and deployment_uid and device_uid and op != "recover_deployment":
        latest = commands_repo.get_latest_lifecycle_for_device(deployment_uid, device_uid)
        if latest and latest["request_id"] != cmd["request_id"]:
            superseded_by = latest

    def ignore_superseded() -> dict:
        event_service.record(
            "AGENT_ACK_IGNORED_SUPERSEDED",
            "Ignored %s %s ack for %s; newer %s %s was sent"
            % (op, status, request_id, superseded_by["operation"], superseded_by["request_id"]),
            severity="WARN",
            owner=(cmd or {}).get("owner_tapis_username"),
            device_uid=device_uid,
            deployment_uid=deployment_uid,
            payload=payload,
        )
        return {"ok": True, "ignored": "superseded"}

    if deployment_uid and device_uid:
        if op in {"deploy_model", "restart_deployment", "recover_deployment"}:
            current = dep_repo.get_any(deployment_uid) or {}
            if (
                op != "restart_deployment"
                and (current.get("status") or "").upper() in dep_repo.SETTLED_STOP_STATUSES
            ):
                event_service.record(
                    "AGENT_ACK_IGNORED_STOPPED",
                    "Ignored %s %s ack; deployment is %s" % (op, status, current.get("status")),
                    severity="WARN",
                    owner=(cmd or {}).get("owner_tapis_username") or current.get("owner_tapis_username"),
                    device_uid=device_uid,
                    deployment_uid=deployment_uid,
                    payload=payload,
                )
                return {"ok": True, "ignored": "stopped"}
            if superseded_by:
                return ignore_superseded()
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
                error="" if mapped == "RUNNING" else error,
            )
            if mapped == "RUNNING":
                dep_repo.update_status(deployment_uid, "RUNNING")
                declared_cmd = cmd
                if op in {"restart_deployment", "recover_deployment"}:
                    declared_cmd = (
                        commands_repo.get_latest_for_deployment(
                            deployment_uid, "deploy_model"
                        )
                        or cmd
                    )
                if declared_cmd:
                    evaluation = provenance_service.evaluate(declared_cmd, payload)
                    observed = evaluation["observed"]
                    dep_repo.update_observed_provenance(
                        deployment_uid,
                        device_uid,
                        artifact_sha256=observed.get("artifact_sha256"),
                        artifact_bytes=observed.get("artifact_bytes"),
                        image_id=observed.get("image_id"),
                        image_digest=observed.get("image_digest"),
                        image=observed.get("image"),
                        status=evaluation["status"],
                        detail="; ".join(evaluation["reasons"]) or None,
                    )
            elif mapped == "FAILED":
                dep_repo.update_status(deployment_uid, "FAILED")
        elif op == "stop_deployment":
            if superseded_by:
                return ignore_superseded()
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

    owner = (cmd or {}).get("owner_tapis_username")
    if not owner and deployment_uid:
        dep = dep_repo.get_any(deployment_uid)
        owner = (dep or {}).get("owner_tapis_username")
    event_service.record(
        f"AGENT_ACK_{op or 'unknown'}_{status or 'unknown'}".upper(),
        f"Agent ack: {op} → {status} ({error or 'ok'})",
        severity="ERROR" if status == "FAILED" else "INFO",
        owner=owner,
        device_uid=device_uid,
        deployment_uid=deployment_uid,
        payload=payload,
    )
    return {"ok": True}
