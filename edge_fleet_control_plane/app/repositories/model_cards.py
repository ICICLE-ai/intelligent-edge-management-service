"""Model-card repository — owns the model card aggregate (card + artifact + spec + compatibility)."""

from __future__ import annotations

import json
from typing import List, Optional

from app.core.ids import gen_uid
from app.core.time import now_iso
from app.db.session import connection, execute, fetch_all, fetch_one


# ---------------------------------------------------------------------------
# Card-level
# ---------------------------------------------------------------------------

def list_published(*, viewer: Optional[str] = None) -> List[dict]:
    """Published apps visible to a viewer (public + own). Omit viewer for all published."""
    sql = """
        SELECT mc.*,
               ca.filename            AS artifact_filename,
               ca.container_path      AS artifact_container_path,
               cs.image_repository    AS image_repository,
               cs.image_tag           AS image_tag,
               cs.container_name      AS container_name
          FROM model_cards mc
          LEFT JOIN model_artifacts  ca ON ca.model_card_uid = mc.model_card_uid
          LEFT JOIN container_specs  cs ON cs.model_card_uid = mc.model_card_uid
         WHERE mc.status = 'PUBLISHED'
    """
    params: tuple = ()
    if viewer is not None:
        sql += " AND (mc.visibility = 'public' OR mc.owner_tapis_username = ?)"
        params = (viewer,)
    sql += " ORDER BY mc.published_at DESC, mc.updated_at DESC"
    rows = fetch_all(sql, params)
    return [dict(r) for r in rows]


def get_by_owner_slug_version(owner: str, slug: str, version: str) -> Optional[dict]:
    row = fetch_one(
        """
        SELECT * FROM model_cards
         WHERE owner_tapis_username = ? AND slug = ? AND version = ?
        """,
        (owner, slug, version),
    )
    return dict(row) if row else None


def get_by_patra_uuid(patra_uuid: str) -> Optional[dict]:
    row = fetch_one(
        "SELECT * FROM model_cards WHERE patra_model_card_uuid = ?",
        (patra_uuid,),
    )
    return dict(row) if row else None


def list_for_owner(owner: str, *, include_drafts: bool = True) -> List[dict]:
    sql = (
        "SELECT * FROM model_cards WHERE owner_tapis_username = ?"
        + (" AND status != 'DRAFT'" if not include_drafts else "")
        + " ORDER BY updated_at DESC"
    )
    rows = fetch_all(sql, (owner,))
    return [dict(r) for r in rows]


def get(model_card_uid: str) -> Optional[dict]:
    row = fetch_one(
        "SELECT * FROM model_cards WHERE model_card_uid = ?", (model_card_uid,)
    )
    return dict(row) if row else None


