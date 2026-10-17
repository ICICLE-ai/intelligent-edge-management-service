"""ICICLE Edge Agent — v2.

A simpler model than the original v1 agent:

* No release manifests, no OTA bundle. Each `deploy_model` command from the
  portal is self-contained: it carries the artifact descriptor, the docker
  image, the container spec, env, mounts, args, etc.
* `stop_deployment` with `purge: false` (default) removes the container but
  keeps the Docker image and on-disk artifacts for a fast restart.
* `stop_deployment` with `purge: true` also removes the image and deployment
  directory for a full cleanup.
* `restart_deployment` creates a fresh container from the saved spec using
  the cached image and model files — no re-download or image pull.
* HTTPs heartbeats every N seconds; MQTT (paho) for inbound commands.
  Heartbeats also pull commands the broker missed while this board was off.
* Acks back to the portal over HTTPs (`/api/agent/ack`) so the UI can show
  real per-device delivery status.
* On agent start, restore from ``deploy_payload.json``: wait until Docker,
  Argus, and the RTSP ingest host are reachable, then relaunch from the saved
  spec (same as Restart: cached image + artifact, no re-download). A heartbeat
  only fills in if the container later dies. Stop records ``desired=stopped``
  so a reboot does not bring that workload back.
* RUNNING means ``docker inspect`` still shows the container running shortly
  after ``docker run -d``, not merely that the run command returned.

This agent has no third-party dependency beyond ``requests`` and (optionally)
``paho-mqtt``. It runs as a systemd unit.

Targets Python 3.6+ so one bundle works on JetPack 4 (Xavier) and JetPack 6 (Orin).
"""

import hashlib
import json
import os
import platform
import pwd
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from urllib.parse import urlparse
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

try:
    import paho.mqtt.client as mqtt
except Exception:  # pragma: no cover
    mqtt = None


AGENT_VERSION = "edge-agent-v2.4.1"


def _subprocess_run(*popenargs, **kwargs):
    """subprocess.run wrapper for Python 3.6 (JetPack 4 / Xavier)."""
    if kwargs.pop("capture_output", False):
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
    if kwargs.pop("text", False):
        kwargs["universal_newlines"] = True
    return subprocess.run(*popenargs, **kwargs)

BASE_DIR = Path(os.environ.get("ICICLE_BASE_DIR", "/opt/icicle-edge"))
CONFIG_PATH = BASE_DIR / "config" / "device_config.json"
ENROLLMENT_PATH = BASE_DIR / "enrollment.json"
CREDENTIALS_PATH = BASE_DIR / "config" / "device_credentials.json"
STATE_DIR = BASE_DIR / "state"
DEPLOYMENTS_DIR = BASE_DIR / "deployments"
LOGS_DIR = BASE_DIR / "logs"


# ---------------------------------------------------------------------------
# I/O utilities
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[icicle-agent] {msg}", flush=True)


def load_json(path: Path) -> Dict[str, Any]:
    with path.open() as f:
        return json.load(f)


def save_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    tmp.replace(path)


ARTIFACT_FILE_MODE = 0o664  # rw-rw-r-- — matches working Jetson engine files
ARTIFACT_DIR_MODE = 0o775   # rwxrwxr-x — group-writable for container bind mounts


