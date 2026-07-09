"""Environment-driven configuration for UNet++ multi-GigE live streaming."""

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
    """Per-camera RTSPS ingest URLs (Option B).

    Supports either:
      STREAM_INGEST_URL_0, STREAM_INGEST_URL_1, …
    or a comma-separated STREAM_INGEST_URLS list.
    """
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
THRESHOLD = float(os.environ.get("THRESHOLD", "0.3"))

# Cameras: indices into the MVS enumerated device list.
_indices_raw = os.environ.get("CAMERA_INDICES", "0,1,2").strip()
CAMERA_INDICES: Optional[List[int]] = (
    None if not _indices_raw or _indices_raw.lower() == "all" else _parse_int_list(_indices_raw)
)

STREAM_INGEST_URLS = _parse_url_list()
STREAM_FPS = max(1, int(os.environ.get("STREAM_FPS", "10")))
STREAM_BITRATE_KBPS = max(500, int(os.environ.get("STREAM_BITRATE_KBPS", "1500")))
STREAM_WIDTH = int(os.environ.get("STREAM_WIDTH", "960"))
STREAM_HEIGHT = int(os.environ.get("STREAM_HEIGHT", "540"))

# Resize before inference/display (keeps aspect ratio).
DISPLAY_WIDTH = int(os.environ.get("DISPLAY_WIDTH", "960"))
DETECT_EVERY_N_FRAMES = max(1, int(os.environ.get("DETECT_EVERY_N_FRAMES", "3")))

ENABLE_AUTO_ADJUSTMENT = os.environ.get("ENABLE_AUTO_ADJUSTMENT", "true").lower() in {
    "1", "true", "yes", "on",
}
AUTO_ADJUST_MODE = os.environ.get("AUTO_ADJUST_MODE", "once")
AUTO_ADJUST_SETTLE_SECONDS = float(os.environ.get("AUTO_ADJUST_SETTLE_SECONDS", "1.5"))
LOCK_AUTO_ADJUST_AFTER_ONCE = os.environ.get("LOCK_AUTO_ADJUST_AFTER_ONCE", "true").lower() in {
    "1", "true", "yes", "on",
}

ENABLE_IMAGE_SAVE = os.environ.get("ENABLE_IMAGE_SAVE", "false").lower() in {
    "1", "true", "yes", "on",
}
IMAGE_SAVE_DIR = os.environ.get("IMAGE_SAVE_DIR", "/data/camera_images")
SAVE_EVERY_N_FRAMES = max(1, int(os.environ.get("SAVE_EVERY_N_FRAMES", "30")))
IMAGE_SAVE_JPEG_QUALITY = int(os.environ.get("IMAGE_SAVE_JPEG_QUALITY", "85"))

ENGINE_LOAD_RETRIES = max(1, int(os.environ.get("ENGINE_LOAD_RETRIES", "3")))
ENGINE_LOAD_RETRY_DELAY = float(os.environ.get("ENGINE_LOAD_RETRY_DELAY", "2.0"))
