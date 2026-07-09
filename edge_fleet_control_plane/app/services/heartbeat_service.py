"""Heartbeat ingestion and offline-watchdog loop."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from app.config import get_settings
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.time import now_iso
from app.repositories import devices as devices_repo
from app.repositories import heartbeats as heartbeats_repo
from app.services import event_service

log = get_logger("heartbeat")


def _should_persist_history(device_uid: str, mode: str, sample_seconds: int) -> bool:
    if mode == "none":
        return False
    if mode == "all":
        return True
    if mode != "sample":
        log.warning("unknown HEARTBEAT_HISTORY_MODE=%r; treating as sample", mode)
    last = heartbeats_repo.latest_received_at(device_uid)
    if not last:
        return True
    try:
        then = datetime.fromisoformat(last.replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - then).total_seconds() >= sample_seconds


def record_heartbeat(payload: Dict[str, Any]) -> Dict[str, Any]:
    device_uid = payload.get("device_id") or payload.get("device_uid")
    if not device_uid:
        raise ValidationError("Missing device_id")
    device = devices_repo.get_any(device_uid)
    if not device:
        raise NotFoundError("Unknown device")
    received = now_iso()
    status = payload.get("status") or "ONLINE"
    settings = get_settings()

    if _should_persist_history(
        device_uid,
        settings.heartbeat_history_mode,
        settings.heartbeat_history_sample_seconds,
    ):
        heartbeats_repo.insert(
            device_uid=device_uid,
            owner=device["owner_tapis_username"],
            status=status,
            payload=payload,
            received_at=received,
            store_payload=settings.heartbeat_store_payload,
        )

    devices_repo.update_after_heartbeat(
        device_uid,
        status=status,
        hostname=payload.get("hostname"),
        ip_address=payload.get("ip_address"),
        agent_version=payload.get("agent_version"),
        heartbeat_at=received,
    )
    return {"ok": True, "received_at": received}


def prune_stale_history() -> int:
    settings = get_settings()
    if settings.heartbeat_history_retention_hours <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.heartbeat_history_retention_hours)
    deleted = heartbeats_repo.delete_older_than(cutoff.isoformat())
    if deleted:
        log.info("pruned %s heartbeat history row(s) older than %sh", deleted, settings.heartbeat_history_retention_hours)
    return deleted


def run_offline_watchdog_forever() -> None:
    settings = get_settings()
    while True:
        try:
            threshold = settings.device_offline_after_seconds
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=threshold)
            stale = devices_repo.find_stale(cutoff.isoformat())
            for d in stale:
                devices_repo.update_status(d["device_uid"], "OFFLINE")
                event_service.record(
                    "DEVICE_OFFLINE",
                    "Device marked offline due to stale heartbeat",
                    severity="WARN",
                    owner=d["owner_tapis_username"],
                    device_uid=d["device_uid"],
                    payload={"last_heartbeat_at": d.get("last_heartbeat_at")},
                )
            prune_stale_history()
        except Exception as e:
            log.exception("offline watchdog error: %s", e)
        time.sleep(settings.offline_monitor_interval_seconds)