def get_full(model_card_uid: str) -> Optional[dict]:
    """Return the complete aggregate: card + artifact + spec + env/mount/args + compatibility."""
    card = get(model_card_uid)
    if not card:
        return None
    card["tags"] = _decode_tags(card.get("tags_json"))
    card["artifact"] = _get_artifact(model_card_uid)
    card["spec"] = _get_spec(model_card_uid)
    card["compatibility"] = _get_compatibility(model_card_uid)
    return card


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def create_full(*, model_card_uid: str, app_id: str, owner: str, slug: str, display_name: str, version: str,
                task_type: Optional[str], framework: Optional[str], description: Optional[str],
                license_name: Optional[str], homepage_url: Optional[str], tags: List[str],
                status: str, visibility: str, patra_model_card_uuid: Optional[str],
                raw_docker_command: Optional[str],
                artifact: dict, spec: dict, compatibility: List[dict]) -> None:
    ts = now_iso()
    published_at = ts if status == "PUBLISHED" else None
    with connection() as con:
        con.execute(
            """
            INSERT INTO model_cards
                (model_card_uid, app_id, owner_tapis_username, slug, display_name, version,
                 task_type, framework, description, license, homepage_url, tags_json,
                 status, visibility, published_at, patra_model_card_uuid, raw_docker_command,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (model_card_uid, app_id, owner, slug, display_name, version, task_type, framework,
             description, license_name, homepage_url, json.dumps(tags), status, visibility,
             published_at, patra_model_card_uuid, raw_docker_command, ts, ts),
        )
        _insert_artifact(con, model_card_uid, artifact, ts)
        _insert_spec(con, model_card_uid, spec, ts)
        for compat in compatibility:
            _insert_compat(con, model_card_uid, compat, ts)


def update_full(*, model_card_uid: str, display_name: str, version: str,
                task_type: Optional[str], framework: Optional[str], description: Optional[str],
                license_name: Optional[str], homepage_url: Optional[str], tags: List[str],
                status: str, visibility: str, patra_model_card_uuid: Optional[str],
                raw_docker_command: Optional[str],
                artifact: dict, spec: dict, compatibility: List[dict]) -> None:
    ts = now_iso()
    with connection() as con:
        cur_status = con.execute(
            "SELECT status, published_at FROM model_cards WHERE model_card_uid = ?",
            (model_card_uid,),
        ).fetchone()
        published_at = cur_status["published_at"] if cur_status else None
        if status == "PUBLISHED" and not published_at:
            published_at = ts
        con.execute(
            """
            UPDATE model_cards
               SET display_name = ?, version = ?, task_type = ?, framework = ?,
                   description = ?, license = ?, homepage_url = ?, tags_json = ?,
                   status = ?, visibility = ?, published_at = ?, patra_model_card_uuid = ?,
                   raw_docker_command = ?, updated_at = ?
             WHERE model_card_uid = ?
            """,
            (display_name, version, task_type, framework, description, license_name,
             homepage_url, json.dumps(tags), status, visibility, published_at,
             patra_model_card_uuid, raw_docker_command, ts, model_card_uid),
        )
        # Update artifact + spec in place so deployment FKs (artifact_uid, spec_uid) stay valid.
        _update_artifact(con, model_card_uid, artifact, ts)
        _update_spec(con, model_card_uid, spec, ts)
        con.execute("DELETE FROM model_compatibility WHERE model_card_uid = ?", (model_card_uid,))
        for compat in compatibility:
            _insert_compat(con, model_card_uid, compat, ts)


def set_status(model_card_uid: str, status: str) -> None:
    ts = now_iso()
    if status == "PUBLISHED":
        execute(
            """
            UPDATE model_cards
               SET status = ?, published_at = COALESCE(published_at, ?), updated_at = ?
             WHERE model_card_uid = ?
            """,
            (status, ts, ts, model_card_uid),
        )
    else:
        execute(
            "UPDATE model_cards SET status = ?, updated_at = ? WHERE model_card_uid = ?",
            (status, ts, model_card_uid),
        )


def delete(model_card_uid: str) -> None:
    execute("DELETE FROM model_cards WHERE model_card_uid = ?", (model_card_uid,))


# ---------------------------------------------------------------------------
# Compatibility helpers
# ---------------------------------------------------------------------------

def models_compatible_with_generation_for_owner(
    generation_uid: str, owner: str,
) -> List[dict]:
    rows = fetch_all(
        """
        SELECT mc.* FROM model_cards mc
          JOIN model_compatibility c ON c.model_card_uid = mc.model_card_uid
         WHERE c.generation_uid = ? AND mc.status = 'PUBLISHED'
           AND mc.owner_tapis_username = ?
         ORDER BY mc.published_at DESC
        """,
        (generation_uid, owner),
    )
    return [dict(r) for r in rows]


def models_compatible_with_generation_public(
    generation_uid: str, owner: str,
) -> List[dict]:
    rows = fetch_all(
        """
        SELECT mc.* FROM model_cards mc
          JOIN model_compatibility c ON c.model_card_uid = mc.model_card_uid
         WHERE c.generation_uid = ? AND mc.status = 'PUBLISHED'
           AND mc.visibility = 'public'
           AND mc.owner_tapis_username != ?
         ORDER BY mc.published_at DESC
        """,
        (generation_uid, owner),
    )
    return [dict(r) for r in rows]


def models_compatible_with_devices_for_owner(
    generation_uids: List[str], owner: str,
) -> List[dict]:
    if not generation_uids:
        return []
    placeholders = ",".join("?" for _ in generation_uids)
    rows = fetch_all(
        f"""
        SELECT mc.* FROM model_cards mc
          JOIN model_compatibility c ON c.model_card_uid = mc.model_card_uid
         WHERE c.generation_uid IN ({placeholders}) AND mc.status = 'PUBLISHED'
           AND mc.owner_tapis_username = ?
         GROUP BY mc.model_card_uid
        HAVING COUNT(DISTINCT c.generation_uid) = ?
         ORDER BY mc.published_at DESC
        """,
        (*generation_uids, owner, len(set(generation_uids))),
    )
    return [dict(r) for r in rows]


def models_compatible_with_devices_public(
    generation_uids: List[str], owner: str,
) -> List[dict]:
    if not generation_uids:
        return []
    placeholders = ",".join("?" for _ in generation_uids)
    rows = fetch_all(
        f"""
        SELECT mc.* FROM model_cards mc
          JOIN model_compatibility c ON c.model_card_uid = mc.model_card_uid
         WHERE c.generation_uid IN ({placeholders}) AND mc.status = 'PUBLISHED'
           AND mc.visibility = 'public'
           AND mc.owner_tapis_username != ?
         GROUP BY mc.model_card_uid
        HAVING COUNT(DISTINCT c.generation_uid) = ?
         ORDER BY mc.published_at DESC
        """,
        (*generation_uids, owner, len(set(generation_uids))),
    )
    return [dict(r) for r in rows]


def models_compatible_with_generation(generation_uid: str) -> List[dict]:
    rows = fetch_all(
        """
        SELECT mc.* FROM model_cards mc
          JOIN model_compatibility c ON c.model_card_uid = mc.model_card_uid
         WHERE c.generation_uid = ? AND mc.status = 'PUBLISHED'
         ORDER BY mc.published_at DESC
        """,
        (generation_uid,),
    )
    return [dict(r) for r in rows]


def models_compatible_with_devices(generation_uids: List[str]) -> List[dict]:
    """Models compatible with every supplied generation (intersection)."""
    if not generation_uids:
        return []
    placeholders = ",".join("?" for _ in generation_uids)
    rows = fetch_all(
        f"""
        SELECT mc.* FROM model_cards mc
          JOIN model_compatibility c ON c.model_card_uid = mc.model_card_uid
         WHERE c.generation_uid IN ({placeholders}) AND mc.status = 'PUBLISHED'
         GROUP BY mc.model_card_uid
        HAVING COUNT(DISTINCT c.generation_uid) = ?
         ORDER BY mc.published_at DESC
        """,
        (*generation_uids, len(set(generation_uids))),
    )
    return [dict(r) for r in rows]


def compatibility_matrix(*, viewer: Optional[str] = None) -> List[dict]:
    sql = """
        SELECT mc.model_card_uid, mc.display_name, mc.version, mc.status,
               mc.visibility, mc.owner_tapis_username,
               c.generation_uid, dg.display_name AS generation_name
          FROM model_cards mc
          JOIN model_compatibility c ON c.model_card_uid = mc.model_card_uid
          JOIN device_generations dg ON dg.generation_uid = c.generation_uid
         WHERE mc.status = 'PUBLISHED'
    """
    params: tuple = ()
    if viewer is not None:
        sql += " AND (mc.visibility = 'public' OR mc.owner_tapis_username = ?)"
        params = (viewer,)
    sql += " ORDER BY mc.display_name, dg.display_name"
    return [dict(r) for r in fetch_all(sql, params)]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _decode_tags(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except json.JSONDecodeError:
        return []


def _get_artifact(model_card_uid: str) -> Optional[dict]:
    row = fetch_one(
        "SELECT * FROM model_artifacts WHERE model_card_uid = ? ORDER BY created_at ASC LIMIT 1",
        (model_card_uid,),
    )
    return dict(row) if row else None


def _get_spec(model_card_uid: str) -> Optional[dict]:
    row = fetch_one(
        "SELECT * FROM container_specs WHERE model_card_uid = ?",
        (model_card_uid,),
    )
    if not row:
        return None
    spec = dict(row)
    spec_uid = spec["spec_uid"]
    spec["env"] = [
        dict(r)
        for r in fetch_all(
            "SELECT * FROM container_spec_env WHERE spec_uid = ? ORDER BY sort_order, id",
            (spec_uid,),
        )
    ]
    spec["mounts"] = [
        dict(r)
        for r in fetch_all(
            "SELECT * FROM container_spec_mounts WHERE spec_uid = ? ORDER BY sort_order, id",
            (spec_uid,),
        )
    ]
    spec["docker_args"] = [
        dict(r)
        for r in fetch_all(
            "SELECT * FROM container_spec_docker_args WHERE spec_uid = ? ORDER BY sort_order, id",
            (spec_uid,),
        )
    ]
    spec["ports"] = [
        dict(r)
        for r in fetch_all(
            "SELECT * FROM container_spec_ports WHERE spec_uid = ? ORDER BY sort_order, id",
            (spec_uid,),
        )
    ]
    return spec


def _get_compatibility(model_card_uid: str) -> List[dict]:
    rows = fetch_all(
        """
        SELECT c.*, dg.display_name AS generation_name, dg.hardware_type
          FROM model_compatibility c
          JOIN device_generations dg ON dg.generation_uid = c.generation_uid
         WHERE c.model_card_uid = ?
         ORDER BY dg.display_name
        """,
        (model_card_uid,),
    )
    return [dict(r) for r in rows]


def _insert_artifact(con, model_card_uid: str, artifact: dict, ts: str) -> None:
    con.execute(
        """
        INSERT INTO model_artifacts
            (artifact_uid, model_card_uid, filename, container_path, size_bytes, sha256,
             source_type, patra_model_card_uuid, download_url, content_type, notes,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            artifact["artifact_uid"],
            model_card_uid,
            artifact["filename"],
            artifact["container_path"],
            artifact.get("size_bytes"),
            artifact.get("sha256"),
            artifact["source_type"],
            artifact.get("patra_model_card_uuid"),
            artifact.get("download_url"),
            artifact.get("content_type"),
            artifact.get("notes"),
            ts,
            ts,
        ),
    )