def ensure_dir_permissions(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(ARTIFACT_DIR_MODE)


def ensure_artifact_permissions(path: Path) -> None:
    path.chmod(ARTIFACT_FILE_MODE)


def validate_downloaded_artifact(path: Path, *, expected_size: Optional[int] = None,
                                  filename: str = "") -> None:
    """Reject obvious bad downloads (JSON/HTML stubs masquerading as model files)."""
    size = path.stat().st_size
    head = path.read_bytes()[:256]
    if head.startswith(b"{") or head.startswith(b"[") or head.startswith(b"<"):
        preview = head.decode("utf-8", errors="replace")[:120]
        raise RuntimeError(
            f"download looks like text/json ({size} bytes), not a model artifact: {preview!r}"
        )
    if filename.endswith(".engine") and size < 1_000_000:
        raise RuntimeError(
            f".engine file is only {size} bytes — expected tens of MB. "
            "The download URL likely points at a manifest or error page, not the engine."
        )
    if expected_size is not None and expected_size > 0 and size != expected_size:
        raise RuntimeError(
            f"artifact size mismatch: expected {expected_size} bytes, got {size}"
        )


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _portal_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Headers for portal API calls (ngrok free tier needs skip-browser-warning)."""
    headers = {
        "ngrok-skip-browser-warning": "1",
        "User-Agent": AGENT_VERSION,
        "Accept": "application/json",
    }
    if extra:
        headers.update(extra)
    return headers


def _explain_enroll_error(url: str, status: int, text: str) -> str:
    snippet = (text or "")[:500]
    if "<!DOCTYPE html" in snippet or "ngrok" in snippet.lower():
        return (
            "Enrollment failed: HTTP %s from %s — received HTML instead of JSON. "
            "The ngrok tunnel is likely offline or APP_BASE_URL in the installer is stale. "
            "On the dev machine: restart ngrok, update APP_BASE_URL, restart the portal, "
            "re-download the installer, and run install.sh again."
            % (status, url)
        )
    return "Enrollment failed: %s %s" % (status, snippet)


def download_file(url: str, destination: Path, *, expected_size: Optional[int] = None,
                  filename: str = "") -> None:
    ensure_dir_permissions(destination.parent)
    log(f"downloading {url} → {destination}")
    tmp = destination.with_suffix(destination.suffix + ".part")
    try:
        urllib.request.urlretrieve(url, str(tmp))
        validate_downloaded_artifact(
            tmp,
            expected_size=expected_size,
            filename=filename or destination.name,
        )
        tmp.replace(destination)
        ensure_artifact_permissions(destination)
    finally:
        _safe_unlink(tmp)


def post_json_response(url: str, payload: Dict[str, Any], *, timeout: int = 15,
                       retries: int = 3, api_key: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """POST JSON to the portal; return the parsed object, or None on failure."""
    headers = _portal_headers()
    if api_key:
        headers["Authorization"] = "Bearer %s" % api_key
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
            if resp.status_code < 400:
                try:
                    data = resp.json()
                except Exception:
                    return {}
                return data if isinstance(data, dict) else {}
            log(f"post {url} rejected ({resp.status_code}): {resp.text[:300]}")
        except Exception as e:
            log(f"post {url} failed (attempt {attempt}/{retries}): {e}")
        if attempt < retries:
            time.sleep(min(2 * attempt, 6))
    return None


def post_json(url: str, payload: Dict[str, Any], *, timeout: int = 15, retries: int = 3,
              api_key: Optional[str] = None) -> bool:
    """POST JSON to the portal; return True when the server accepts the payload."""
    return post_json_response(url, payload, timeout=timeout, retries=retries, api_key=api_key) is not None


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def enroll_if_needed() -> Dict[str, Any]:
    # A freshly delivered enrollment.json (e.g. from a re-run installer)
    # always wins — it carries a brand-new token bound to the latest portal
    # base URL and device row. If it succeeds, we replace any stale
    # device_config.json on disk.
    if ENROLLMENT_PATH.exists():
        enr = load_json(ENROLLMENT_PATH)
        if enr.get("mode") != "http":
            raise RuntimeError("Only http enrollment mode is supported")
        url = enr["platform_url"].rstrip("/") + "/api/agent/enroll"
        payload = {
            "enrollment_token": enr["enrollment_token"],
            "hostname": socket.gethostname(),
            "ip_address": detect_primary_ip(),
            "agent_version": AGENT_VERSION,
        }
        log("enrolling with %s" % url)
        resp = requests.post(url, json=payload, headers=_portal_headers(), timeout=30)
        if resp.status_code >= 400:
            # If we already have a working config from a previous successful
            # enroll, fall through and keep using it rather than crash-loop.
            if CONFIG_PATH.exists():
                log("enrollment failed (%s); keeping existing device_config.json" % resp.status_code)
                return load_json(CONFIG_PATH)
            raise RuntimeError(_explain_enroll_error(url, resp.status_code, resp.text))
        cfg = resp.json()["device_config"]
        api_key = resp.json().get("device_api_key")
        save_json(CONFIG_PATH, cfg)
        if api_key:
            save_json(CREDENTIALS_PATH, {"api_key": api_key})
        _safe_unlink(ENROLLMENT_PATH)
        log("enrollment successful; device_config.json written")
        return cfg
    if CONFIG_PATH.exists():
        return load_json(CONFIG_PATH)
    raise RuntimeError("No device_config.json or enrollment.json found")


def detect_primary_ip() -> Optional[str]:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return None


def load_api_key() -> Optional[str]:
    if CREDENTIALS_PATH.exists():
        return load_json(CREDENTIALS_PATH).get("api_key")
    return None


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Platform facts used by the control plane's deploy-time compatibility check
# ---------------------------------------------------------------------------

# Jetson module name (from the device tree) -> CUDA compute capability.
_JETSON_COMPUTE_CAPABILITY = (
    ("orin", "8.7"),
    ("xavier", "7.2"),
    ("tx2", "6.2"),
    ("nano", "5.3"),
    ("tx1", "5.3"),
)

_static_platform: Optional[Dict[str, Any]] = None


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", errors="replace").replace("\x00", "").strip()
    except OSError:
        return None


def _command_output(cmd: List[str]) -> Optional[str]:
    try:
        result = _subprocess_run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _leading_version(text: Optional[str]) -> Optional[str]:
    match = re.match(r"\s*(\d+(?:\.\d+)*)", text or "")
    return match.group(1) if match else None


def _detect_l4t_version() -> Optional[str]:
    match = re.search(r"R(\d+)\b.*?REVISION:\s*([\d.]+)", _read_text("/etc/nv_tegra_release") or "")
    return "%s.%s" % (match.group(1), match.group(2)) if match else None


def _detect_tensorrt_version() -> Optional[str]:
    for package in ("tensorrt", "libnvinfer10", "libnvinfer8", "libnvinfer7"):
        version = _leading_version(_command_output(["dpkg-query", "-W", "-f=${Version}", package]))
        if version:
            return version
    return None


def _detect_compute_capability(jetson_model: Optional[str]) -> Optional[str]:
    if jetson_model:
        lowered = jetson_model.lower()
        for needle, capability in _JETSON_COMPUTE_CAPABILITY:
            if needle in lowered:
                return capability
    out = _command_output(["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"])
    return _leading_version(out.splitlines()[0]) if out else None


def _detect_memory_mb() -> Optional[int]:
    match = re.search(r"MemTotal:\s*(\d+)\s*kB", _read_text("/proc/meminfo") or "")
    return int(match.group(1)) // 1024 if match else None


def _detect_nvidia_runtime() -> Optional[bool]:
    out = _command_output(["docker", "info", "--format", "{{json .Runtimes}}"])
    return None if out is None else "nvidia" in out


def detect_platform() -> Dict[str, Any]:
    global _static_platform
    if _static_platform is None:
        model = _read_text("/proc/device-tree/model")
        _static_platform = {
            "machine": platform.machine(),
            "jetson_model": model,
            "gpu_compute_capability": _detect_compute_capability(model),
            "l4t_version": _detect_l4t_version(),
            "tensorrt_version": _detect_tensorrt_version(),
            "memory_mb": _detect_memory_mb(),
        }
    facts = dict(_static_platform)
    facts["nvidia_runtime"] = _detect_nvidia_runtime()
    disk_root = "/var/lib/docker" if os.path.isdir("/var/lib/docker") else str(BASE_DIR)
    try:
        facts["disk_free_gb"] = round(shutil.disk_usage(disk_root).free / 1e9, 1)
    except OSError:
        facts["disk_free_gb"] = None
    return facts


def heartbeat_loop(cfg: Dict[str, Any]) -> None:
    interval = int(cfg.get("heartbeat", {}).get("interval_seconds", 30))
    url = cfg["portal"]["base_url"].rstrip("/") + "/api/agent/heartbeat"
    api_key = load_api_key()
    while True:
        try:
            payload = {
                "device_id": cfg["device_id"],
                "status": "ONLINE",
                "hostname": socket.gethostname(),
                "ip_address": detect_primary_ip(),
                "agent_version": AGENT_VERSION,
                "active_containers": list_active_containers(),
                "docker_running": docker_running(),
                "platform": detect_platform(),
                "timestamp": time.time(),
            }
            body = post_json_response(url, payload, api_key=api_key) or {}
            for command in body.get("pending_commands") or []:
                if isinstance(command, dict) and command.get("operation"):
                    handle_command(cfg, command)
            restore_desired_deployments(cfg, reason="heartbeat")
        except Exception as e:
            log(f"heartbeat failed: {e}")
        time.sleep(interval)


def docker_running() -> bool:
    r = subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode == 0


def list_active_containers() -> List[Dict[str, Any]]:
    try:
        out = subprocess.check_output(
            ["docker", "ps", "--format", "{{.Names}}|{{.Image}}|{{.State}}|{{.Status}}"],
            stderr=subprocess.DEVNULL,
            universal_newlines=True,
        )
    except Exception:
        return []
    rows = []
    for line in out.strip().splitlines():
        parts = line.split("|")
        if len(parts) >= 4:
            rows.append({"name": parts[0], "image": parts[1], "state": parts[2], "status_text": parts[3]})
    return rows


# ---------------------------------------------------------------------------
# Acking commands back to portal
# ---------------------------------------------------------------------------

def send_ack(cfg: Dict[str, Any], *, request_id: str, deployment_uid: Optional[str],
             operation: str, status: str, container_id: Optional[str] = None,
             container_name: Optional[str] = None, error: Optional[str] = None,
             timings: Optional[Dict[str, Any]] = None,
             provenance: Optional[Dict[str, Any]] = None) -> None:
    url = cfg["portal"]["base_url"].rstrip("/") + "/api/agent/ack"
    payload = {
        "device_id": cfg["device_id"],
        "request_id": request_id,
        "deployment_uid": deployment_uid,
        "operation": operation,
        "status": status,
        "container_id": container_id,
        "container_name": container_name,
        "error": error,
        "ts": time.time(),
    }
    if timings is not None:
        payload["timings"] = dict(timings)
    if provenance is not None:
        payload["provenance"] = dict(provenance)
    if not post_json(url, payload, api_key=load_api_key()):
        log(f"ack not accepted by portal: {operation} → {status} (request_id={request_id})")


# ---------------------------------------------------------------------------
# Deployment lifecycle
# ---------------------------------------------------------------------------

def deployment_dir(deployment_uid: str) -> Path:
    return DEPLOYMENTS_DIR / deployment_uid


def resolve_patra_url(cfg: Dict[str, Any], patra_uuid: str) -> str:
    base = cfg.get("patra", {}).get("base_url", "").rstrip("/")
    if not base:
        raise RuntimeError("device_config.json missing patra.base_url")
    url = f"{base}/modelcard/{patra_uuid}/download_url"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not data.get("download_url"):
        raise RuntimeError(f"Patra did not return download_url for {patra_uuid}: {data}")
    return data["download_url"]


def resolve_artifact_url(cfg: Dict[str, Any], artifact: Dict[str, Any]) -> str:
    """Pick the best download URL for an artifact descriptor."""
    source_type = (artifact.get("source_type") or "url").lower()
    patra_uuid = artifact.get("patra_model_card_uuid")
    if source_type == "patra":
        if not patra_uuid:
            raise RuntimeError("source_type=patra requires patra_model_card_uuid")
        return resolve_patra_url(cfg, patra_uuid)
    url = artifact.get("download_url")
    if url:
        return url
    if patra_uuid:
        log("artifact has no download_url; resolving via Patra")
        return resolve_patra_url(cfg, patra_uuid)
    raise RuntimeError("No artifact source URL provided")


def _x11_session_context() -> Optional[Dict[str, Any]]:
    """Detect the logged-in user's X session (agent runs as root via systemd)."""
    display = os.environ.get("DISPLAY") or ":0"
    display_num = display.lstrip(":").split(".")[0]
    sock = Path(f"/tmp/.X11-unix/X{display_num}")
    if not sock.exists():
        sock = Path("/tmp/.X11-unix/X0")
        display = ":0"
    if not sock.exists():
        return None

    uid = sock.stat().st_uid
    if uid == 0:
        return {"user": "root", "display": display, "xauthority": os.environ.get("XAUTHORITY"), "uid": 0}

    pw = pwd.getpwuid(uid)
    home = Path(pw.pw_dir)
    xauthority: Optional[str] = os.environ.get("XAUTHORITY")
    candidates: List[Path] = []
    if xauthority:
        candidates.append(Path(xauthority))
    candidates.extend([
        Path(f"/run/user/{uid}/gdm/Xauthority"),
        home / ".Xauthority",
    ])
    candidates.extend(sorted(home.glob(".serverauth.*"), reverse=True))

    resolved_xauth: Optional[str] = None
    for path in candidates:
        if path.exists():
            resolved_xauth = str(path)
            break

    override_user = os.environ.get("ICICLE_DISPLAY_USER")
    return {
        "user": override_user or pw.pw_name,
        "display": display,
        "xauthority": resolved_xauth,
        "uid": uid,
    }


def _run_xhost_as_user(ctx: Dict[str, Any], xhost_args: List[str]) -> subprocess.CompletedProcess:
    env: Dict[str, str] = {"DISPLAY": ctx["display"]}
    if ctx.get("xauthority"):
        env["XAUTHORITY"] = ctx["xauthority"]
    env_list = [f"{k}={v}" for k, v in env.items()]
    if ctx["user"] == "root" or os.geteuid() != 0:
        return _subprocess_run(xhost_args, env={**os.environ, **env}, capture_output=True, text=True)
    if shutil.which("runuser"):
        return _subprocess_run(
            ["runuser", "-u", ctx["user"], "--", "env", *env_list, *xhost_args],
            capture_output=True,
            text=True,
        )
    shell_cmd = " ".join([f"{k}={v}" for k, v in env.items()] + xhost_args)
    return _subprocess_run(["su", "-", ctx["user"], "-c", shell_cmd], capture_output=True, text=True)


def prepare_host_display_for_camera() -> None:
    """Allow Docker containers (root inside) to use the host X display for camera preview.

    The agent systemd unit runs as root without the desktop user's X authority cookie.
    xhost must be executed as the user who owns :0 — the same as running it manually
    in a desktop terminal on the Jetson.
    """
    ctx = _x11_session_context()
    if not ctx:
        log("display prep skipped — no local X session (/tmp/.X11-unix/X0 missing)")
        return

    xhost_variants = (
        ["xhost", "+local:root"],
        ["xhost", "+local:docker"],
        ["xhost", "+local:"],
        ["xhost", "+si:localuser:root"],
    )
    errors: List[str] = []
    for args in xhost_variants:
        result = _run_xhost_as_user(ctx, args)
        if result.returncode == 0:
            who = ctx["user"]
            log(f"display prep: {' '.join(args)} as {who} (DISPLAY={ctx['display']})")
            return
        detail = (result.stderr or result.stdout or "xhost failed").strip()
        errors.append(f"{' '.join(args)}: {detail[:120]}")

    log(
        "display prep FAILED — camera access in containers will likely fail until "
        f"xhost +local:root is run on the device as {ctx['user']}. "
        + "; ".join(errors[:2])
    )


def ensure_image(image: str, pull_policy: str) -> bool:
    """Make ``image`` available locally. Returns True if it was pulled."""
    inspect = subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    have = inspect.returncode == 0
    if pull_policy == "never":
        if not have:
            raise RuntimeError(f"Docker image not present locally: {image}")
        return False
    if pull_policy == "always" or not have:
        log(f"pulling image {image} (this can take several minutes on Jetson)")
        result = _subprocess_run(["docker", "pull", image], capture_output=True, text=True)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "docker pull failed").strip()
            raise RuntimeError(f"docker pull failed: {detail[:500]}")
        log(f"image pull complete: {image}")
        return True
    return False


