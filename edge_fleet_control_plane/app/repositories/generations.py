"""Device-generation catalog repository."""

from __future__ import annotations

from typing import List, Optional

from app.core.time import now_iso
from app.db.session import execute, fetch_all, fetch_one


def list_active() -> List[dict]:
    rows = fetch_all(
        "SELECT * FROM device_generations WHERE is_active = 1 ORDER BY display_name"
    )
    return [dict(r) for r in rows]


def list_all() -> List[dict]:
    rows = fetch_all("SELECT * FROM device_generations ORDER BY display_name")
    return [dict(r) for r in rows]


def get(generation_uid: str) -> Optional[dict]:
    row = fetch_one(
        "SELECT * FROM device_generations WHERE generation_uid = ?",
        (generation_uid,),
    )
    return dict(row) if row else None


def upsert(record: dict) -> None:
    ts = now_iso()
    existing = get(record["generation_uid"])
    if existing:
        execute(
            """
            UPDATE device_generations
               SET display_name = ?, vendor = ?, device_family = ?, hardware_type = ?,
                   architecture = ?, cuda_supported = ?, default_runtime = ?,
                   cpu_cores = ?, memory_mb = ?, storage_gb = ?, description = ?,
                   is_active = ?, updated_at = ?
             WHERE generation_uid = ?
            """,
            (
                record["display_name"],
                record.get("vendor"),
                record.get("device_family"),
                record["hardware_type"],
                record["architecture"],
                int(bool(record.get("cuda_supported", True))),
                record.get("default_runtime"),
                record.get("cpu_cores"),
                record.get("memory_mb"),
                record.get("storage_gb"),
                record.get("description"),
                int(bool(record.get("is_active", True))),
                ts,
                record["generation_uid"],
            ),
        )
        return
    execute(
        """
        INSERT INTO device_generations
            (generation_uid, display_name, vendor, device_family, hardware_type,
             architecture, cuda_supported, default_runtime, cpu_cores, memory_mb,
             storage_gb, description, is_active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record["generation_uid"],
            record["display_name"],
            record.get("vendor"),
            record.get("device_family"),
            record["hardware_type"],
            record["architecture"],
            int(bool(record.get("cuda_supported", True))),
            record.get("default_runtime"),
            record.get("cpu_cores"),
            record.get("memory_mb"),
            record.get("storage_gb"),
            record.get("description"),
            int(bool(record.get("is_active", True))),
            ts,
            ts,
        ),
    )
