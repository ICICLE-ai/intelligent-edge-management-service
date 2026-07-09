"""User repository — minimal, the auth provider is Tapis (external)."""

from __future__ import annotations

from typing import Optional

from app.core.time import now_iso
from app.db.session import execute, fetch_one


def get(username: str) -> Optional[dict]:
    row = fetch_one("SELECT * FROM users WHERE tapis_username = ?", (username,))
    return dict(row) if row else None


def ensure(username: str, display_name: Optional[str] = None, role: str = "operator") -> dict:
    existing = get(username)
    if existing:
        if display_name and display_name != existing.get("display_name"):
            execute(
                "UPDATE users SET display_name = ?, updated_at = ? WHERE tapis_username = ?",
                (display_name, now_iso(), username),
            )
            existing = get(username)
        return existing  # type: ignore[return-value]
    ts = now_iso()
    execute(
        """
        INSERT INTO users (tapis_username, display_name, role, status, created_at, updated_at)
        VALUES (?, ?, ?, 'active', ?, ?)
        """,
        (username, display_name or username, role, ts, ts),
    )
    return get(username)  # type: ignore[return-value]


def set_role(username: str, role: str) -> None:
    execute(
        "UPDATE users SET role = ?, updated_at = ? WHERE tapis_username = ?",
        (role, now_iso(), username),
    )
