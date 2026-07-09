"""Live camera streaming — control-plane side (MJPEG relay).

The video itself never touches MQTT or the database. We only publish small
``stream_start`` / ``stream_stop`` *commands* to the device over MQTT (the same
channel used for deployments). The agent reacts by running a GStreamer pipeline
that JPEG-encodes the camera and pushes it over HTTPS straight back into this
control plane, which fans it out to browsers as ``multipart/x-mixed-replace``.

    Portal  --(MQTT stream_start)-->  Agent
    Agent   --(HTTPS MJPEG PUT)----->  /api/stream/{device}/ingest  --> relay
    Browser <--(multipart/x-mixed-replace)-- /devices/{device}/stream.mjpg

This rides entirely on the control plane's existing port 443, so it works
behind the single-ingress-per-pod model of Tapis (no MediaMTX, no RTSP, no
second port).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional

from app.config import get_settings
from app.core.errors import ValidationError
from app.core.ids import gen_request_id, gen_uid
from app.core.logging import get_logger
from app.core.time import now_iso
from app.repositories import commands as commands_repo
from app.repositories import devices as devices_repo
from app.repositories import mqtt_audit as audit_repo
from app.services import device_capabilities, device_service, event_service, mqtt_service, stream_relay

log = get_logger("stream")


# ---------------------------------------------------------------------------
# Ingest tokens (stateless, HMAC-signed; no DB row needed)
# ---------------------------------------------------------------------------

def _sign(msg: str) -> str:
    secret = get_settings().secret_key.encode()
    return hmac.new(secret, msg.encode(), hashlib.sha256).hexdigest()


def mint_token(device_uid: str) -> str:
    """Mint a short-lived URL-safe token authorising MJPEG ingest for a device."""
    exp = int(time.time()) + get_settings().media.token_ttl_seconds
    msg = f"{device_uid}:{exp}"
    raw = f"{msg}:{_sign(msg)}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def verify_token(token: str, device_uid: str) -> bool:
    if not token:
        return False
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        dev, exp, sig = raw.rsplit(":", 2)
    except Exception:
        return False
    if dev != device_uid:
        return False
    if not exp.isdigit() or int(exp) < int(time.time()):
        return False
    return hmac.compare_digest(sig, _sign(f"{dev}:{exp}"))


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

def ingest_url(device_uid: str) -> str:
    """URL the device pushes video to.

    * relay mode → control-plane MJPEG ingest with a signed token in the query.
    * mediamtx mode → RTSPS URL of the MediaMTX ingest pod.
    """
    media = get_settings().media
    if media.mode == "mediamtx":
        return media.rtsp_ingest_url(device_uid)
    base = get_settings().base_url_clean
    token = mint_token(device_uid)
    return f"{base}/api/stream/{device_uid}/ingest?token={token}"


def view_url(device_uid: str) -> str:
    """Same-origin path the browser reads the MJPEG (relay) stream from."""
    return f"/devices/{device_uid}/stream.mjpg"


# ---------------------------------------------------------------------------
# MQTT command payloads
# ---------------------------------------------------------------------------

def _build_start_payload(device_uid: str, request_id: str, camera_type: str) -> Dict[str, Any]:
    media = get_settings().media
    camera: Dict[str, Any] = {
        "type": camera_type,
        "device": "/dev/video0",
        "width": media.default_width,
        "height": media.default_height,
        "fps": media.default_fps,
    }
    if media.mode == "mediamtx":
        protocol = "rtsp"
        camera["bitrate_kbps"] = media.default_bitrate_kbps
    else:
        protocol = "mjpeg-http"
        camera["jpeg_quality"] = media.jpeg_quality
    return {
        "operation": "stream_start",
        "request_id": request_id,
        "device_id": device_uid,
        "issued_at": now_iso(),
        "stream": {
            "path": media.path_for(device_uid),
            "ingest_url": ingest_url(device_uid),
            "protocol": protocol,
            "camera": camera,
        },
    }


def _build_stop_payload(device_uid: str, request_id: str) -> Dict[str, Any]:
    media = get_settings().media
    return {
        "operation": "stream_stop",
        "request_id": request_id,
        "device_id": device_uid,
        "issued_at": now_iso(),
        "stream": {"path": media.path_for(device_uid)},
    }


def _publish(*, owner: str, device_uid: str, operation: str, request_id: str,
             payload: Dict[str, Any]) -> tuple[str, Optional[str]]:
    """Record the command row first, then publish over MQTT (mirrors deployment_service)."""
    settings = get_settings()
    topic = mqtt_service.topic_for("DEVICE", device_uid)
    commands_repo.insert(
        command_uid=gen_uid("cmd"),
        owner=owner,
        deployment_uid=None,
        target_type="DEVICE",
        target_uid=device_uid,
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
    if not settings.mqtt.enabled:
        return "RECORDED", None
    try:
        mqtt_service.publish(topic, payload)
        commands_repo.update_send_status(request_id, status="MQTT_SENT", sent_at=now_iso())
        return "MQTT_SENT", None
    except Exception as e:  # pragma: no cover - network failure path
        log.exception("stream MQTT publish failed: %s", e)
        commands_repo.update_send_status(request_id, status="MQTT_FAILED", error=str(e))
        return "MQTT_FAILED", str(e)


def start(*, owner: str, device_uid: str, camera_type: Optional[str] = None) -> dict:
    settings = get_settings()
    if not settings.media.configured:
        raise ValidationError(
            "Live streaming is not enabled. Set MEDIA_ENABLED=true on the control plane."
        )
    device_service.get(device_uid, owner)  # ownership / existence check (raises DomainError)

    camera = (camera_type or settings.media.default_camera).strip().lower()
    if camera not in {"csi", "usb"}:
        camera = settings.media.default_camera

    request_id = gen_request_id()
    payload = _build_start_payload(device_uid, request_id, camera)
    status, err = _publish(
        owner=owner, device_uid=device_uid, operation="stream_start",
        request_id=request_id, payload=payload,
    )
    event_service.record(
        "STREAM_START_DISPATCHED" if status == "MQTT_SENT" else "STREAM_START_RECORDED",
        f"Live stream requested for device {device_uid}"
        + (f" — MQTT error: {err}" if err else ""),
        severity="WARN" if status == "MQTT_FAILED" else "INFO",
        owner=owner,
        device_uid=device_uid,
        payload={"request_id": request_id, "camera": camera},
    )
    return {"status": status, "request_id": request_id}


def stop(*, owner: str, device_uid: str) -> dict:
    device_service.get(device_uid, owner)
    request_id = gen_request_id()
    payload = _build_stop_payload(device_uid, request_id)
    status, err = _publish(
        owner=owner, device_uid=device_uid, operation="stream_stop",
        request_id=request_id, payload=payload,
    )
    event_service.record(
        "STREAM_STOP_DISPATCHED" if status == "MQTT_SENT" else "STREAM_STOP_RECORDED",
        f"Live stream stop requested for device {device_uid}"
        + (f" — MQTT error: {err}" if err else ""),
        severity="WARN" if status == "MQTT_FAILED" else "INFO",
        owner=owner,
        device_uid=device_uid,
        payload={"request_id": request_id},
    )
    return {"status": status, "request_id": request_id}


def _ack_status(cmd: dict) -> Optional[str]:
    raw = cmd.get("response_json")
    if not raw:
        return None
    try:
        return (json.loads(raw).get("status") or "").upper() or None
    except (json.JSONDecodeError, TypeError):
        return None


def stream_context(device_uid: str, owner: str) -> dict:
    """Build the template context for the live-stream card.

    ``active`` reflects the last command intent (a non-failed ``stream_start``);
    ``live`` reflects whether JPEG frames are actually arriving right now.
    """
    media = get_settings().media
    if not media.configured:
        return {"enabled": False}
    active = False
    last_op: Optional[str] = None
    for cmd in commands_repo.list_for_device(device_uid, owner, limit=30):
        if cmd["operation"] in {"stream_start", "stream_stop"}:
            last_op = cmd["operation"]
            if cmd["operation"] == "stream_start":
                failed = cmd["status"] == "MQTT_FAILED" or _ack_status(cmd) == "FAILED"
                active = not failed
            break
    ctx = {
        "enabled": True,
        "mode": media.mode,
        "path": media.path_for(device_uid),
        "default_camera": media.default_camera,
        "active": active,
        "last_op": last_op,
    }
    if media.mode == "mediamtx":
        ctx["hls_url"] = media.hls_url(device_uid)
    else:
        ctx["view_url"] = view_url(device_uid)
        ctx["live"] = stream_relay.is_live(device_uid, media.live_after_seconds)
    return ctx


def _multi_camera_inference(card: Optional[dict], camera_count: int) -> bool:
    """Match deployment_service._append_media_env: indexed paths only when count > 1."""
    if not card or camera_count <= 1:
        return False
    tags = {t.lower() for t in (card.get("tags") or [])}
    return "multi-camera" in tags


def inference_streams_context(device_rows: list, card: Optional[dict] = None) -> dict:
    """HLS tiles for deployment/group inference feeds (container RTSP push)."""
    media = get_settings().media
    if not media.configured or media.mode != "mediamtx":
        return {"enabled": False}
    tiles = []
    any_multi = False
    for row in device_rows:
        status = row.get("status") or ""
        device = devices_repo.get_any(row["device_uid"])
        caps = (device or {}).get("capabilities") or device_capabilities.parse(None)
        device_name = row.get("device_name") or (device or {}).get("device_name") or row["device_uid"]
        camera_count = max(1, int(caps.get("camera_count") or 1))
        multi = _multi_camera_inference(card, camera_count)
        if multi:
            any_multi = True
        stream_count = camera_count if multi else 1
        for cam_i in range(stream_count):
            label = device_name if stream_count == 1 else "%s · cam %d" % (device_name, cam_i)
            cam_index = cam_i if multi else None
            tiles.append({
                "device_uid": row["device_uid"],
                "device_name": label,
                "camera_index": cam_index,
                "hls_url": media.hls_url(row["device_uid"], cam_index),
                "active": status == "RUNNING",
                "status": status,
            })
    return {
        "enabled": True,
        "mode": "mediamtx",
        "multi_camera": any_multi,
        "tiles": tiles,
        "any_active": any(t["active"] for t in tiles),
    }


def inference_poll_devices(device_rows: list) -> list:
    """Minimal rows for inference tile polling on group/device pages."""
    media = get_settings().media
    if media.mode != "mediamtx":
        return []
    return [
        {
            "device_uid": row["device_uid"],
            "status": row.get("status") or "",
            "hls_url": media.hls_url(row["device_uid"]),
        }
        for row in device_rows
    ]
