"""Device API key repository — long-lived credentials for edge agents."""

from __future__ import annotations

from typing import Optional

from app.core.time import now_iso
from app.db.session import execute, fetch_one


def create(*, device_uid: str, key_hash: str) -> None:
    execute(
        """
        INSERT INTO device_credentials (device_uid, key_hash, created_at)
        VALUES (?, ?, ?)
        """,
        (device_uid, key_hash, now_iso()),
    )


def get_active_by_hash(key_hash: str) -> Optional[dict]:
    row = fetch_one(
        """
        SELECT * FROM device_credentials
         WHERE key_hash = ? AND revoked_at IS NULL
        """,
        (key_hash,),
    )
    return dict(row) if row else None


def revoke_all_for_device(device_uid: str) -> None:
    execute(
        """
        UPDATE device_credentials
           SET revoked_at = ?
         WHERE device_uid = ? AND revoked_at IS NULL
        """,
        (now_iso(), device_uid),
    )
