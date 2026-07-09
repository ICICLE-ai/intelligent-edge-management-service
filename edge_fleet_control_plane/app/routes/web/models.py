from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import current_username
from app.core.errors import DomainError
from app.core.templates import templates
from app.repositories import generations as gen_repo
from app.routes._helpers import http_error_from_domain, redirect_with_notice
from app.services import model_service

router = APIRouter(tags=["web"])


# Legacy redirects — old /models URLs still work --------------------------------
@router.get("/models", include_in_schema=False)
def _redirect_models_list():
    return RedirectResponse("/apps", status_code=307)


@router.get("/models/new", include_in_schema=False)
def _redirect_models_new():
    return RedirectResponse("/apps/new", status_code=307)


@router.get("/models/compatibility", include_in_schema=False)
def _redirect_models_compat():
    return RedirectResponse("/apps/compatibility", status_code=307)


@router.get("/models/{model_card_uid}", include_in_schema=False)
def _redirect_model_detail(model_card_uid: str):
    return RedirectResponse(f"/apps/{model_card_uid}", status_code=307)


@router.get("/models/{model_card_uid}/edit", include_in_schema=False)
def _redirect_model_edit(model_card_uid: str):
    return RedirectResponse(f"/apps/{model_card_uid}/edit", status_code=307)


@router.post("/models", include_in_schema=False, name="legacy_models_create")
async def _legacy_models_create(request: Request):
    return await create_app(request)


@router.post("/models/{model_card_uid}/edit", include_in_schema=False)
async def _legacy_models_update(request: Request, model_card_uid: str):
    return await update_app(request, model_card_uid)


@router.post("/models/{model_card_uid}/publish", include_in_schema=False)
def _legacy_models_publish(request: Request, model_card_uid: str):
    return publish_app(request, model_card_uid)


@router.post("/models/{model_card_uid}/draft", include_in_schema=False)
def _legacy_models_draft(request: Request, model_card_uid: str):
    return unpublish_app(request, model_card_uid)


@router.post("/models/{model_card_uid}/deprecate", include_in_schema=False)
def _legacy_models_deprecate(request: Request, model_card_uid: str):
    return deprecate_app(request, model_card_uid)


@router.post("/models/{model_card_uid}/delete", include_in_schema=False)
def _legacy_models_delete(request: Request, model_card_uid: str):
    return delete_app(request, model_card_uid)


# App catalog -------------------------------------------------------------------

@router.get("/apps", response_class=HTMLResponse, name="apps_list")
def list_apps(request: Request):
    owner = current_username(request)
    return templates.TemplateResponse(
        "models/list.html",
        {
            "request": request,
            "user": owner,
            "active_nav": "apps",
            "published": model_service.list_published(owner),
            "mine": model_service.list_for_owner(owner),
        },
    )


@router.get("/apps/new", response_class=HTMLResponse, name="apps_new")
def new_app(request: Request):
    owner = current_username(request)
    return templates.TemplateResponse(
        "models/form.html",
        {
            "request": request,
            "user": owner,
            "active_nav": "apps_new",
            "mode": "create",
            "card": None,
            "generations": gen_repo.list_active(),
            "errors": [],
            "form": {},
        },
    )


@router.post("/apps", name="apps_create")
async def create_app(request: Request):
    owner = current_username(request)
    form_data = await request.form()
    form_dict = _form_to_dict(form_data)
    payload = model_service.build_payload_from_form(form_dict)
    try:
        card = model_service.create_from_payload(owner, payload)
    except DomainError as e:
        return templates.TemplateResponse(
            "models/form.html",
            {
                "request": request,
                "user": owner,
                "active_nav": "apps_new",
                "mode": "create",
                "card": None,
                "generations": gen_repo.list_active(),
                "errors": str(e).split("; "),
                "form": form_dict,
            },
            status_code=422,
        )
    return RedirectResponse(f"/apps/{card['model_card_uid']}", status_code=303)


