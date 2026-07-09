"""Idempotent seed data — runs after migrations on startup."""

from __future__ import annotations

import json
from typing import Any

from app.config import CONFIG_DIR
from app.core.ids import gen_uid
from app.core.logging import get_logger
from app.db.session import fetch_one
from app.repositories import generations as gen_repo
from app.repositories import model_cards as mc_repo
from app.repositories import users as user_repo

log = get_logger("seed")


def seed_everything() -> None:
    from app.config import get_settings
    settings = get_settings()
    user_repo.ensure("system", display_name="System", role="admin")
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
            "env": [
                {"key": k, "value": v, "is_secret": False}
                for k, v in (container.get("env") or {}).items()
            ],
            "mounts": container.get("mounts") or [],
            "docker_args": container.get("docker_args") or [],
            "ports": container.get("ports") or [],
        }
        mc_repo.create_full(
            model_card_uid=r["model_card_uid"],
            app_id=r.get("app_id") or gen_uid("app"),
            owner="system",
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