def _update_artifact(con, model_card_uid: str, artifact: dict, ts: str) -> None:
    existing = con.execute(
        "SELECT artifact_uid FROM model_artifacts WHERE model_card_uid = ? LIMIT 1",
        (model_card_uid,),
    ).fetchone()
    if existing:
        artifact["artifact_uid"] = existing["artifact_uid"]
        con.execute(
            """
            UPDATE model_artifacts
               SET filename = ?, container_path = ?, size_bytes = ?, sha256 = ?,
                   source_type = ?, patra_model_card_uuid = ?, download_url = ?,
                   content_type = ?, notes = ?, updated_at = ?
             WHERE artifact_uid = ?
            """,
            (
                artifact["filename"],
                artifact["container_path"],
                artifact.get("size_bytes"),
                artifact.get("sha256"),
                artifact["source_type"],
                artifact.get("patra_model_card_uuid"),
                artifact.get("download_url"),
                artifact.get("content_type"),
                artifact.get("notes"),
                ts,
                existing["artifact_uid"],
            ),
        )
    else:
        if not artifact.get("artifact_uid"):
            artifact["artifact_uid"] = gen_uid("art")
        _insert_artifact(con, model_card_uid, artifact, ts)


def _insert_spec(con, model_card_uid: str, spec: dict, ts: str) -> None:
    con.execute(
        """
        INSERT INTO container_specs
            (spec_uid, model_card_uid, image_registry, image_repository, image_tag,
             image_digest, container_name, pull_policy, remove_after_exit,
             restart_policy, entrypoint_json, command_json, working_dir, model_env_var,
             network_mode, gpus, runtime, privileged, ipc_mode, shm_size,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            spec["spec_uid"],
            model_card_uid,
            spec.get("image_registry"),
            spec["image_repository"],
            spec.get("image_tag") or "latest",
            spec.get("image_digest"),
            spec["container_name"],
            spec.get("pull_policy") or "if_not_present",
            int(bool(spec.get("remove_after_exit"))),
            spec.get("restart_policy") or "no",
            json.dumps(spec.get("entrypoint")) if spec.get("entrypoint") else None,
            json.dumps(spec.get("command")) if spec.get("command") else None,
            spec.get("working_dir"),
            spec.get("model_env_var"),
            spec.get("network_mode"),
            spec.get("gpus"),
            spec.get("runtime"),
            int(bool(spec.get("privileged"))),
            spec.get("ipc_mode"),
            spec.get("shm_size"),
            ts,
            ts,
        ),
    )
    _insert_spec_children(con, spec)


def _update_spec(con, model_card_uid: str, spec: dict, ts: str) -> None:
    existing = con.execute(
        "SELECT spec_uid FROM container_specs WHERE model_card_uid = ? LIMIT 1",
        (model_card_uid,),
    ).fetchone()
    if existing:
        spec_uid = existing["spec_uid"]
        spec["spec_uid"] = spec_uid
        con.execute(
            """
            UPDATE container_specs
               SET image_registry = ?, image_repository = ?, image_tag = ?,
                   image_digest = ?, container_name = ?, pull_policy = ?,
                   remove_after_exit = ?, restart_policy = ?, entrypoint_json = ?,
                   command_json = ?, working_dir = ?, model_env_var = ?,
                   network_mode = ?, gpus = ?, runtime = ?, privileged = ?,
                   ipc_mode = ?, shm_size = ?, updated_at = ?
             WHERE spec_uid = ?
            """,
            (
                spec.get("image_registry"),
                spec["image_repository"],
                spec.get("image_tag") or "latest",
                spec.get("image_digest"),
                spec["container_name"],
                spec.get("pull_policy") or "if_not_present",
                int(bool(spec.get("remove_after_exit"))),
                spec.get("restart_policy") or "no",
                json.dumps(spec.get("entrypoint")) if spec.get("entrypoint") else None,
                json.dumps(spec.get("command")) if spec.get("command") else None,
                spec.get("working_dir"),
                spec.get("model_env_var"),
                spec.get("network_mode"),
                spec.get("gpus"),
                spec.get("runtime"),
                int(bool(spec.get("privileged"))),
                spec.get("ipc_mode"),
                spec.get("shm_size"),
                ts,
                spec_uid,
            ),
        )
        con.execute("DELETE FROM container_spec_env WHERE spec_uid = ?", (spec_uid,))
        con.execute("DELETE FROM container_spec_mounts WHERE spec_uid = ?", (spec_uid,))
        con.execute("DELETE FROM container_spec_docker_args WHERE spec_uid = ?", (spec_uid,))
        con.execute("DELETE FROM container_spec_ports WHERE spec_uid = ?", (spec_uid,))
        _insert_spec_children(con, spec)
    else:
        if not spec.get("spec_uid"):
            spec["spec_uid"] = gen_uid("spec")
        _insert_spec(con, model_card_uid, spec, ts)


def _insert_spec_children(con, spec: dict) -> None:
    spec_uid = spec["spec_uid"]
    for i, e in enumerate(spec.get("env") or []):
        con.execute(
            """
            INSERT INTO container_spec_env (spec_uid, var_key, var_value, is_secret, sort_order)
            VALUES (?, ?, ?, ?, ?)
            """,
            (spec_uid, e["key"], e["value"], int(bool(e.get("is_secret"))), i),
        )
    for i, m in enumerate(spec.get("mounts") or []):
        con.execute(
            """
            INSERT INTO container_spec_mounts
                (spec_uid, source, target, mount_style, mount_type, mode, sort_order)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (spec_uid, m["source"], m["target"], m.get("style") or "volume",
             m.get("type") or "bind", m.get("mode"), i),
        )
    for i, a in enumerate(spec.get("docker_args") or []):
        con.execute(
            "INSERT INTO container_spec_docker_args (spec_uid, arg, sort_order) VALUES (?, ?, ?)",
            (spec_uid, a, i),
        )
    for i, p in enumerate(spec.get("ports") or []):
        con.execute(
            """
            INSERT INTO container_spec_ports
                (spec_uid, host_port, container_port, protocol, sort_order)
            VALUES (?, ?, ?, ?, ?)
            """,
            (spec_uid, p.get("host_port"), p["container_port"], p.get("protocol") or "tcp", i),
        )


def _insert_compat(con, model_card_uid: str, compat: dict, ts: str) -> None:
    con.execute(
        """
        INSERT INTO model_compatibility
            (model_card_uid, generation_uid, min_memory_mb, min_storage_gb,
             requires_cuda, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            model_card_uid,
            compat["generation_uid"],
            compat.get("min_memory_mb"),
            compat.get("min_storage_gb"),
            int(bool(compat.get("requires_cuda", True))),
            compat.get("notes"),
            ts,
        ),
    )
