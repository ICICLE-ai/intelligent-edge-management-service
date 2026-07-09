"""Event-log repository."""

from __future__ import annotations

import json
from typing import List, Optional

from app.core.time import now_iso
from app.db.session import execute, fetch_all


def insert(*, event_type: str, message: Optional[str] = None, severity: str = "INFO",
           owner: Optional[str] = None, device_uid: Optional[str] = None,
           deployment_uid: Optional[str] = None, payload: Optional[dict] = None) -> None:
    execute(
        """
        INSERT INTO events
            (owner_tapis_username, device_uid, deployment_uid, event_type, severity,
             message, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (owner, device_uid, deployment_uid, event_type, severity, message,
         json.dumps(payload or {}), now_iso()),
    )


def for_owner(owner: str, limit: int = 100, severity: Optional[str] = None,
              device_uid: Optional[str] = None, deployment_uid: Optional[str] = None) -> List[dict]:
    sql = "SELECT * FROM events WHERE owner_tapis_username = ?"
    params: list = [owner]
    if severity:
        sql += " AND severity = ?"
        params.append(severity)
    if device_uid:
        sql += " AND device_uid = ?"
        params.append(device_uid)
    if deployment_uid:
        sql += " AND deployment_uid = ?"
        params.append(deployment_uid)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = fetch_all(sql, params)
    return [dict(r) for r in rows]


def for_device(device_uid: str, limit: int = 50) -> List[dict]:
    rows = fetch_all(
        """
        SELECT * FROM events
         WHERE device_uid = ?
         ORDER BY created_at DESC LIMIT ?
        """,
        (device_uid, limit),
    )
    return [dict(r) for r in rows]
