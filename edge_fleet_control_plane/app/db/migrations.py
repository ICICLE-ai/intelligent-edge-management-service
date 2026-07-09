"""Lightweight SQL-file migration runner."""

from __future__ import annotations

import hashlib
from typing import Iterable

from app.config import MIGRATIONS_DIR, get_settings
from app.core.logging import get_logger
from app.core.time import now_iso
from app.db.session import _execute, _open, _uses_postgres, _split_sql_statements

log = get_logger("migrations")

_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version       TEXT PRIMARY KEY,
    sha256        TEXT NOT NULL,
    applied_at    TEXT NOT NULL
);
"""


def _migration_files() -> Iterable[tuple[str, str]]:
    root = MIGRATIONS_DIR / "postgres" if _uses_postgres() else MIGRATIONS_DIR
    files = sorted(p for p in root.glob("*.sql"))
    for f in files:
        yield f.stem, f.read_text()


def run_migrations() -> None:
    if _uses_postgres():
        _run_postgres_migrations()
    else:
        _run_sqlite_migrations()


def _run_sqlite_migrations() -> None:
    con = _open()
    try:
        con.execute(_TRACKING_TABLE)
        applied = {row["version"] for row in con.execute("SELECT version FROM schema_migrations")}
        for version, body in _migration_files():
            if version in applied:
                continue
            digest = hashlib.sha256(body.encode()).hexdigest()
            log.info("applying migration %s", version)
            con.executescript(body)
            con.execute(
                "INSERT INTO schema_migrations(version, sha256, applied_at) VALUES (?, ?, ?)",
                (version, digest, now_iso()),
            )
        con.commit()
    finally:
        con.close()


def _run_postgres_migrations() -> None:
    con = _open()
    try:
        con.execute("BEGIN")
        _execute(con, _TRACKING_TABLE)
        applied = {
            row["version"]
            for row in _execute(con, "SELECT version FROM schema_migrations").fetchall()
        }
        for version, body in _migration_files():
            if version in applied:
                continue
            digest = hashlib.sha256(body.encode()).hexdigest()
            log.info("applying migration %s", version)
            for stmt in _split_sql_statements(body):
                if stmt.strip():
                    con.execute(stmt)
            _execute(
                con,
                "INSERT INTO schema_migrations(version, sha256, applied_at) VALUES (?, ?, ?)",
                (version, digest, now_iso()),
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
