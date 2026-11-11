"""Device hardware capabilities — camera buses, counts, host mounts.

A scan station has no camera of its own. Its capabilities hold
``scan_station``: the SSH host name of the capture station, the token
shared by the job server and the capture agent, and the folder on the
station that holds the SSH key and config for reaching the capture station.
"""

from __future__ import annotations

import json
import re
import secrets
from typing import Any, Dict, List, Optional

from app.core.errors import ValidationError

VALID_CAMERA_BUSES = frozenset({"csi", "gige-mvs", "usb-v4l2", "rtsp"})

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
    "rtsp": {
        "camera_buses": ["rtsp"],
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
    station = data.get("scan_station")
    if isinstance(station, dict):
        return {
            "camera_buses": [],
            "camera_count": 0,
            "camera_indices": [],
            "host_mounts": {},
            "platform": (data.get("platform") or "").strip() or None,
            "scan_station": {
                "capture_host": str(station.get("capture_host") or DEFAULT_CAPTURE_HOST),
                "token": str(station.get("token") or ""),
                "ssh_dir": str(station.get("ssh_dir") or DEFAULT_SSH_DIR),
            },
        }
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


DEFAULT_CAPTURE_HOST = "jetson"
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$")
DEFAULT_SSH_DIR = "/srv/scanbot-ssh"
# The token ends up inside a quoted TOML string, so no quotes, backslashes or spaces.
_TOKEN_RE = re.compile(r"^[^\s\"'\\]{4,256}$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]{1,250}$")


def new_scan_token() -> str:
    return secrets.token_urlsafe(24)


def scan_station_caps(*, capture_host: str, token: str, ssh_dir: str = "") -> dict:
    """Validate scan-station settings. A blank token gets a new random one."""
    host = (capture_host or "").strip() or DEFAULT_CAPTURE_HOST
    if not _HOST_RE.match(host):
        raise ValidationError(
            "Capture host must be an SSH host name or alias, such as jetson or cycea@192.168.0.50."
        )
    token = (token or "").strip() or new_scan_token()
    if not _TOKEN_RE.match(token):
        raise ValidationError(
            "Scan token must be 4 to 256 characters with no spaces, quotes or backslashes."
        )
    ssh = (ssh_dir or "").strip().rstrip("/") or DEFAULT_SSH_DIR
    if not _PATH_RE.match(ssh) or ".." in ssh.split("/"):
        raise ValidationError("SSH folder must be an absolute path on the station, such as /home/cycea/.ssh.")
    return normalise({"scan_station": {"capture_host": host, "token": token, "ssh_dir": ssh}})


def scan_station(device: Optional[dict]) -> Optional[dict]:
    """The device's scan-station settings, or None for a camera device."""
    if not device:
        return None
    caps = device.get("capabilities") or parse(device.get("capabilities_json"))
    return caps.get("scan_station")


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
        raise ValidationError("camera_bus must be one of: csi, gige-mvs, usb-v4l2, rtsp.")
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
    if bus == "rtsp" and "rtsp" in tags:
        return True
    return False


def device_compatible_with_card(device: dict, card: dict) -> Optional[str]:
    """Return error message if incompatible, else None."""
    caps = parse(device.get("capabilities_json"))
    station = caps.get("scan_station")
    if (card.get("app_kind") or "inference") == "scanbot":
        if not station:
            return "Scanbot apps run on a scan station; this device is set up for cameras."
        if not station.get("token"):
            return "Scan station has no scan token; set one in the station settings."
        return None
    if station:
        return "This device is a scan station and has no camera for live apps."
    tags = {t.lower() for t in (card.get("tags") or [])}
    if "gige-mvs" in tags and "gige-mvs" not in caps["camera_buses"]:
        return "App requires GigE (MVS) cameras; update device hardware setup."
    if "csi" in tags and "csi" not in caps["camera_buses"]:
        return "App requires Jetson CSI cameras; update device hardware setup."
    if "rtsp" in tags and "rtsp" not in caps["camera_buses"]:
        return "App requires wireless video (no local camera); update device hardware setup."
    return None
