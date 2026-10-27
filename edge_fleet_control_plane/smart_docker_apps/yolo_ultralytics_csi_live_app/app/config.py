"""Environment-driven configuration for the Ultralytics YOLO CSI live stream."""

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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# The model card mounts the weights at /workspace/models and sets this path.
# ENGINE_PATH is accepted so a card can use either name.
MODEL_PATH = (
    os.environ.get("MODEL_PATH")
    or os.environ.get("ENGINE_PATH")
    or "/workspace/models/model.pt"
).strip()

CONFIDENCE = float(os.environ.get("CONFIDENCE", "0.25"))
IOU = float(os.environ.get("IOU", "0.45"))
# 0 means "use the size stored in the weights".
IMAGE_SIZE = int(os.environ.get("IMAGE_SIZE", "0"))
DEVICE = (os.environ.get("DEVICE") or "0").strip()
HALF = _env_bool("HALF", False)

SHOW_WINDOW = os.environ.get("SHOW_WINDOW", "0") == "1"

STREAM_INGEST_URL = (os.environ.get("STREAM_INGEST_URL") or "").strip()
STREAM_INGEST_URLS = _parse_url_list()
STREAM_FPS = max(1, int(os.environ.get("STREAM_FPS", "10")))
STREAM_BITRATE_KBPS = max(500, int(os.environ.get("STREAM_BITRATE_KBPS", "1500")))
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
DETECT_EVERY_N_FRAMES = max(1, int(os.environ.get("DETECT_EVERY_N_FRAMES", "2")))


def sensor_ids() -> List[int]:
    if CAMERA_INDICES:
        return list(CAMERA_INDICES)
    return [0]
