// Scan station page: live scan list, previews, and per-scan actions.
(function () {
  "use strict";

  const POLL_MS = 4000;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function fmtDur(sec) {
    if (sec == null || !isFinite(sec)) return "–";
    sec = Math.max(0, Math.round(sec));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    if (h) return h + "h " + m + "m";
    if (m) return m + "m " + (s < 10 ? "0" : "") + s + "s";
    return s + "s";
  }

  function fmtTime(t) {
    if (!t) return "";
    return new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  function fmtBytes(n) {
    if (!n) return "0 MB";
    return n >= 1e9 ? (n / 1e9).toFixed(2) + " GB" : (n / 1e6).toFixed(1) + " MB";
  }

  function badgeFor(state) {
    if (state === "done") return "badge--green";
    if (state === "failed") return "badge--red";
    if (state === "cancelled") return "badge--grey";
    if (state === "queued" || state === "frames_ready") return "badge--yellow";
    return "badge--blue";
  }

  function stepBar(job, steps) {
    const failed = job.state === "failed" || job.state === "cancelled";
    return '<div class="scan-steps">' + steps.map((label, i) => {
      let cls = "";
      if (job.state === "done" || i < job.step) cls = "is-done";
      else if (i === job.step) cls = failed ? "is-failed" : "is-active";
      return '<div class="scan-steps__item ' + cls + '"><span></span>' + esc(label) + "</div>";
    }).join("") + "</div>";
  }

  function actionForm(dev, job, action, label, cls, confirmText) {
    return '<form method="post" action="/scans/' + esc(dev) + "/jobs/" + esc(job.id) + "/" + action + '"'
      + (confirmText ? ' data-scan-confirm="' + esc(confirmText) + '"' : "")
      + ' style="display:inline-block"><button class="btn btn--sm ' + cls + '">' + esc(label) + "</button></form>";
  }

  function cancelNote(state) {
    if (state === "queued") return "This scan hasn't been filmed yet. Cancelling removes it from the line.";
    if (["capturing", "extracting", "uploading_frames"].indexOf(state) !== -1)
      return "The capture station is filming this scan right now. It finishes its current step (about a minute) and may move the scan on anyway; cancel again if it does.";
    if (state === "frames_ready") return "The frames are saved. Cancelling keeps them, so you can rebuild later.";
    return "The GPU stops building the model within a few seconds. The frames are kept, so you can rebuild later.";
  }

  function bundleBlock(dev, job) {
    const b = job.bundle || {};
    const size = job.station_bundle_mb ? " (" + Math.round(job.station_bundle_mb) + " MB)" : "";
    if (b.state === "ready") {
      return '<a class="btn btn--sm btn--success" href="' + esc(job.download_url) + '">Download results' + esc(size) + "</a> "
        + actionForm(dev, job, "delete-bundle", "Remove portal copy", "btn--ghost", "Remove the portal copy of these results? The station keeps its copy.");
    }
    if (b.state === "requested") return '<span class="small text-soft">Asked the station for the results…</span>';
    if (b.state === "uploading") {
      const pct = b.total ? Math.floor(100 * (b.bytes || 0) / b.total) : 0;
      return '<span class="small text-soft">Receiving results: ' + fmtBytes(b.bytes) + " of " + fmtBytes(b.total) + " (" + pct + "%)</span>";
    }
    let out = actionForm(dev, job, "fetch", "Fetch full results" + size, "btn--outline", "");
    if (b.state === "failed") out += ' <span class="tiny" style="color:#b91c1c">Last fetch failed: ' + esc(b.error || "unknown error") + "</span>";
    return out;
  }

  function jobCard(dev, job, steps) {
    const now = Date.now() / 1000;
    const elapsed = (job.ended || now) - (job.created || now);
    let body = stepBar(job, steps);
    body += '<div class="row small text-soft" style="gap:18px;margin:10px 0">'
      + "<span>Started " + esc(fmtTime(job.created)) + "</span>"
      + "<span>" + (job.terminal ? "Took " : "Elapsed ") + fmtDur(elapsed) + "</span>"
      + "<span>" + esc(job.rotations || 1) + " rotation(s) · " + esc(Number(job.iterations).toLocaleString()) + " iterations</span>"
      + "</div>";
    if (job.progress != null) {
      const pct = Math.round(job.progress * 100);
      body += '<div class="scan-progress"><div style="width:' + pct + '%"></div></div>'
        + '<p class="tiny text-mute">' + esc(job.message || "Building the 3D model") + " · " + pct + "%</p>";
    } else if (job.message && !job.terminal) {
      body += '<p class="small text-soft">' + esc(job.message) + "</p>";
    }
    if (job.state === "failed" && (job.error || job.message)) {
      body += '<div class="notice notice--danger notice--inline small">' + esc(job.error || job.message) + "</div>";
    }
    if (job.previews.length) {
      body += '<div class="grid grid-2" style="margin-top:12px">' + job.previews.map((p) => (
        '<figure style="margin:0"><video controls muted loop playsinline preload="metadata" src="' + esc(p.url) + '" style="width:100%;border-radius:10px;background:#000"></video>'
        + '<figcaption class="tiny text-mute">' + esc(p.title) + "</figcaption></figure>"
      )).join("") + "</div>";
    } else if (job.state === "done") {
      body += '<p class="small text-soft">Preview videos are on their way from the station…</p>';
    }
    const actions = [];
    if (job.can_cancel) actions.push(actionForm(dev, job, "cancel", "Cancel scan", "btn--ghost", "Cancel \"" + job.object + "\"? " + cancelNote(job.state)));
    if (job.can_retry) actions.push(actionForm(dev, job, "retry", "Rebuild from saved frames", "btn--outline", ""));
    if (job.can_fetch) actions.push(bundleBlock(dev, job));
    if (actions.length) body += '<div class="row" style="margin-top:12px;gap:8px;align-items:center">' + actions.join("") + "</div>";

    const history = job.history.map((h) => (
      new Date(h.t * 1000).toLocaleTimeString() + "  " + (h.label || h.state) + (h.message ? "  " + h.message : "")
    ));
    if (job.log_tail) history.push("", "Last log lines from the GPU worker:", ...String(job.log_tail).split(" | ").map((l) => "  " + l));
    body += '<details style="margin-top:10px"><summary class="small">Activity</summary><pre class="tiny">' + esc(history.join("\n")) + "</pre></details>";

    return '<header class="card__head"><h2>' + esc(job.object) + '</h2>'
      + '<span class="badge ' + badgeFor(job.state) + '">' + esc(job.label) + "</span>"
      + '<span class="tiny text-mute mono" style="margin-left:auto">' + esc(job.id) + "</span></header>"
      + '<div class="card__body">' + body + "</div>";
  }

  function signature(job) {
    return JSON.stringify([job.state, job.updated, job.previews.length, job.bundle, job.progress]);
  }

  function render(root, data) {
    const dev = root.dataset.device;
    const steps = data.steps;
    const seen = new Set();
    let prev = null;
    if (!data.jobs.length) {
      root.innerHTML = '<div class="empty"><h3>No scans yet</h3><p>Start one above.</p></div>';
      return;
    }
    const empty = root.querySelector(".empty");
    if (empty) empty.remove();
    data.jobs.forEach((job) => {
      seen.add(job.id);
      let card = root.querySelector('[data-job="' + CSS.escape(job.id) + '"]');
      const sig = signature(job);
      if (!card) {
        card = document.createElement("section");
        card.className = "card section";
        card.id = "job-" + job.id;
        card.dataset.job = job.id;
      }
      if (card.dataset.sig !== sig || !job.terminal) {
        const open = card.querySelector("details[open]") != null;
        const playing = Array.from(card.querySelectorAll("video")).some((v) => !v.paused);
        if (!(playing && card.dataset.sig === sig)) {
          card.innerHTML = jobCard(dev, job, steps);
          if (open) card.querySelector("details").open = true;
        }
        card.dataset.sig = sig;
      }
      if (prev) prev.after(card); else root.prepend(card);
      prev = card;
    });
    root.querySelectorAll("[data-job]").forEach((el) => { if (!seen.has(el.dataset.job)) el.remove(); });
  }

  function renderPending(el, pending) {
    if (!pending.length) { el.innerHTML = ""; return; }
    el.innerHTML = pending.map((p) => (
      '<div class="notice ' + (p.failed ? "notice--danger" : "notice--info") + '" style="margin-bottom:10px">'
      + "<b>" + esc(p.object) + "</b>: " + esc(p.label) + "</div>"
    )).join("");
  }

  function renderService(svc) {
    const badge = document.querySelector("[data-scan-service-badge]");
    const detail = document.querySelector("[data-scan-service-detail]");
    if (badge) {
      badge.textContent = svc.headline;
      badge.className = "badge " + (svc.ready ? "badge--green" : "badge--amber");
    }
    if (detail) detail.textContent = svc.detail;
    updateSubmit(svc.ready);
  }

  let serviceReady = false;
  function updateSubmit(ready) {
    if (ready != null) serviceReady = ready;
    const form = document.querySelector("[data-scan-submit]");
    if (!form) return;
    const btn = form.querySelector("[data-scan-submit-btn]");
    const hint = form.querySelector("[data-scan-submit-hint]");
    const checks = form.querySelectorAll("[data-scan-check]");
    const allChecked = Array.from(checks).every((c) => c.checked);
    const named = form.querySelector("#object_name").value.trim() !== "";
    btn.disabled = !(serviceReady && allChecked && named);
    hint.textContent = !serviceReady ? "The scan service is not ready."
      : !named ? "Enter an object name."
      : !allChecked ? "Tick every checklist item." : "";
    const iters = parseInt(form.querySelector("[data-scan-iterations]").value || "30000", 10) || 30000;
    const recon = 150 + 1260 * iters / 30000;
    form.querySelector("[data-scan-estimate]").textContent =
      "Estimated time: about " + fmtDur(150 + recon + 60) + " (filming under a minute, model building about " + fmtDur(recon) + ").";
  }

  function init() {
    const root = document.querySelector("[data-scan-list]");
    if (!root) return;
    const pendingEl = document.querySelector("[data-scan-pending]");
    const initial = JSON.parse(document.getElementById("scan-initial").textContent);
    render(root, initial);
    renderPending(pendingEl, initial.pending);
    serviceReady = initial.service.ready;

    const form = document.querySelector("[data-scan-submit]");
    if (form) form.addEventListener("input", () => updateSubmit());
    updateSubmit();

    document.addEventListener("submit", (e) => {
      const f = e.target.closest("form[data-scan-confirm]");
      if (f && !window.confirm(f.dataset.scanConfirm)) e.preventDefault();
    });

    async function poll() {
      try {
        const res = await fetch(root.dataset.url, { headers: { Accept: "application/json" } });
        if (res.ok) {
          const data = await res.json();
          render(root, data);
          renderPending(pendingEl, data.pending);
          renderService(data.service);
        }
      } catch (err) { /* keep polling */ }
      setTimeout(poll, POLL_MS);
    }
    setTimeout(poll, POLL_MS);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
