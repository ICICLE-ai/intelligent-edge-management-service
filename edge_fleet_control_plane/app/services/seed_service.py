"""Idempotent seed data — runs after migrations on startup."""

from __future__ import annotations

import json
from typing import Any

from app.config import CONFIG_DIR
from app.core.ids import gen_uid
from app.core.logging import get_logger
from app.db.session import execute, fetch_one
from app.repositories import generations as gen_repo
from app.repositories import model_cards as mc_repo
from app.repositories import users as user_repo

log = get_logger("seed")


def _seed_env(raw: dict) -> list[dict]:
    """Seed env values are strings, except a secret marked as {"value", "is_secret"}."""
    env: list[dict] = []
    for key, value in raw.items():
        if isinstance(value, dict):
            env.append({
                "key": key,
                "value": "" if value.get("value") is None else str(value.get("value")),
                "is_secret": bool(value.get("is_secret")),
            })
        else:
            env.append({"key": key, "value": str(value), "is_secret": False})
    return env

# Seeded catalog cards belong to this Tapis account, so their owner can edit
# the compatibility minimums the portal enforces.
CATALOG_OWNER = "habg21"


def seed_everything() -> None:
    from app.config import get_settings
    settings = get_settings()
    user_repo.ensure("system", display_name="System", role="admin")
    user_repo.ensure(CATALOG_OWNER, display_name=CATALOG_OWNER, role="operator")
    if settings.local_dev_auth:
        user_repo.ensure(settings.local_dev_username, display_name=settings.local_dev_username, role="admin")
        user_repo.set_role(settings.local_dev_username, "admin")
    _seed_generations()
    _seed_models()


def _seed_generations() -> None:
    path = CONFIG_DIR / "device_generations.json"
    if not path.exists():
        return
    records = json.loads(path.read_text())
    for r in records:
        gen_repo.upsert(r)


def _seed_models() -> None:
    path = CONFIG_DIR / "seed_models.json"
    if not path.exists():
        return
    records: list[dict[str, Any]] = json.loads(path.read_text())
    for r in records:
        if fetch_one("SELECT 1 FROM model_cards WHERE model_card_uid = ?", (r["model_card_uid"],)):
            _refresh_seed_artifact(r)
            _apply_app_kind(r)
            continue
        log.info("seeding model card %s", r["model_card_uid"])
        artifact = r["artifact"].copy()
        artifact["artifact_uid"] = gen_uid("art")
        container = r["container"].copy()
        spec = {
            "spec_uid": gen_uid("spec"),
            "image_registry": container.get("image_registry"),
            "image_repository": container["image_repository"],
            "image_tag": container.get("image_tag") or "latest",
            "container_name": container["container_name"],
            "pull_policy": container.get("pull_policy") or "if_not_present",
            "remove_after_exit": container.get("remove_after_exit"),
            "restart_policy": container.get("restart_policy") or "no",
            "model_env_var": container.get("model_env_var"),
            "network_mode": container.get("network_mode"),
            "gpus": container.get("gpus"),
            "runtime": container.get("runtime"),
            "privileged": container.get("privileged"),
            "ipc_mode": container.get("ipc_mode"),
            "shm_size": container.get("shm_size"),
            "working_dir": container.get("working_dir"),
            "entrypoint": container.get("entrypoint"),
            "command": container.get("command"),
            "env": _seed_env(container.get("env") or {}),
            "mounts": container.get("mounts") or [],
            "docker_args": container.get("docker_args") or [],
            "ports": container.get("ports") or [],
        }
        mc_repo.create_full(
            model_card_uid=r["model_card_uid"],
            app_id=r.get("app_id") or gen_uid("app"),
            owner=CATALOG_OWNER,
            slug=r["slug"],
            display_name=r["display_name"],
            version=r["version"],
            task_type=r.get("task_type"),
            framework=r.get("framework"),
            description=r.get("description"),
            license_name=r.get("license"),
            homepage_url=r.get("homepage_url"),
            tags=r.get("tags") or [],
            status=r.get("status") or "DRAFT",
            visibility=r.get("visibility") or "public",
            patra_model_card_uuid=r.get("patra_model_card_uuid") or artifact.get("patra_model_card_uuid"),
            raw_docker_command=r.get("raw_docker_command"),
            artifact=artifact,
            spec=spec,
            compatibility=r.get("compatibility") or [],
        )
        _apply_app_kind(r)


# Station settings supply these at deploy time; a card value would be stale.
_STATION_ENV_KEYS = ("SCANBOT_TOKEN", "SCANBOT_PULL_HOST")


def _apply_app_kind(r: dict[str, Any]) -> None:
    kind = r.get("app_kind") or "inference"
    uid = r["model_card_uid"]
    execute("UPDATE model_cards SET app_kind = ? WHERE model_card_uid = ?", (kind, uid))
    if kind == "scanbot":
        # The catalog's scan service tracks the seed file, so a new image tag reaches existing portals.
        execute(
            "UPDATE model_cards SET version = ?, raw_docker_command = ? WHERE model_card_uid = ?",
            (r["version"], r.get("raw_docker_command"), uid),
        )
        execute(
            "UPDATE container_specs SET image_tag = ? WHERE model_card_uid = ?",
            (r["container"].get("image_tag") or "latest", uid),
        )
        execute(
            """
            DELETE FROM container_spec_env
             WHERE var_key IN (?, ?)
               AND spec_uid IN (SELECT spec_uid FROM container_specs WHERE model_card_uid = ?)
            """,
            (*_STATION_ENV_KEYS, uid),
        )


def _refresh_seed_artifact(r: dict[str, Any]) -> None:
    """Fill Patra UUID / download URL / engine target on already-seeded cards when seed JSON is updated."""
    artifact = r.get("artifact") or {}
    uid = r["model_card_uid"]
    execute(
        """
        UPDATE model_cards
           SET owner_tapis_username = ?
         WHERE model_card_uid = ? AND owner_tapis_username = 'system'
        """,
        (CATALOG_OWNER, uid),
    )
    for field in mc_repo.ENGINE_FIELDS:
        if artifact.get(field):
            execute(
                f"UPDATE model_artifacts SET {field} = ? WHERE model_card_uid = ?",
                (artifact[field], uid),
            )
    patra = r.get("patra_model_card_uuid") or artifact.get("patra_model_card_uuid")
    download = artifact.get("download_url")
    if not patra and not download:
        return
    if patra:
        execute(
            "UPDATE model_cards SET patra_model_card_uuid = ? WHERE model_card_uid = ?",
            (patra, uid),
        )
        execute(
            "UPDATE model_artifacts SET patra_model_card_uuid = ? WHERE model_card_uid = ?",
            (patra, uid),
        )
    if download:
        execute(
            "UPDATE model_artifacts SET download_url = ? WHERE model_card_uid = ?",
            (download, uid),
        )
