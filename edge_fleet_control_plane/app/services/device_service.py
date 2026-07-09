"""Device lifecycle: register, move, delete, query views."""

from __future__ import annotations

from typing import List, Optional

from app.core.errors import NotFoundError, ValidationError
from app.core.ids import gen_uid
from app.repositories import devices as devices_repo
from app.repositories import generations as gen_repo
from app.repositories import groups as groups_repo
from app.services import device_capabilities, event_service


def register(
    *,
    owner: str,
    device_name: str,
    device_alias: Optional[str],
    generation_uid: str,
    group_uid: Optional[str],
    site_name: Optional[str],
    camera_bus: Optional[str] = None,
    camera_count: Optional[int] = None,
    camera_indices: Optional[str] = None,
) -> dict:
    generation = gen_repo.get(generation_uid)
    if not generation or not generation.get("is_active"):
        raise ValidationError("Unsupported or inactive device generation.")
    if group_uid:
        if not groups_repo.get(group_uid, owner):
            raise ValidationError("Invalid device group for this owner.")
    count = max(1, min(16, int(camera_count or 1)))
    caps = device_capabilities.validate_form(
        camera_bus=camera_bus or "csi",
        camera_count=count,
        camera_indices=camera_indices or "",
    )
    caps_json = device_capabilities.to_json(caps)
    device_uid = gen_uid("dev")
    devices_repo.create(
        device_uid=device_uid,
        owner=owner,
        device_name=device_name,
        device_alias=device_alias or None,
        generation_uid=generation_uid,
        group_uid=group_uid or None,
        site_name=site_name or None,
        capabilities_json=caps_json,
    )
    device = devices_repo.get(device_uid, owner)
    if not device:
        raise RuntimeError("Failed to read back created device")
    event_service.record(
        "DEVICE_REGISTERED",
        f"Device '{device_name}' registered ({generation['display_name']})",
        owner=owner,
        device_uid=device_uid,
        payload={"generation_uid": generation_uid, "group_uid": group_uid},
    )
    return device


def get(device_uid: str, owner: str) -> dict:
    device = devices_repo.get(device_uid, owner)
    if not device:
        raise NotFoundError("Device not found")
    return device


def list_for_owner(owner: str) -> List[dict]:
    return devices_repo.list_for_owner(owner)


def counts(owner: str) -> dict:
    return devices_repo.count_for_owner(owner)


def move_to_group(device_uid: str, owner: str, group_uid: Optional[str]) -> None:
    device = get(device_uid, owner)
    if group_uid and not groups_repo.get(group_uid, owner):
        raise ValidationError("Invalid device group.")
    devices_repo.update(
        device_uid,
        owner,
        device_name=device["device_name"],
        device_alias=device["device_alias"],
        group_uid=group_uid or None,
        site_name=device["site_name"],
    )
    event_service.record(
        "DEVICE_MOVED",
        "Device group changed",
        owner=owner,
        device_uid=device_uid,
        payload={"new_group_uid": group_uid},
    )


def update(device_uid: str, owner: str, *, device_name: str, device_alias: Optional[str],
           group_uid: Optional[str], site_name: Optional[str]) -> None:
    get(device_uid, owner)
    if group_uid and not groups_repo.get(group_uid, owner):
        raise ValidationError("Invalid device group.")
    devices_repo.update(
        device_uid,
        owner,
        device_name=device_name,
        device_alias=device_alias or None,
        group_uid=group_uid or None,
        site_name=site_name or None,
    )
    event_service.record(
        "DEVICE_UPDATED",
        f"Device '{device_name}' updated",
        owner=owner,
        device_uid=device_uid,
    )


def update_generation(device_uid: str, owner: str, generation_uid: str) -> dict:
    device = get(device_uid, owner)
    generation = gen_repo.get(generation_uid)
    if not generation or not generation.get("is_active"):
        raise ValidationError("Unsupported or inactive device generation.")
    if generation_uid == device["generation_uid"]:
        return device
    devices_repo.update_generation(device_uid, owner, generation_uid)
    event_service.record(
        "DEVICE_GENERATION_CHANGED",
        "Device generation changed to %s" % generation["display_name"],
        owner=owner,
        device_uid=device_uid,
        payload={
            "from_generation_uid": device["generation_uid"],
            "to_generation_uid": generation_uid,
        },
    )
    return get(device_uid, owner)


def delete(device_uid: str, owner: str) -> None:
    device = get(device_uid, owner)
    event_service.record(
        "DEVICE_DELETED",
        f"Device '{device['device_name']}' deleted",
        owner=owner,
        payload={"device_uid": device_uid},
    )
    devices_repo.delete(device_uid, owner)


def update_capabilities(
    device_uid: str,
    owner: str,
    *,
    camera_bus: str,
    camera_count: int,
    camera_indices: str,
) -> dict:
    get(device_uid, owner)
    caps = device_capabilities.validate_form(
        camera_bus=camera_bus,
        camera_count=camera_count,
        camera_indices=camera_indices,
    )
    devices_repo.update_capabilities(device_uid, owner, device_capabilities.to_json(caps))
    event_service.record(
        "DEVICE_CAPABILITIES_UPDATED",
        "Hardware setup updated (%s, %d camera(s))" % (
            caps["camera_buses"][0], caps["camera_count"],
        ),
        owner=owner,
        device_uid=device_uid,
        payload=caps,
    )
    return get(device_uid, owner)


def setup_readiness(
    device: dict,
    *,
    enrollment: Optional[dict] = None,
    compatible_app_count: int = 0,
) -> dict:
    """Checklist for the device setup wizard / post-register flow."""
    status = (device.get("status") or "").upper()
    caps = device.get("capabilities") or device_capabilities.parse(device.get("capabilities_json"))
    enrolled = bool(enrollment and enrollment.get("used_at"))
    online = status in {"ONLINE", "RUNNING", "ENROLLED"}
    installer_done = bool(enrollment and enrollment.get("installer_downloaded_at"))
    steps = [
        {
            "id": "registered",
            "label": "Device registered",
            "detail": device.get("device_name") or device.get("device_uid"),
            "done": True,
        },
        {
            "id": "hardware",
            "label": "Hardware profile saved",
            "detail": "%s · %d camera(s)" % (
                (caps.get("camera_buses") or ["csi"])[0],
                caps.get("camera_count") or 1,
            ),
            "done": True,
        },
        {
            "id": "installer",
            "label": "Agent installer downloaded",
            "detail": "Run install.sh on the Jetson as root",
            "done": installer_done,
        },
        {
            "id": "enrolled",
            "label": "Agent enrolled",
            "detail": "Enrollment token used on device",
            "done": enrolled,
        },
        {
            "id": "online",
            "label": "Device online",
            "detail": "Heartbeats reaching the portal",
            "done": online,
        },
        {
            "id": "apps",
            "label": "Deploy apps",
            "detail": (
                "%d compatible app(s) in catalog — available once the device is online"
                % compatible_app_count
                if compatible_app_count and not online
                else "%d compatible app(s) ready to deploy" % compatible_app_count
                if compatible_app_count
                else "Publish or browse public apps for this generation"
            ),
            "done": online and compatible_app_count > 0,
        },
    ]
    core_complete = all(s["done"] for s in steps[:5])
    return {
        "steps": steps,
        "complete": core_complete and online,
        "core_complete": core_complete,
        "next_step": next((s for s in steps if not s["done"]), None),
    }
