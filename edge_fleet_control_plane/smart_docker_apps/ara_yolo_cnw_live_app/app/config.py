"""Environment-driven configuration for YOLO CNW live streaming."""

import os
from typing import List, Tuple


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


def _env_int(*names: str, default: int) -> int:
    for name in names:
        raw = os.environ.get(name)
        if raw is not None and raw.strip() != "":
            return int(raw)
    return default


MODEL_PATH = os.environ.get("MODEL_PATH", "/workspace/models/Yolo-cnw.pt")
SOURCE = (os.environ.get("SOURCE") or "").strip() or "/workspace/input-h264.sdp"

STREAM_INGEST_URL = (os.environ.get("STREAM_INGEST_URL") or "").strip()
STREAM_INGEST_URLS = _parse_url_list()
PUBLISH_URL = (os.environ.get("PUBLISH_URL") or "").strip()

STREAM_FPS = max(1, _env_int("STREAM_FPS", "PUBLISH_FPS", default=15))
STREAM_WIDTH = _env_int("STREAM_WIDTH", "OUTPUT_WIDTH", default=480)
STREAM_HEIGHT = _env_int("STREAM_HEIGHT", "OUTPUT_HEIGHT", default=320)
STREAM_BITRATE_KBPS = max(500, _env_int("STREAM_BITRATE_KBPS", default=1800))

INPUT_WIDTH = _env_int("INPUT_WIDTH", default=480)
INPUT_HEIGHT = _env_int("INPUT_HEIGHT", default=320)


def ingest_url() -> str:
    """RTSPS URL to publish annotated video.

    IEMS injects STREAM_INGEST_URL (or STREAM_INGEST_URL_0) on fleet deploy.
    PUBLISH_URL remains a manual-run fallback.
    """
    if STREAM_INGEST_URL:
        return STREAM_INGEST_URL
    if STREAM_INGEST_URLS:
        return STREAM_INGEST_URLS[0]
    return PUBLISH_URL


def stream_bitrate() -> str:
    raw = (os.environ.get("VIDEO_BITRATE") or "").strip()
    if raw:
        return raw
    return "%dk" % STREAM_BITRATE_KBPS
