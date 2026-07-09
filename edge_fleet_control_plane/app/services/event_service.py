"""Event log facade."""

from __future__ import annotations

from typing import Optional

from app.core.logging import get_logger
from app.repositories import events as events_repo

log = get_logger("events")


def record(event_type: str, message: str, *, severity: str = "INFO",
           owner: Optional[str] = None, device_uid: Optional[str] = None,
           deployment_uid: Optional[str] = None, payload: Optional[dict] = None) -> None:
    events_repo.insert(
        event_type=event_type,
        message=message,
        severity=severity,
        owner=owner,
        device_uid=device_uid,
        deployment_uid=deployment_uid,
        payload=payload,
    )
    log.info("event %s | %s | dev=%s dep=%s", event_type, message, device_uid, deployment_uid)