def resolve_env_value(value: Any) -> str:
    if isinstance(value, str) and value == "$DISPLAY":
        return os.environ.get("DISPLAY", ":0")
    if isinstance(value, str) and value.startswith("$"):
        return os.environ.get(value[1:], value)
    return str(value)


def substitute_path(value: Any, env: Dict[str, str]) -> str:
    if not isinstance(value, str):
        return value
    out = value
    for k, v in env.items():
        out = out.replace("${" + k + "}", v)
    return out


def mount_template_env(deployment_path: Path, artifact_local_path: Path) -> Dict[str, str]:
    return {
        "DEPLOYMENT_DIR": str(deployment_path),
        "MODEL_FILE": str(artifact_local_path),
        "MODEL_DIR": str(artifact_local_path.parent),
    }


def validate_bind_mounts(payload: Dict[str, Any], deployment_path: Path,
                         artifact_local_path: Path) -> None:
    """Fail fast when a host bind source required by the model card is missing."""
    runtime = payload.get("runtime", {})
    template_env = mount_template_env(deployment_path, artifact_local_path)
    missing: List[str] = []
    for m in runtime.get("mounts") or []:
        src = substitute_path(m["source"], template_env)
        if not src.startswith("/"):
            continue
        if Path(src).exists():
            continue
        missing.append(f"{src} → {m['target']}")
    if missing:
        raise RuntimeError(
            "bind mount source(s) missing on device: "
            + "; ".join(missing)
            + " — create them or update the model card mounts before deploying."
        )


