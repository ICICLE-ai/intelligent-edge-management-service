// =============================================================================
// ICICLE Edge Control Plane — front-end glue
// -----------------------------------------------------------------------------
// Vanilla JS, no build step. Handles:
//   - Sidebar accordion (open/close categories, remember in localStorage)
//   - Loading state on form submissions
//   - Toast notifications driven by ?notice=… in URL
//   - Repeater rows on the model card form (env, mounts, ports, compatibility)
//   - Auto-fill helpers (deployment target/model selectors)
// =============================================================================

(function () {
  "use strict";

  // ---------------------------------------------------------------------------
  // Sidebar accordion
  // ---------------------------------------------------------------------------
  function setupSidebar() {
    const KEY = "icicle.sidebar.open";
    let openSet;
    try {
      openSet = new Set(JSON.parse(localStorage.getItem(KEY) || "[]"));
    } catch (e) {
      openSet = new Set();
    }
    document.querySelectorAll(".nav-group").forEach((group) => {
      const id = group.dataset.group;
      const hasActive = group.querySelector(".nav-link.is-active");
      if (hasActive || openSet.has(id) || group.dataset.defaultOpen === "true") {
        group.classList.add("is-open");
      }
      const header = group.querySelector(".nav-group__header");
      header.addEventListener("click", () => {
        group.classList.toggle("is-open");
        const open = [];
        document.querySelectorAll(".nav-group.is-open").forEach((g) => open.push(g.dataset.group));
        try { localStorage.setItem(KEY, JSON.stringify(open)); } catch (e) {}
      });
    });
  }

  // ---------------------------------------------------------------------------
  // Form loading state
  // ---------------------------------------------------------------------------
  function setupFormLoading() {
    document.querySelectorAll("form[data-loading]").forEach((form) => {
      form.addEventListener("submit", () => {
        const btn = form.querySelector('button[type="submit"], button:not([type="button"])');
        if (!btn) return;
        btn.classList.add("is-loading");
        btn.dataset.originalText = btn.textContent;
        btn.disabled = true;
        if (btn.dataset.loadingText) {
          btn.textContent = btn.dataset.loadingText;
        }
      });
    });
  }

  // ---------------------------------------------------------------------------
  // Toasts from ?notice=...
  // ---------------------------------------------------------------------------
  function setupToasts() {
    const params = new URLSearchParams(window.location.search);
    const msg = params.get("notice");
    const level = params.get("level") || "success";
    if (!msg) return;
    const el = document.createElement("div");
    el.className = "toast toast--" + level;
    el.textContent = msg.replace(/_/g, " ");
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 4200);
  }

  // ---------------------------------------------------------------------------
  // Confirmation prompts
  // ---------------------------------------------------------------------------
  function setupConfirms() {
    document.querySelectorAll("form[data-confirm]").forEach((form) => {
      form.addEventListener("submit", (e) => {
        if (!confirm(form.dataset.confirm)) {
          e.preventDefault();
        }
      });
    });
  }

  // ---------------------------------------------------------------------------
  // Generic repeater (used by model card form)
  // ---------------------------------------------------------------------------
  function setupRepeaters() {
    document.querySelectorAll("[data-repeater]").forEach((root) => {
      const list = root.querySelector("[data-repeater-list]");
      const tmpl = root.querySelector("template[data-repeater-template]");
      const addBtn = root.querySelector("[data-repeater-add]");
      if (!list || !tmpl || !addBtn) return;
      addBtn.addEventListener("click", () => {
        const node = tmpl.content.cloneNode(true);
        list.appendChild(node);
      });
      list.addEventListener("click", (e) => {
        const rm = e.target.closest("[data-repeater-remove]");
        if (rm) {
          const row = rm.closest(".repeater__row");
          if (row) row.remove();
        }
      });
    });
  }

  // ---------------------------------------------------------------------------
  // Copy to clipboard helper (used for tokens, UIDs, live docker preview)
  // ---------------------------------------------------------------------------
  function flashToast(message, level) {
    const el = document.createElement("div");
    el.className = "toast toast--" + (level || "success");
    el.textContent = message;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 1600);
  }

  function setupCopy() {
    document.querySelectorAll("[data-copy]").forEach((el) => {
      el.style.cursor = "copy";
      el.addEventListener("click", () => {
        const text = el.dataset.copy === "self" ? el.textContent.trim() : el.dataset.copy;
        navigator.clipboard.writeText(text).then(() => flashToast("Copied to clipboard")).catch(() => {});
      });
    });
    document.querySelectorAll("[data-copy-target]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const sel = btn.dataset.copyTarget;
        const target = document.querySelector(sel);
        if (!target) return;
        const text = (target.textContent || "").trim();
        if (!text) return;
        navigator.clipboard.writeText(text).then(() => flashToast("Copied to clipboard")).catch(() => {});
      });
    });
  }

  // ---------------------------------------------------------------------------
  // Device tile checked-state mirror (so styles work without :has())
  // ---------------------------------------------------------------------------
  function setupDeviceTiles() {
    document.querySelectorAll(".device-tile").forEach((tile) => {
      const input = tile.querySelector('input[type="checkbox"]');
      if (!input) return;
      const sync = () => tile.classList.toggle("is-checked", input.checked);
      input.addEventListener("change", sync);
      sync();
    });
  }

  // ---------------------------------------------------------------------------
  // Deployment target selector (DEVICE/GROUP toggles)
  // ---------------------------------------------------------------------------
  function setupTargetSwitcher() {
    document.querySelectorAll("[data-target-switcher]").forEach((root) => {
      const radios = root.querySelectorAll('input[name="target_type"]');
      function refresh() {
        const v = root.querySelector('input[name="target_type"]:checked');
        root.querySelectorAll("[data-target-for]").forEach((el) => {
          el.style.display = el.dataset.targetFor === (v ? v.value : "") ? "" : "none";
        });
      }
      radios.forEach((r) => r.addEventListener("change", refresh));
      refresh();
    });
  }

  // ---------------------------------------------------------------------------
  // Source-type toggler on model card form
  // ---------------------------------------------------------------------------
  function setupArtifactSource() {
    const root = document.querySelector("[data-artifact-source]");
    if (!root) return;
    const radios = root.querySelectorAll('input[name="artifact_source_type"]');
    function refresh() {
      const v = root.querySelector('input[name="artifact_source_type"]:checked');
      root.querySelectorAll("[data-source-for]").forEach((el) => {
        el.style.display = el.dataset.sourceFor === (v ? v.value : "") ? "" : "none";
      });
    }
    radios.forEach((r) => r.addEventListener("change", refresh));
    refresh();
  }

  document.addEventListener("DOMContentLoaded", () => {
    setupSidebar();
    setupFormLoading();
    setupToasts();
    setupConfirms();
    setupRepeaters();
    setupCopy();
    setupTargetSwitcher();
    setupArtifactSource();
    setupDeviceTiles();
  });
})();
