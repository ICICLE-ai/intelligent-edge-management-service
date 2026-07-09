"""Model-card publishing logic.

The Researcher interface posts a (possibly large) form representing the whole
aggregate: card metadata, artifact source, container spec, env vars, mounts,
docker args, ports, and device compatibility. This service builds the right
shape for the repository, validates it, and persists it atomically.
"""

from __future__ import annotations

import json
import re
from typing import List, Optional

from app.core.errors import ForbiddenError, NotFoundError, ValidationError
from app.core.ids import gen_uid
from app.repositories import generations as gen_repo
from app.repositories import model_cards as mc_repo
from app.services import event_service
from app.services.docker_parser import (
    MODEL_HOST_TEMPLATE,
    ParsedCommand,
    parse as parse_docker_command,
    render_from_spec,
)

_VALID_STATUSES = {"DRAFT", "PUBLISHED", "DEPRECATED"}
_VALID_VISIBILITY = {"private", "public"}
_VALID_PULL_POLICIES = {"always", "if_not_present", "never"}
_VALID_RESTART_POLICIES = {"no", "on-failure", "always", "unless-stopped"}
_VALID_MOUNT_STYLES = {"mount", "volume"}
_VALID_MOUNT_TYPES = {"bind", "volume", "tmpfs"}
_VALID_MOUNT_MODES = {None, "", "ro", "rw"}
_VALID_PORT_PROTO = {"tcp", "udp"}
_VALID_NETWORK_MODES = {None, "", "bridge", "host", "none"}
_VALID_SOURCE_TYPES = {"patra", "url"}

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_PATRA_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


# ---------------------------------------------------------------------------
# Docker command preview / parse (used by the live form)
# ---------------------------------------------------------------------------

def parse_command_for_form(command: str,
                           *,
                           model_container_path: Optional[str] = None,
                           model_env_var: Optional[str] = None) -> dict:
    """Parse a raw docker command into the structured shape consumed by the
    form, and produce a canonical re-rendered preview."""
    parsed = parse_docker_command(
        command,
        model_container_path=model_container_path,
        model_env_var=model_env_var,
    )
    spec = parsed.as_form_dict()
    preview = render_from_spec(spec)
    return {
        "spec": spec,
        "warnings": parsed.warnings,
        "preview_command": preview,
        "model_host_template": MODEL_HOST_TEMPLATE,
    }


def render_preview_from_form(form: dict) -> str:
    """Best-effort preview of the docker command from the current form state."""
    payload = build_payload_from_form(form)
    spec = payload["spec"]
    return render_from_spec(spec)


def list_published(owner: Optional[str] = None) -> List[dict]:
    """Published apps visible to the caller. Pass owner to filter private apps."""
    return mc_repo.list_published(viewer=owner)


def list_for_owner(owner: str) -> List[dict]:
    return mc_repo.list_for_owner(owner)


def get_full(model_card_uid: str) -> dict:
    """Load an app without access checks — internal / admin use only."""
    card = mc_repo.get_full(model_card_uid)
    if not card:
        raise NotFoundError("App not found")
    return card


def can_view(owner: str, card: dict) -> bool:
    if card.get("owner_tapis_username") == owner:
        return True
    if card.get("status") != "PUBLISHED":
        return False
    return (card.get("visibility") or "private") == "public"


def is_owner(owner: str, card: dict) -> bool:
    return card.get("owner_tapis_username") == owner


def require_owner(owner: str, card: dict) -> None:
    if not is_owner(owner, card):
        raise ForbiddenError("You don't own this app.")


def get_full_for_user(owner: str, model_card_uid: str) -> dict:
    card = get_full(model_card_uid)
    if not can_view(owner, card):
        raise ForbiddenError("You don't have access to this app.")
    return card


def get_full_for_owner(owner: str, model_card_uid: str) -> dict:
    card = get_full(model_card_uid)
    require_owner(owner, card)
    return card


def can_deploy(owner: str, card: dict) -> bool:
    if card.get("status") != "PUBLISHED":
        return False
    if card.get("owner_tapis_username") == owner:
        return True
    return (card.get("visibility") or "private") == "public"


def deployable_for_device(owner: str, device: dict) -> dict:
    if not device:
        return {"mine": [], "others": []}
    gen = device["generation_uid"]
    return {
        "mine": mc_repo.models_compatible_with_generation_for_owner(gen, owner),
        "others": mc_repo.models_compatible_with_generation_public(gen, owner),
    }