def build_run_command(payload: Dict[str, Any], deployment_path: Path,
                      artifact_local_path: Path) -> Tuple[List[str], str]:
    container = payload["container"]
    runtime = payload.get("runtime", {})
    name = container["container_name"]
    image = container.get("image") or f"{container['image_repository']}:{container.get('image_tag','latest')}"

    cmd: List[str] = ["docker", "run", "-d", "--name", name]

    if container.get("network_mode"):
        cmd += ["--network", container["network_mode"]]
    if container.get("ipc_mode"):
        cmd += ["--ipc", container["ipc_mode"]]
    if container.get("runtime"):
        cmd += ["--runtime", container["runtime"]]
    if container.get("gpus"):
        cmd += ["--gpus", container["gpus"]]
    if container.get("privileged"):
        cmd.append("--privileged")
    if container.get("restart_policy") and container["restart_policy"] != "no":
        cmd += ["--restart", container["restart_policy"]]
    if container.get("shm_size"):
        cmd += ["--shm-size", container["shm_size"]]
    if container.get("working_dir"):
        cmd += ["-w", container["working_dir"]]
    if container.get("remove_after_exit"):
        cmd.append("--rm")

    extra_args = runtime.get("docker_args") or []
    for a in extra_args:
        cmd.append(str(a))

    template_env = mount_template_env(deployment_path, artifact_local_path)

    env_keys = {e["key"] for e in (runtime.get("environment") or [])}
    for env_entry in runtime.get("environment") or []:
        key = env_entry["key"]
        val = resolve_env_value(env_entry["value"])
        cmd += ["-e", f"{key}={val}"]
    # Implicit model env var (skip if already declared above)
    if runtime.get("model_env_var") and runtime["model_env_var"] not in env_keys:
        cmd += ["-e", f"{runtime['model_env_var']}={payload['artifact']['container_path']}"]

    for m in runtime.get("mounts") or []:
        src = substitute_path(m["source"], template_env)
        tgt = m["target"]
        if m.get("style") == "mount":
            spec = f"type={m.get('type','bind')},source={src},target={tgt}"
            mode = m.get("mode")
            if mode in {"ro", "readonly"}:
                spec += ",readonly"
            elif mode:
                spec += f",{mode}"
            cmd += ["--mount", spec]
        else:
            vol = f"{src}:{tgt}"
            if m.get("mode"):
                vol += f":{m['mode']}"
            cmd += ["-v", vol]

    for p in runtime.get("ports") or []:
        host = p.get("host_port")
        cont = p["container_port"]
        proto = p.get("protocol", "tcp")
        if host:
            cmd += ["-p", f"{host}:{cont}/{proto}"]
        else:
            cmd += ["-p", f"{cont}/{proto}"]

    if container.get("entrypoint"):
        cmd += ["--entrypoint", json.dumps(container["entrypoint"])]  # simplest safe form

    cmd.append(image)

    if container.get("command"):
        cmd += [str(a) for a in container["command"]]

    return cmd, name


