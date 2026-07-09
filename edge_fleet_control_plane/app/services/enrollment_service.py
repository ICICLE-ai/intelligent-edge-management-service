"""Device enrollment, installer minting, and Jetson agent bootstrap."""

from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
import tarfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from app.config import AGENT_PACKAGE_DIR, get_settings
from app.core.errors import ConflictError, ForbiddenError, GoneError, NotFoundError, ValidationError
from app.core.ids import gen_uid
from app.core.time import now_iso, parse_iso
from app.repositories import device_credentials as creds_repo
from app.repositories import devices as devices_repo
from app.repositories import enrollments as enrollments_repo
from app.repositories import groups as groups_repo
from app.services import event_service


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def mint_device_api_key(device_uid: str) -> str:
    """Issue a long-lived API key for an enrolled device. Revokes prior keys."""
    raw = f"dkey_{secrets.token_urlsafe(32)}"
    creds_repo.revoke_all_for_device(device_uid)
    creds_repo.create(device_uid=device_uid, key_hash=_hash_token(raw))
    return raw


def device_runtime_config(device: dict) -> Dict[str, Any]:
    """Configuration delivered to the Jetson on successful enrollment."""
    settings = get_settings()
    group_uid = device.get("group_uid")
    return {
        "device_id": device["device_uid"],
        "device_name": device["device_name"],
        "device_alias": device.get("device_alias"),
        "generation_uid": device["generation_uid"],
        "user_groups": [group_uid] if group_uid else [],
        "site_name": device.get("site_name"),
        "hardware_type": device.get("generation_hardware"),
        "heartbeat": {"interval_seconds": settings.heartbeat_interval_seconds},
        "portal": {"base_url": settings.base_url_clean},
        "mqtt": {
            "enabled": settings.mqtt.enabled,
            "host": settings.mqtt.host,
            "port": settings.mqtt.port,
            "tls_enabled": settings.mqtt.tls,
            "base_topic": settings.mqtt.base_topic,
            "username": settings.mqtt.username,
            "password": settings.mqtt.password,
        },
        "patra": {"base_url": settings.patra_base_url},
        "paths": {
            "base_dir": "/opt/icicle-edge",
            "deployments_dir": "/opt/icicle-edge/deployments",
            "logs_dir": "/opt/icicle-edge/logs",
            "state_dir": "/opt/icicle-edge/state",
        },
    }


def mint_enrollment(device_uid: str, owner: str, ttl_hours: int = 24) -> str:
    raw = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat()
    enrollments_repo.create(
        device_uid=device_uid,
        owner=owner,
        token_hash=_hash_token(raw),
        expires_at=expires,
    )
    return raw


def build_installer(device_uid: str, owner: str) -> bytes:
    settings = get_settings()
    settings.assert_reachable_base_url()
    device = devices_repo.get(device_uid, owner)
    if not device:
        raise NotFoundError("Device not found")
    raw_token = mint_enrollment(device_uid, owner)
    enrollments_repo.mark_downloaded(device_uid)
    devices_repo.update_status(device_uid, "INSTALLER_READY")
    event_service.record(
        "INSTALLER_GENERATED",
        "Installer bundle generated",
        owner=owner,
        device_uid=device_uid,
    )

    enrollment_blob = {
        "mode": "http",
        "platform_url": settings.base_url_clean,
        "enrollment_token": raw_token,
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        def add(path: str, contents: bytes, mode: int = 0o644) -> None:
            info = tarfile.TarInfo(f"icicle-edge-agent/{path}")
            info.size = len(contents)
            info.mode = mode
            tf.addfile(info, io.BytesIO(contents))

        for name in ("install.sh", "uninstall.sh", "requirements.txt"):
            p = AGENT_PACKAGE_DIR / name
            add(name, p.read_bytes(), 0o755 if name.endswith(".sh") else 0o644)
        for p in (AGENT_PACKAGE_DIR / "agent").glob("*.py"):
            add(f"agent/{p.name}", p.read_bytes())
        add(
            "systemd/icicle-edge-agent.service",
            (AGENT_PACKAGE_DIR / "systemd" / "icicle-edge-agent.service").read_bytes(),
        )
        add(
            "x11/99-icicle-docker-xhost.sh",
            (AGENT_PACKAGE_DIR / "x11" / "99-icicle-docker-xhost.sh").read_bytes(),
            0o755,
        )
        add(
            "config/enrollment.json",
            json.dumps(enrollment_blob, indent=2).encode("utf-8"),
        )
    return buf.getvalue()


def enroll(payload: Dict[str, Any]) -> Dict[str, Any]:
    token = payload.get("enrollment_token")
    if not token:
        raise ValidationError("Missing enrollment_token")
    rec = enrollments_repo.get_by_hash(_hash_token(token))
    if not rec:
        raise ForbiddenError("Invalid enrollment token")
    if rec.get("used_at"):
        raise ConflictError("Enrollment token already used")
    expires_at = parse_iso(rec["expires_at"])
    if expires_at and expires_at < datetime.now(timezone.utc):
        raise GoneError("Enrollment token expired")
    device = devices_repo.get_any(rec["device_uid"])
    if not device:
        raise NotFoundError("Device record missing")
    enrollments_repo.mark_used(rec["id"])
    devices_repo.update_after_enrollment(
        device["device_uid"],
        hostname=payload.get("hostname"),
        ip_address=payload.get("ip_address"),
        agent_version=payload.get("agent_version"),
    )
    event_service.record(
        "DEVICE_ENROLLED",
        "Device enrollment completed",
        owner=device["owner_tapis_username"],
        device_uid=device["device_uid"],
        payload=payload,
    )
    device_api_key = mint_device_api_key(device["device_uid"])
    return {
        "status": "ENROLLED",
        "device_api_key": device_api_key,
        "device_config": device_runtime_config(devices_repo.get_any(device["device_uid"]) or device),
    }


def latest_enrollment_for_device(device_uid: str) -> Optional[dict]:
    return enrollments_repo.get_latest_for_device(device_uid)