def deployable_for_group_devices(owner: str, group_devices: List[dict]) -> dict:
    gens = list({d["generation_uid"] for d in group_devices if d.get("generation_uid")})
    if not gens:
        return {"mine": [], "others": []}
    return {
        "mine": mc_repo.models_compatible_with_devices_for_owner(gens, owner),
        "others": mc_repo.models_compatible_with_devices_public(gens, owner),
    }


def compatibility_matrix(owner: Optional[str] = None) -> List[dict]:
    return mc_repo.compatibility_matrix(viewer=owner)


# ---------------------------------------------------------------------------
# Form ingestion
# ---------------------------------------------------------------------------

def build_payload_from_form(form: dict) -> dict:
    """Translate the flat HTML form into a structured aggregate dict.

    Form conventions (so we keep templates simple):
        artifact_*           — artifact fields
        spec_*               — container spec fields
        env_key[],  env_value[],  env_is_secret[]
        mount_source[], mount_target[], mount_style[], mount_type[], mount_mode[]
        docker_arg[]
        port_host[], port_container[], port_protocol[]
        compat_generation_uid[], compat_min_memory_mb[], compat_min_storage_gb[],
            compat_requires_cuda[], compat_notes[]
    """
    def get_list(key: str) -> List[str]:
        v = form.get(key)
        if v is None:
            return []
        if isinstance(v, list):
            return [str(x) for x in v]
        return [str(v)]

    artifact = {
        "filename": (form.get("artifact_filename") or "").strip(),
        "container_path": (form.get("artifact_container_path") or "").strip(),
        "source_type": (form.get("artifact_source_type") or "url").strip(),
        "patra_model_card_uuid": (form.get("artifact_patra_uuid") or "").strip() or None,
        "download_url": (form.get("artifact_download_url") or "").strip() or None,
        "content_type": (form.get("artifact_content_type") or "").strip() or None,
        "size_bytes": _to_int(form.get("artifact_size_bytes")),
        "sha256": (form.get("artifact_sha256") or "").strip() or None,
        "notes": (form.get("artifact_notes") or "").strip() or None,
    }

    env_keys = get_list("env_key")
    env_vals = get_list("env_value")
    env_secrets = set(get_list("env_is_secret"))
    env = []
    for i, k in enumerate(env_keys):
        k = k.strip()
        if not k:
            continue
        env.append({
            "key": k,
            "value": (env_vals[i] if i < len(env_vals) else ""),
            "is_secret": str(i) in env_secrets or k in env_secrets,
        })

    mount_src = get_list("mount_source")
    mount_tgt = get_list("mount_target")
    mount_style = get_list("mount_style")
    mount_type = get_list("mount_type")
    mount_mode = get_list("mount_mode")
    mounts: List[dict] = []
    for i, src in enumerate(mount_src):
        src = src.strip()
        if not src:
            continue
        tgt = mount_tgt[i] if i < len(mount_tgt) else ""
        if not tgt.strip():
            continue
        mounts.append({
            "source": src,
            "target": tgt.strip(),
            "style": (mount_style[i] if i < len(mount_style) else "volume") or "volume",
            "type": (mount_type[i] if i < len(mount_type) else "bind") or "bind",
            "mode": (mount_mode[i] if i < len(mount_mode) else "") or None,
        })

    docker_args = [a.strip() for a in get_list("docker_arg") if a.strip()]

    p_host = get_list("port_host")
    p_cont = get_list("port_container")
    p_proto = get_list("port_protocol")
    ports: List[dict] = []
    for i, cp in enumerate(p_cont):
        if not cp.strip():
            continue
        ports.append({
            "host_port": _to_int(p_host[i]) if i < len(p_host) else None,
            "container_port": _to_int(cp),
            "protocol": (p_proto[i] if i < len(p_proto) else "tcp") or "tcp",
        })

    spec = {
        "image_registry": (form.get("spec_image_registry") or "").strip() or None,
        "image_repository": (form.get("spec_image_repository") or "").strip(),
        "image_tag": (form.get("spec_image_tag") or "latest").strip() or "latest",
        "image_digest": (form.get("spec_image_digest") or "").strip() or None,
        "container_name": (form.get("spec_container_name") or "").strip(),
        "pull_policy": (form.get("spec_pull_policy") or "if_not_present").strip(),
        "remove_after_exit": _checkbox(form.get("spec_remove_after_exit")),
        "restart_policy": (form.get("spec_restart_policy") or "no").strip(),
        "model_env_var": (form.get("spec_model_env_var") or "").strip() or None,
        "network_mode": (form.get("spec_network_mode") or "").strip() or None,
        "gpus": (form.get("spec_gpus") or "").strip() or None,
        "runtime": (form.get("spec_runtime") or "").strip() or None,
        "privileged": _checkbox(form.get("spec_privileged")),
        "ipc_mode": (form.get("spec_ipc_mode") or "").strip() or None,
        "shm_size": (form.get("spec_shm_size") or "").strip() or None,
        "working_dir": (form.get("spec_working_dir") or "").strip() or None,
        "entrypoint": _split_optional_json_or_lines(form.get("spec_entrypoint")),
        "command": _split_optional_json_or_lines(form.get("spec_command")),
        "env": env,
        "mounts": mounts,
        "docker_args": docker_args,
        "ports": ports,
    }

    compat_gen = get_list("compat_generation_uid")
    compat_mem = get_list("compat_min_memory_mb")
    compat_sto = get_list("compat_min_storage_gb")
    compat_cuda = set(get_list("compat_requires_cuda"))
    compat_notes = get_list("compat_notes")
    compatibility: List[dict] = []
    for i, gen in enumerate(compat_gen):
        gen = gen.strip()
        if not gen:
            continue
        compatibility.append({
            "generation_uid": gen,
            "min_memory_mb": _to_int(compat_mem[i]) if i < len(compat_mem) else None,
            "min_storage_gb": _to_int(compat_sto[i]) if i < len(compat_sto) else None,
            "requires_cuda": (str(i) in compat_cuda or gen in compat_cuda),
            "notes": (compat_notes[i].strip() if i < len(compat_notes) else "") or None,
        })

    tags = [t.strip() for t in (form.get("tags") or "").split(",") if t.strip()]

    patra_uuid = (form.get("patra_model_card_uuid") or form.get("artifact_patra_uuid") or "").strip() or None

    # When the source is Patra, the card's UUID is authoritative; keep the
    # artifact UUID in sync for the deploy payload.
    if patra_uuid and artifact["source_type"] == "patra":
        artifact["patra_model_card_uuid"] = patra_uuid

    raw_command = form.get("raw_docker_command") or None
    if raw_command is not None:
        raw_command = raw_command.strip() or None

    # Graceful fallback for clients without the live JS parser: if the user
    # supplied a raw docker command but the structured fields are mostly
    # empty, parse the raw command server-side and fill in the gaps so the
    # form still works without JavaScript.
    if raw_command and not spec.get("image_repository"):
        parsed = parse_docker_command(
            raw_command,
            model_container_path=artifact.get("container_path") or None,
            model_env_var=spec.get("model_env_var") or None,
        )
        fallback = parsed.as_form_dict()
        for key, value in fallback.items():
            if key in {"env", "mounts", "ports", "docker_args"}:
                if not spec.get(key):
                    spec[key] = value
            elif not spec.get(key):
                spec[key] = value
        if not artifact.get("filename"):
            cp = artifact.get("container_path") or ""
            if cp:
                artifact["filename"] = cp.rsplit("/", 1)[-1]

    # Always normalise the model mount so the agent's designated path is used
    # at deploy time, regardless of whether the user edited it by hand.
    _ensure_model_mount(spec, artifact)

    visibility = (form.get("visibility") or "private").strip().lower()
    if visibility not in _VALID_VISIBILITY:
        visibility = "private"

    return {
        "slug": (form.get("slug") or "").strip().lower() or None,
        "display_name": (form.get("display_name") or "").strip(),
        "version": (form.get("version") or "").strip(),
        "task_type": (form.get("task_type") or "").strip() or None,
        "framework": (form.get("framework") or "").strip() or None,
        "description": (form.get("description") or "").strip() or None,
        "license": (form.get("license") or "").strip() or None,
        "homepage_url": (form.get("homepage_url") or "").strip() or None,
        "tags": tags,
        "status": (form.get("status") or "DRAFT").strip().upper(),
        "visibility": visibility,
        "patra_model_card_uuid": patra_uuid,
        "raw_docker_command": raw_command,
        "artifact": artifact,
        "spec": spec,
        "compatibility": compatibility,
    }


