"""Scan jobs reported by a scan station's agent, and the station's last service status."""

from __future__ import annotations

import json
from typing import List, Optional

from app.core.time import now_iso
from app.db.session import execute, fetch_all, fetch_one


def _job_row(row) -> dict:
    out = dict(row)
    try:
        out["job"] = json.loads(out.pop("job_json") or "{}")
    except json.JSONDecodeError:
        out["job"] = {}
    return out


def upsert_job(*, device_uid: str, owner: str, deployment_uid: Optional[str], job: dict) -> None:
    job_id = job["id"]
    created = job.get("created")
    updated = job.get("updated")
    name = str(job.get("object") or "")[:200] or None
    args = (
        deployment_uid, name, job.get("state") or "queued", json.dumps(job),
        created, updated, now_iso(),
    )
    existing = fetch_one(
        "SELECT job_id FROM scan_jobs WHERE device_uid = ? AND job_id = ?",
        (device_uid, job_id),
    )
    if existing:
        execute(
            """
            UPDATE scan_jobs
               SET deployment_uid = ?, object_name = ?, state = ?, job_json = ?,
                   created_ts = ?, updated_ts = ?, reported_at = ?
             WHERE device_uid = ? AND job_id = ?
            """,
            args + (device_uid, job_id),
        )
    else:
        execute(
            """
            INSERT INTO scan_jobs
                (deployment_uid, object_name, state, job_json, created_ts, updated_ts,
                 reported_at, device_uid, job_id, owner_tapis_username)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            args + (device_uid, job_id, owner),
        )


def list_for_device(device_uid: str, limit: int = 100) -> List[dict]:
    rows = fetch_all(
        """
        SELECT * FROM scan_jobs
         WHERE device_uid = ?
         ORDER BY created_ts DESC LIMIT ?
        """,
        (device_uid, limit),
    )
    return [_job_row(r) for r in rows]


def get(device_uid: str, job_id: str) -> Optional[dict]:
    row = fetch_one(
        "SELECT * FROM scan_jobs WHERE device_uid = ? AND job_id = ?",
        (device_uid, job_id),
    )
    return _job_row(row) if row else None


def counts_by_state(device_uid: str) -> dict:
    rows = fetch_all(
        "SELECT state, COUNT(*) AS c FROM scan_jobs WHERE device_uid = ? GROUP BY state",
        (device_uid,),
    )
    return {r["state"]: r["c"] for r in rows}


def set_status(*, device_uid: str, deployment_uid: Optional[str], reachable: bool,
               error: Optional[str]) -> None:
    args = (deployment_uid, 1 if reachable else 0, error, now_iso(), device_uid)
    if fetch_one("SELECT device_uid FROM scan_service_status WHERE device_uid = ?", (device_uid,)):
        execute(
            """
            UPDATE scan_service_status
               SET deployment_uid = ?, reachable = ?, error = ?, reported_at = ?
             WHERE device_uid = ?
            """,
            args,
        )
    else:
        execute(
            """
            INSERT INTO scan_service_status (deployment_uid, reachable, error, reported_at, device_uid)
            VALUES (?, ?, ?, ?, ?)
            """,
            args,
        )


def get_status(device_uid: str) -> Optional[dict]:
    row = fetch_one("SELECT * FROM scan_service_status WHERE device_uid = ?", (device_uid,))
    if not row:
        return None
    out = dict(row)
    out["reachable"] = bool(out.get("reachable"))
    return out
