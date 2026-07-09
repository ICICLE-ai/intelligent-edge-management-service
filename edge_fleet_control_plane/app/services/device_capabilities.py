"""Device hardware capabilities — camera buses, counts, host mounts."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.core.errors import ValidationError

VALID_CAMERA_BUSES = frozenset({"csi", "gige-mvs", "usb-v4l2"})

_DEFAULTS_BY_BUS = {
    "csi": {"camera_buses": ["csi"], "camera_count": 1, "camera_indices": [0], "host_mounts": {}},
    "gige-mvs": {
        "camera_buses": ["gige-mvs"],
        "camera_count": 1,
        "camera_indices": [0],
        "host_mounts": {"/opt/MVS": "ro"},
    },
    "usb-v4l2": {
        "camera_buses": ["usb-v4l2"],
        "camera_count": 1,
        "camera_indices": [0],
        "host_mounts": {},
    },
}


def defaults_for_bus(camera_bus: str) -> dict:
    bus = (camera_bus or "csi").strip().lower()
    if bus not in VALID_CAMERA_BUSES:
        bus = "csi"
    return dict(_DEFAULTS_BY_BUS[bus])


def parse(raw: Any) -> dict:
    """Return normalised capabilities dict from DB JSON or dict."""
    if raw is None or raw == "":
        return defaults_for_bus("csi")
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(str(raw))
        except (TypeError, json.JSONDecodeError):
            return defaults_for_bus("csi")
    return normalise(data)


def normalise(data: dict) -> dict:
    buses = data.get("camera_buses") or ["csi"]
    if isinstance(buses, str):
        buses = [b.strip() for b in buses.split(",") if b.strip()]
    buses = [b.lower() for b in buses if b.lower() in VALID_CAMERA_BUSES] or ["csi"]

    try:
        count = max(1, min(16, int(data.get("camera_count") or 1)))
    except (TypeError, ValueError):
        count = 1

    raw_indices = data.get("camera_indices")
    if isinstance(raw_indices, str):
        indices = [int(x.strip()) for x in raw_indices.split(",") if x.strip().isdigit()]
    elif isinstance(raw_indices, list):
        indices = [int(x) for x in raw_indices if str(x).isdigit()]
    else:
        indices = list(range(count))
    if not indices:
        indices = list(range(count))
    if len(indices) < count:
        seen = set(indices)
        n = 0
        while len(indices) < count:
            if n not in seen:
                indices.append(n)
                seen.add(n)
            n += 1

    host_mounts = data.get("host_mounts") or {}
    if isinstance(host_mounts, list):
        host_mounts = {m["source"]: m.get("mode", "ro") for m in host_mounts if m.get("source")}

    return {
        "camera_buses": buses,
        "camera_count": count,
        "camera_indices": indices[:count],
        "host_mounts": dict(host_mounts),
        "platform": (data.get("platform") or "").strip() or None,
    }


def to_json(caps: dict) -> str:
    return json.dumps(normalise(caps), separators=(",", ":"))


def enrich_device(device: Optional[dict]) -> Optional[dict]:
    if not device:
        return device
    out = dict(device)
    out["capabilities"] = parse(device.get("capabilities_json"))
    return out


def validate_form(
    *,
    camera_bus: str,
    camera_count: int,
    camera_indices: str,
) -> dict:
    bus = (camera_bus or "csi").strip().lower()
    if bus not in VALID_CAMERA_BUSES:
        raise ValidationError("camera_bus must be one of: csi, gige-mvs, usb-v4l2.")
    try:
        count = max(1, min(16, int(camera_count)))
    except (TypeError, ValueError):
        raise ValidationError("camera_count must be an integer between 1 and 16.")

    indices: List[int] = []
    for part in (camera_indices or "").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise ValidationError("camera_indices must be comma-separated integers.")
        indices.append(int(part))
    if not indices:
        indices = list(range(count))
    if len(indices) != count:
        raise ValidationError("Provide exactly %d camera index values." % count)

    base = defaults_for_bus(bus)
    base["camera_count"] = count
    base["camera_indices"] = indices
    return normalise(base)


def model_requires_bus(card: dict, bus: str) -> bool:
    """True if model card tags/requirements expect a camera bus."""
    tags = {t.lower() for t in (card.get("tags") or [])}
    bus = bus.lower()
    if bus == "gige-mvs" and "gige-mvs" in tags:
        return True
    if bus == "csi" and "csi" in tags:
        return True
    if bus == "usb-v4l2" and "usb-v4l2" in tags:
        return True
    return False


def device_compatible_with_card(device: dict, card: dict) -> Optional[str]:
    """Return error message if incompatible, else None."""
    caps = parse(device.get("capabilities_json"))
    tags = {t.lower() for t in (card.get("tags") or [])}
    if "gige-mvs" in tags and "gige-mvs" not in caps["camera_buses"]:
        return "App requires GigE (MVS) cameras; update device hardware setup."
    if "csi" in tags and "csi" not in caps["camera_buses"]:
        return "App requires Jetson CSI cameras; update device hardware setup."
    return None
