// Multi-step device setup wizard — generation → cameras → identity → review.
(function () {
  "use strict";

  const TOTAL_STEPS = 4;

  function indicesForCount(n) {
    return Array.from({ length: Math.max(1, n) }, (_, i) => i).join(",");
  }

  function syncTileChecks(root) {
    root.querySelectorAll(".device-tile").forEach((tile) => {
      const input = tile.querySelector('input[type="radio"]');
      if (!input) return;
      tile.classList.toggle("is-checked", input.checked);
    });
  }

  function updateCameraFields(root) {
    const bus = root.querySelector("[data-setup-camera-bus], select[name='camera_bus']");
    const count = root.querySelector("[data-setup-camera-count], input[name='camera_count']");
    const indices = root.querySelector("[data-setup-camera-indices], input[name='camera_indices']");
    const indicesWrap = root.querySelector("[data-setup-indices-wrap]");
    const mountHint = root.querySelector("[data-setup-mount-hint]");
    if (!bus || !count) return;

    const isMvs = bus.value === "gige-mvs";
    const isCsi = bus.value === "csi";
    const n = parseInt(count.value || "1", 10) || 1;
    const showIndices = isMvs || (isCsi && n > 1);

    if (indicesWrap) {
      indicesWrap.hidden = !showIndices;
    }
    if (mountHint) mountHint.hidden = !isMvs;

    const label = root.querySelector("[data-setup-indices-label]");
    const hint = root.querySelector("[data-setup-indices-hint]");
    if (label) {
      label.textContent = isMvs
        ? "MVS camera indices"
        : "CSI sensor indices";
    }
    if (hint) {
      hint.textContent = isMvs
        ? "Comma-separated indices into the MVS device list. Must match camera count."
        : "Comma-separated nvarguscamerasrc sensor-id values (usually 0, 1, …). Must match camera count.";
    }

    if (indices) {
      indices.disabled = !showIndices;
      if (!showIndices) indices.removeAttribute("name");
      else indices.setAttribute("name", "camera_indices");
    }

    if (indices && showIndices) {
      const current = (indices.value || "").trim();
      const auto = indicesForCount(n);
      if (!current || /^[\d,\s]+$/.test(current)) {
        const parts = current.split(",").map((x) => x.trim()).filter(Boolean);
        if (!parts.length || parts.length !== n) {
          indices.value = auto;
        }
      }
    }
  }

  function updateReview(root) {
    const review = root.querySelector("[data-setup-review]");
    if (!review) return;

    const genInput = root.querySelector('input[name="generation_uid"]:checked');
    const genTile = genInput ? genInput.closest(".device-tile") : null;
    const genName = genTile ? genTile.querySelector(".device-tile__name") : null;

    const bus = root.querySelector("[data-setup-camera-bus]");
    const count = root.querySelector("[data-setup-camera-count]");
    const indices = root.querySelector("[data-setup-camera-indices]");
    const name = root.querySelector("#device_name");
    const group = root.querySelector("#group_uid");
    const site = root.querySelector("#site_name");

    const busLabel = bus ? bus.options[bus.selectedIndex].text : "—";
    let camText = busLabel + " · " + (count ? count.value : "1") + " camera(s)";
    if (bus && (bus.value === "gige-mvs" || bus.value === "csi") && indices && indices.value) {
      camText += " · indices " + indices.value;
    }

    const set = (key, val) => {
      const el = review.querySelector('[data-review="' + key + '"]');
      if (el) el.textContent = val || "—";
    };

    set("generation", genName ? genName.textContent.trim() : "—");
    set("cameras", camText);
    set("name", name ? name.value.trim() : "—");
    set("group", group && group.value ? group.options[group.selectedIndex].text : "No group");
    set("site", site ? site.value.trim() || "—" : "—");
  }

  function setStep(root, step) {
    const n = Math.max(1, Math.min(TOTAL_STEPS, step));
    root.dataset.currentStep = String(n);

    root.querySelectorAll("[data-step-panel]").forEach((panel) => {
      const panelStep = parseInt(panel.dataset.stepPanel, 10);
      panel.hidden = panelStep !== n;
      panel.classList.toggle("is-active", panelStep === n);
    });

    root.querySelectorAll("[data-step-nav]").forEach((nav) => {
      const navStep = parseInt(nav.dataset.stepNav, 10);
      nav.classList.toggle("is-active", navStep === n);
      nav.classList.toggle("is-complete", navStep < n);
    });

    if (n === TOTAL_STEPS) updateReview(root);
    if (n === 2) updateCameraFields(root);
  }

  function validateStep(root, step) {
    const panel = root.querySelector('[data-step-panel="' + step + '"]');
    if (!panel) return true;

    if (step === 1) {
      const checked = panel.querySelector('input[name="generation_uid"]:checked');
      if (!checked) {
        alert("Select a device generation.");
        return false;
      }
    }
    if (step === 3) {
      const name = root.querySelector("#device_name");
      if (name && !name.value.trim()) {
        name.focus();
        alert("Enter a device name.");
        return false;
      }
    }
    return true;
  }

  function bindCameraBusForm(form) {
    const bus = form.querySelector("[data-setup-camera-bus], select[name='camera_bus']");
    if (!bus) return;
    const update = () => updateCameraFields(form);
    bus.addEventListener("change", update);
    const count = form.querySelector("[data-setup-camera-count], input[name='camera_count']");
    if (count) count.addEventListener("input", update);
    update();
  }

  function initWizard(form) {
    setStep(form, 1);
    syncTileChecks(form);
    bindCameraBusForm(form);

    form.querySelectorAll('input[name="generation_uid"]').forEach((input) => {
      input.addEventListener("change", () => syncTileChecks(form));
    });

    const count = form.querySelector("[data-setup-camera-count]");
    if (count) count.addEventListener("input", () => updateCameraFields(form));

    form.querySelector("[data-setup-next]")?.addEventListener("click", () => {
      const current = parseInt(form.dataset.currentStep || "1", 10);
      if (!validateStep(form, current)) return;
      setStep(form, current + 1);
    });

    form.querySelector("[data-setup-back]")?.addEventListener("click", () => {
      const current = parseInt(form.dataset.currentStep || "1", 10);
      setStep(form, current - 1);
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-device-setup-wizard]").forEach(initWizard);
    document.querySelectorAll("[data-device-capabilities-form]").forEach(bindCameraBusForm);
  });
})();
