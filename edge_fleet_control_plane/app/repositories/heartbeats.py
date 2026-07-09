"""Heartbeat repository."""

from __future__ import annotations

import json
from typing import List, Optional

from app.db.session import connection, execute, fetch_all


def insert(*, device_uid: str, owner: str, status: str, payload: dict, received_at: str,
           store_payload: bool = False) -> None:
    active = payload.get("active_containers") or []
    execute(
        """
        INSERT INTO device_heartbeats
            (device_uid, owner_tapis_username, status, cpu_percent, memory_used_mb,
             disk_used_gb, gpu_temp_c, docker_running, active_containers_json,
             payload_json, received_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            device_uid,
            owner,
            status,
            payload.get("cpu_percent"),
            payload.get("memory_used_mb"),
            payload.get("disk_used_gb"),
            payload.get("gpu_temp_c"),
            int(bool(payload.get("docker_running"))) if payload.get("docker_running") is not None else None,
            json.dumps(active),
            json.dumps(payload) if store_payload else None,
            received_at,
        ),
    )


def latest_received_at(device_uid: str) -> Optional[str]:
    row = fetch_all(
        """
        SELECT received_at FROM device_heartbeats
         WHERE device_uid = ?
         ORDER BY received_at DESC LIMIT 1
        """,
        (device_uid,),
    )
    return row[0]["received_at"] if row else None


def delete_older_than(cutoff_iso: str) -> int:
    with connection() as con:
        cur = con.execute(
            "DELETE FROM device_heartbeats WHERE received_at < ?",
            (cutoff_iso,),
        )
        return cur.rowcount if cur.rowcount is not None else 0


def latest_for_device(device_uid: str, limit: int = 20) -> List[dict]:
    rows = fetch_all(
        """
        SELECT * FROM device_heartbeats
         WHERE device_uid = ?
         ORDER BY received_at DESC LIMIT ?
        """,
        (device_uid, limit),
    )
    return [dict(r) for r in rows]


def latest(device_uid: str) -> Optional[dict]:
    rows = latest_for_device(device_uid, 1)
    return rows[0] if rows else None
