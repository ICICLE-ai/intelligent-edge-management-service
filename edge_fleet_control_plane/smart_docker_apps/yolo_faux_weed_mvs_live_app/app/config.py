"""Environment-driven configuration for YOLO faux-weed MVS live streaming."""

import os


def _env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int_list(raw):
    out = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            out.append(int(part))
    return out


def _parse_url_list():
    """Per-camera RTSPS ingest URLs.

    Accepts STREAM_INGEST_URL_0, STREAM_INGEST_URL_1, … (what IEMS injects for a
    multi-camera device), a comma-separated STREAM_INGEST_URLS, or a single
    STREAM_INGEST_URL for a one-camera manual run.
    """
    indexed = []
    for key, val in os.environ.items():
        if not key.startswith("STREAM_INGEST_URL_") or key == "STREAM_INGEST_URLS":
            continue
        suffix = key[len("STREAM_INGEST_URL_"):]
        if suffix.isdigit() and val.strip():
            indexed.append((int(suffix), val.strip()))
    if indexed:
        indexed.sort(key=lambda x: x[0])
        return [url for _, url in indexed]

    raw = (os.environ.get("STREAM_INGEST_URLS") or "").strip()
    if raw:
        return [u.strip() for u in raw.split(",") if u.strip()]

    single = (os.environ.get("STREAM_INGEST_URL") or "").strip()
    if single:
        return [single]
    return []


# A TensorRT engine, built on the target device. Inference resolution is baked
# into the engine, so there is no IMAGE_SIZE knob here.
MODEL_PATH = os.environ.get(
    "MODEL_PATH", "/workspace/models/faux_weed_1x3x1920x1920_agx_xavier_jp4.engine"
)

CONFIDENCE = float(os.environ.get("CONFIDENCE", "0.25"))
IOU = float(os.environ.get("IOU", "0.45"))
MAX_DETECTIONS = int(os.environ.get("MAX_DETECTIONS", "300"))

# An engine carries no class names, so labels have to be supplied here.
_names_raw = (os.environ.get("CLASS_NAMES") or "").strip()
CLASS_NAMES = [n.strip() for n in _names_raw.split(",") if n.strip()]

# Cameras: indices into the MVS enumerated device list ("all" = every camera).
_indices_raw = os.environ.get("CAMERA_INDICES", "0").strip()
CAMERA_INDICES = (
    None if not _indices_raw or _indices_raw.lower() == "all"
    else _parse_int_list(_indices_raw)
)

STREAM_INGEST_URLS = _parse_url_list()
STREAM_FPS = max(1, int(os.environ.get("STREAM_FPS", "10")))
STREAM_BITRATE_KBPS = max(500, int(os.environ.get("STREAM_BITRATE_KBPS", "1500")))
STREAM_WIDTH = int(os.environ.get("STREAM_WIDTH", "960"))
STREAM_HEIGHT = int(os.environ.get("STREAM_HEIGHT", "540"))

# Resize before inference/display (keeps aspect ratio).
DISPLAY_WIDTH = int(os.environ.get("DISPLAY_WIDTH", "1920"))
DETECT_EVERY_N_FRAMES = max(1, int(os.environ.get("DETECT_EVERY_N_FRAMES", "3")))

ENABLE_AUTO_ADJUSTMENT = _env_bool("ENABLE_AUTO_ADJUSTMENT", True)
AUTO_ADJUST_MODE = os.environ.get("AUTO_ADJUST_MODE", "once")
AUTO_ADJUST_SETTLE_SECONDS = float(os.environ.get("AUTO_ADJUST_SETTLE_SECONDS", "1.5"))
LOCK_AUTO_ADJUST_AFTER_ONCE = _env_bool("LOCK_AUTO_ADJUST_AFTER_ONCE", True)

# Local X11 preview (cv2.imshow). Off by default so the portal image stays headless.
SHOW_WINDOW = _env_bool("SHOW_WINDOW", False)

ENABLE_IMAGE_SAVE = _env_bool("ENABLE_IMAGE_SAVE", False)
IMAGE_SAVE_DIR = os.environ.get("IMAGE_SAVE_DIR", "/data/camera_images")
SAVE_EVERY_N_FRAMES = max(1, int(os.environ.get("SAVE_EVERY_N_FRAMES", "30")))
IMAGE_SAVE_JPEG_QUALITY = int(os.environ.get("IMAGE_SAVE_JPEG_QUALITY", "85"))

MODEL_LOAD_RETRIES = max(1, int(os.environ.get("MODEL_LOAD_RETRIES", "3")))
MODEL_LOAD_RETRY_DELAY = float(os.environ.get("MODEL_LOAD_RETRY_DELAY", "2.0"))


def class_name(class_id):
    if 0 <= class_id < len(CLASS_NAMES):
        return CLASS_NAMES[class_id]
    return "id %d" % class_id
