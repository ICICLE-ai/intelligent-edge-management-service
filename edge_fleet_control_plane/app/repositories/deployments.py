"""Deployment repository — both the logical deployment and per-device materialisations."""

from __future__ import annotations

from typing import List, Optional

from app.core.time import now_iso
from app.db.session import execute, fetch_all, fetch_one


# ---------------------------------------------------------------------------
# Deployments (logical)
# ---------------------------------------------------------------------------

_DEPLOYMENT_SELECT = """
SELECT d.*,
       mc.display_name        AS model_display_name,
       mc.version             AS model_version,
       mc.slug                AS model_slug,
       cs.image_repository    AS image_repository,
       cs.image_tag           AS image_tag,
       cs.container_name      AS container_name
  FROM deployments d
  LEFT JOIN model_cards     mc ON mc.model_card_uid = d.model_card_uid
  LEFT JOIN container_specs cs ON cs.model_card_uid = d.model_card_uid
"""


def create(*, deployment_uid: str, owner: str, model_card_uid: str, artifact_uid: str,
           spec_uid: str, target_type: str, target_uid: str, target_name: Optional[str],
           request_id: str, notes: Optional[str] = None) -> None:
    ts = now_iso()
    execute(
        """
        INSERT INTO deployments
            (deployment_uid, owner_tapis_username, model_card_uid, artifact_uid,
             spec_uid, target_type, target_uid, target_name, status, request_id,
             notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?)
        """,
        (deployment_uid, owner, model_card_uid, artifact_uid, spec_uid, target_type,
         target_uid, target_name, request_id, notes, ts, ts),
    )


def get(deployment_uid: str, owner: str) -> Optional[dict]:
    row = fetch_one(
        _DEPLOYMENT_SELECT + " WHERE d.deployment_uid = ? AND d.owner_tapis_username = ?",
        (deployment_uid, owner),
    )
    return dict(row) if row else None


def get_any(deployment_uid: str) -> Optional[dict]:
    row = fetch_one(_DEPLOYMENT_SELECT + " WHERE d.deployment_uid = ?", (deployment_uid,))
    return dict(row) if row else None


def list_for_owner(owner: str, *, active_only: bool = False) -> List[dict]:
    sql = _DEPLOYMENT_SELECT + " WHERE d.owner_tapis_username = ?"
    if active_only:
        sql += " AND d.status NOT IN ('STOPPED','FAILED','CANCELLED')"
    sql += " ORDER BY d.created_at DESC"
    return [dict(r) for r in fetch_all(sql, (owner,))]


def list_for_device(device_uid: str, owner: str) -> List[dict]:
    rows = fetch_all(
        _DEPLOYMENT_SELECT
        + """
        JOIN device_deployments dd ON dd.deployment_uid = d.deployment_uid
        WHERE dd.device_uid = ? AND d.owner_tapis_username = ?
        ORDER BY d.created_at DESC
        """,
        (device_uid, owner),
    )
    return [dict(r) for r in rows]


def list_for_group(group_uid: str, owner: str) -> List[dict]:
    rows = fetch_all(
        _DEPLOYMENT_SELECT
        + """ WHERE d.owner_tapis_username = ?
                   AND d.target_type = 'GROUP'
                   AND d.target_uid = ?
              ORDER BY d.created_at DESC """,
        (owner, group_uid),
    )
    return [dict(r) for r in rows]


def update_status(deployment_uid: str, status: str, *, error: Optional[str] = None) -> None:
    ts = now_iso()
    if status in {"STOPPED", "CANCELLED"}:
        execute(
            """
            UPDATE deployments
               SET status = ?, stopped_at = COALESCE(stopped_at, ?), updated_at = ?
             WHERE deployment_uid = ?
            """,
            (status, ts, ts, deployment_uid),
        )
    elif status == "RUNNING":
        execute(
            """
            UPDATE deployments
               SET status = ?, started_at = COALESCE(started_at, ?), updated_at = ?
             WHERE deployment_uid = ?
            """,
            (status, ts, ts, deployment_uid),
        )
    else:
        execute(
            "UPDATE deployments SET status = ?, updated_at = ? WHERE deployment_uid = ?",
            (status, ts, deployment_uid),
        )


def count_for_owner(owner: str) -> dict:
    rows = fetch_all(
        "SELECT status, COUNT(*) c FROM deployments WHERE owner_tapis_username = ? GROUP BY status",
        (owner,),
    )
    by_status = {r["status"]: r["c"] for r in rows}
    return {
        "total": sum(by_status.values()),
        "active": by_status.get("RUNNING", 0) + by_status.get("DELIVERING", 0) + by_status.get("PENDING", 0),
        "stopped": by_status.get("STOPPED", 0),
        "failed": by_status.get("FAILED", 0),
        "by_status": by_status,
    }


# ---------------------------------------------------------------------------
# Device-level rows
# ---------------------------------------------------------------------------

def create_device_deployment(*, device_deployment_uid: str, deployment_uid: str, device_uid: str,
                             status: str = "PENDING") -> None:
    ts = now_iso()
    execute(
        """
        INSERT INTO device_deployments
            (device_deployment_uid, deployment_uid, device_uid, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (device_deployment_uid, deployment_uid, device_uid, status, ts, ts),
    )


def update_device_deployment_status(deployment_uid: str, device_uid: str, *, status: str,
                                    container_id: Optional[str] = None,
                                    container_name: Optional[str] = None,
                                    error: Optional[str] = None) -> None:
    ts = now_iso()
    sets = ["status = ?", "updated_at = ?", "last_status_at = ?"]
    params: list = [status, ts, ts]
    if container_id is not None:
        sets.append("container_id = ?")
        params.append(container_id)
    if container_name is not None:
        sets.append("container_name = ?")
        params.append(container_name)
    if error is not None:
        sets.append("error_message = ?")
        params.append(error)
    if status == "RUNNING":
        sets.append("started_at = COALESCE(started_at, ?)")
        params.append(ts)
    if status in {"STOPPED", "FAILED"}:
        sets.append("stopped_at = ?")
        params.append(ts)
    params.extend([deployment_uid, device_uid])
    execute(
        f"""
        UPDATE device_deployments
           SET {', '.join(sets)}
         WHERE deployment_uid = ? AND device_uid = ?
        """,
        params,
    )


def list_device_deployments(deployment_uid: str) -> List[dict]:
    rows = fetch_all(
        """
        SELECT dd.*, d.device_name, d.device_alias, d.status AS device_status
          FROM device_deployments dd
          JOIN devices d ON d.device_uid = dd.device_uid
         WHERE dd.deployment_uid = ?
         ORDER BY d.device_name
        """,
        (deployment_uid,),
    )
    return [dict(r) for r in rows]


def active_for_device(device_uid: str) -> List[dict]:
    rows = fetch_all(
        """
        SELECT dd.*, d.deployment_uid AS dep_uid, d.status AS deployment_status,
               mc.display_name AS model_display_name, mc.version AS model_version
          FROM device_deployments dd
          JOIN deployments d ON d.deployment_uid = dd.deployment_uid
          JOIN model_cards mc ON mc.model_card_uid = d.model_card_uid
         WHERE dd.device_uid = ? AND dd.status NOT IN ('STOPPED','FAILED')
         ORDER BY dd.created_at DESC
        """,
        (device_uid,),
    )
    return [dict(r) for r in rows]
