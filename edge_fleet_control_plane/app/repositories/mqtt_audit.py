"""Raw MQTT audit trail."""

from __future__ import annotations

import json
from typing import List, Optional

from app.core.time import now_iso
from app.db.session import execute, fetch_all


def insert(*, owner: Optional[str], topic: str, direction: str, request_id: Optional[str],
           device_uid: Optional[str], payload: dict) -> None:
    execute(
        """
        INSERT INTO mqtt_audit
            (owner_tapis_username, topic, direction, request_id, device_uid, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (owner, topic, direction, request_id, device_uid, json.dumps(payload), now_iso()),
    )


def list_recent(owner: Optional[str], limit: int = 100) -> List[dict]:
    if owner:
        rows = fetch_all(
            """
            SELECT * FROM mqtt_audit
             WHERE owner_tapis_username = ? OR owner_tapis_username IS NULL
             ORDER BY created_at DESC LIMIT ?
            """,
            (owner, limit),
        )
    else:
        rows = fetch_all(
            "SELECT * FROM mqtt_audit ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    return [dict(r) for r in rows]
