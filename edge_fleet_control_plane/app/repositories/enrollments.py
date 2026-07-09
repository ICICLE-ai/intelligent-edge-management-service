"""Enrollment-token repository."""

from __future__ import annotations

from typing import Optional

from app.core.time import now_iso
from app.db.session import execute, fetch_one


def create(*, device_uid: str, owner: str, token_hash: str, expires_at: str) -> None:
    execute(
        """
        INSERT INTO device_enrollments
            (device_uid, owner_tapis_username, token_hash, expires_at, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (device_uid, owner, token_hash, expires_at, now_iso()),
    )


def get_by_hash(token_hash: str) -> Optional[dict]:
    row = fetch_one("SELECT * FROM device_enrollments WHERE token_hash = ?", (token_hash,))
    return dict(row) if row else None


def get_latest_for_device(device_uid: str) -> Optional[dict]:
    row = fetch_one(
        """
        SELECT * FROM device_enrollments
         WHERE device_uid = ?
         ORDER BY created_at DESC LIMIT 1
        """,
        (device_uid,),
    )
    return dict(row) if row else None


def mark_downloaded(device_uid: str) -> None:
    execute(
        """
        UPDATE device_enrollments
           SET installer_downloaded_at = ?
         WHERE device_uid = ? AND used_at IS NULL
        """,
        (now_iso(), device_uid),
    )


def mark_used(enrollment_id: int) -> None:
    execute("UPDATE device_enrollments SET used_at = ? WHERE id = ?", (now_iso(), enrollment_id))
