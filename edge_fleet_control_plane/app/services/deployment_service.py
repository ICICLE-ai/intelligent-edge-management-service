"""Deployment service — the core of the simplified two-interface design.

A deployment is the operator's intent to run a *model card* on a *device or
group*. The service:

1. Validates the model card and target.
2. Creates a `deployments` row plus per-device `device_deployments` rows
   (so the UI can later show per-device delivery status).
3. Builds a *self-contained* `deploy_model` payload — no manifest lookup is
   required on the agent.
4. Records the payload as a `device_commands` row and publishes it via MQTT.
5. Provides `stop_deployment` (remove container by default, optional full purge)
   and `restart_deployment` (new container from cached image + artifacts).
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from app.config import get_settings
from app.core.errors import ConflictError, DomainError, NotFoundError, ValidationError
from app.core.ids import gen_request_id, gen_uid
from app.core.logging import get_logger
from app.core.time import now_iso
from app.repositories import commands as commands_repo
from app.repositories import deployments as dep_repo
from app.repositories import devices as devices_repo
from app.repositories import groups as groups_repo
from app.repositories import model_cards as mc_repo
from app.repositories import mqtt_audit as audit_repo
from app.services import device_capabilities, event_service, mqtt_service

log = get_logger("deployment")


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------

def _resolve_patra_download_url(patra_uuid: str) -> Optional[str]:
    """Best-effort resolution of a Patra UUID to a download URL.

    The agent can do this on its own as well. We pre-resolve here so the
    operator can see what the device will fetch.
    """
    settings = get_settings()
    base = settings.patra_base_url
    if not base or not patra_uuid:
        return None
    return f"{base}/modelcard/{patra_uuid}/download_url"


def build_deploy_payload(
    *,
    deployment_uid: str,
    request_id: str,
    card: dict,
    device_uid: Optional[str] = None,
    device: Optional[dict] = None,
) -> Dict[str, Any]:
    """Assemble the self-contained deploy_model MQTT payload from a card aggregate."""
    artifact = card["artifact"]
    spec = card["spec"]

    container = {
        "image": _docker_image_ref(spec),
        "image_repository": spec["image_repository"],
        "image_tag": spec.get("image_tag") or "latest",
        "image_digest": spec.get("image_digest"),
        "container_name": spec["container_name"],
        "pull_policy": spec.get("pull_policy") or "if_not_present",
        "remove_after_exit": bool(spec.get("remove_after_exit")),
        "restart_policy": spec.get("restart_policy") or "no",
        "network_mode": spec.get("network_mode"),
        "gpus": spec.get("gpus"),
        "runtime": spec.get("runtime"),
        "privileged": bool(spec.get("privileged")),
        "ipc_mode": spec.get("ipc_mode"),
        "shm_size": spec.get("shm_size"),
        "working_dir": spec.get("working_dir"),
        "entrypoint": _decode_json_list(spec.get("entrypoint_json")),
        "command": _decode_json_list(spec.get("command_json")),
    }

    env = [
        {"key": e["var_key"], "value": e["var_value"], "is_secret": bool(e.get("is_secret"))}
        for e in spec.get("env", [])
    ]
    if device_uid:
        if device is None:
            device = devices_repo.get_any(device_uid)
        env = _append_media_env(env, device_uid, device, card)
        mounts = _merge_capability_mounts(spec.get("mounts", []), device)
    else:
        mounts = _merge_capability_mounts(spec.get("mounts", []), None)

    payload = {
        "operation": "deploy_model",
        "request_id": request_id,
        "deployment_uid": deployment_uid,
        "issued_at": now_iso(),
        "model": {
            "app_id": card.get("app_id"),
            "model_card_uid": card["model_card_uid"],
            "slug": card["slug"],
            "display_name": card["display_name"],
            "version": card["version"],
            "task_type": card.get("task_type"),
            "framework": card.get("framework"),
        },
        "artifact": {
            "artifact_uid": artifact["artifact_uid"],
            "filename": artifact["filename"],
            "container_path": artifact["container_path"],
            "source_type": artifact["source_type"],
            "patra_model_card_uuid": artifact.get("patra_model_card_uuid"),
            "download_url": artifact.get("download_url"),
            "sha256": artifact.get("sha256"),
            "size_bytes": artifact.get("size_bytes"),
            "content_type": artifact.get("content_type"),
            "patra_resolve_url": _resolve_patra_download_url(artifact.get("patra_model_card_uuid") or ""),
        },
        "container": container,
        "runtime": {
            "model_env_var": spec.get("model_env_var"),
            "environment": env,
            "mounts": mounts,
            "docker_args": [a["arg"] for a in spec.get("docker_args", [])],
            "ports": [
                {
                    "host_port": p.get("host_port"),
                    "container_port": p["container_port"],
                    "protocol": p.get("protocol") or "tcp",
                }
                for p in spec.get("ports", [])
            ],
        },
    }
    return payload


def build_stop_payload(*, deployment_uid: str, request_id: str, deployment: dict,
                       card: dict, purge: bool = False) -> Dict[str, Any]:
    spec = card["spec"]
    artifact = card["artifact"]
    return {
        "operation": "stop_deployment",
        "request_id": request_id,
        "deployment_uid": deployment_uid,
        "issued_at": now_iso(),
        "container_name": spec["container_name"],
        "image": _docker_image_ref(spec),
        "image_repository": spec["image_repository"],
        "image_tag": spec.get("image_tag") or "latest",
        "artifact_filename": artifact["filename"],
        "purge": purge,
    }


def build_restart_payload(*, deployment_uid: str, request_id: str, card: dict) -> Dict[str, Any]:
    spec = card["spec"]
    return {
        "operation": "restart_deployment",
        "request_id": request_id,
        "deployment_uid": deployment_uid,
        "issued_at": now_iso(),
        "container_name": spec["container_name"],
    }


def _is_multi_camera_app(card: dict) -> bool:
    tags = {t.lower() for t in (card.get("tags") or [])}
    return "multi-camera" in tags


def _merge_capability_mounts(spec_mounts: List[dict], device: Optional[dict]) -> List[dict]:
    """Add host bind mounts from device capabilities (e.g. /opt/MVS)."""
    out = [
        {
            "source": m["source"],
            "target": m["target"],
            "style": m["mount_style"],
            "type": m["mount_type"],
            "mode": m.get("mode"),
        }
        for m in spec_mounts
    ]
    if not device:
        return out
    caps = device.get("capabilities") or device_capabilities.parse(device.get("capabilities_json"))
    targets = {(m.get("target") or "").rstrip("/") for m in out}
    for source, mode in (caps.get("host_mounts") or {}).items():
        source = str(source).strip()
        if not source:
            continue
        if source in {m.get("source") for m in out}:
            continue
        target = source
        if target in targets:
            continue
        out.append({
            "source": source,
            "target": target,
            "style": "volume",
            "type": "bind",
            "mode": mode or "ro",
        })
        targets.add(target.rstrip("/"))
    return out


def _append_media_env(
    env: List[dict],
    device_uid: str,
    device: Optional[dict],
    card: dict,
) -> List[dict]:
    """Inject RTSPS ingest URL(s) when MediaMTX mode is active."""
    media = get_settings().media
    if media.mode != "mediamtx":
        return env
    keys = {e["key"] for e in env}
    out = list(env)
    caps = device_capabilities.parse(
        (device or {}).get("capabilities_json") if device else None
    )
    if device and device.get("capabilities"):
        caps = device["capabilities"]
    camera_count = max(1, int(caps.get("camera_count") or 1))
    multi = _is_multi_camera_app(card) and camera_count > 1

    if multi:
        for i in range(camera_count):
            key = "STREAM_INGEST_URL_%d" % i
            if key not in keys:
                out.append({
                    "key": key,
                    "value": media.rtsp_ingest_url(device_uid, i),
                    "is_secret": False,
                })
        if "CAMERA_INDICES" not in keys:
            indices = caps.get("camera_indices") or list(range(camera_count))
            out.append({
                "key": "CAMERA_INDICES",
                "value": ",".join(str(x) for x in indices[:camera_count]),
                "is_secret": False,
            })
    else:
        if "STREAM_INGEST_URL" not in keys:
            out.append({
                "key": "STREAM_INGEST_URL",
                "value": media.rtsp_ingest_url(device_uid),
                "is_secret": False,
            })
    return out


def _dispatch_deploy(
    *,
    owner: str,
    deployment_uid: str,
    card: dict,
    target_type: str,
    target_uid: str,
    target_devices: List[dict],
    request_id: str,
    payload_builder,
) -> tuple[str, Optional[str], Optional[str]]:
    """Publish deploy_model to device topic(s).

    Group deploys publish once per device so each container gets its own
    ``STREAM_INGEST_URL`` (``cam-{device_uid}`` on the ingest pod).
    """
    results: List[tuple[str, Optional[str], Optional[str]]] = []
    if target_type == "DEVICE":
        targets = [(target_uid, request_id)]
    else:
        targets = [(d["device_uid"], gen_request_id()) for d in target_devices]

    for device_uid, cmd_request_id in targets:
        payload = payload_builder(device_uid)
        payload["request_id"] = cmd_request_id
        results.append(
            _publish_command(
                owner=owner,
                dep={"target_type": "DEVICE", "target_uid": device_uid},
                deployment_uid=deployment_uid,
                operation="deploy_model",
                request_id=cmd_request_id,
                payload=payload,
            )
        )

    statuses = [r[0] for r in results]
    errs = [r[1] for r in results if r[1]]
    topic = results[-1][2] if results else None
    if any(s == "MQTT_SENT" for s in statuses):
        status = "MQTT_SENT"
    elif all(s == "MQTT_FAILED" for s in statuses):
        status = "MQTT_FAILED"
    else:
        status = statuses[0] if statuses else "RECORDED"
    err = "; ".join(errs) if errs else None
    return status, err, topic


_ACTIVE_DEPLOYMENT_STATUSES = {
    "RUNNING", "STARTING", "DELIVERING", "DOWNLOADING", "PULLING", "STOPPING",
}


def get_active_group_deployment(group_uid: str, owner: str) -> Optional[dict]:
    """Most recent non-settled deployment for a group, with per-device rows."""
    for dep in dep_repo.list_for_group(group_uid, owner):
        reconcile_deployment_status(dep["deployment_uid"])
        devices = dep_repo.list_device_deployments(dep["deployment_uid"])
        status = effective_status(dep["status"], devices)
        if status in _ACTIVE_DEPLOYMENT_STATUSES:
            full = get_full(dep["deployment_uid"], owner)
            full["effective_status"] = status
            return full
    return None


def get_active_device_deployment(device_uid: str, owner: str) -> Optional[dict]:
    """Most recent non-settled deployment targeting this device."""
    for dep in dep_repo.list_for_device(device_uid, owner):
        reconcile_deployment_status(dep["deployment_uid"])
        devices = dep_repo.list_device_deployments(dep["deployment_uid"])
        status = effective_status(dep["status"], devices)
        if status in _ACTIVE_DEPLOYMENT_STATUSES:
            full = get_full(dep["deployment_uid"], owner)
            full["effective_status"] = status
            full["devices"] = [r for r in full["devices"] if r["device_uid"] == device_uid]
            return full
    return None


def _publish_command(
    *,
    owner: str,
    dep: dict,
    deployment_uid: str,
    operation: str,
    request_id: str,
    payload: Dict[str, Any],
) -> tuple[str, Optional[str], Optional[str]]:
    """Record the command first, then publish via MQTT.

    The agent can ACK within milliseconds of receiving MQTT — the row must
    exist before the broker delivers the message.
    """
    topic = mqtt_service.topic_for(dep["target_type"], dep["target_uid"])
    settings = get_settings()
    device_uid = dep["target_uid"] if dep["target_type"] == "DEVICE" else None
    commands_repo.insert(
        command_uid=gen_uid("cmd"),
        owner=owner,
        deployment_uid=deployment_uid,
        target_type=dep["target_type"],
        target_uid=dep["target_uid"],
        device_uid=device_uid,
        operation=operation,
        request_id=request_id,
        topic=topic,
        payload=payload,
        status="RECORDED",
    )
    audit_repo.insert(
        owner=owner,
        topic=topic,
        direction="OUTBOUND",
        request_id=request_id,
        device_uid=device_uid,
        payload=payload,
    )
    status = "RECORDED"
    err: Optional[str] = None
    sent_at: Optional[str] = None
    if settings.mqtt.enabled:
        try:
            mqtt_service.publish(topic, payload)
            status = "MQTT_SENT"
            sent_at = now_iso()
            commands_repo.update_send_status(request_id, status=status, sent_at=sent_at)
        except Exception as e:
            log.exception("MQTT publish failed: %s", e)
            status = "MQTT_FAILED"
            err = str(e)
            commands_repo.update_send_status(request_id, status=status, error=err)
    return status, err, topic


# ---------------------------------------------------------------------------
# Create / stop
# ---------------------------------------------------------------------------

def create(*, owner: str, model_card_uid: str, target_type: str, target_uid: str,
           notes: Optional[str] = None) -> dict:
    target_type = target_type.upper()
    if target_type not in {"DEVICE", "GROUP"}:
        raise ValidationError("target_type must be DEVICE or GROUP.")

    card = mc_repo.get_full(model_card_uid)
    if not card:
        raise NotFoundError("App not found")
    if card["status"] != "PUBLISHED":
        raise ValidationError("Only published apps can be deployed.")
    if not card.get("artifact") or not card.get("spec"):
        raise ValidationError("App is missing artifact or container spec.")

    from app.services import model_service
    if not model_service.can_deploy(owner, card):
        raise ValidationError("You don't have permission to deploy this app.")

    target_devices, target_name = _resolve_target(owner, target_type, target_uid)
    if not target_devices:
        raise ValidationError("Target has no devices.")

    _assert_compatibility(card, target_devices)

    deployment_uid = gen_uid("dpl")
    request_id = gen_request_id()
    dep_repo.create(
        deployment_uid=deployment_uid,
        owner=owner,
        model_card_uid=card["model_card_uid"],
        artifact_uid=card["artifact"]["artifact_uid"],
        spec_uid=card["spec"]["spec_uid"],
        target_type=target_type,
        target_uid=target_uid,
        target_name=target_name,
        request_id=request_id,
        notes=notes,
    )
    for d in target_devices:
        dep_repo.create_device_deployment(
            device_deployment_uid=gen_uid("dd"),
            deployment_uid=deployment_uid,
            device_uid=d["device_uid"],
        )

    status, err, topic = _dispatch_deploy(
        owner=owner,
        deployment_uid=deployment_uid,
        card=card,
        target_type=target_type,
        target_uid=target_uid,
        target_devices=target_devices,
        request_id=request_id,
        payload_builder=lambda device_uid: build_deploy_payload(
            deployment_uid=deployment_uid,
            request_id=request_id,
            card=card,
            device_uid=device_uid,
            device=devices_repo.get_any(device_uid),
        ),
    )
    if status == "MQTT_SENT":
        dep_repo.update_status(deployment_uid, "DELIVERING")
        event_service.record(
            "DEPLOYMENT_DISPATCHED",
            f"Deploy '{card['display_name']}' → {target_type.lower()} '{target_name}'",
            owner=owner,
            deployment_uid=deployment_uid,
            payload={"request_id": request_id, "topic": topic},
        )
    else:
        event_service.record(
            "DEPLOYMENT_RECORDED" if status == "RECORDED" else "DEPLOYMENT_PUBLISH_FAILED",
            f"Deploy '{card['display_name']}' → {target_type.lower()} '{target_name}'"
            + (f" — MQTT error: {err}" if err else " — MQTT disabled, payload recorded"),
            severity="WARN" if status == "MQTT_FAILED" else "INFO",
            owner=owner,
            deployment_uid=deployment_uid,
            payload={"request_id": request_id, "topic": topic, "error": err},
        )
    return get_full(deployment_uid, owner)


def redispatch(*, owner: str, deployment_uid: str) -> dict:
    """Rebuild the deploy_model payload and re-publish it.

    Useful when the original publish failed (e.g. broker outage, control
    plane misconfigured at the time the user clicked Deploy) and the
    deployment is stuck in PENDING. We mint a fresh request_id so any
    later ACKs are unambiguous.
    """
    dep = dep_repo.get(deployment_uid, owner)
    if not dep:
        raise NotFoundError("Deployment not found")
    if dep["status"] in {"STOPPED", "FAILED", "CANCELLED", "RUNNING", "STOPPING"}:
        raise ConflictError("Deployment is already terminal — create a new one.")

    card = mc_repo.get_full(dep["model_card_uid"])
    if not card:
        raise NotFoundError("Underlying model card is missing")
    if card["status"] != "PUBLISHED":
        raise ValidationError("Underlying model card is no longer published.")
    if not card.get("artifact") or not card.get("spec"):
        raise ValidationError("Model card is missing artifact or container spec.")

    request_id = gen_request_id()
    target_devices, _ = _resolve_target(owner, dep["target_type"], dep["target_uid"])
    status, err, topic = _dispatch_deploy(
        owner=owner,
        deployment_uid=deployment_uid,
        card=card,
        target_type=dep["target_type"],
        target_uid=dep["target_uid"],
        target_devices=target_devices,
        request_id=request_id,
        payload_builder=lambda device_uid: build_deploy_payload(
            deployment_uid=deployment_uid,
            request_id=request_id,
            card=card,
            device_uid=device_uid,
            device=devices_repo.get_any(device_uid),
        ),
    )
    if status == "MQTT_SENT":
        dep_repo.update_status(deployment_uid, "DELIVERING")
        event_service.record(
            "DEPLOYMENT_REDISPATCHED",
            f"Re-dispatched '{card['display_name']}' → {dep['target_type'].lower()} '{dep['target_name']}'",
            owner=owner,
            deployment_uid=deployment_uid,
            payload={"request_id": request_id, "topic": topic},
        )
    else:
        event_service.record(
            "DEPLOYMENT_REDISPATCH_FAILED",
            f"Re-dispatch failed for '{card['display_name']}' — {err or 'MQTT disabled'}",
            severity="WARN" if status == "MQTT_FAILED" else "INFO",
            owner=owner,
            deployment_uid=deployment_uid,
            payload={"request_id": request_id, "topic": topic, "error": err},
        )
    return get_full(deployment_uid, owner)


def stop(*, owner: str, deployment_uid: str, purge: bool = False) -> dict:
    dep = dep_repo.get(deployment_uid, owner)
    if not dep:
        raise NotFoundError("Deployment not found")
    reconcile_deployment_status(deployment_uid)
    dep = dep_repo.get(deployment_uid, owner)
    if dep["status"] in {"STOPPED", "FAILED"}:
        if purge:
            pass  # purge-only cleanup of a stopped deployment
        else:
            raise ConflictError("Deployment is already stopped.")

    card = mc_repo.get_full(dep["model_card_uid"])
    if not card:
        raise NotFoundError("Underlying model card is missing")

    request_id = gen_request_id()
    payload = build_stop_payload(
        deployment_uid=deployment_uid,
        request_id=request_id,
        deployment=dep,
        card=card,
        purge=purge,
    )
    status, err, topic = _publish_command(
        owner=owner,
        dep=dep,
        deployment_uid=deployment_uid,
        operation="stop_deployment",
        request_id=request_id,
        payload=payload,
    )
    if status == "MQTT_SENT":
        if dep["status"] not in {"STOPPED", "FAILED"}:
            dep_repo.update_status(deployment_uid, "STOPPING")
            for dd in dep_repo.list_device_deployments(deployment_uid):
                if dd["status"] not in {"STOPPED", "FAILED"}:
                    dep_repo.update_device_deployment_status(
                        deployment_uid, dd["device_uid"], status="STOPPING"
                    )
        event_name = "DEPLOYMENT_PURGE_DISPATCHED" if purge else "DEPLOYMENT_STOP_DISPATCHED"
        event_msg = (
            f"Purge '{card['display_name']}' sent"
            if purge
            else f"Stop '{card['display_name']}' sent"
        )
        event_service.record(
            event_name,
            event_msg,
            owner=owner,
            deployment_uid=deployment_uid,
            payload={"request_id": request_id, "purge": purge},
        )
    else:
        event_service.record(
            "DEPLOYMENT_STOP_RECORDED" if status == "RECORDED" else "DEPLOYMENT_STOP_FAILED",
            f"Stop '{card['display_name']}' — {err or 'MQTT disabled'}",
            severity="WARN" if status == "MQTT_FAILED" else "INFO",
            owner=owner,
            deployment_uid=deployment_uid,
            payload={"request_id": request_id, "topic": topic, "error": err},
        )
    return get_full(deployment_uid, owner)


def dismiss(*, owner: str, deployment_uid: str) -> dict:
    """Mark a stuck deployment stopped in the control plane without waiting for the agent."""
    dep = dep_repo.get(deployment_uid, owner)
    if not dep:
        raise NotFoundError("Deployment not found")
    if dep["status"] in {"STOPPED", "FAILED", "CANCELLED"}:
        raise ConflictError("Deployment is already settled.")
    if dep["status"] == "RUNNING":
        raise ConflictError("Use Stop to request the agent to remove the container.")

    for dd in dep_repo.list_device_deployments(deployment_uid):
        if dd["status"] not in {"STOPPED", "FAILED"}:
            dep_repo.update_device_deployment_status(
                deployment_uid,
                dd["device_uid"],
                status="STOPPED",
                error="Dismissed in control plane — agent may not have responded",
            )
    dep_repo.update_status(deployment_uid, "STOPPED")
    event_service.record(
        "DEPLOYMENT_DISMISSED",
        f"Deployment {deployment_uid} marked stopped in control plane",
        severity="WARN",
        owner=owner,
        deployment_uid=deployment_uid,
    )
    return get_full(deployment_uid, owner)


def cancel(*, owner: str, deployment_uid: str) -> dict:
    """Remove a deployment from the active list and purge artifacts on the device."""
    dep = dep_repo.get(deployment_uid, owner)
    if not dep:
        raise NotFoundError("Deployment not found")
    if dep["status"] == "CANCELLED":
        raise ConflictError("Deployment is already removed.")

    purge_sent = False
    try:
        stop(owner=owner, deployment_uid=deployment_uid, purge=True)
        purge_sent = True
    except DomainError as exc:
        event_service.record(
            "DEPLOYMENT_CANCEL_PURGE_SKIPPED",
            f"Purge before remove skipped: {exc}",
            severity="WARN",
            owner=owner,
            deployment_uid=deployment_uid,
        )

    for dd in dep_repo.list_device_deployments(deployment_uid):
        if dd["status"] not in {"STOPPED", "FAILED"}:
            dep_repo.update_device_deployment_status(
                deployment_uid,
                dd["device_uid"],
                status="STOPPED",
            )
    dep_repo.update_status(deployment_uid, "CANCELLED")
    event_service.record(
        "DEPLOYMENT_CANCELLED",
        f"Deployment {deployment_uid} removed from active list"
        + (" — purge sent to device" if purge_sent else " — device purge not sent"),
        owner=owner,
        deployment_uid=deployment_uid,
        payload={"purge_sent": purge_sent},
    )
    return get_full(deployment_uid, owner)


def restart(*, owner: str, deployment_uid: str) -> dict:
    reconcile_deployment_status(deployment_uid)
    dep = dep_repo.get(deployment_uid, owner)
    if not dep:
        raise NotFoundError("Deployment not found")
    if dep["status"] != "STOPPED":
        raise ConflictError("Only stopped deployments can be restarted.")

    card = mc_repo.get_full(dep["model_card_uid"])
    if not card:
        raise NotFoundError("Underlying model card is missing")

    request_id = gen_request_id()
    payload = build_restart_payload(
        deployment_uid=deployment_uid,
        request_id=request_id,
        card=card,
    )
    status, err, topic = _publish_command(
        owner=owner,
        dep=dep,
        deployment_uid=deployment_uid,
        operation="restart_deployment",
        request_id=request_id,
        payload=payload,
    )
    if status == "MQTT_SENT":
        dep_repo.update_status(deployment_uid, "STARTING")
        for dd in dep_repo.list_device_deployments(deployment_uid):
            dep_repo.update_device_deployment_status(
                deployment_uid, dd["device_uid"], status="STARTING"
            )
        event_service.record(
            "DEPLOYMENT_RESTART_DISPATCHED",
            f"Restart '{card['display_name']}' sent",
            owner=owner,
            deployment_uid=deployment_uid,
            payload={"request_id": request_id, "topic": topic},
        )
    else:
        event_service.record(
            "DEPLOYMENT_RESTART_FAILED" if status == "MQTT_FAILED" else "DEPLOYMENT_RESTART_RECORDED",
            f"Restart '{card['display_name']}' — {err or 'MQTT disabled'}",
            severity="WARN" if status == "MQTT_FAILED" else "INFO",
            owner=owner,
            deployment_uid=deployment_uid,
            payload={"request_id": request_id, "topic": topic, "error": err},
        )
    return get_full(deployment_uid, owner)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def list_for_owner(owner: str, *, active_only: bool = False) -> List[dict]:
    return dep_repo.list_for_owner(owner, active_only=active_only)


def list_for_device(device_uid: str, owner: str) -> List[dict]:
    return dep_repo.list_for_device(device_uid, owner)


def list_for_group(group_uid: str, owner: str) -> List[dict]:
    return dep_repo.list_for_group(group_uid, owner)


def effective_status(parent_status: str, device_rows: List[dict]) -> str:
    """Resolve display/action status when parent row lags per-device rows."""
    if not device_rows:
        return parent_status
    statuses = {r["status"] for r in device_rows}
    parent = parent_status
    terminal = {"STOPPED", "FAILED"}

    if parent == "STOPPING" and statuses <= terminal:
        return "FAILED" if statuses == {"FAILED"} else "STOPPED"
    if parent in {"STARTING", "DELIVERING", "DOWNLOADING", "PULLING"} and statuses == {"RUNNING"}:
        return "RUNNING"
    if parent in {"STARTING", "DELIVERING", "DOWNLOADING", "PULLING"} and statuses <= terminal:
        return "FAILED" if "FAILED" in statuses else "STOPPED"
    return parent


def reconcile_deployment_status(deployment_uid: str) -> None:
    """Promote parent deployment status when every device row has settled.

    Handles the common case where an agent ACK updated device_deployments but
    the parent deployments row was left in STOPPING/STARTING/DELIVERING.
    """
    dep = dep_repo.get_any(deployment_uid)
    if not dep:
        return
    rows = dep_repo.list_device_deployments(deployment_uid)
    if not rows:
        return

    if dep["status"] == "STOPPING":
        _reconcile_stopping_from_command_acks(deployment_uid, rows)
        rows = dep_repo.list_device_deployments(deployment_uid)
        dep = dep_repo.get_any(deployment_uid) or dep

    settled = effective_status(dep["status"], rows)
    if settled != dep["status"] and settled in {"STOPPED", "FAILED", "RUNNING"}:
        dep_repo.update_status(deployment_uid, settled)


def _reconcile_stopping_from_command_acks(deployment_uid: str, rows: List[dict]) -> None:
    """Heal device rows left in STOPPING when a stop command was already acked."""
    stop_cmds = [
        c for c in commands_repo.list_for_deployment(deployment_uid)
        if c.get("operation") == "stop_deployment" and c.get("acked_at")
    ]
    if not stop_cmds:
        return

    for dd in rows:
        if dd["status"] != "STOPPING":
            continue
        matched = [
            c for c in stop_cmds
            if not c.get("device_uid") or c["device_uid"] == dd["device_uid"]
        ]
        if not matched:
            continue
        latest = matched[0]
        try:
            resp = json.loads(latest.get("response_json") or "{}")
        except json.JSONDecodeError:
            continue
        agent_status = (resp.get("status") or "").upper()
        if agent_status in {"STOPPED", "OK", "SUCCESS"}:
            dep_repo.update_device_deployment_status(
                deployment_uid, dd["device_uid"], status="STOPPED"
            )
        elif agent_status == "FAILED":
            dep_repo.update_device_deployment_status(
                deployment_uid,
                dd["device_uid"],
                status="FAILED",
                error=resp.get("error"),
            )


def poll_snapshot(deployment_uid: str, owner: str) -> dict:
    """Small JSON payload for live deployment detail polling."""
    dep = dep_repo.get(deployment_uid, owner)
    if not dep:
        raise NotFoundError("Deployment not found")
    reconcile_deployment_status(deployment_uid)
    dep = dep_repo.get(deployment_uid, owner)
    devices = dep_repo.list_device_deployments(deployment_uid)
    commands = commands_repo.list_for_deployment(deployment_uid)
    status = effective_status(dep["status"], devices)
    media = get_settings().media
    device_rows = []
    for row in devices:
        entry = {
            "device_uid": row["device_uid"],
            "status": row["status"],
            "container_id": row.get("container_id"),
            "container_name": row.get("container_name"),
            "error_message": row.get("error_message"),
            "updated_at": row.get("updated_at"),
        }
        if media.mode == "mediamtx":
            entry["hls_url"] = media.hls_url(row["device_uid"])
        device_rows.append(entry)
    return {
        "deployment_uid": deployment_uid,
        "status": status,
        "raw_status": dep["status"],
        "devices": device_rows,
        "commands": [
            {
                "request_id": cmd["request_id"],
                "status": cmd["status"],
                "acked_at": cmd.get("acked_at"),
            }
            for cmd in commands
        ],
    }


def get_full(deployment_uid: str, owner: str) -> dict:
    dep = dep_repo.get(deployment_uid, owner)
    if not dep:
        raise NotFoundError("Deployment not found")
    reconcile_deployment_status(deployment_uid)
    dep = dep_repo.get(deployment_uid, owner)
    dep["devices"] = dep_repo.list_device_deployments(deployment_uid)
    dep["commands"] = commands_repo.list_for_deployment(deployment_uid)
    dep["card"] = mc_repo.get_full(dep["model_card_uid"])
    return dep


def counts(owner: str) -> dict:
    return dep_repo.count_for_owner(owner)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_target(owner: str, target_type: str, target_uid: str) -> tuple[List[dict], str]:
    if target_type == "DEVICE":
        d = devices_repo.get(target_uid, owner)
        if not d:
            raise NotFoundError("Device not found.")
        return [d], d["device_name"]
    g = groups_repo.get(target_uid, owner)
    if not g:
        raise NotFoundError("Group not found.")
    devices = devices_repo.list_in_group(owner, target_uid)
    return devices, g["group_name"]


def _assert_compatibility(card: dict, devices: List[dict]) -> None:
    allowed = {c["generation_uid"] for c in card.get("compatibility", [])}
    if allowed:
        incompatible = [d for d in devices if d["generation_uid"] not in allowed]
        if incompatible:
            names = ", ".join(d["device_name"] for d in incompatible[:5])
            raise ValidationError(
                f"Model '{card['display_name']}' is not compatible with: {names}. "
                "Update model compatibility on the model card, or pick a different target."
            )
    cap_errors = []
    for d in devices:
        err = device_capabilities.device_compatible_with_card(d, card)
        if err:
            cap_errors.append("%s: %s" % (d["device_name"], err))
    if cap_errors:
        raise ValidationError("; ".join(cap_errors))


def _docker_image_ref(spec: dict) -> str:
    registry = spec.get("image_registry")
    repo = spec["image_repository"]
    tag = spec.get("image_tag") or "latest"
    digest = spec.get("image_digest")
    base = f"{registry}/{repo}" if registry else repo
    if digest:
        return f"{base}@{digest}"
    return f"{base}:{tag}"


def _decode_json_list(raw: Optional[str]) -> Optional[List[str]]:
    if not raw:
        return None
    try:
        v = json.loads(raw)
        if isinstance(v, list):
            return [str(x) for x in v]
    except json.JSONDecodeError:
        return None
    return None
