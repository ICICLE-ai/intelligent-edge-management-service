"""Device group lifecycle."""

from __future__ import annotations

from typing import List, Optional

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.ids import gen_uid
from app.repositories import groups as groups_repo
from app.services import event_service

_VALID_COLORS = {"indigo", "violet", "emerald", "amber", "rose", "sky", "cyan", "fuchsia"}


def list_for_owner(owner: str) -> List[dict]:
    return groups_repo.list_for_owner(owner)


def get(group_uid: str, owner: str) -> dict:
    group = groups_repo.get(group_uid, owner)
    if not group:
        raise NotFoundError("Group not found")
    return group


def create(*, owner: str, group_name: str, description: Optional[str], site_name: Optional[str],
           color_tag: str = "indigo") -> dict:
    name = (group_name or "").strip()
    if not name:
        raise ValidationError("Group name is required")
    if color_tag not in _VALID_COLORS:
        color_tag = "indigo"
    group_uid = gen_uid("dg")
    try:
        groups_repo.create(
            group_uid=group_uid,
            owner=owner,
            group_name=name,
            description=description,
            site_name=site_name,
            color_tag=color_tag,
        )
    except Exception as e:
        raise ConflictError(f"Could not create group: {e}") from e
    event_service.record("GROUP_CREATED", f"Group '{name}' created", owner=owner,
                         payload={"group_uid": group_uid})
    return groups_repo.get(group_uid, owner)  # type: ignore[return-value]


def update(group_uid: str, owner: str, *, group_name: str, description: Optional[str],
           site_name: Optional[str], color_tag: str) -> None:
    get(group_uid, owner)
    if color_tag not in _VALID_COLORS:
        color_tag = "indigo"
    groups_repo.update(group_uid, owner,
                       group_name=group_name.strip(),
                       description=description,
                       site_name=site_name,
                       color_tag=color_tag)
    event_service.record("GROUP_UPDATED", f"Group '{group_name}' updated", owner=owner,
                         payload={"group_uid": group_uid})


def delete(group_uid: str, owner: str) -> None:
    get(group_uid, owner)
    groups_repo.delete(group_uid, owner)
    event_service.record("GROUP_DELETED", "Group deleted", owner=owner,
                         payload={"group_uid": group_uid})
