"""Environment-driven configuration for UNet++ CSI live streaming."""

import os
from typing import List, Optional, Tuple


def _parse_int_list(raw: str) -> List[int]:
    out: List[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            out.append(int(part))
    return out


def _parse_url_list() -> List[str]:
    indexed = []  # type: List[Tuple[int, str]]
    for key, val in os.environ.items():
        if not key.startswith("STREAM_INGEST_URL_") or key == "STREAM_INGEST_URLS":
            continue
        suffix = key[len("STREAM_INGEST_URL_") :]
        if suffix.isdigit() and val.strip():
            indexed.append((int(suffix), val.strip()))
    if indexed:
        indexed.sort(key=lambda x: x[0])
        return [url for _, url in indexed]

    raw = (os.environ.get("STREAM_INGEST_URLS") or "").strip()
    if raw:
        return [u.strip() for u in raw.split(",") if u.strip()]
    return []


ENGINE_PATH = os.environ.get(
    "ENGINE_PATH", "/workspace/models/unetpp_teacher_512x768_fp16.engine"
)
SHOW_WINDOW = os.environ.get("SHOW_WINDOW", "1") == "1"
THRESHOLD = float(os.environ.get("THRESHOLD", "0.3"))

STREAM_INGEST_URL = (os.environ.get("STREAM_INGEST_URL") or "").strip()
STREAM_INGEST_URLS = _parse_url_list()
STREAM_FPS = max(1, int(os.environ.get("STREAM_FPS", "10")))
STREAM_BITRATE_KBPS = max(500, int(os.environ.get("STREAM_BITRATE_KBPS", "1500")))
# Encoder: auto (try nvv4l2h264enc then x264enc), hw, or sw
STREAM_ENCODER = (os.environ.get("STREAM_ENCODER") or "auto").strip().lower()
STREAM_WIDTH = int(os.environ.get("STREAM_WIDTH", "960"))
STREAM_HEIGHT = int(os.environ.get("STREAM_HEIGHT", "540"))

CAM_WIDTH = int(os.environ.get("CAM_WIDTH", "1280"))
CAM_HEIGHT = int(os.environ.get("CAM_HEIGHT", "720"))
CAM_FPS = int(os.environ.get("CAM_FPS", "30"))

_indices_raw = os.environ.get("CAMERA_INDICES", "").strip()
CAMERA_INDICES: Optional[List[int]] = (
    None if not _indices_raw or _indices_raw.lower() == "all" else _parse_int_list(_indices_raw)
)

DISPLAY_WIDTH = int(os.environ.get("DISPLAY_WIDTH", "960"))
DETECT_EVERY_N_FRAMES = max(1, int(os.environ.get("DETECT_EVERY_N_FRAMES", "4")))

ENGINE_LOAD_RETRIES = max(1, int(os.environ.get("ENGINE_LOAD_RETRIES", "3")))
ENGINE_LOAD_RETRY_DELAY = float(os.environ.get("ENGINE_LOAD_RETRY_DELAY", "2.0"))


def sensor_ids() -> List[int]:
    if CAMERA_INDICES:
        return list(CAMERA_INDICES)
    return [0]


def is_multicam_mode() -> bool:
    """True when more than one CSI sensor is configured."""
    return len(sensor_ids()) > 1
