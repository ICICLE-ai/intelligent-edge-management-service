"""Database connection helpers used by repositories.

Supports SQLite (local dev) and PostgreSQL (production on Tapis pods) via
DATABASE_URL. Repository SQL uses ``?`` placeholders; they are translated to
``%s`` for PostgreSQL automatically.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Optional, Sequence, Tuple, Union

from app.config import get_settings

Connection = Union[sqlite3.Connection, Any]


class _ConnectionWrapper:
    """Wraps a DB connection so ``execute()`` always uses adapted placeholders."""

    __slots__ = ("_con",)

    def __init__(self, con: Connection) -> None:
        self._con = con

    def execute(self, sql: str, params: Sequence[Any] | Tuple = ()) -> Any:
        return _execute(self._con, sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._con, name)


def _uses_postgres() -> bool:
    return bool(get_settings().database_url)


def _adapt_sql(sql: str) -> str:
    if _uses_postgres():
        return sql.replace("?", "%s")
    return sql


def _open() -> Connection:
    settings = get_settings()
    if settings.database_url:
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(
            settings.database_url,
            row_factory=dict_row,
            connect_timeout=10,
        )
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(
        str(settings.database_path),
        check_same_thread=False,
        isolation_level=None,
        timeout=30,
    )
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def _begin(con: Connection) -> None:
    if _uses_postgres():
        con.execute("BEGIN")
    else:
        con.execute("BEGIN")


def _commit(con: Connection) -> None:
    if _uses_postgres():
        con.commit()
    else:
        con.execute("COMMIT")


def _rollback(con: Connection) -> None:
    if _uses_postgres():
        con.rollback()
    else:
        con.execute("ROLLBACK")


def _execute(con: Connection, sql: str, params: Sequence[Any] | Tuple = ()) -> Any:
    return con.execute(_adapt_sql(sql), tuple(params))


@contextmanager
def connection() -> Iterator[_ConnectionWrapper]:
    con = _open()
    wrapped = _ConnectionWrapper(con)
    try:
        _begin(con)
        yield wrapped
        _commit(con)
    except Exception:
        _rollback(con)
        raise
    finally:
        con.close()


def fetch_one(sql: str, params: Sequence[Any] | Tuple = ()) -> Optional[Any]:
    with connection() as con:
        cur = _execute(con, sql, params)
        return cur.fetchone()


def fetch_all(sql: str, params: Sequence[Any] | Tuple = ()) -> list[Any]:
    with connection() as con:
        cur = _execute(con, sql, params)
        return list(cur.fetchall())


def execute(sql: str, params: Sequence[Any] | Tuple = ()) -> int:
    with connection() as con:
        cur = _execute(con, sql, params)
        return cur.rowcount if _uses_postgres() else cur.lastrowid


def executemany(sql: str, seq: Sequence[Sequence[Any]]) -> None:
    if not seq:
        return
    adapted = _adapt_sql(sql)
    rows = [tuple(row) for row in seq]
    with connection() as con:
        if _uses_postgres():
            with con.cursor() as cur:
                cur.executemany(adapted, rows)
        else:
            con.executemany(adapted, rows)


def execute_script(script: str) -> None:
    con = _open()
    try:
        if _uses_postgres():
            import psycopg

            for stmt in _split_sql_statements(script):
                if stmt.strip():
                    con.execute(stmt)
            con.commit()
        else:
            con.executescript(script)
            con.commit()
    finally:
        con.close()


def _split_sql_statements(script: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        buf.append(line)
        if ";" in line:
            chunk = "\n".join(buf)
            for piece in chunk.split(";"):
                piece = piece.strip()
                if piece:
                    parts.append(piece)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts
