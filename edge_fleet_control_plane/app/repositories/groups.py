"""Device group repository."""

from __future__ import annotations

from typing import List, Optional

from app.core.time import now_iso
from app.db.session import execute, fetch_all, fetch_one


def list_for_owner(owner: str) -> List[dict]:
    rows = fetch_all(
        """
        SELECT g.*,
               COUNT(d.device_uid) AS device_count,
               SUM(CASE WHEN d.status IN ('ONLINE','RUNNING') THEN 1 ELSE 0 END) AS online_count,
               SUM(CASE WHEN d.status = 'OFFLINE' THEN 1 ELSE 0 END) AS offline_count
          FROM device_groups g
          LEFT JOIN devices d ON d.group_uid = g.group_uid
         WHERE g.owner_tapis_username = ?
         GROUP BY g.group_uid
         ORDER BY g.created_at DESC
        """,
        (owner,),
    )
    return [dict(r) for r in rows]


def get(group_uid: str, owner: str) -> Optional[dict]:
    row = fetch_one(
        "SELECT * FROM device_groups WHERE group_uid = ? AND owner_tapis_username = ?",
        (group_uid, owner),
    )
    return dict(row) if row else None


def get_any(group_uid: str) -> Optional[dict]:
    row = fetch_one("SELECT * FROM device_groups WHERE group_uid = ?", (group_uid,))
    return dict(row) if row else None


def create(*, group_uid: str, owner: str, group_name: str, description: Optional[str], site_name: Optional[str], color_tag: str = "indigo") -> None:
    ts = now_iso()
    execute(
        """
        INSERT INTO device_groups
            (group_uid, owner_tapis_username, group_name, description, site_name, color_tag, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (group_uid, owner, group_name, description, site_name, color_tag, ts, ts),
    )


def update(group_uid: str, owner: str, *, group_name: str, description: Optional[str], site_name: Optional[str], color_tag: str) -> None:
    execute(
        """
        UPDATE device_groups
           SET group_name = ?, description = ?, site_name = ?, color_tag = ?, updated_at = ?
         WHERE group_uid = ? AND owner_tapis_username = ?
        """,
        (group_name, description, site_name, color_tag, now_iso(), group_uid, owner),
    )


def delete(group_uid: str, owner: str) -> None:
    execute("DELETE FROM device_groups WHERE group_uid = ? AND owner_tapis_username = ?", (group_uid, owner))