def create_from_payload(owner: str, payload: dict) -> dict:
    _validate_payload(payload, slug_required=False)
    app_id = gen_uid("app")
    slug = payload.get("slug") or _unique_slug(owner, payload["display_name"], payload["version"])
    model_card_uid = gen_uid("mc")
    artifact = dict(payload["artifact"])
    artifact["artifact_uid"] = gen_uid("art")
    spec = dict(payload["spec"])
    spec["spec_uid"] = gen_uid("spec")
    mc_repo.create_full(
        model_card_uid=model_card_uid,
        app_id=app_id,
        owner=owner,
        slug=slug,
        display_name=payload["display_name"],
        version=payload["version"],
        task_type=payload.get("task_type"),
        framework=payload.get("framework"),
        description=payload.get("description"),
        license_name=payload.get("license"),
        homepage_url=payload.get("homepage_url"),
        tags=payload.get("tags") or [],
        status=payload["status"],
        visibility=payload.get("visibility") or "private",
        patra_model_card_uuid=payload.get("patra_model_card_uuid"),
        raw_docker_command=payload.get("raw_docker_command"),
        artifact=artifact,
        spec=spec,
        compatibility=payload.get("compatibility") or [],
    )
    event_service.record(
        "APP_CREATED",
        f"App '{payload['display_name']}' v{payload['version']} created ({payload['status']})",
        owner=owner,
        payload={"model_card_uid": model_card_uid, "app_id": app_id, "status": payload["status"]},
    )
    return get_full(model_card_uid)