def sha256_file(path: Path) -> Optional[str]:
    try:
        digest = hashlib.sha256()
        with open(str(path), "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as e:
        log("could not hash %s: %s" % (path, e))
        return None


def inspect_image_provenance(image: str, container_id: Optional[str]) -> Dict[str, Optional[str]]:
    image_id = None
    if container_id:
        image_id = _command_output(["docker", "inspect", "-f", "{{.Image}}", container_id])
    if not image_id and image:
        image_id = _command_output(["docker", "inspect", "-f", "{{.Id}}", image])
    digest = _command_output(["docker", "inspect", "-f", "{{index .RepoDigests 0}}", image]) if image else None
    if digest in ("", "<no value>", "<none>"):
        digest = None
    return {"image_id": image_id, "image_digest": digest, "image": image}


def collect_provenance(payload: Dict[str, Any], artifact_path: Path, *,
                       image: str, container_id: Optional[str]) -> Dict[str, Any]:
    observed = {
        "artifact_sha256": sha256_file(artifact_path) if artifact_path.exists() else None,
        "artifact_bytes": artifact_path.stat().st_size if artifact_path.exists() else None,
        "container_id": container_id,
        "agent_version": AGENT_VERSION,
    }
    observed.update(inspect_image_provenance(image, container_id))
    declared = (payload.get("artifact") or {}).get("sha256")
    if declared and observed.get("artifact_sha256") and declared.lower().replace("sha256:", "") != observed["artifact_sha256"]:
        log("artifact sha256 drift: command declared %s, file is %s" % (declared, observed["artifact_sha256"]))
    return observed


def _container_running_state(container_name: str) -> Optional[bool]:
    """Return True if running, False if stopped, None if the container does not exist."""
    result = _subprocess_run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip().lower() == "true"


def _container_logs_tail(container_name: str, lines: int = 40) -> str:
    result = _subprocess_run(
        ["docker", "logs", "--tail", str(lines), container_name],
        capture_output=True,
        text=True,
    )
    text = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
    return text


def _confirm_container_running(container_name: str, timeout_s: float = 2.5) -> None:
    """Fail if the container is not still Running after a short grace period."""
    deadline = time.monotonic() + timeout_s
    saw_running = False
    last_state = None  # type: Optional[bool]
    while time.monotonic() < deadline:
        last_state = _container_running_state(container_name)
        if last_state is True:
            saw_running = True
        elif last_state is False:
            logs = _container_logs_tail(container_name)
            raise RuntimeError(
                "container exited after start: %s" % (logs[:400] or container_name)
            )
        elif last_state is None and saw_running:
            raise RuntimeError("container disappeared after start: %s" % container_name)
        time.sleep(0.35)
    if not saw_running or _container_running_state(container_name) is not True:
        logs = _container_logs_tail(container_name)
        raise RuntimeError(
            "container not running after docker start (state=%s): %s"
            % (last_state, logs[:400] or container_name)
        )


def _load_json_if_exists(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return load_json(path)
    except Exception as e:
        log("could not read %s: %s" % (path, e))
        return None


def _merge_state(dpath: Path, **fields: Any) -> Dict[str, Any]:
    state = _load_json_if_exists(dpath / "state.json") or {}
    state.update(fields)
    save_json(dpath / "state.json", state)
    return state


def _deployment_desired_running(state: Optional[Dict[str, Any]]) -> bool:
    if not state:
        return False
    desired = state.get("desired")
    if desired == "stopped":
        return False
    if desired == "running":
        return True
    # Agent < 2.4.0 wrote state without desired. Stop already docker-rm'd the
    # container; a reboot leftover is still present as a stopped container.
    cname = state.get("container_name")
    if not cname:
        return False
    return _container_running_state(cname) is not None


def _run_container(payload: Dict[str, Any], dpath: Path, artifact_path: Path,
                   *, image: str, cname: str, confirm_s: float = 2.5) -> str:
    prepare_host_display_for_camera()
    subprocess.run(["docker", "rm", "-f", cname],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cmd, _ = build_run_command(payload, dpath, artifact_path)
    log("running: " + " ".join(cmd))
    result = _subprocess_run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "docker run failed").strip()
        raise RuntimeError(detail[:500])
    container_id = result.stdout.strip()
    _confirm_container_running(cname, timeout_s=confirm_s)
    return container_id


def _start_container_from_payload(cfg: Dict[str, Any], payload: Dict[str, Any], *,
                                  request_id: str, operation: str,
                                  skip_download: bool = False,
                                  received_mono: Optional[float] = None) -> None:
    # Phase durations use the device's monotonic clock so they are immune to
    # clock skew between the device and the control plane.
    started_mono = time.monotonic()
    origin_mono = received_mono if received_mono is not None else started_mono
    timings: Dict[str, Any] = {
        "agent_version": AGENT_VERSION,
        "received_wall_ts": time.time() - (started_mono - origin_mono),
        "queue_s": round(started_mono - origin_mono, 4),
    }

    def phase_end(key: str, began: float) -> None:
        timings[key] = round(time.monotonic() - began, 4)

    deployment_uid = payload["deployment_uid"]
    artifact = payload["artifact"]
    container = payload["container"]
    image = container.get("image") or f"{container['image_repository']}:{container.get('image_tag','latest')}"
    cname = container["container_name"]

    dpath = deployment_dir(deployment_uid)
    ensure_dir_permissions(dpath)
    save_json(dpath / "deploy_payload.json", payload)

    model_dir = dpath / "model"
    ensure_dir_permissions(model_dir)
    artifact_path = model_dir / artifact["filename"]

    try:
        if skip_download and artifact_path.exists() and artifact_path.stat().st_size > 0:
            log(f"reusing cached artifact: {artifact_path}")
            timings["artifact_cached"] = True
        else:
            timings["artifact_cached"] = False
            began = time.monotonic()
            url = resolve_artifact_url(cfg, artifact)
            phase_end("resolve_s", began)
            send_ack(cfg, request_id=request_id, deployment_uid=deployment_uid,
                     operation=operation, status="DOWNLOADING", timings=timings)
            began = time.monotonic()
            download_file(
                url,
                artifact_path,
                expected_size=artifact.get("size_bytes"),
                filename=artifact["filename"],
            )
            phase_end("download_s", began)
            log(f"artifact ready: {artifact_path} ({artifact_path.stat().st_size} bytes)")
        timings["artifact_bytes"] = artifact_path.stat().st_size if artifact_path.exists() else None

        validate_bind_mounts(payload, dpath, artifact_path)

        send_ack(cfg, request_id=request_id, deployment_uid=deployment_uid,
                 operation=operation, status="PULLING", timings=timings)
        pull_policy = container.get("pull_policy", "if_not_present")
        if skip_download and operation in {"restart_deployment", "recover_deployment"}:
            pull_policy = "never"
        began = time.monotonic()
        pulled = ensure_image(image, pull_policy)
        phase_end("pull_s", began)
        timings["image_cached"] = not pulled

        send_ack(cfg, request_id=request_id, deployment_uid=deployment_uid,
                 operation=operation, status="STARTING",
                 container_name=cname, timings=timings)

        began = time.monotonic()
        container_id = _run_container(payload, dpath, artifact_path, image=image, cname=cname)
        phase_end("run_s", began)
        log(f"container started: {cname} ({container_id[:12]})")
        provenance = collect_provenance(payload, artifact_path, image=image, container_id=container_id)
        save_json(dpath / "state.json", {
            "deployment_uid": deployment_uid,
            "container_name": cname,
            "container_id": container_id,
            "image": image,
            "artifact_path": str(artifact_path),
            "started_at": time.time(),
            "desired": "running",
            "last_request_id": request_id,
            "last_operation": operation,
            "provenance": provenance,
        })
        phase_end("total_s", origin_mono)
        send_ack(cfg, request_id=request_id, deployment_uid=deployment_uid,
                 operation=operation, status="RUNNING",
                 container_id=container_id, container_name=cname, timings=timings,
                 provenance=provenance)
    except Exception as e:
        log(f"{operation} failed: {e}")
        phase_end("total_s", origin_mono)
        send_ack(cfg, request_id=request_id, deployment_uid=deployment_uid,
                 operation=operation, status="FAILED",
                 container_name=cname, error=str(e), timings=timings)


def deploy_model(cfg: Dict[str, Any], payload: Dict[str, Any],
                 received_mono: Optional[float] = None) -> None:
    _start_container_from_payload(
        cfg,
        payload,
        request_id=payload["request_id"],
        operation="deploy_model",
        skip_download=False,
        received_mono=received_mono,
    )


def restart_deployment(cfg: Dict[str, Any], payload: Dict[str, Any],
                       received_mono: Optional[float] = None) -> None:
    deployment_uid = payload["deployment_uid"]
    request_id = payload["request_id"]
    dpath = deployment_dir(deployment_uid)
    saved_path = dpath / "deploy_payload.json"
    cname = payload.get("container_name") or ""

    try:
        if not saved_path.exists():
            raise RuntimeError(
                "No saved deployment on device — purge and create a new deployment."
            )
        saved = load_json(saved_path)
        cname = saved["container"]["container_name"]
        log(f"restart: creating new container {cname} from cached image and artifacts")
        _start_container_from_payload(
            cfg,
            saved,
            request_id=request_id,
            operation="restart_deployment",
            skip_download=True,
            received_mono=received_mono,
        )
    except Exception as e:
        log(f"restart_deployment failed: {e}")
        send_ack(cfg, request_id=request_id, deployment_uid=deployment_uid,
                 operation="restart_deployment", status="FAILED",
                 container_name=cname, error=str(e))


def stop_deployment(cfg: Dict[str, Any], payload: Dict[str, Any]) -> None:
    deployment_uid = payload["deployment_uid"]
    request_id = payload["request_id"]
    cname = payload["container_name"]
    image = payload.get("image")
    purge = bool(payload.get("purge", False))
    dpath = deployment_dir(deployment_uid)

    try:
        if dpath.exists() and not purge:
            _merge_state(
                dpath,
                desired="stopped",
                stopped_at=time.time(),
                last_request_id=request_id,
                last_operation="stop_deployment",
            )
        if purge:
            subprocess.run(["docker", "rm", "-f", cname], check=False)
            if image:
                log(f"removing image {image}")
                subprocess.run(["docker", "image", "rm", "-f", image], check=False)
            if dpath.exists():
                log(f"removing deployment directory {dpath}")
                shutil.rmtree(dpath, ignore_errors=True)
        else:
            log(f"removing container {cname} (keeping image and artifacts)")
            subprocess.run(["docker", "rm", "-f", cname], check=False)
        send_ack(cfg, request_id=request_id, deployment_uid=deployment_uid,
                 operation="stop_deployment", status="STOPPED",
                 container_name=cname)
    except Exception as e:
        log(f"stop_deployment failed: {e}")
        send_ack(cfg, request_id=request_id, deployment_uid=deployment_uid,
                 operation="stop_deployment", status="FAILED",
                 container_name=cname, error=str(e))


_restore_lock = threading.Lock()
_restore_fail_until: Dict[str, float] = {}
_seen_request_ids: Set[str] = set()


def _wait_for_docker(timeout_s: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if docker_running():
            return True
        log("waiting for docker before recover")
        time.sleep(2)
    log("docker not ready; recover will retry on heartbeat")
    return False


def _ingest_endpoints(payload: Dict[str, Any]) -> List[Tuple[str, int]]:
    endpoints: List[Tuple[str, int]] = []
    seen = set()  # type: Set[Tuple[str, int]]
    for entry in (payload.get("runtime") or {}).get("environment") or []:
        key = entry.get("key") or ""
        if not key.startswith("STREAM_INGEST_URL"):
            continue
        parsed = urlparse(entry.get("value") or "")
        host = parsed.hostname
        if not host:
            continue
        if parsed.port:
            port = int(parsed.port)
        elif parsed.scheme in ("rtsps", "https"):
            port = 443
        else:
            port = 554
        item = (host, port)
        if item not in seen:
            seen.add(item)
            endpoints.append(item)
    return endpoints


def _tcp_open(host: str, port: int, timeout_s: float = 3.0) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    try:
        sock.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _wait_for_argus(timeout_s: float = 30.0) -> bool:
    sock_path = Path("/tmp/argus_socket")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if sock_path.exists():
            return True
        log("waiting for Argus socket %s" % sock_path)
        time.sleep(1)
    log("Argus socket not ready; continuing")
    return False


def _wait_for_ingest(payload: Dict[str, Any], timeout_s: float = 60.0) -> bool:
    endpoints = _ingest_endpoints(payload)
    if not endpoints:
        return True
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if all(_tcp_open(host, port) for host, port in endpoints):
            log("ingest reachable: %s" % ", ".join("%s:%s" % e for e in endpoints))
            return True
        log("waiting for ingest %s" % ", ".join("%s:%s" % e for e in endpoints))
        time.sleep(2)
    log("ingest not reachable yet; launching anyway")
    return False


def _wait_for_recover_ready(payload: Dict[str, Any]) -> None:
    _wait_for_docker()
    _wait_for_argus()
    _wait_for_ingest(payload)


def _ack_recovered(cfg: Dict[str, Any], payload: Dict[str, Any], dpath: Path,
                   *, cname: str, container_id: Optional[str], status: str,
                   error: Optional[str] = None) -> None:
    uid = payload["deployment_uid"]
    request_id = "recover-%s-%s" % (uid, int(time.time()))
    artifact = payload.get("artifact") or {}
    artifact_path = Path(
        (_load_json_if_exists(dpath / "state.json") or {}).get("artifact_path")
        or str(dpath / "model" / (artifact.get("filename") or "model.bin"))
    )
    container = payload.get("container") or {}
    image = container.get("image") or "%s:%s" % (
        container.get("image_repository") or "",
        container.get("image_tag") or "latest",
    )
    provenance = None
    if status == "RUNNING":
        provenance = collect_provenance(
            payload, artifact_path, image=image, container_id=container_id
        )
        _merge_state(
            dpath,
            deployment_uid=uid,
            container_name=cname,
            container_id=container_id,
            image=image,
            artifact_path=str(artifact_path),
            desired="running",
            last_operation="recover_deployment",
            recovered_at=time.time(),
            provenance=provenance,
        )
    send_ack(
        cfg,
        request_id=request_id,
        deployment_uid=uid,
        operation="recover_deployment",
        status=status,
        container_id=container_id,
        container_name=cname,
        error=error,
        provenance=provenance,
    )


def _relaunch_from_payload(payload: Dict[str, Any], dpath: Path,
                           state: Dict[str, Any], cname: str,
                           confirm_s: float) -> str:
    artifact = payload["artifact"]
    artifact_path = Path(
        state.get("artifact_path")
        or str(dpath / "model" / artifact["filename"])
    )
    container = payload.get("container") or {}
    image = container.get("image") or "%s:%s" % (
        container.get("image_repository"),
        container.get("image_tag") or "latest",
    )
    if not artifact_path.exists():
        raise RuntimeError("cached artifact missing: %s" % artifact_path)
    return _run_container(
        payload, dpath, artifact_path,
        image=image, cname=cname, confirm_s=confirm_s,
    )


def _recover_one(cfg: Dict[str, Any], dpath: Path, payload: Dict[str, Any],
                 state: Dict[str, Any], reason: str) -> None:
    uid = payload["deployment_uid"]
    container = payload.get("container") or {}
    cname = container.get("container_name") or state.get("container_name")
    if not cname:
        return
    now = time.monotonic()
    if now < _restore_fail_until.get(uid, 0):
        return
    running = _container_running_state(cname)
    # Agent start must relaunch even if Docker already has a "running"
    # leftover from a too-early start (camera up, RTSP ingest dead).
    if running is True and reason != "boot":
        return
    if reason == "boot":
        _wait_for_recover_ready(payload)

    attempts = 3 if reason == "boot" else 1
    confirm_s = 8.0 if reason == "boot" else 2.5
    last_error = None  # type: Optional[Exception]
    for attempt in range(1, attempts + 1):
        try:
            if reason == "boot":
                log("recover (%s): relaunch %s from saved payload (attempt %s/%s)" % (
                    reason, cname, attempt, attempts))
                container_id = _relaunch_from_payload(
                    payload, dpath, state, cname, confirm_s,
                )
            else:
                current = _container_running_state(cname)
                if current is False:
                    log("recover (%s): docker start %s (attempt %s/%s)" % (
                        reason, cname, attempt, attempts))
                    prepare_host_display_for_camera()
                    result = _subprocess_run(
                        ["docker", "start", cname], capture_output=True, text=True
                    )
                    if result.returncode != 0:
                        detail = (result.stderr or result.stdout or "docker start failed").strip()
                        raise RuntimeError(detail[:500])
                    _confirm_container_running(cname, timeout_s=confirm_s)
                    container_id = _command_output(
                        ["docker", "inspect", "-f", "{{.Id}}", cname]
                    )
                else:
                    log("recover (%s): relaunch %s from saved payload (attempt %s/%s)" % (
                        reason, cname, attempt, attempts))
                    container_id = _relaunch_from_payload(
                        payload, dpath, state, cname, confirm_s,
                    )
            _ack_recovered(
                cfg, payload, dpath, cname=cname,
                container_id=container_id, status="RUNNING",
            )
            _restore_fail_until.pop(uid, None)
            log("recover (%s): %s is running" % (reason, cname))
            return
        except Exception as e:
            last_error = e
            log("recover (%s) attempt %s/%s failed for %s: %s" % (
                reason, attempt, attempts, cname, e))
            if attempt < attempts:
                time.sleep(5)

    _restore_fail_until[uid] = time.monotonic() + 60
    log("recover (%s) giving up on %s until cooldown; %s" % (
        reason, cname, last_error))


def restore_desired_deployments(cfg: Dict[str, Any], reason: str = "boot") -> None:
    """Bring back containers this agent still intends to run."""
    if not DEPLOYMENTS_DIR.exists():
        return
    if not docker_running():
        return
    with _restore_lock:
        for dpath in sorted(DEPLOYMENTS_DIR.iterdir()):
            if not dpath.is_dir():
                continue
            payload = _load_json_if_exists(dpath / "deploy_payload.json")
            if not payload or not payload.get("deployment_uid"):
                continue
            uid = payload["deployment_uid"]
            if uid in _active_deployments:
                continue
            state = _load_json_if_exists(dpath / "state.json") or {}
            if not _deployment_desired_running(state):
                continue
            try:
                _recover_one(cfg, dpath, payload, state, reason)
            except Exception as e:
                log("recover (%s) skipped %s: %s" % (reason, uid, e))


# ---------------------------------------------------------------------------
# Live camera streaming (stream_start / stream_stop)
# ---------------------------------------------------------------------------

_streams_lock = threading.Lock()
_streams: Dict[str, "subprocess.Popen"] = {}


def _redact_url(value: str) -> str:
    """Strip a ``token=...`` query value so it never lands in logs."""
    if not isinstance(value, str) or "token=" not in value:
        return value
    head, _, _tail = value.partition("token=")
    return f"{head}token=***"


def build_stream_pipeline(stream: Dict[str, Any]) -> List[str]:
    """Build a GStreamer pipeline for the requested streaming protocol.

    ``stream.protocol`` selects the transport:
    * ``mjpeg-http`` (default) -> JPEG-encode and push as Motion-JPEG over HTTPS
      straight into the control plane (``souphttpclientsink``). No hardware
      encoder needed (sidesteps the Orin Nano's missing NVENC) and rides on the
      control plane's single HTTPS port. The server scans for JPEG markers, so no
      muxer is required.
    * ``rtsp`` -> H.264-encode (software ``x264enc``, again no NVENC required) and
      push over RTSPS to a MediaMTX ingest pod (``rtspclientsink protocols=tcp``).

    ``camera.type`` selects the source:
    * ``csi`` -> nvarguscamerasrc (Jetson CSI; NVMM frames, converted via nvvidconv)
    * ``usb`` -> v4l2src (/dev/videoN)
    """
    cam = stream.get("camera", {}) or {}
    protocol = (stream.get("protocol") or "mjpeg-http").lower()
    cam_type = (cam.get("type") or "csi").lower()
    width = int(cam.get("width", 1280))
    height = int(cam.get("height", 720))
    fps = int(cam.get("fps", 15))
    device = cam.get("device", "/dev/video0")
    ingest_url = stream["ingest_url"]

    gst = shutil.which("gst-launch-1.0") or "gst-launch-1.0"

    if protocol == "rtsp":
        bitrate_kbps = int(cam.get("bitrate_kbps", 2000))
        # protocols=tcp forces RTP interleaving over the single TLS connection
        # (Tapis exposes one TLS/TCP endpoint per pod). tls-validation-flags=0
        # tolerates the proxy's cert handling.
        sink = [
            "rtspclientsink", f"location={ingest_url}",
            "protocols=tcp", "tls-validation-flags=0",
        ]

        def h264() -> List[str]:
            return [
                "x264enc", "tune=zerolatency", "speed-preset=ultrafast",
                f"bitrate={bitrate_kbps}", f"key-int-max={fps * 2}",
            ]

        if cam_type == "usb":
            return [
                gst, "-e",
                "v4l2src", f"device={device}", "!",
                "videoconvert", "!",
                f"video/x-raw,width={width},height={height},framerate={fps}/1", "!",
                *h264(), "!", "h264parse", "!", *sink,
            ]
        return [
            gst, "-e",
            "nvarguscamerasrc", "!",
            f"video/x-raw(memory:NVMM),width={width},height={height},framerate={fps}/1", "!",
            "nvvidconv", "!", "video/x-raw,format=I420", "!",
            *h264(), "!", "h264parse", "!", *sink,
        ]

    # Default: MJPEG relay over HTTPS into the control plane.
    quality = int(cam.get("jpeg_quality", 80))
    sink = ["souphttpclientsink", f"location={ingest_url}"]
    if cam_type == "usb":
        return [
            gst, "-e",
            "v4l2src", f"device={device}", "!",
            "videoconvert", "!",
            f"video/x-raw,width={width},height={height},framerate={fps}/1", "!",
            "jpegenc", f"quality={quality}", "!",
            *sink,
        ]
    return [
        gst, "-e",
        "nvarguscamerasrc", "!",
        f"video/x-raw(memory:NVMM),width={width},height={height},framerate={fps}/1", "!",
        "nvvidconv", "!", "video/x-raw,format=I420", "!",
        "jpegenc", f"quality={quality}", "!",
        *sink,
    ]


def stream_start(cfg: Dict[str, Any], payload: Dict[str, Any]) -> None:
    request_id = payload.get("request_id", "?")
    stream = payload.get("stream") or {}
    path = stream.get("path") or "default"
    try:
        if not stream.get("ingest_url"):
            raise RuntimeError("stream_start missing stream.ingest_url")
        pipeline = build_stream_pipeline(stream)
        with _streams_lock:
            existing = _streams.pop(path, None)
        if existing and existing.poll() is None:
            log(f"stream {path}: replacing existing pipeline (pid={existing.pid})")
            existing.terminate()
            try:
                existing.wait(timeout=5)
            except subprocess.TimeoutExpired:
                existing.kill()
        # Redact the ingest token from logs (it lives in the URL query string).
        safe_pipeline = [_redact_url(a) for a in pipeline]
        log("stream pipeline: " + " ".join(safe_pipeline))
        proc = subprocess.Popen(pipeline, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        # Give GStreamer a moment to negotiate; a bad pipeline exits almost immediately.
        time.sleep(2.0)
        if proc.poll() is not None:
            err = (proc.stderr.read().decode("utf-8", "replace") if proc.stderr else "").strip()
            raise RuntimeError(f"gst-launch exited immediately: {err[:500]}")
        with _streams_lock:
            _streams[path] = proc
        log(f"stream {path}: streaming to {_redact_url(stream.get('ingest_url', ''))} (pid={proc.pid})")
        send_ack(cfg, request_id=request_id, deployment_uid=None,
                 operation="stream_start", status="RUNNING", container_name=path)
    except Exception as e:
        log(f"stream_start failed: {e}")
        send_ack(cfg, request_id=request_id, deployment_uid=None,
                 operation="stream_start", status="FAILED", container_name=path, error=str(e))


def stream_stop(cfg: Dict[str, Any], payload: Dict[str, Any]) -> None:
    request_id = payload.get("request_id", "?")
    stream = payload.get("stream") or {}
    path = stream.get("path") or "default"
    try:
        with _streams_lock:
            proc = _streams.pop(path, None)
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            log(f"stream {path}: stopped")
        else:
            log(f"stream {path}: no active pipeline to stop")
        send_ack(cfg, request_id=request_id, deployment_uid=None,
                 operation="stream_stop", status="STOPPED", container_name=path)
    except Exception as e:
        log(f"stream_stop failed: {e}")
        send_ack(cfg, request_id=request_id, deployment_uid=None,
                 operation="stream_stop", status="FAILED", container_name=path, error=str(e))


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------

_command_lock = threading.Lock()
_active_deployments: Set[str] = set()


def _run_command(cfg: Dict[str, Any], payload: Dict[str, Any],
                 received_mono: Optional[float] = None) -> None:
    op = payload.get("operation")
    deployment_uid = payload.get("deployment_uid")
    try:
        if op == "deploy_model":
            if not deployment_uid:
                raise RuntimeError("deploy_model missing deployment_uid")
            with _command_lock:
                if deployment_uid in _active_deployments:
                    log(f"deploy already in progress for {deployment_uid}; ignoring duplicate")
                    return
                _active_deployments.add(deployment_uid)
            try:
                deploy_model(cfg, payload, received_mono=received_mono)
            finally:
                with _command_lock:
                    _active_deployments.discard(deployment_uid)
        elif op == "stop_deployment":
            stop_deployment(cfg, payload)
        elif op == "restart_deployment":
            restart_deployment(cfg, payload, received_mono=received_mono)
        elif op == "stream_start":
            stream_start(cfg, payload)
        elif op == "stream_stop":
            stream_stop(cfg, payload)
        elif op == "status_container":
            subprocess.run(["docker", "ps", "-a"], check=False)
        elif op == "get_container_logs":
            subprocess.run(["docker", "logs", "--tail", "100", payload["container_name"]], check=False)
        else:
            log(f"unsupported operation: {op}")
    except Exception as e:
        log(f"command handler error: {e}")


def _command_already_applied(payload: Dict[str, Any]) -> bool:
    request_id = payload.get("request_id")
    uid = payload.get("deployment_uid")
    if not request_id or not uid:
        return False
    state = _load_json_if_exists(deployment_dir(uid) / "state.json") or {}
    if state.get("last_request_id") != request_id:
        return False
    op = payload.get("operation")
    cname = state.get("container_name")
    if op in {"deploy_model", "restart_deployment"}:
        return bool(cname) and _container_running_state(cname) is True
    if op == "stop_deployment":
        return state.get("desired") == "stopped"
    return False


def handle_command(cfg: Dict[str, Any], payload: Dict[str, Any]) -> None:
    received_mono = time.monotonic()
    op = payload.get("operation")
    request_id = payload.get("request_id", "?")
    if request_id and request_id != "?" and request_id in _seen_request_ids:
        log("duplicate command ignored: %s (request_id=%s)" % (op, request_id))
        return
    if request_id and request_id != "?":
        _seen_request_ids.add(request_id)
    if _command_already_applied(payload):
        log("command already applied: %s (request_id=%s)" % (op, request_id))
        return
    log(f"command received: {op} (request_id={request_id})")
    threading.Thread(
        target=_run_command,
        args=(cfg, payload, received_mono),
        daemon=True,
        name=f"icicle-cmd-{op or 'unknown'}",
    ).start()


def mqtt_loop(cfg: Dict[str, Any]) -> None:
    mcfg = cfg.get("mqtt", {})
    if not mcfg.get("enabled") or mqtt is None:
        log("MQTT disabled or paho-mqtt unavailable; HTTP heartbeat active only")
        while True:
            time.sleep(3600)
    base = mcfg.get("base_topic", "icicle/v1")
    topics: List[Tuple[str, int]] = []
    device_id = cfg.get("device_id")
    if device_id:
        topics.append((f"{base}/commands/device/{device_id}", 1))
    for group in cfg.get("user_groups") or []:
        topics.append((f"{base}/commands/device-group/{group}", 1))
    generation_uid = cfg.get("generation_uid")
    if generation_uid:
        topics.append((f"{base}/commands/generation/{generation_uid}", 1))
    if not topics:
        log("device_config.json has no device_id/generation_uid; HTTP heartbeat only")
        while True:
            time.sleep(3600)

    client_id = "icicle-agent-%s" % (device_id or "unknown")
    mqtt_kwargs = {"client_id": client_id, "clean_session": False}
    callback_api = getattr(mqtt, "CallbackAPIVersion", None)
    if callback_api is not None:
        mqtt_kwargs["callback_api_version"] = callback_api.VERSION1
    try:
        client = mqtt.Client(**mqtt_kwargs)
    except TypeError:
        mqtt_kwargs.pop("callback_api_version", None)
        client = mqtt.Client(**mqtt_kwargs)
    log("mqtt client_id=%s clean_session=false" % client_id)
    if mcfg.get("tls_enabled"):
        client.tls_set()
    if mcfg.get("username"):
        client.username_pw_set(mcfg["username"], mcfg.get("password") or "")

    def on_connect(c, userdata, flags, rc):
        if rc != 0:
            log("mqtt connect failed rc=%s" % rc)
            return
        log("mqtt connected; subscribing %s" % topics)
        for topic, qos in topics:
            c.subscribe(topic, qos)

    def on_message(c, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            handle_command(cfg, payload)
        except Exception as e:
            log(f"mqtt message handling failed: {e}")

    client.on_connect = on_connect
    client.on_message = on_message
    while True:
        try:
            client.connect(mcfg["host"], int(mcfg.get("port", 443)), 60)
            client.loop_forever()
        except Exception as e:
            log(f"mqtt loop error: {e}; retrying in 10s")
            time.sleep(10)


def main() -> None:
    ensure_dir_permissions(DEPLOYMENTS_DIR)
    ensure_dir_permissions(STATE_DIR)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    cfg = enroll_if_needed()
    _wait_for_docker()
    try:
        restore_desired_deployments(cfg, reason="boot")
    except Exception as e:
        log("boot recover failed: %s" % e)
    threading.Thread(target=heartbeat_loop, args=(cfg,), daemon=True).start()
    mqtt_loop(cfg)


if __name__ == "__main__":
    main()
