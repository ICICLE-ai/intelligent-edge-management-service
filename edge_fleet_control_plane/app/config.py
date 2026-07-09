"""Centralised runtime configuration.

All environment-variable access is funnelled through this module so the rest
of the codebase can stay framework-agnostic and easy to unit-test.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote_plus

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
CONFIG_DIR = BASE_DIR / "config"
APP_DIR = BASE_DIR / "app"
TEMPLATE_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
MIGRATIONS_DIR = APP_DIR / "db" / "sql"
AGENT_PACKAGE_DIR = APP_DIR / "agent_package"

if load_dotenv is not None:
    load_dotenv(BASE_DIR / ".env")

DATA_DIR.mkdir(parents=True, exist_ok=True)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_list(name: str, default: List[str] | None = None) -> List[str]:
    raw = os.getenv(name)
    if not raw:
        return list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]


def _build_database_url() -> str:
    """Assemble Postgres URL from parts (Tapis pods limit env values to 128 chars)."""
    explicit = os.getenv("DATABASE_URL", "").strip()
    if explicit:
        return explicit
    host = os.getenv("DB_HOST", "").strip()
    if not host:
        return ""
    user = os.getenv("DB_USER", "").strip()
    password = os.getenv("DB_PASSWORD", "")
    name = os.getenv("DB_NAME", user or "postgres").strip()
    port = os.getenv("DB_PORT", "5432").strip()
    sslmode = os.getenv("DB_SSLMODE", "require").strip()
    auth = f"{quote_plus(user)}:{quote_plus(password)}@" if user else ""
    return f"postgresql://{auth}{host}:{port}/{quote_plus(name)}?sslmode={quote_plus(sslmode)}"


@dataclass(frozen=True)
class TapisConfig:
    base_url: str
    client_id: str
    client_key: str
    callback_url: str
    admin_usernames: List[str] = field(default_factory=list)

    @property
    def oauth_base(self) -> str:
        return f"{self.base_url.rstrip('/')}/v3/oauth2"

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.client_id and self.client_key and self.callback_url)


@dataclass(frozen=True)
class MQTTConfig:
    enabled: bool
    host: str
    port: int
    tls: bool
    base_topic: str
    username: str
    password: str
    client_id_prefix: str


@dataclass(frozen=True)
class MediaConfig:
    """Live-camera streaming configuration. Two modes, auto-selected:

    * **relay** (default) — the device pushes a Motion-JPEG stream over HTTPS
      straight into the control plane (``/api/stream/{device}/ingest``), played
      back as ``multipart/x-mixed-replace`` from ``/devices/{device}/stream.mjpg``.
      No external media server, no extra ports.
    * **mediamtx** — used when ``MEDIA_INGEST_URL`` + ``MEDIA_HLS_BASE_URL`` are
      set. The device pushes H.264 over RTSPS to a MediaMTX ingest pod and the
      browser plays HLS from a MediaMTX playback pod. Higher quality / more
      viewers; requires the two-pod MediaMTX deployment (see deploy/mediamtx/).
    """

    enabled: bool
    stream_prefix: str
    default_camera: str
    default_width: int
    default_height: int
    default_fps: int
    jpeg_quality: int
    default_bitrate_kbps: int
    token_ttl_seconds: int
    live_after_seconds: int
    hls_base_url: str
    ingest_url: str

    @property
    def configured(self) -> bool:
        return bool(self.enabled)

    @property
    def mode(self) -> str:
        """``mediamtx`` when external MediaMTX URLs are set, else ``relay``."""
        if not self.enabled:
            return "off"
        if self.hls_base_url and self.ingest_url:
            return "mediamtx"
        return "relay"

    def path_for(self, device_uid: str, camera_index: Optional[int] = None) -> str:
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in device_uid)
        base = f"{self.stream_prefix}-{safe}".strip("-")
        if camera_index is not None:
            return f"{base}-{int(camera_index)}"
        return base

    def hls_url(self, device_uid: str, camera_index: Optional[int] = None) -> str:
        return f"{self.hls_base_url.rstrip('/')}/{self.path_for(device_uid, camera_index)}/index.m3u8"

    def rtsp_ingest_url(self, device_uid: str, camera_index: Optional[int] = None) -> str:
        return f"{self.ingest_url.rstrip('/')}/{self.path_for(device_uid, camera_index)}"


@dataclass(frozen=True)
class AppSettings:
    env: str
    debug: bool
    secret_key: str
    base_url: str
    database_path: Path
    database_url: str
    allow_localhost_installer: bool
    heartbeat_interval_seconds: int
    device_offline_after_seconds: int
    offline_monitor_interval_seconds: int
    heartbeat_history_mode: str
    heartbeat_history_sample_seconds: int
    heartbeat_history_retention_hours: int
    heartbeat_store_payload: bool
    patra_base_url: str
    local_dev_auth: bool
    local_dev_username: str
    tapis: TapisConfig
    mqtt: MQTTConfig
    media: MediaConfig
    cors_origins: List[str] = field(default_factory=list)
    tapis_portal_origins: List[str] = field(default_factory=list)
    session_same_site: str = "lax"

    @property
    def base_url_clean(self) -> str:
        return self.base_url.rstrip("/")

    @property
    def uses_postgres(self) -> bool:
        return bool(self.database_url)

    def assert_reachable_base_url(self) -> None:
        """Refuse to mint installers that point at localhost from a Jetson."""
        if self.allow_localhost_installer:
            return
        url = self.base_url_clean.lower()
        for bad in ("localhost", "127.0.0.1", "0.0.0.0"):
            if bad in url:
                raise ValueError(
                    "APP_BASE_URL is not reachable by edge devices. "
                    "Set APP_BASE_URL to a public URL (ngrok / LAN IP / domain) "
                    "before generating an installer."
                )


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    mqtt = MQTTConfig(
        enabled=_env_bool("MQTT_ENABLED", False),
        host=os.getenv("MQTT_HOST", ""),
        port=_env_int("MQTT_PORT", 443),
        tls=_env_bool("MQTT_TLS", True),
        base_topic=os.getenv("MQTT_BASE_TOPIC", "icicle/v1"),
        username=os.getenv("MQTT_USERNAME", ""),
        password=os.getenv("MQTT_PASSWORD", ""),
        client_id_prefix=os.getenv("MQTT_CLIENT_ID_PREFIX", "icicle-control-plane"),
    )
    media = MediaConfig(
        enabled=_env_bool("MEDIA_ENABLED", False),
        stream_prefix=os.getenv("MEDIA_STREAM_PREFIX", "cam").strip() or "cam",
        default_camera=os.getenv("MEDIA_DEFAULT_CAMERA", "csi").strip().lower() or "csi",
        default_width=_env_int("MEDIA_DEFAULT_WIDTH", 1280),
        default_height=_env_int("MEDIA_DEFAULT_HEIGHT", 720),
        default_fps=_env_int("MEDIA_DEFAULT_FPS", 15),
        jpeg_quality=_env_int("MEDIA_JPEG_QUALITY", 80),
        default_bitrate_kbps=_env_int("MEDIA_DEFAULT_BITRATE_KBPS", 2000),
        token_ttl_seconds=_env_int("MEDIA_TOKEN_TTL_SECONDS", 86400),
        live_after_seconds=_env_int("MEDIA_LIVE_AFTER_SECONDS", 12),
        hls_base_url=os.getenv("MEDIA_HLS_BASE_URL", "").rstrip("/"),
        ingest_url=os.getenv("MEDIA_INGEST_URL", "").rstrip("/"),
    )
    db_path_raw = os.getenv("DATABASE_PATH", str(DATA_DIR / "edge_control_plane.db"))
    db_path = Path(db_path_raw)
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path
    base_url = os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")
    tapis_base = os.getenv("TAPIS_BASE_URL", "https://icicleai.tapis.io").rstrip("/")
    tapis_callback = os.getenv("TAPIS_CALLBACK_URL", f"{base_url}/auth/callback").rstrip("/")
    tapis = TapisConfig(
        base_url=tapis_base,
        client_id=os.getenv("TAPIS_CLIENT_ID", "").strip(),
        client_key=os.getenv("TAPIS_CLIENT_KEY", "").strip(),
        callback_url=tapis_callback,
        admin_usernames=_env_list("TAPIS_ADMIN_USERNAMES"),
    )
    return AppSettings(
        env=os.getenv("APP_ENV", "development"),
        debug=_env_bool("APP_DEBUG", True),
        secret_key=os.getenv("APP_SECRET", "dev-change-me"),
        base_url=base_url,
        database_path=db_path,
        database_url=_build_database_url(),
        allow_localhost_installer=_env_bool("ALLOW_LOCALHOST_INSTALLER", False),
        heartbeat_interval_seconds=_env_int("HEARTBEAT_INTERVAL_SECONDS", 30),
        device_offline_after_seconds=_env_int("DEVICE_OFFLINE_AFTER_SECONDS", 120),
        offline_monitor_interval_seconds=_env_int("OFFLINE_MONITOR_INTERVAL_SECONDS", 30),
        heartbeat_history_mode=os.getenv("HEARTBEAT_HISTORY_MODE", "sample").strip().lower(),
        heartbeat_history_sample_seconds=_env_int("HEARTBEAT_HISTORY_SAMPLE_SECONDS", 300),
        heartbeat_history_retention_hours=_env_int("HEARTBEAT_HISTORY_RETENTION_HOURS", 48),
        heartbeat_store_payload=_env_bool("HEARTBEAT_STORE_PAYLOAD", False),
        patra_base_url=os.getenv("PATRA_BASE_URL", "https://patrabackend.pods.icicleai.tapis.io").rstrip("/"),
        local_dev_auth=_env_bool("LOCAL_DEV_AUTH", True),
        local_dev_username=os.getenv("LOCAL_DEV_USERNAME", "local_tapis_user"),
        tapis=tapis,
        mqtt=mqtt,
        media=media,
        cors_origins=_env_list("CORS_ORIGINS"),
        tapis_portal_origins=_env_list(
            "TAPIS_PORTAL_ORIGINS",
            default=[
                "http://localhost:3000",
                "http://localhost:8080",
                "https://icicleai.tapis.io",
            ],
        ),
        session_same_site=os.getenv("SESSION_SAME_SITE", "").strip().lower()
        or ("none" if base_url.startswith("https://") else "lax"),
    )