def update_from_payload(owner: str, model_card_uid: str, payload: dict) -> dict:
    existing = mc_repo.get(model_card_uid)
    if not existing:
        raise NotFoundError("App not found")
    require_owner(owner, existing)
    _validate_payload(payload, slug_required=False)
    existing_full = mc_repo.get_full(model_card_uid) or {}
    existing_artifact = existing_full.get("artifact") or {}
    existing_spec = existing_full.get("spec") or {}
    artifact = dict(payload["artifact"])
    artifact["artifact_uid"] = existing_artifact.get("artifact_uid") or gen_uid("art")
    spec = dict(payload["spec"])
    spec["spec_uid"] = existing_spec.get("spec_uid") or gen_uid("spec")
    mc_repo.update_full(
        model_card_uid=model_card_uid,
        display_name=payload["display_name"],
        version=payload["version"],
        task_type=payload.get("task_type"),
        framework=payload.get("framework"),
        description=payload.get("description"),
        license_name=payload.get("license"),
        homepage_url=payload.get("homepage_url"),
        tags=payload.get("tags") or [],
        status=payload["status"],
        visibility=payload.get("visibility") or existing.get("visibility") or "private",
        patra_model_card_uuid=payload.get("patra_model_card_uuid"),
        raw_docker_command=payload.get("raw_docker_command"),
        artifact=artifact,
        spec=spec,
        compatibility=payload.get("compatibility") or [],
    )
    event_service.record(
        "APP_UPDATED",
        f"App '{payload['display_name']}' updated",
        owner=owner,
        payload={"model_card_uid": model_card_uid, "status": payload["status"]},
    )
    return get_full(model_card_uid)


def set_status(owner: str, model_card_uid: str, status: str) -> None:
    card = mc_repo.get(model_card_uid)
    if not card:
        raise NotFoundError("App not found")
    require_owner(owner, card)
    if status not in _VALID_STATUSES:
        raise ValidationError(f"Invalid status {status}")
    mc_repo.set_status(model_card_uid, status)
    event_service.record(
        f"APP_{status}",
        f"App '{card['display_name']}' is now {status}",
        owner=owner,
        payload={"model_card_uid": model_card_uid},
    )


