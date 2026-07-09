"""Device command audit repository."""

from __future__ import annotations

import json
from typing import List, Optional

from app.core.time import now_iso
from app.db.session import execute, fetch_all, fetch_one


def insert(*, command_uid: str, owner: str, deployment_uid: Optional[str], target_type: str,
           target_uid: str, device_uid: Optional[str], operation: str, request_id: str,
           topic: str, payload: dict, status: str = "RECORDED",
           sent_at: Optional[str] = None, error: Optional[str] = None) -> None:
    ts = now_iso()
    execute(
        """
        INSERT INTO device_commands
            (command_uid, owner_tapis_username, deployment_uid, target_type, target_uid,
             device_uid, operation, request_id, status, topic, payload_json,
             error_message, created_at, sent_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (command_uid, owner, deployment_uid, target_type, target_uid, device_uid,
         operation, request_id, status, topic, json.dumps(payload), error, ts, sent_at),
    )


def list_for_owner(owner: str, limit: int = 100) -> List[dict]:
    rows = fetch_all(
        """
        SELECT * FROM device_commands
         WHERE owner_tapis_username = ?
         ORDER BY created_at DESC LIMIT ?
        """,
        (owner, limit),
    )
    return [dict(r) for r in rows]


def list_for_deployment(deployment_uid: str) -> List[dict]:
    rows = fetch_all(
        """
        SELECT * FROM device_commands
         WHERE deployment_uid = ?
         ORDER BY created_at DESC
        """,
        (deployment_uid,),
    )
    return [dict(r) for r in rows]


def list_for_device(device_uid: str, owner: str, limit: int = 50) -> List[dict]:
    rows = fetch_all(
        """
        SELECT * FROM device_commands
         WHERE owner_tapis_username = ? AND (device_uid = ? OR target_uid = ?)
         ORDER BY created_at DESC LIMIT ?
        """,
        (owner, device_uid, device_uid, limit),
    )
    return [dict(r) for r in rows]


def get_by_request_id(request_id: str) -> Optional[dict]:
    row = fetch_one("SELECT * FROM device_commands WHERE request_id = ?", (request_id,))
    return dict(row) if row else None


def update_send_status(
    request_id: str,
    *,
    status: str,
    sent_at: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    execute(
        """
        UPDATE device_commands
           SET status = ?,
               sent_at = COALESCE(?, sent_at),
               error_message = COALESCE(?, error_message)
         WHERE request_id = ?
        """,
        (status, sent_at, error, request_id),
    )


def get_latest_for_deployment(deployment_uid: str, operation: str) -> Optional[dict]:
    row = fetch_one(
        """
        SELECT * FROM device_commands
         WHERE deployment_uid = ? AND operation = ?
         ORDER BY created_at DESC LIMIT 1
        """,
        (deployment_uid, operation),
    )
    return dict(row) if row else None


def mark_acked(request_id: str, response: dict) -> None:
    agent_status = (response.get("status") or "").upper()
    terminal = agent_status in {"RUNNING", "STOPPED", "FAILED", "OK", "SUCCESS"}
    cmd_status = "ACK" if terminal else (agent_status or "ACK")
    execute(
        """
        UPDATE device_commands
           SET status = ?, response_json = ?, acked_at = ?
         WHERE request_id = ?
        """,
        (cmd_status, json.dumps(response), now_iso(), request_id),
    )


def get_latest_open_for_deployment(deployment_uid: str, operation: str) -> Optional[dict]:
    """Backward-compatible alias — returns latest command row for the operation."""
    return get_latest_for_deployment(deployment_uid, operation)