@router.get("/apps/compatibility", response_class=HTMLResponse, name="apps_compat")
def compatibility(request: Request):
    owner = current_username(request)
    matrix = model_service.compatibility_matrix(owner)
    generations = gen_repo.list_active()
    cards = model_service.list_published(owner)

    by_card: dict[str, set[str]] = {}
    name_by_card: dict[str, str] = {}
    for row in matrix:
        by_card.setdefault(row["model_card_uid"], set()).add(row["generation_uid"])
        name_by_card[row["model_card_uid"]] = f"{row['display_name']} v{row['version']}"

    return templates.TemplateResponse(
        "models/compatibility.html",
        {
            "request": request,
            "user": owner,
            "active_nav": "apps_compat",
            "generations": generations,
            "cards": cards,
            "by_card": by_card,
            "name_by_card": name_by_card,
        },
    )


@router.get("/apps/{model_card_uid}", response_class=HTMLResponse, name="app_detail")
def app_detail(request: Request, model_card_uid: str):
    owner = current_username(request)
    try:
        card = model_service.get_full_for_user(owner, model_card_uid)
    except DomainError as e:
        raise http_error_from_domain(e)
    return templates.TemplateResponse(
        "models/detail.html",
        {
            "request": request,
            "user": owner,
            "active_nav": "apps",
            "card": card,
            "is_owner": model_service.is_owner(owner, card),
        },
    )


@router.get("/apps/{model_card_uid}/edit", response_class=HTMLResponse, name="app_edit")
def edit_app(request: Request, model_card_uid: str):
    owner = current_username(request)
    try:
        card = model_service.get_full_for_owner(owner, model_card_uid)
    except DomainError as e:
        raise http_error_from_domain(e)
    return templates.TemplateResponse(
        "models/form.html",
        {
            "request": request,
            "user": owner,
            "active_nav": "apps",
            "mode": "edit",
            "card": card,
            "generations": gen_repo.list_active(),
            "errors": [],
            "form": _card_to_form(card),
        },
    )


@router.post("/apps/{model_card_uid}/edit", name="app_update")
async def update_app(request: Request, model_card_uid: str):
    owner = current_username(request)
    form_data = await request.form()
    form_dict = _form_to_dict(form_data)
    payload = model_service.build_payload_from_form(form_dict)
    try:
        model_service.update_from_payload(owner, model_card_uid, payload)
    except DomainError as e:
        return templates.TemplateResponse(
            "models/form.html",
            {
                "request": request,
                "user": owner,
                "active_nav": "apps",
                "mode": "edit",
                "card": model_service.get_full_for_owner(owner, model_card_uid),
                "generations": gen_repo.list_active(),
                "errors": str(e).split("; "),
                "form": form_dict,
            },
            status_code=422,
        )
    return RedirectResponse(f"/apps/{model_card_uid}", status_code=303)


@router.post("/apps/{model_card_uid}/publish", name="app_publish")
def publish_app(request: Request, model_card_uid: str):
    owner = current_username(request)
    try:
        model_service.set_status(owner, model_card_uid, "PUBLISHED")
    except DomainError as e:
        raise http_error_from_domain(e)
    return redirect_with_notice(f"/apps/{model_card_uid}", notice="App published")


@router.post("/apps/{model_card_uid}/draft", name="app_unpublish")
def unpublish_app(request: Request, model_card_uid: str):
    owner = current_username(request)
    try:
        model_service.set_status(owner, model_card_uid, "DRAFT")
    except DomainError as e:
        raise http_error_from_domain(e)
    return redirect_with_notice(f"/apps/{model_card_uid}", notice="App moved to draft")


@router.post("/apps/{model_card_uid}/deprecate", name="app_deprecate")
def deprecate_app(request: Request, model_card_uid: str):
    owner = current_username(request)
    try:
        model_service.set_status(owner, model_card_uid, "DEPRECATED")
    except DomainError as e:
        raise http_error_from_domain(e)
    return redirect_with_notice(f"/apps/{model_card_uid}", notice="App deprecated")


