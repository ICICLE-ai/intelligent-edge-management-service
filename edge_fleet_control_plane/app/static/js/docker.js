// =============================================================================
// ICICLE Edge — docker run command parser & form sync (researcher UX)
// -----------------------------------------------------------------------------
// Mirrors the Python parser in app/services/docker_parser.py so the preview
// updates instantly without a network round trip. The two implementations
// stay in lockstep on what they recognise; behavior differences are documented
// inline.
//
// Wiring:
//   <textarea data-docker-command>
//     paste docker run …
//   </textarea>
//   <pre data-docker-preview></pre>
//   <input  data-model-container-path>
//   <input  name="spec_model_env_var">
//
// The script:
//   * Listens for paste/input on the raw textarea -> parses -> populates the
//     advanced fields (image, env, mounts, ports, docker_args, etc.).
//   * Listens for any change in the structured form fields -> re-renders the
//     preview pre-block from the current form state.
//   * Identifies the "model mount" (the volume whose target contains the
//     model file inside the container) and rewrites its host side to
//     ${DEPLOYMENT_DIR}/model so the agent's designated download path is
//     always used at deploy time.
// =============================================================================

(function () {
  "use strict";

  const MODEL_HOST_TEMPLATE = "${DEPLOYMENT_DIR}/model";

  const BOOL_FLAGS = new Set([
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
    "-q", "--quiet",
  ]);

  const VALUE_FLAGS_FIELD = {
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
  };

  const PULL_POLICY_MAP = {
    always: "always",
    missing: "if_not_present",
    never: "never",
  };
  const PULL_POLICY_REVERSE = {
    always: "always",
    if_not_present: "missing",
    never: "never",
  };

  // ---------------------------------------------------------------------------
  // Tokeniser — shell-aware, handles single/double quotes and \<newline>
  // continuations. Not a full shell but covers the docker-run subset.
  // ---------------------------------------------------------------------------
  function tokenize(text) {
    if (!text) return [];
    const tokens = [];
    let buf = "";
    let inSingle = false;
    let inDouble = false;
    let i = 0;
    let hasChars = false;
    const flush = () => {
      if (hasChars) tokens.push(buf);
      buf = "";
      hasChars = false;
    };
    while (i < text.length) {
      const c = text[i];
      if (!inSingle && !inDouble && c === "\\") {
        const next = text[i + 1];
        if (next === "\n") { i += 2; continue; }   // line continuation
        if (next === undefined) { i += 1; continue; }
        buf += next;
        hasChars = true;
        i += 2;
        continue;
      }
      if (inDouble && c === "\\") {
        const next = text[i + 1];
        if (next === "\n") { i += 2; continue; }
        if (next === "$" || next === '"' || next === "\\" || next === "`") {
          buf += next;
          hasChars = true;
          i += 2;
          continue;
        }
        buf += "\\";
        hasChars = true;
        i += 1;
        continue;
      }
      if (!inDouble && c === "'") {
        inSingle = !inSingle;
        hasChars = true;
        i += 1;
        continue;
      }
      if (!inSingle && c === '"') {
        inDouble = !inDouble;
        hasChars = true;
        i += 1;
        continue;
      }
      if (!inSingle && !inDouble && /\s/.test(c)) {
        flush();
        i += 1;
        continue;
      }
      buf += c;
      hasChars = true;
      i += 1;
    }
    flush();
    return tokens;
  }

  // ---------------------------------------------------------------------------
  // Mount/port helpers
  // ---------------------------------------------------------------------------
  function parseVolumeSpec(s) {
    if (!s) return null;
    const parts = s.split(":");
    if (parts.length < 2) return null;
    const [src, tgt, mode] = parts;
    return {
      source: src,
      target: tgt,
      style: "volume",
      type: "bind",
      mode: mode === "ro" || mode === "rw" ? mode : null,
    };
  }

  function parseMountKV(s) {
    if (!s) return null;
    const kv = {};
    s.split(",").forEach((chunk) => {
      chunk = chunk.trim();
      if (!chunk) return;
      const eq = chunk.indexOf("=");
      if (eq >= 0) kv[chunk.slice(0, eq).trim()] = chunk.slice(eq + 1).trim();
      else kv[chunk] = "true";
    });
    const src = kv.source || kv.src;
    const tgt = kv.target || kv.destination || kv.dst;
    if (!src || !tgt) return null;
    const mode = kv.readonly === "true" || kv.ro === "true" ? "ro" : null;
    return {
      source: src,
      target: tgt,
      style: "mount",
      type: kv.type || "bind",
      mode: mode,
    };
  }

  function parsePortSpec(s) {
    if (!s) return null;
    let proto = "tcp";
    if (s.indexOf("/") >= 0) {
      const i = s.indexOf("/");
      proto = s.slice(i + 1) || "tcp";
      s = s.slice(0, i);
    }
    let host = null;
    let cont = s;
    if (s.indexOf(":") >= 0) {
      const idx = s.lastIndexOf(":");
      host = s.slice(0, idx);
      cont = s.slice(idx + 1);
    }
    const cn = parseInt(cont, 10);
    if (!Number.isFinite(cn)) return null;
    const hn = host == null ? null : parseInt(host, 10);
    return {
      host_port: Number.isFinite(hn) ? hn : null,
      container_port: cn,
      protocol: proto,
    };
  }

  function parseImageRef(image) {
    let registry = null;
    let digest = null;
    if (image.indexOf("@") >= 0) {
      const at = image.indexOf("@");
      digest = image.slice(at + 1);
      image = image.slice(0, at);
    }
    if (image.indexOf("/") >= 0) {
      const slash = image.indexOf("/");
      const first = image.slice(0, slash);
      const rest = image.slice(slash + 1);
      if (first.indexOf(".") >= 0 || first.indexOf(":") >= 0 || first === "localhost") {
        registry = first;
        image = rest;
      }
    }
    let tag = null;
    if (image.indexOf(":") >= 0) {
      const idx = image.lastIndexOf(":");
      tag = image.slice(idx + 1);
      image = image.slice(0, idx);
    }
    return { registry: registry, repository: image, tag: tag, digest: digest };
  }

  // ---------------------------------------------------------------------------
  // Parse
  // ---------------------------------------------------------------------------
  function parse(command, opts) {
    opts = opts || {};
    const out = {
      spec: {},
      env: [],
      mounts: [],
      ports: [],
      docker_args: [],
      command_override: [],
      warnings: [],
    };
    const tokens = tokenize(command);
    let i = 0;
    if (tokens[i] === "docker") i++;
    if (tokens[i] === "run") i++;

    while (i < tokens.length) {
      const t = tokens[i];
      if (!t.startsWith("-")) break;

      let flag = t;
      let val = null;
      if (t.startsWith("--") && t.indexOf("=") >= 0) {
        const eq = t.indexOf("=");
        flag = t.slice(0, eq);
        val = t.slice(eq + 1);
      }

      if (BOOL_FLAGS.has(flag)) {
        applyBool(flag, out);
        i++;
        continue;
      }

      if (val == null) {
        i++;
        if (i >= tokens.length) {
          out.warnings.push("Flag " + flag + " is missing its value.");
          break;
        }
        val = tokens[i];
      }

      if (flag === "-e" || flag === "--env") {
        const eqIdx = val.indexOf("=");
        const key = eqIdx >= 0 ? val.slice(0, eqIdx) : val;
        const value = eqIdx >= 0 ? val.slice(eqIdx + 1) : "";
        if (key) out.env.push({ key: key, value: value, is_secret: false });
      } else if (flag === "-v" || flag === "--volume") {
        const m = parseVolumeSpec(val);
        if (m) out.mounts.push(m);
      } else if (flag === "--mount") {
        const m = parseMountKV(val);
        if (m) out.mounts.push(m);
      } else if (flag === "-p" || flag === "--publish") {
        const p = parsePortSpec(val);
        if (p) out.ports.push(p);
      } else if (flag in VALUE_FLAGS_FIELD) {
        const field = VALUE_FLAGS_FIELD[flag];
        if (field === "pull_policy") {
          out.spec.pull_policy = PULL_POLICY_MAP[val] || "if_not_present";
        } else if (field === "entrypoint") {
          out.spec.entrypoint = [val];
        } else {
          out.spec[field] = val;
        }
      } else {
        out.docker_args.push(flag);
        out.docker_args.push(val);
      }
      i++;
    }

    if (i < tokens.length) {
      const image = tokens[i++];
      const ref = parseImageRef(image);
      if (ref.registry) out.spec.image_registry = ref.registry;
      out.spec.image_repository = ref.repository;
      out.spec.image_tag = ref.tag || "latest";
      if (ref.digest) out.spec.image_digest = ref.digest;
    }
    if (i < tokens.length) out.command_override = tokens.slice(i);

    out.spec.pull_policy = out.spec.pull_policy || "if_not_present";
    out.spec.restart_policy = out.spec.restart_policy || "no";
    if (!out.spec.container_name) {
      const repo = out.spec.image_repository || "container";
      out.spec.container_name = repo.split("/").pop().replace(":", "_") + "_infer";
    }

    // Resolve model details
    let modelContainerPath = opts.model_container_path || "";
    const modelEnvVar = opts.model_env_var || "";
    if (!modelContainerPath && modelEnvVar) {
      const hit = out.env.find((e) => e.key === modelEnvVar && e.value);
      if (hit) modelContainerPath = hit.value;
    }
    if (modelEnvVar) out.spec.model_env_var = modelEnvVar;
    if (modelContainerPath) rewriteModelMount(out, modelContainerPath);

    return out;
  }

  function applyBool(flag, out) {
    if (flag === "--rm") out.spec.remove_after_exit = true;
    else if (flag === "--privileged") out.spec.privileged = true;
    else if (["-d", "--detach", "-i", "--interactive", "-t", "--tty"].indexOf(flag) >= 0) return;
    else out.docker_args.push(flag);
  }

  function rewriteModelMount(out, modelContainerPath) {
    let bestIdx = -1;
    let bestLen = -1;
    out.mounts.forEach((m, idx) => {
      const tgt = (m.target || "").replace(/\/+$/, "");
      if (!tgt) return;
      if (modelContainerPath === tgt || modelContainerPath.startsWith(tgt + "/")) {
        if (tgt.length > bestLen) {
          bestLen = tgt.length;
          bestIdx = idx;
        }
      }
    });
    if (bestIdx < 0) {
      const lastSlash = modelContainerPath.lastIndexOf("/");
      const targetDir = lastSlash > 0 ? modelContainerPath.slice(0, lastSlash) : "/models";
      out.mounts.push({
        source: MODEL_HOST_TEMPLATE,
        target: targetDir,
        style: "volume",
        type: "bind",
        mode: "ro",
        is_model_mount: true,
      });
      out.warnings.push(
        "No mount in the command pointed at " + modelContainerPath +
          "; added a read-only model mount at " + targetDir + "."
      );
      return;
    }
    out.mounts[bestIdx].source = MODEL_HOST_TEMPLATE;
    if (!out.mounts[bestIdx].mode) out.mounts[bestIdx].mode = "ro";
    out.mounts[bestIdx].is_model_mount = true;
  }

  // ---------------------------------------------------------------------------
  // Render: structured spec -> deterministic command string
  // ---------------------------------------------------------------------------
  function render(spec, opts) {
    opts = opts || { line_continuations: true };
    const groups = [];
    groups.push(["docker", "run", "-d"]);
    if (spec.container_name) groups.push(["--name", spec.container_name]);
    if (spec.runtime) groups.push(["--runtime", spec.runtime]);
    if (spec.gpus) groups.push(["--gpus", spec.gpus]);
    if (spec.network_mode) groups.push(["--network", spec.network_mode]);
    if (spec.ipc_mode) groups.push(["--ipc", spec.ipc_mode]);
    if (spec.shm_size) groups.push(["--shm-size", spec.shm_size]);
    if (spec.working_dir) groups.push(["-w", spec.working_dir]);
    if (spec.privileged) groups.push(["--privileged"]);
    if (spec.remove_after_exit) groups.push(["--rm"]);
    if (spec.restart_policy && spec.restart_policy !== "no") groups.push(["--restart", spec.restart_policy]);
    if (spec.pull_policy && spec.pull_policy !== "if_not_present") {
      const cli = PULL_POLICY_REVERSE[spec.pull_policy];
      if (cli) groups.push(["--pull", cli]);
    }
    if (spec.entrypoint) {
      const ep = Array.isArray(spec.entrypoint) ? spec.entrypoint[0] : spec.entrypoint;
      if (ep) groups.push(["--entrypoint", ep]);
    }
    (spec.env || []).forEach((e) => {
      const key = e.key || e.var_key;
      const value = e.value != null ? e.value : e.var_value || "";
      if (key) groups.push(["-e", key + "=" + value]);
    });
    (spec.mounts || []).forEach((m) => {
      const src = m.source;
      const tgt = m.target;
      if (!src || !tgt) return;
      const style = m.style || m.mount_style || "volume";
      const mtype = m.type || m.mount_type || "bind";
      const mode = m.mode || "";
      if (style === "mount") {
        const kv = ["type=" + mtype, "source=" + src, "target=" + tgt];
        if (mode === "ro") kv.push("readonly");
        groups.push(["--mount", kv.join(",")]);
      } else {
        let v = src + ":" + tgt;
        if (mode) v += ":" + mode;
        groups.push(["-v", v]);
      }
    });
    (spec.ports || []).forEach((p) => {
      const cport = p.container_port;
      if (!cport) return;
      let str = p.host_port ? p.host_port + ":" + cport : "" + cport;
      const proto = p.protocol || "tcp";
      if (proto && proto !== "tcp") str += "/" + proto;
      groups.push(["-p", str]);
    });
    const extras = spec.docker_args || [];
    if (extras.length) groups.push(extras.slice());
    const image = imageRef(spec);
    if (image) groups.push([image]);
    if (spec.command && spec.command.length) {
      groups.push(spec.command.map(String));
    }
    return groups
      .map((g) => g.map(quote).join(" "))
      .join(opts.line_continuations ? " \\\n  " : " ");
  }

  function imageRef(spec) {
    if (!spec.image_repository) return null;
    const base = spec.image_registry ? spec.image_registry + "/" + spec.image_repository : spec.image_repository;
    if (spec.image_digest) return base + "@" + spec.image_digest;
    return base + ":" + (spec.image_tag || "latest");
  }

  function quote(token) {
    if (token === "" || token == null) return "''";
    const s = String(token);
    if (/^[A-Za-z0-9_@%+=:,./\-${}]+$/.test(s)) return s;
    return "'" + s.replace(/'/g, "'\\''") + "'";
  }

  // ---------------------------------------------------------------------------
  // Form ↔ spec marshalling
  // ---------------------------------------------------------------------------
  function $form() {
    return document.querySelector("form[data-model-form]");
  }

  function setFieldValue(name, value) {
    const form = $form();
    if (!form) return;
    const el = form.querySelector(`[name="${name}"]`);
    if (!el) return;
    if (el.type === "checkbox") el.checked = !!value;
    else el.value = value == null ? "" : value;
  }

  function getFieldValue(name) {
    const form = $form();
    if (!form) return "";
    const el = form.querySelector(`[name="${name}"]`);
    if (!el) return "";
    if (el.type === "checkbox") return el.checked ? "on" : "";
    return el.value;
  }

  function repeaterRoot(name) {
    return document.querySelector(`[data-repeater][data-repeater-name="${name}"]`);
  }

  function clearRepeater(root) {
    if (!root) return;
    const list = root.querySelector("[data-repeater-list]");
    if (list) list.innerHTML = "";
  }

  function addRepeaterRow(root) {
    if (!root) return null;
    const list = root.querySelector("[data-repeater-list]");
    const tmpl = root.querySelector("template[data-repeater-template]");
    if (!list || !tmpl) return null;
    const frag = tmpl.content.cloneNode(true);
    list.appendChild(frag);
    return list.lastElementChild;
  }

  function setRowField(row, name, value) {
    const el = row.querySelector(`[name="${name}"]`);
    if (!el) return;
    if (el.type === "checkbox") el.checked = !!value;
    else el.value = value == null ? "" : value;
  }

  function populateForm(parsed) {
    const spec = parsed.spec || {};
    setFieldValue("spec_image_registry", spec.image_registry || "");
    setFieldValue("spec_image_repository", spec.image_repository || "");
    setFieldValue("spec_image_tag", spec.image_tag || "latest");
    setFieldValue("spec_image_digest", spec.image_digest || "");
    setFieldValue("spec_container_name", spec.container_name || "");
    setFieldValue("spec_runtime", spec.runtime || "");
    setFieldValue("spec_gpus", spec.gpus || "");
    setFieldValue("spec_network_mode", spec.network_mode || "");
    setFieldValue("spec_ipc_mode", spec.ipc_mode || "");
    setFieldValue("spec_shm_size", spec.shm_size || "");
    setFieldValue("spec_working_dir", spec.working_dir || "");
    setFieldValue("spec_pull_policy", spec.pull_policy || "if_not_present");
    setFieldValue("spec_restart_policy", spec.restart_policy || "no");
    setFieldValue("spec_privileged", !!spec.privileged);
    setFieldValue("spec_remove_after_exit", !!spec.remove_after_exit);

    // Env
    const envRoot = repeaterRoot("env");
    if (envRoot) {
      clearRepeater(envRoot);
      (parsed.env || []).forEach((e) => {
        const row = addRepeaterRow(envRoot);
        if (!row) return;
        setRowField(row, "env_key", e.key);
        setRowField(row, "env_value", e.value);
        setRowField(row, "env_is_secret", e.is_secret);
      });
    }

    // Mounts
    const mountRoot = repeaterRoot("mounts");
    if (mountRoot) {
      clearRepeater(mountRoot);
      (parsed.mounts || []).forEach((m) => {
        const row = addRepeaterRow(mountRoot);
        if (!row) return;
        setRowField(row, "mount_source", m.source);
        setRowField(row, "mount_target", m.target);
        setRowField(row, "mount_style", m.style || "volume");
        setRowField(row, "mount_type", m.type || "bind");
        setRowField(row, "mount_mode", m.mode || "");
        if (m.is_model_mount) row.classList.add("is-model-mount");
      });
    }

    // Ports
    const portRoot = repeaterRoot("ports");
    if (portRoot) {
      clearRepeater(portRoot);
      (parsed.ports || []).forEach((p) => {
        const row = addRepeaterRow(portRoot);
        if (!row) return;
        setRowField(row, "port_host", p.host_port == null ? "" : p.host_port);
        setRowField(row, "port_container", p.container_port);
        setRowField(row, "port_protocol", p.protocol || "tcp");
      });
    }

    // Docker args
    const argsRoot = repeaterRoot("docker_args");
    if (argsRoot) {
      clearRepeater(argsRoot);
      (parsed.docker_args || []).forEach((a) => {
        const row = addRepeaterRow(argsRoot);
        if (!row) return;
        setRowField(row, "docker_arg", a);
      });
    }

    // Model env var (always reflect the explicit field if user set one)
    if (spec.model_env_var) setFieldValue("spec_model_env_var", spec.model_env_var);
  }

  function gatherSpec() {
    const form = $form();
    if (!form) return {};
    const spec = {
      image_registry: getFieldValue("spec_image_registry"),
      image_repository: getFieldValue("spec_image_repository"),
      image_tag: getFieldValue("spec_image_tag") || "latest",
      image_digest: getFieldValue("spec_image_digest"),
      container_name: getFieldValue("spec_container_name"),
      runtime: getFieldValue("spec_runtime"),
      gpus: getFieldValue("spec_gpus"),
      network_mode: getFieldValue("spec_network_mode"),
      ipc_mode: getFieldValue("spec_ipc_mode"),
      shm_size: getFieldValue("spec_shm_size"),
      working_dir: getFieldValue("spec_working_dir"),
      pull_policy: getFieldValue("spec_pull_policy") || "if_not_present",
      restart_policy: getFieldValue("spec_restart_policy") || "no",
      privileged: !!getFieldValue("spec_privileged"),
      remove_after_exit: !!getFieldValue("spec_remove_after_exit"),
    };

    spec.env = collectRepeater("env", (row) => ({
      key: row.querySelector('[name="env_key"]')?.value || "",
      value: row.querySelector('[name="env_value"]')?.value || "",
    })).filter((e) => e.key);

    spec.mounts = collectRepeater("mounts", (row) => ({
      source: row.querySelector('[name="mount_source"]')?.value || "",
      target: row.querySelector('[name="mount_target"]')?.value || "",
      style: row.querySelector('[name="mount_style"]')?.value || "volume",
      type: row.querySelector('[name="mount_type"]')?.value || "bind",
      mode: row.querySelector('[name="mount_mode"]')?.value || "",
    })).filter((m) => m.source && m.target);

    spec.ports = collectRepeater("ports", (row) => ({
      host_port: parseInt(row.querySelector('[name="port_host"]')?.value || "", 10) || null,
      container_port: parseInt(row.querySelector('[name="port_container"]')?.value || "", 10) || 0,
      protocol: row.querySelector('[name="port_protocol"]')?.value || "tcp",
    })).filter((p) => p.container_port);

    spec.docker_args = collectRepeater("docker_args", (row) => row.querySelector('[name="docker_arg"]')?.value || "")
      .filter(Boolean);

    return spec;
  }

  function collectRepeater(name, mapper) {
    const root = repeaterRoot(name);
    if (!root) return [];
    return Array.from(root.querySelectorAll(".repeater__row")).map(mapper);
  }

  // ---------------------------------------------------------------------------
  // Live sync
  // ---------------------------------------------------------------------------
  function init() {
    const form = $form();
    if (!form) return;
    const textarea = form.querySelector("[data-docker-command]");
    const preview = form.querySelector("[data-docker-preview]");
    const modelPathInput = form.querySelector("[data-model-container-path]");
    const modelEnvInput = form.querySelector('[name="spec_model_env_var"]');
    const warningsBox = form.querySelector("[data-docker-warnings]");

    function modelContext() {
      return {
        model_container_path: modelPathInput ? modelPathInput.value.trim() : "",
        model_env_var: modelEnvInput ? modelEnvInput.value.trim() : "",
      };
    }

    function showWarnings(list) {
      if (!warningsBox) return;
      warningsBox.innerHTML = "";
      if (!list || !list.length) {
        warningsBox.hidden = true;
        return;
      }
      warningsBox.hidden = false;
      list.forEach((w) => {
        const li = document.createElement("li");
        li.textContent = w;
        warningsBox.appendChild(li);
      });
    }

    function refreshPreview() {
      if (!preview) return;
      const spec = gatherSpec();
      try {
        preview.textContent = render(spec);
      } catch (e) {
        preview.textContent = "(unable to render — " + e.message + ")";
      }
    }

    function parseAndPopulate() {
      if (!textarea) return;
      const cmd = textarea.value;
      if (!cmd || !cmd.trim()) {
        showWarnings([]);
        refreshPreview();
        return;
      }
      const parsed = parse(cmd, modelContext());
      // Sync the artifact fields based on the model-path input, if filled.
      const mc = modelContext();
      if (mc.model_container_path) {
        const cp = mc.model_container_path;
        const slash = cp.lastIndexOf("/");
        const filename = slash >= 0 ? cp.slice(slash + 1) : cp;
        if (filename) setFieldValue("artifact_filename", filename);
        setFieldValue("artifact_container_path", cp);
      }
      populateForm(parsed);
      showWarnings(parsed.warnings);
      refreshPreview();
    }

    if (textarea) {
      ["input", "change", "paste"].forEach((evt) => {
        textarea.addEventListener(evt, () => {
          // Debounce slightly so paste handlers don't double-parse.
          clearTimeout(textarea._t);
          textarea._t = setTimeout(parseAndPopulate, 30);
        });
      });
    }
    if (modelPathInput) modelPathInput.addEventListener("input", parseAndPopulate);
    if (modelEnvInput) modelEnvInput.addEventListener("input", () => {
      const v = modelEnvInput.value.trim();
      // If a matching env row exists, also reflect the model path.
      if (modelPathInput && !modelPathInput.value && v) {
        const envRoot = repeaterRoot("env");
        if (envRoot) {
          const match = Array.from(envRoot.querySelectorAll(".repeater__row"))
            .find((r) => (r.querySelector('[name="env_key"]')?.value || "") === v);
          if (match) modelPathInput.value = match.querySelector('[name="env_value"]')?.value || "";
        }
      }
      parseAndPopulate();
    });

    form.addEventListener("input", (e) => {
      if (e.target === textarea) return;          // textarea already handled
      if (e.target === modelPathInput) return;
      if (e.target === modelEnvInput) return;
      refreshPreview();
    });
    form.addEventListener("change", (e) => {
      if (e.target === textarea) return;
      refreshPreview();
    });

    // First render reflects the server-side defaults / saved values.
    refreshPreview();
  }

  // Expose for ad-hoc debugging in devtools.
  window.IcicleDocker = { parse: parse, render: render, gatherSpec: gatherSpec };

  document.addEventListener("DOMContentLoaded", init);
})();
