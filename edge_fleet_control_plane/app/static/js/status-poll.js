// Live status polling — keeps deployment/device pages fresh without manual reload.
(function () {
  "use strict";

  const DEPLOYMENT_TERMINAL = new Set(["FAILED", "CANCELLED"]);
  const REDISPATCHABLE = new Set(["PENDING", "RECORDED", "DELIVERING", "MQTT_FAILED", "MQTT_SENT"]);
  const STOPPABLE = new Set(["RUNNING", "DELIVERING", "STARTING", "DOWNLOADING", "PULLING", "STOPPING"]);
  const RESTARTABLE = new Set(["STOPPED"]);
  const DISMISSABLE = new Set(["STOPPING", "STARTING", "DELIVERING", "DOWNLOADING", "PULLING", "PENDING", "RECORDED", "MQTT_FAILED", "MQTT_SENT"]);

  function badgeClass(value) {
    if (!value) return "badge--neutral";
    const v = String(value).toUpperCase();
    const GREEN = new Set(["ONLINE", "RUNNING", "ACK", "SUCCEEDED", "PUBLISHED", "DELIVERED", "ACTIVE"]);
    const YELLOW = new Set(["PENDING", "DELIVERING", "RECORDED", "MQTT_SENT", "STOPPING", "INSTALLER_READY", "REGISTERED", "ENROLLED", "DRAFT", "DOWNLOADING", "PULLING", "STARTING"]);
    const RED = new Set(["OFFLINE", "FAILED", "ERROR", "MQTT_FAILED", "DEPRECATED", "CANCELLED"]);
    const GREY = new Set(["STOPPED", "REGISTERED_NOT_INSTALLED", "INACTIVE"]);
    if (GREEN.has(v)) return "badge--green";
    if (YELLOW.has(v)) return "badge--yellow";
    if (RED.has(v)) return "badge--red";
    if (GREY.has(v)) return "badge--grey";
    return "badge--neutral";
  }

  function setBadge(el, status) {
    if (!el || !status) return;
    el.textContent = status;
    el.className = "badge " + badgeClass(status);
    el.dataset.status = status;
  }

  function relativeTime(iso) {
    if (!iso) return "—";
    const then = Date.parse(iso);
    if (Number.isNaN(then)) return iso;
    const sec = Math.max(0, Math.floor((Date.now() - then) / 1000));
    if (sec < 60) return sec + "s ago";
    if (sec < 3600) return Math.floor(sec / 60) + "m ago";
    if (sec < 86400) return Math.floor(sec / 3600) + "h ago";
    return Math.floor(sec / 86400) + "d ago";
  }

  async function fetchJson(url) {
    const resp = await fetch(url, {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    });
    if (!resp.ok) throw new Error("poll failed " + resp.status);
    return resp.json();
  }

  function isDeploymentTerminal(status) {
    return DEPLOYMENT_TERMINAL.has(String(status || "").toUpperCase());
  }

  /** Parent row can lag behind settled device rows. */
  function effectiveDeploymentStatus(data) {
    const raw = String(data.status || "").toUpperCase();
    const devices = data.devices || [];
    if (!devices.length) return raw;
    const statuses = devices.map((d) => String(d.status || "").toUpperCase());
    const allStopped = statuses.every((st) => st === "STOPPED" || st === "FAILED");
    const allRunning = statuses.every((st) => st === "RUNNING");
    if (raw === "STOPPING" && allStopped) return "STOPPED";
    if (["STARTING", "DELIVERING", "DOWNLOADING", "PULLING"].includes(raw) && allRunning) {
      return "RUNNING";
    }
    if (["STARTING", "DELIVERING", "DOWNLOADING", "PULLING"].includes(raw) && allStopped) {
      return statuses.every((st) => st === "FAILED") ? "FAILED" : "STOPPED";
    }
    return raw;
  }

  function setActionVisible(el, visible) {
    if (!el) return;
    if (visible) {
      el.removeAttribute("hidden");
    } else {
      el.setAttribute("hidden", "");
    }
  }

  function updateDeploymentActions(root, status) {
    const s = String(status || "").toUpperCase();
    const actions = root.querySelector("[data-poll-actions]");
    const scope = actions || root;
    const retry = scope.querySelector('[data-poll-action="retry"]');
    const stop = scope.querySelector('[data-poll-action="stop"]');
    const restart = scope.querySelector('[data-poll-action="restart"]');
    const dismiss = scope.querySelector('[data-poll-action="dismiss"]');
    const del = scope.querySelector('[data-poll-action="delete"]');
    setActionVisible(retry, REDISPATCHABLE.has(s));
    setActionVisible(stop, STOPPABLE.has(s));
    setActionVisible(restart, RESTARTABLE.has(s));
    setActionVisible(dismiss, DISMISSABLE.has(s));
    setActionVisible(del, s !== "CANCELLED");
  }

  function updateDeploymentRowActions(tr, status) {
    if (!tr) return;
    const s = String(status || "").toUpperCase();
    const actions = tr.querySelector("[data-poll-deployment-actions]");
    const scope = actions || tr;
    const stop = scope.querySelector('[data-poll-action="stop"]');
    const restart = scope.querySelector('[data-poll-action="restart"]');
    setActionVisible(stop, STOPPABLE.has(s));
    setActionVisible(restart, RESTARTABLE.has(s));
  }

  function pollDeploymentDetail(root) {
    const url = root.dataset.pollUrl;
    const interval = parseInt(root.dataset.pollInterval || "2000", 10);
    let timer = null;
    const statusBadge = root.querySelector("[data-poll-deployment-status]");

    async function tick() {
      if (document.hidden) return;
      try {
        const data = await fetchJson(url);
        const status = effectiveDeploymentStatus(data);
        setBadge(statusBadge, status);
        updateDeploymentActions(root, status);

        (data.devices || []).forEach((row) => {
          const tr = root.querySelector('[data-poll-device-row="' + row.device_uid + '"]');
          if (!tr) return;
          setBadge(tr.querySelector("[data-poll-device-status]"), row.status);
          const container = tr.querySelector("[data-poll-device-container]");
          if (container) {
            const id = row.container_id ? " (" + row.container_id.slice(0, 12) + ")" : "";
            container.textContent = (row.container_name || "—") + id;
          }
          const err = tr.querySelector("[data-poll-device-error]");
          if (err) err.textContent = row.error_message || "";
          const updated = tr.querySelector("[data-poll-device-updated]");
          if (updated) updated.textContent = relativeTime(row.updated_at);
        });

        const streamRoot = document.getElementById("inference-streams");
        if (streamRoot && window.IcicleHls && window.IcicleHls.syncInferenceTiles) {
          window.IcicleHls.syncInferenceTiles(streamRoot, data.devices || [], {
            waitingMessage: "Waiting for inference stream…",
            connectingMessage: "Connecting…",
          });
        }

        (data.commands || []).forEach((cmd) => {
          const tr = root.querySelector('[data-poll-command-row="' + cmd.request_id + '"]');
          if (!tr) return;
          setBadge(tr.querySelector("[data-poll-command-status]"), cmd.status);
          const acked = tr.querySelector("[data-poll-command-acked]");
          if (acked) acked.textContent = cmd.acked_at ? relativeTime(cmd.acked_at) : "—";
        });

        if (isDeploymentTerminal(status)) {
          clearInterval(timer);
          root.classList.remove("is-polling");
        }
      } catch (e) {
        /* keep polling — transient network errors are normal */
      }
    }

    root.classList.add("is-polling");
    if (statusBadge) {
      updateDeploymentActions(root, statusBadge.dataset.status || statusBadge.textContent);
    }
    tick();
    timer = setInterval(tick, interval);
    if (new URLSearchParams(window.location.search).has("notice")) {
      const burst = setInterval(tick, 500);
      setTimeout(() => clearInterval(burst), 15000);
    }
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) tick();
    });
  }

  function updateSetupChecklist(readiness) {
    const root = document.querySelector("[data-setup-checklist]");
    if (!root || !readiness || !readiness.steps) return;

    readiness.steps.forEach((step) => {
      const item = root.querySelector('[data-setup-step="' + step.id + '"]');
      if (!item) return;
      item.classList.toggle("is-done", !!step.done);
      item.classList.toggle("is-pending", !step.done);
      const icon = item.querySelector(".setup-checklist__icon");
      if (icon) icon.textContent = step.done ? "✓" : "○";
      const detail = item.querySelector('[data-setup-step-detail="' + step.id + '"]');
      if (detail) detail.textContent = step.detail || "";
    });

    if (readiness.complete) {
      root.classList.add("setup-checklist-card--complete");
    }
  }

  function pollDeviceDetail(root) {
    const url = root.dataset.pollUrl;
    const activityUrl = root.dataset.pollActivityUrl;
    const interval = parseInt(root.dataset.pollInterval || "4000", 10);

    async function tick() {
      if (document.hidden) return;
      try {
        const data = await fetchJson(url);
        setBadge(root.querySelector("[data-poll-device-status]"), data.status);
        const lastSeen = root.querySelector("[data-poll-last-seen]");
        if (lastSeen) lastSeen.textContent = relativeTime(data.last_heartbeat_at);
      } catch (e) {
        /* ignore */
      }
      if (!activityUrl) return;
      try {
        const activity = await fetchJson(activityUrl);
        setBadge(root.querySelector("[data-poll-device-status]"), activity.status);
        const lastSeen = root.querySelector("[data-poll-last-seen]");
        if (lastSeen) lastSeen.textContent = relativeTime(activity.last_heartbeat_at);
        (activity.deployments || []).forEach((row) => {
          const tr = document.querySelector('[data-poll-deployment-row="' + row.deployment_uid + '"]');
          if (!tr) return;
          const status = row.status;
          setBadge(tr.querySelector("[data-poll-list-status]"), status);
          updateDeploymentRowActions(tr, status);
        });
        (activity.commands || []).forEach((cmd) => {
          const tr = document.querySelector('[data-poll-command-row="' + cmd.request_id + '"]');
          if (!tr) return;
          setBadge(tr.querySelector("[data-poll-command-status]"), cmd.status);
        });
        const streamRoot = document.getElementById("inference-streams");
        if (streamRoot && window.IcicleHls && window.IcicleHls.syncInferenceTiles && activity.inference_devices) {
          window.IcicleHls.syncInferenceTiles(streamRoot, activity.inference_devices, {
            waitingMessage: "Waiting for inference stream…",
            connectingMessage: "Connecting…",
          });
        }
        if (activity.setup_readiness) {
          updateSetupChecklist(activity.setup_readiness);
        }
        if (activity.enrollment) {
          const panel = document.querySelector("[data-setup-checklist]");
          if (panel) {
            const dl = panel.querySelector('[data-setup-step-detail="installer-time"]');
            if (dl && activity.enrollment.installer_downloaded_at) {
              dl.textContent = activity.enrollment.installer_downloaded_at;
            }
            const en = panel.querySelector('[data-setup-step-detail="enrolled-time"]');
            if (en && activity.enrollment.used_at) {
              en.textContent = activity.enrollment.used_at;
            }
          }
        }
      } catch (e) {
        /* ignore */
      }
    }

    root.classList.add("is-polling");
    document.querySelectorAll("[data-poll-deployment-row]").forEach((tr) => {
      const badge = tr.querySelector("[data-poll-list-status]");
      if (badge) updateDeploymentRowActions(tr, badge.dataset.status || badge.textContent);
    });
    tick();
    setInterval(tick, interval);
  }

  function pollGroupDetail(root) {
    const url = root.dataset.pollUrl;
    const interval = parseInt(root.dataset.pollInterval || "4000", 10);

    async function tick() {
      if (document.hidden) return;
      try {
        const data = await fetchJson(url);
        (data.devices || []).forEach((row) => {
          const tr = root.querySelector('[data-poll-list-row="' + row.device_uid + '"]');
          if (!tr) return;
          setBadge(tr.querySelector("[data-poll-list-status]"), row.status);
          const seen = tr.querySelector("[data-poll-list-seen]");
          if (seen) seen.textContent = relativeTime(row.last_heartbeat_at);
        });
        (data.deployments || []).forEach((row) => {
          const tr = root.querySelector('[data-poll-deployment-row="' + row.deployment_uid + '"]');
          if (!tr) return;
          setBadge(tr.querySelector("[data-poll-list-status]"), row.status);
        });
        const streamRoot = document.getElementById("inference-streams");
        if (streamRoot && window.IcicleHls && window.IcicleHls.syncInferenceTiles && data.inference_devices) {
          window.IcicleHls.syncInferenceTiles(streamRoot, data.inference_devices, {
            waitingMessage: "Waiting for inference stream…",
            connectingMessage: "Connecting…",
          });
        }
      } catch (e) {
        /* ignore */
      }
    }

    root.classList.add("is-polling");
    tick();
    setInterval(tick, interval);
  }

  function pollCommandsList(root) {
    const url = root.dataset.pollUrl || "/api/operations/commands";
    const interval = parseInt(root.dataset.pollInterval || "4000", 10);

    async function tick() {
      if (document.hidden) return;
      try {
        const rows = await fetchJson(url);
        rows.forEach((cmd) => {
          const tr = root.querySelector('[data-poll-command-row="' + cmd.request_id + '"]');
          if (!tr) return;
          setBadge(tr.querySelector("[data-poll-command-status]"), cmd.status);
        });
      } catch (e) {
        /* ignore */
      }
    }

    root.classList.add("is-polling");
    tick();
    setInterval(tick, interval);
  }

  function pollDevicesList(root) {
    const url = root.dataset.pollUrl || "/api/devices";
    const interval = parseInt(root.dataset.pollInterval || "5000", 10);

    async function tick() {
      if (document.hidden) return;
      try {
        const rows = await fetchJson(url);
        rows.forEach((d) => {
          const tr = root.querySelector('[data-poll-list-row="' + d.device_uid + '"]');
          if (!tr) return;
          setBadge(tr.querySelector("[data-poll-list-status]"), d.status);
          const seen = tr.querySelector("[data-poll-list-seen]");
          if (seen) seen.textContent = relativeTime(d.last_heartbeat_at);
        });
      } catch (e) {
        /* ignore */
      }
    }

    tick();
    setInterval(tick, interval);
  }

  function pollDeploymentsList(root) {
    const url = root.dataset.pollUrl;
    const interval = parseInt(root.dataset.pollInterval || "4000", 10);

    async function tick() {
      if (document.hidden) return;
      try {
        const rows = await fetchJson(url);
        rows.forEach((d) => {
          const tr = root.querySelector('[data-poll-list-row="' + d.deployment_uid + '"]');
          if (!tr) return;
          setBadge(tr.querySelector("[data-poll-list-status]"), d.status);
        });
      } catch (e) {
        /* ignore */
      }
    }

    tick();
    setInterval(tick, interval);
  }

  document.addEventListener("DOMContentLoaded", () => {
    const pollRoots = document.querySelectorAll(
      "[data-poll-deployment-detail], [data-poll-device-detail], [data-poll-devices-list], [data-poll-deployments-list], [data-poll-group-detail], [data-poll-commands-list]"
    );
    if (pollRoots.length) document.body.classList.add("has-live-poll");

    document.querySelectorAll("[data-poll-deployment-detail]").forEach(pollDeploymentDetail);
    document.querySelectorAll("[data-poll-device-detail]").forEach(pollDeviceDetail);
    document.querySelectorAll("[data-poll-devices-list]").forEach(pollDevicesList);
    document.querySelectorAll("[data-poll-deployments-list]").forEach(pollDeploymentsList);
    document.querySelectorAll("[data-poll-group-detail]").forEach(pollGroupDetail);
    document.querySelectorAll("[data-poll-commands-list]").forEach(pollCommandsList);
  });
})();