@router.post("/apps/{model_card_uid}/delete", name="app_delete")
def delete_app(request: Request, model_card_uid: str):
    owner = current_username(request)
    try:
        model_service.delete(owner, model_card_uid)
    except DomainError as e:
        raise http_error_from_domain(e)
    return RedirectResponse("/apps", status_code=303)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _form_to_dict(form) -> dict:
    """Convert FastAPI's FormData into a dict that preserves repeated keys as lists."""
    out: dict[str, object] = {}
    for key in form.keys():
        values = form.getlist(key)
        out[key] = values if len(values) > 1 or key.endswith("[]") else values[0]
        if key.endswith("[]"):
            out[key[:-2]] = values
    return out


def _card_to_form(card: dict) -> dict:
    """Render the aggregate back as form input list-ish dict for the edit form."""
    artifact = card.get("artifact") or {}
    spec = card.get("spec") or {}
    form = {
        "display_name": card.get("display_name"),
        "version": card.get("version"),
        "task_type": card.get("task_type"),
        "framework": card.get("framework"),
        "description": card.get("description"),
        "license": card.get("license"),
        "homepage_url": card.get("homepage_url"),
        "tags": ", ".join(card.get("tags") or []),
        "status": card.get("status"),
        "visibility": card.get("visibility") or "private",
        "patra_model_card_uuid": card.get("patra_model_card_uuid") or artifact.get("patra_model_card_uuid"),
        "raw_docker_command": card.get("raw_docker_command"),
        "artifact_filename": artifact.get("filename"),
        "artifact_container_path": artifact.get("container_path"),
        "artifact_source_type": artifact.get("source_type"),
        "artifact_patra_uuid": artifact.get("patra_model_card_uuid"),
        "artifact_download_url": artifact.get("download_url"),
        "artifact_content_type": artifact.get("content_type"),
        "artifact_size_bytes": artifact.get("size_bytes"),
        "artifact_sha256": artifact.get("sha256"),
        "artifact_notes": artifact.get("notes"),
        "spec_image_registry": spec.get("image_registry"),
        "spec_image_repository": spec.get("image_repository"),
        "spec_image_tag": spec.get("image_tag"),
        "spec_image_digest": spec.get("image_digest"),
        "spec_container_name": spec.get("container_name"),
        "spec_pull_policy": spec.get("pull_policy"),
        "spec_remove_after_exit": "on" if spec.get("remove_after_exit") else "",
        "spec_restart_policy": spec.get("restart_policy"),
        "spec_model_env_var": spec.get("model_env_var"),
        "spec_network_mode": spec.get("network_mode"),
        "spec_gpus": spec.get("gpus"),
        "spec_runtime": spec.get("runtime"),
        "spec_privileged": "on" if spec.get("privileged") else "",
        "spec_ipc_mode": spec.get("ipc_mode"),
        "spec_shm_size": spec.get("shm_size"),
        "spec_working_dir": spec.get("working_dir"),
        "env_key": [e["var_key"] for e in (spec.get("env") or [])],
        "env_value": [e["var_value"] for e in (spec.get("env") or [])],
        "env_is_secret": [str(i) for i, e in enumerate(spec.get("env") or []) if e.get("is_secret")],
        "mount_source": [m["source"] for m in (spec.get("mounts") or [])],
        "mount_target": [m["target"] for m in (spec.get("mounts") or [])],
        "mount_style": [m["mount_style"] for m in (spec.get("mounts") or [])],
        "mount_type": [m["mount_type"] for m in (spec.get("mounts") or [])],
        "mount_mode": [m.get("mode") or "" for m in (spec.get("mounts") or [])],
        "docker_arg": [a["arg"] for a in (spec.get("docker_args") or [])],
        "port_host": [p.get("host_port") or "" for p in (spec.get("ports") or [])],
        "port_container": [p["container_port"] for p in (spec.get("ports") or [])],
        "port_protocol": [p.get("protocol") or "tcp" for p in (spec.get("ports") or [])],
        "compat_generation_uid": [c["generation_uid"] for c in (card.get("compatibility") or [])],
    }
    return form