def delete(owner: str, model_card_uid: str) -> None:
    card = mc_repo.get(model_card_uid)
    if not card:
        raise NotFoundError("App not found")
    require_owner(owner, card)
    mc_repo.delete(model_card_uid)
    event_service.record(
        "APP_DELETED",
        f"App '{card['display_name']}' deleted",
        owner=owner,
        payload={"model_card_uid": model_card_uid},
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_payload(p: dict, *, slug_required: bool = False) -> None:
    errors: List[str] = []

    if slug_required:
        if not p.get("slug"):
            errors.append("Slug is required.")
        elif not _SLUG_RE.match(p["slug"]):
            errors.append("Slug must be lowercase letters, digits, and dashes.")
    if not p.get("display_name"):
        errors.append("Display name is required.")
    if not p.get("version"):
        errors.append("Version is required.")
    status = p.get("status")
    if status not in _VALID_STATUSES:
        errors.append(f"Status must be one of {sorted(_VALID_STATUSES)}.")
    visibility = p.get("visibility") or "private"
    if visibility not in _VALID_VISIBILITY:
        errors.append("Visibility must be 'private' or 'public'.")

    a = p.get("artifact") or {}
    if not a.get("filename"):
        errors.append("Artifact filename is required.")
    if not a.get("container_path"):
        errors.append("Artifact container path is required.")
    if a.get("source_type") not in _VALID_SOURCE_TYPES:
        errors.append("Artifact source must be 'patra' or 'url'.")
    if a.get("source_type") == "patra" and not a.get("patra_model_card_uuid"):
        errors.append("Patra source requires a Patra model-card UUID.")
    if a.get("source_type") == "url" and not a.get("download_url"):
        errors.append("URL source requires a download_url.")
    patra_uuid = p.get("patra_model_card_uuid")
    if patra_uuid and not _PATRA_UUID_RE.match(patra_uuid):
        errors.append("Patra model card UUID must be a valid v4 UUID.")

    s = p.get("spec") or {}
    if not s.get("image_repository"):
        errors.append("Image repository is required.")
    if not s.get("container_name"):
        errors.append("Container name is required.")
    if s.get("pull_policy") not in _VALID_PULL_POLICIES:
        errors.append(f"Pull policy must be one of {sorted(_VALID_PULL_POLICIES)}.")
    if s.get("restart_policy") not in _VALID_RESTART_POLICIES:
        errors.append(f"Restart policy must be one of {sorted(_VALID_RESTART_POLICIES)}.")
    if s.get("network_mode") not in _VALID_NETWORK_MODES and s.get("network_mode") is not None:
        # Allow custom values too (user-defined Docker network).
        pass

    for m in s.get("mounts") or []:
        if m.get("style") not in _VALID_MOUNT_STYLES:
            errors.append(f"Mount style {m.get('style')} is invalid.")
        if m.get("type") not in _VALID_MOUNT_TYPES:
            errors.append(f"Mount type {m.get('type')} is invalid.")
        if m.get("mode") not in _VALID_MOUNT_MODES:
            errors.append(f"Mount mode {m.get('mode')} is invalid.")

    for port in s.get("ports") or []:
        if (port.get("protocol") or "tcp") not in _VALID_PORT_PROTO:
            errors.append("Port protocol must be tcp or udp.")

    valid_generations = {g["generation_uid"] for g in gen_repo.list_active()}
    for c in p.get("compatibility") or []:
        if c.get("generation_uid") not in valid_generations:
            errors.append(f"Unknown generation {c.get('generation_uid')}.")

    if errors:
        raise ValidationError("; ".join(errors))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower().strip())
    s = s.strip("-")
    return s or "app"


def _unique_slug(owner: str, display_name: str, version: str) -> str:
    base = _slugify(display_name)
    slug = base
    n = 2
    while mc_repo.get_by_owner_slug_version(owner, slug, version):
        slug = f"{base}-{n}"
        n += 1
    return slug


def _to_int(v) -> Optional[int]:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _checkbox(v) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in {"1", "on", "true", "yes"}


def _ensure_model_mount(spec: dict, artifact: dict) -> None:
    """Make sure exactly one mount represents the model directory and that
    its host side uses our deployment-time template.

    Strategy: identify the mount whose container-side target is a parent of
    the artifact's ``container_path``. Rewrite its host side to
    ``${DEPLOYMENT_DIR}/model`` and force read-only mode by default.
    """
    container_path = (artifact.get("container_path") or "").strip()
    if not container_path:
        return
    mounts = spec.get("mounts") or []
    best_idx: Optional[int] = None
    best_len = -1
    for idx, m in enumerate(mounts):
        tgt = (m.get("target") or "").rstrip("/")
        if not tgt:
            continue
        if container_path == tgt or container_path.startswith(tgt + "/"):
            if len(tgt) > best_len:
                best_len = len(tgt)
                best_idx = idx
    if best_idx is None:
        target_dir = container_path.rsplit("/", 1)[0] or "/models"
        mounts.append({
            "source": MODEL_HOST_TEMPLATE,
            "target": target_dir,
            "style": "volume",
            "type": "bind",
            "mode": "ro",
        })
    else:
        mounts[best_idx]["source"] = MODEL_HOST_TEMPLATE
        if not mounts[best_idx].get("mode"):
            mounts[best_idx]["mode"] = "ro"
    spec["mounts"] = mounts


def _split_optional_json_or_lines(raw: Optional[str]) -> Optional[List[str]]:
    if not raw or not raw.strip():
        return None
    s = raw.strip()
    if s.startswith("["):
        try:
            v = json.loads(s)
            if isinstance(v, list):
                return [str(x) for x in v]
        except json.JSONDecodeError:
            pass
    return [line.strip() for line in s.splitlines() if line.strip()]
