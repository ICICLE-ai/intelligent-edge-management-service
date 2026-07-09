"""Device repository."""

from __future__ import annotations

from typing import List, Optional

from app.core.time import now_iso
from app.db.session import execute, fetch_all, fetch_one
from app.services import device_capabilities


_BASE_SELECT = """
SELECT d.*,
       g.group_name      AS group_name,
       g.color_tag       AS group_color,
       dg.display_name   AS generation_name,
       dg.hardware_type  AS generation_hardware,
       dg.architecture   AS generation_arch
  FROM devices d
  LEFT JOIN device_groups       g  ON g.group_uid       = d.group_uid
  LEFT JOIN device_generations  dg ON dg.generation_uid = d.generation_uid
"""


def _row_dict(row) -> dict:
    return device_capabilities.enrich_device(dict(row)) or {}


def list_for_owner(owner: str) -> List[dict]:
    rows = fetch_all(
        _BASE_SELECT
        + " WHERE d.owner_tapis_username = ? ORDER BY d.created_at DESC",
        (owner,),
    )
    return [_row_dict(r) for r in rows]


def list_in_group(owner: str, group_uid: str) -> List[dict]:
    rows = fetch_all(
        _BASE_SELECT
        + " WHERE d.owner_tapis_username = ? AND d.group_uid = ? ORDER BY d.device_name",
        (owner, group_uid),
    )
    return [_row_dict(r) for r in rows]


def get(device_uid: str, owner: str) -> Optional[dict]:
    row = fetch_one(
        _BASE_SELECT + " WHERE d.device_uid = ? AND d.owner_tapis_username = ?",
        (device_uid, owner),
    )
    return _row_dict(row) if row else None


def get_any(device_uid: str) -> Optional[dict]:
    row = fetch_one(_BASE_SELECT + " WHERE d.device_uid = ?", (device_uid,))
    return _row_dict(row) if row else None


def count_for_owner(owner: str) -> dict:
    rows = fetch_all(
        """
        SELECT status, COUNT(*) AS c
          FROM devices
         WHERE owner_tapis_username = ?
         GROUP BY status
        """,
        (owner,),
    )
    by_status = {r["status"]: r["c"] for r in rows}
    total = sum(by_status.values())
    return {
        "total": total,
        "online": by_status.get("ONLINE", 0) + by_status.get("RUNNING", 0),
        "offline": by_status.get("OFFLINE", 0),
        "not_installed": by_status.get("REGISTERED_NOT_INSTALLED", 0) + by_status.get("INSTALLER_READY", 0),
        "by_status": by_status,
    }


def create(
    *,
    device_uid: str,
    owner: str,
    device_name: str,
    device_alias: Optional[str],
    generation_uid: str,
    group_uid: Optional[str],
    site_name: Optional[str],
    capabilities_json: Optional[str] = None,
) -> None:
    ts = now_iso()
    execute(
        """
        INSERT INTO devices
            (device_uid, owner_tapis_username, device_name, device_alias,
             generation_uid, group_uid, site_name, status, capabilities_json,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'REGISTERED_NOT_INSTALLED', ?, ?, ?)
        """,
        (
            device_uid, owner, device_name, device_alias, generation_uid,
            group_uid, site_name, capabilities_json, ts, ts,
        ),
    )


def update_capabilities(device_uid: str, owner: str, capabilities_json: str) -> None:
    execute(
        """
        UPDATE devices
           SET capabilities_json = ?, updated_at = ?
         WHERE device_uid = ? AND owner_tapis_username = ?
        """,
        (capabilities_json, now_iso(), device_uid, owner),
    )


def update(device_uid: str, owner: str, *, device_name: str, device_alias: Optional[str], group_uid: Optional[str], site_name: Optional[str]) -> None:
    execute(
        """
        UPDATE devices
           SET device_name = ?, device_alias = ?, group_uid = ?, site_name = ?, updated_at = ?
         WHERE device_uid = ? AND owner_tapis_username = ?
        """,
        (device_name, device_alias, group_uid, site_name, now_iso(), device_uid, owner),
    )


def update_generation(device_uid: str, owner: str, generation_uid: str) -> None:
    execute(
        """
        UPDATE devices
           SET generation_uid = ?, updated_at = ?
         WHERE device_uid = ? AND owner_tapis_username = ?
        """,
        (generation_uid, now_iso(), device_uid, owner),
    )


def update_status(device_uid: str, status: str) -> None:
    execute(
        "UPDATE devices SET status = ?, updated_at = ? WHERE device_uid = ?",
        (status, now_iso(), device_uid),
    )


def update_after_enrollment(device_uid: str, *, hostname: Optional[str], ip_address: Optional[str], agent_version: Optional[str]) -> None:
    ts = now_iso()
    execute(
        """
        UPDATE devices
           SET status = 'ENROLLED', hostname = ?, ip_address = ?, agent_version = ?, updated_at = ?
         WHERE device_uid = ?
        """,
        (hostname, ip_address, agent_version, ts, device_uid),
    )


def update_after_heartbeat(device_uid: str, *, status: str, hostname: Optional[str], ip_address: Optional[str], agent_version: Optional[str], heartbeat_at: str) -> None:
    execute(
        """
        UPDATE devices
           SET status = ?,
               last_heartbeat_at = ?,
               last_seen_at = ?,
               hostname = COALESCE(?, hostname),
               ip_address = COALESCE(?, ip_address),
               agent_version = COALESCE(?, agent_version),
               updated_at = ?
         WHERE device_uid = ?
        """,
        (status, heartbeat_at, heartbeat_at, hostname, ip_address, agent_version, heartbeat_at, device_uid),
    )


def delete(device_uid: str, owner: str) -> None:
    execute("DELETE FROM devices WHERE device_uid = ? AND owner_tapis_username = ?", (device_uid, owner))


def find_stale(threshold_iso: str) -> List[dict]:
    rows = fetch_all(
        """
        SELECT * FROM devices
         WHERE status IN ('ONLINE','RUNNING','ENROLLED')
           AND last_heartbeat_at IS NOT NULL
           AND last_heartbeat_at < ?
        """,
        (threshold_iso,),
    )
    return [dict(r) for r in rows]
