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
* Acks back to the portal over HTTPs (`/api/agent/ack`) so the UI can show
  real per-device delivery status.

This agent has no third-party dependency beyond ``requests`` and (optionally)
``paho-mqtt``. It runs as a systemd unit.

Targets Python 3.6+ so one bundle works on JetPack 4 (Xavier) and JetPack 6 (Orin).
"""

import json
import os
import pwd
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

try:
    import paho.mqtt.client as mqtt
except Exception:  # pragma: no cover
    mqtt = None


AGENT_VERSION = "edge-agent-v2.0.1"


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


def post_json(url: str, payload: Dict[str, Any], *, timeout: int = 15, retries: int = 3,
              api_key: Optional[str] = None) -> bool:
    """POST JSON to the portal; return True when the server accepts the payload."""
    headers = _portal_headers()
    if api_key:
        headers["Authorization"] = "Bearer %s" % api_key
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
            if resp.status_code < 400:
                return True
            log(f"post {url} rejected ({resp.status_code}): {resp.text[:300]}")
        except Exception as e:
            log(f"post {url} failed (attempt {attempt}/{retries}): {e}")
        if attempt < retries:
            time.sleep(min(2 * attempt, 6))
    return False


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
                "timestamp": time.time(),
            }
            post_json(url, payload, api_key=api_key)
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
             container_name: Optional[str] = None, error: Optional[str] = None) -> None:
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


def ensure_image(image: str, pull_policy: str) -> None:
    inspect = subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    have = inspect.returncode == 0
    if pull_policy == "never":
        if not have:
            raise RuntimeError(f"Docker image not present locally: {image}")
        return
    if pull_policy == "always" or not have:
        log(f"pulling image {image} (this can take several minutes on Jetson)")
        result = _subprocess_run(["docker", "pull", image], capture_output=True, text=True)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "docker pull failed").strip()
            raise RuntimeError(f"docker pull failed: {detail[:500]}")
        log(f"image pull complete: {image}")


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


def _run_container(payload: Dict[str, Any], dpath: Path, artifact_path: Path,
                   *, image: str, cname: str) -> str:
    prepare_host_display_for_camera()
    subprocess.run(["docker", "rm", "-f", cname],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cmd, _ = build_run_command(payload, dpath, artifact_path)
    log("running: " + " ".join(cmd))
    result = _subprocess_run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "docker run failed").strip()
        raise RuntimeError(detail[:500])
    return result.stdout.strip()


def _start_container_from_payload(cfg: Dict[str, Any], payload: Dict[str, Any], *,
                                  request_id: str, operation: str,
                                  skip_download: bool = False) -> None:
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
        else:
            url = resolve_artifact_url(cfg, artifact)
            send_ack(cfg, request_id=request_id, deployment_uid=deployment_uid,
                     operation=operation, status="DOWNLOADING")
            download_file(
                url,
                artifact_path,
                expected_size=artifact.get("size_bytes"),
                filename=artifact["filename"],
            )
            log(f"artifact ready: {artifact_path} ({artifact_path.stat().st_size} bytes)")

        validate_bind_mounts(payload, dpath, artifact_path)

        send_ack(cfg, request_id=request_id, deployment_uid=deployment_uid,
                 operation=operation, status="PULLING")
        pull_policy = container.get("pull_policy", "if_not_present")
        if skip_download and operation == "restart_deployment":
            pull_policy = "never"
        ensure_image(image, pull_policy)

        send_ack(cfg, request_id=request_id, deployment_uid=deployment_uid,
                 operation=operation, status="STARTING",
                 container_name=cname)

        container_id = _run_container(payload, dpath, artifact_path, image=image, cname=cname)
        log(f"container started: {cname} ({container_id[:12]})")
        save_json(dpath / "state.json", {
            "deployment_uid": deployment_uid,
            "container_name": cname,
            "container_id": container_id,
            "image": image,
            "artifact_path": str(artifact_path),
            "started_at": time.time(),
        })
        send_ack(cfg, request_id=request_id, deployment_uid=deployment_uid,
                 operation=operation, status="RUNNING",
                 container_id=container_id, container_name=cname)
    except Exception as e:
        log(f"{operation} failed: {e}")
        send_ack(cfg, request_id=request_id, deployment_uid=deployment_uid,
                 operation=operation, status="FAILED",
                 container_name=cname, error=str(e))


def deploy_model(cfg: Dict[str, Any], payload: Dict[str, Any]) -> None:
    _start_container_from_payload(
        cfg,
        payload,
        request_id=payload["request_id"],
        operation="deploy_model",
        skip_download=False,
    )


def restart_deployment(cfg: Dict[str, Any], payload: Dict[str, Any]) -> None:
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


def _run_command(cfg: Dict[str, Any], payload: Dict[str, Any]) -> None:
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
                deploy_model(cfg, payload)
            finally:
                with _command_lock:
                    _active_deployments.discard(deployment_uid)
        elif op == "stop_deployment":
            stop_deployment(cfg, payload)
        elif op == "restart_deployment":
            restart_deployment(cfg, payload)
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


def handle_command(cfg: Dict[str, Any], payload: Dict[str, Any]) -> None:
    op = payload.get("operation")
    request_id = payload.get("request_id", "?")
    log(f"command received: {op} (request_id={request_id})")
    threading.Thread(
        target=_run_command,
        args=(cfg, payload),
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

    client = mqtt.Client()
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
    threading.Thread(target=heartbeat_loop, args=(cfg,), daemon=True).start()
    mqtt_loop(cfg)


if __name__ == "__main__":
    main()
