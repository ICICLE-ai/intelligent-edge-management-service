"""Docker `run` command parser & serializer.

The researcher pastes a raw `docker run …` command. This module turns that
text into the same structured shape the model-card form/repository already
speak (container spec + env + mounts + ports + docker_args), and can render
the structure back into a canonical command string.

The model mount is special: when we know the model file's *container-side*
path, we locate the mount whose target is a prefix of it and rewrite the
host side to the literal string ``${DEPLOYMENT_DIR}/model`` — the constant
the agent substitutes at deploy time. This is what lets the agent always
download the model into the same well-known directory on the device while
preserving the inside-container path the researcher's image expects.

Implementation notes
--------------------
* Tokenisation is shell-aware: handles single & double quotes, backslash
  escapes, and ``\\<newline>`` line continuations (as you'd type by hand).
* Unknown flags are preserved verbatim under ``docker_args`` so nothing is
  silently dropped.
* The serializer mirrors :func:`parse` deterministically, so the live
  preview stays stable as fields are edited.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

MODEL_HOST_TEMPLATE = "${DEPLOYMENT_DIR}/model"
"""The token the agent substitutes with the per-deployment host path."""

# Flags that don't take a value.
_BOOL_FLAGS = {
    "-d", "--detach",
    "-i", "--interactive",
    "-t", "--tty",
    "--rm",
    "--privileged",
    "--init",
    "--read-only",
    "--no-healthcheck",
    "--oom-kill-disable",
    "-P", "--publish-all",
    "--disable-content-trust",
    "--sig-proxy",
    "--help",
    "--quiet", "-q",
}

# Flags whose value maps directly onto a single spec field.
_VALUE_FLAGS_FIELD: Dict[str, str] = {
    "--name": "container_name",
    "--network": "network_mode",
    "--net": "network_mode",
    "--ipc": "ipc_mode",
    "--restart": "restart_policy",
    "--runtime": "runtime",
    "--gpus": "gpus",
    "--shm-size": "shm_size",
    "-w": "working_dir",
    "--workdir": "working_dir",
    "--entrypoint": "entrypoint",
    "--pull": "pull_policy",
}

_PULL_POLICY_MAP = {
    "always": "always",
    "missing": "if_not_present",
    "never": "never",
}


@dataclass
class ParsedCommand:
    spec: Dict[str, Any] = field(default_factory=dict)
    env: List[Dict[str, Any]] = field(default_factory=list)
    mounts: List[Dict[str, Any]] = field(default_factory=list)
    ports: List[Dict[str, Any]] = field(default_factory=list)
    docker_args: List[str] = field(default_factory=list)
    command_override: List[str] = field(default_factory=list)
    raw: str = ""
    warnings: List[str] = field(default_factory=list)

    def as_form_dict(self) -> Dict[str, Any]:
        """Return the same shape that ``model_service.build_payload_from_form``
        already consumes (so the route, validator, and repository remain
        unchanged)."""
        spec = self.spec.copy()
        spec["env"] = self.env
        spec["mounts"] = self.mounts
        spec["ports"] = self.ports
        spec["docker_args"] = self.docker_args
        if self.command_override:
            spec["command"] = self.command_override
        return spec


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse(command: str,
          *,
          model_container_path: Optional[str] = None,
          model_env_var: Optional[str] = None) -> ParsedCommand:
    """Parse a ``docker run`` command into structured fields.

    Args:
        command: The raw command text (may span multiple lines with ``\\``
            continuations).
        model_container_path: The path of the model file *inside the
            container*. Used to identify which `-v/--mount` is the model
            mount so we can rewrite its host side to ``${DEPLOYMENT_DIR}/model``.
            If omitted, falls back to the value of the env var named by
            ``model_env_var`` when present.
        model_env_var: The name of the env var whose value points at the
            model file (e.g. ``ENGINE_PATH``). Used to auto-derive
            ``model_container_path`` if missing.
    """
    out = ParsedCommand(raw=command)
    tokens = _tokenize(command)
    if not tokens:
        return out

    # Strip optional "docker" / "run" prefix.
    if tokens and tokens[0] == "docker":
        tokens = tokens[1:]
    if tokens and tokens[0] == "run":
        tokens = tokens[1:]

    i = 0
    n = len(tokens)
    while i < n:
        t = tokens[i]
        if not t.startswith("-"):
            break  # everything from here on is image + cmd

        # --flag=value form
        flag, val = (t.split("=", 1) + [None])[:2] if (t.startswith("--") and "=" in t) else (t, None)

        if flag in _BOOL_FLAGS:
            _apply_bool(flag, out)
            i += 1
            continue

        # value flag — value is next token unless we already have it from --flag=value
        if val is None:
            i += 1
            if i >= n:
                out.warnings.append(f"Flag {flag} is missing its value.")
                break
            val = tokens[i]

        if flag in ("-e", "--env"):
            key, _, v = val.partition("=")
            if key:
                out.env.append({"key": key, "value": v, "is_secret": False})
        elif flag in ("-v", "--volume"):
            mount = _parse_volume_spec(val)
            if mount:
                out.mounts.append(mount)
        elif flag == "--mount":
            mount = _parse_mount_kv(val)
            if mount:
                out.mounts.append(mount)
        elif flag in ("-p", "--publish"):
            port = _parse_port_spec(val)
            if port:
                out.ports.append(port)
        elif flag in _VALUE_FLAGS_FIELD:
            field_name = _VALUE_FLAGS_FIELD[flag]
            if field_name == "pull_policy":
                out.spec["pull_policy"] = _PULL_POLICY_MAP.get(val, "if_not_present")
            elif field_name == "entrypoint":
                out.spec["entrypoint"] = [val]
            else:
                out.spec[field_name] = val
        else:
            out.docker_args.extend([flag, val])

        i += 1

    # Image
    if i < n:
        image = tokens[i]
        i += 1
        registry, repo, tag, digest = _parse_image_ref(image)
        if registry:
            out.spec["image_registry"] = registry
        out.spec["image_repository"] = repo
        out.spec["image_tag"] = tag or "latest"
        if digest:
            out.spec["image_digest"] = digest

    # Anything left is the command override.
    if i < n:
        out.command_override = tokens[i:]

    # Sensible defaults.
    out.spec.setdefault("pull_policy", "if_not_present")
    out.spec.setdefault("restart_policy", "no")
    if out.spec.get("container_name") is None:
        # Derive from image repo so it's not empty.
        repo = out.spec.get("image_repository") or "container"
        out.spec["container_name"] = repo.split("/")[-1].replace(":", "_") + "_infer"

    # Resolve model-related details and rewrite the model mount.
    if not model_container_path and model_env_var:
        for e in out.env:
            if e["key"] == model_env_var and e.get("value"):
                model_container_path = e["value"]
                break

    if model_container_path:
        out.spec["model_env_var"] = model_env_var or out.spec.get("model_env_var")
        _rewrite_model_mount(out, model_container_path)

    return out


def render(parsed: ParsedCommand, *, line_continuations: bool = True) -> str:
    """Re-render a parsed command into a clean, deterministic shell string."""
    return render_from_spec(parsed.as_form_dict(), line_continuations=line_continuations)


def render_from_spec(spec: Dict[str, Any], *, line_continuations: bool = True) -> str:
    """Render a spec dict (same shape as in the form payload) into a docker
    run command. The output's flag order matches what :func:`parse` writes,
    so the round-trip is stable for the UI."""
    parts: List[List[str]] = []
    push = parts.append

    push(["docker", "run", "-d"])

    if spec.get("container_name"):
        push(["--name", spec["container_name"]])
    if spec.get("runtime"):
        push(["--runtime", spec["runtime"]])
    if spec.get("gpus"):
        push(["--gpus", spec["gpus"]])
    if spec.get("network_mode"):
        push(["--network", spec["network_mode"]])
    if spec.get("ipc_mode"):
        push(["--ipc", spec["ipc_mode"]])
    if spec.get("shm_size"):
        push(["--shm-size", spec["shm_size"]])
    if spec.get("working_dir"):
        push(["-w", spec["working_dir"]])
    if spec.get("privileged"):
        push(["--privileged"])
    if spec.get("remove_after_exit"):
        push(["--rm"])
    if spec.get("restart_policy") and spec["restart_policy"] != "no":
        push(["--restart", spec["restart_policy"]])
    if spec.get("pull_policy") and spec["pull_policy"] != "if_not_present":
        # Docker's CLI uses "missing" for our "if_not_present".
        cli = {"always": "always", "if_not_present": "missing", "never": "never"}.get(spec["pull_policy"])
        if cli:
            push(["--pull", cli])
    if spec.get("entrypoint"):
        ep = spec["entrypoint"]
        ep_val = ep if isinstance(ep, str) else (ep[0] if ep else "")
        if ep_val:
            push(["--entrypoint", ep_val])

    for e in spec.get("env") or []:
        key = e.get("key") or e.get("var_key")
        value = e.get("value") if "value" in e else e.get("var_value", "")
        if key:
            push(["-e", f"{key}={value}"])

    for m in spec.get("mounts") or []:
        src = m.get("source")
        tgt = m.get("target")
        if not src or not tgt:
            continue
        style = m.get("style") or m.get("mount_style") or "volume"
        mtype = m.get("type") or m.get("mount_type") or "bind"
        mode = m.get("mode") or ""
        if style == "mount":
            parts_kv = [f"type={mtype}", f"source={src}", f"target={tgt}"]
            if mode == "ro":
                parts_kv.append("readonly")
            push(["--mount", ",".join(parts_kv)])
        else:
            vol = f"{src}:{tgt}"
            if mode:
                vol += f":{mode}"
            push(["-v", vol])

    for p in spec.get("ports") or []:
        cport = p.get("container_port")
        if not cport:
            continue
        host = p.get("host_port")
        proto = p.get("protocol") or "tcp"
        if host:
            spec_str = f"{host}:{cport}"
        else:
            spec_str = f"{cport}"
        if proto and proto != "tcp":
            spec_str += f"/{proto}"
        push(["-p", spec_str])

    # Extra args (free-form). They were stored as alternating tokens.
    extra = spec.get("docker_args") or []
    # Render extras as their own line group so they remain visible.
    if extra:
        push(list(extra))

    image = _image_ref(spec)
    if image:
        push([image])

    cmd_override = spec.get("command")
    if cmd_override:
        if isinstance(cmd_override, list):
            push([str(x) for x in cmd_override])
        else:
            push([str(cmd_override)])

    return _format_parts(parts, line_continuations=line_continuations)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _apply_bool(flag: str, out: ParsedCommand) -> None:
    if flag == "--rm":
        out.spec["remove_after_exit"] = True
    elif flag == "--privileged":
        out.spec["privileged"] = True
    elif flag in ("-d", "--detach", "-i", "--interactive", "-t", "--tty"):
        # No-ops in our model: the agent always runs detached and never wires
        # the TTY back. We don't store these.
        return
    else:
        out.docker_args.append(flag)


def _tokenize(command: str) -> List[str]:
    if not command or not command.strip():
        return []
    # Collapse "\<newline>" continuations into spaces so shlex treats them as
    # whitespace.
    collapsed = command.replace("\\\n", " ")
    try:
        return shlex.split(collapsed, comments=False, posix=True)
    except ValueError:
        # Recover from an unterminated quote by trying non-posix mode.
        return shlex.split(collapsed, comments=False, posix=False)


def _parse_volume_spec(spec: str) -> Optional[Dict[str, Any]]:
    if not spec:
        return None
    parts = spec.split(":")
    if len(parts) == 1:
        # Anonymous volume — ignore for our purposes.
        return None
    if len(parts) == 2:
        src, tgt = parts
        mode = None
    else:
        src = parts[0]
        tgt = parts[1]
        mode = parts[2] if parts[2] in ("ro", "rw") else None
    return {
        "source": src,
        "target": tgt,
        "style": "volume",
        "type": "bind",
        "mode": mode,
    }


def _parse_mount_kv(spec: str) -> Optional[Dict[str, Any]]:
    if not spec:
        return None
    kv: Dict[str, str] = {}
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            kv[k.strip()] = v.strip()
        else:
            # bare flag (e.g. "readonly", "ro")
            kv[chunk] = "true"
    src = kv.get("source") or kv.get("src")
    tgt = kv.get("target") or kv.get("destination") or kv.get("dst")
    if not src or not tgt:
        return None
    mode = None
    if kv.get("readonly") == "true" or kv.get("ro") == "true":
        mode = "ro"
    return {
        "source": src,
        "target": tgt,
        "style": "mount",
        "type": kv.get("type") or "bind",
        "mode": mode,
    }


def _parse_port_spec(spec: str) -> Optional[Dict[str, Any]]:
    if not spec:
        return None
    proto = "tcp"
    if "/" in spec:
        spec, proto = spec.split("/", 1)
    if ":" in spec:
        host, cont = spec.rsplit(":", 1)
    else:
        host, cont = None, spec
    try:
        cont_n = int(cont)
    except ValueError:
        return None
    try:
        host_n = int(host) if host else None
    except ValueError:
        host_n = None
    return {"host_port": host_n, "container_port": cont_n, "protocol": proto}


def _parse_image_ref(image: str) -> Tuple[Optional[str], str, Optional[str], Optional[str]]:
    """Return (registry, repository, tag, digest)."""
    registry: Optional[str] = None
    digest: Optional[str] = None
    if "@" in image:
        image, digest = image.split("@", 1)
    if "/" in image:
        first, rest = image.split("/", 1)
        if "." in first or ":" in first or first == "localhost":
            registry = first
            image = rest
    tag: Optional[str] = None
    if ":" in image:
        image, tag = image.rsplit(":", 1)
    return registry, image, tag, digest


def _image_ref(spec: Dict[str, Any]) -> Optional[str]:
    repo = spec.get("image_repository")
    if not repo:
        return None
    base = f"{spec['image_registry']}/{repo}" if spec.get("image_registry") else repo
    tag = spec.get("image_tag") or "latest"
    digest = spec.get("image_digest")
    if digest:
        return f"{base}@{digest}"
    return f"{base}:{tag}"


def _rewrite_model_mount(out: ParsedCommand, model_container_path: str) -> None:
    """Find the mount whose target contains the model file and rewrite its
    host side to ``${DEPLOYMENT_DIR}/model``. Also normalise the mode to ``ro``
    if it wasn't set, since the agent's downloaded artifact is read-only by
    convention.
    """
    if not model_container_path or not out.mounts:
        return
    best: Optional[int] = None
    best_len = -1
    for idx, m in enumerate(out.mounts):
        tgt = (m.get("target") or "").rstrip("/")
        if not tgt:
            continue
        # Match if the model path lives inside this mount target.
        if model_container_path == tgt or model_container_path.startswith(tgt + "/"):
            if len(tgt) > best_len:
                best_len = len(tgt)
                best = idx
    if best is None:
        # No mount yet — synthesise one based on the directory of the model file.
        target_dir = model_container_path.rsplit("/", 1)[0] or "/models"
        out.mounts.append({
            "source": MODEL_HOST_TEMPLATE,
            "target": target_dir,
            "style": "volume",
            "type": "bind",
            "mode": "ro",
            "is_model_mount": True,
        })
        out.warnings.append(
            f"No mount in the command pointed at {model_container_path}; "
            f"added a read-only model mount at {target_dir}."
        )
        return
    out.mounts[best]["source"] = MODEL_HOST_TEMPLATE
    if not out.mounts[best].get("mode"):
        out.mounts[best]["mode"] = "ro"
    out.mounts[best]["is_model_mount"] = True


def _format_parts(parts: List[List[str]], *, line_continuations: bool) -> str:
    rendered: List[str] = []
    for group in parts:
        rendered.append(" ".join(_quote(p) for p in group))
    if line_continuations and len(rendered) > 1:
        # Put one logical clause per line for readability.
        return " \\\n  ".join(rendered)
    return " ".join(rendered)


def _quote(token: str) -> str:
    if token == "":
        return "''"
    if any(c.isspace() for c in token) or any(c in token for c in "\"'`$\\"):
        return shlex.quote(token)
    return token
