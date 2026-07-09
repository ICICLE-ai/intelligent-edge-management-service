(function () {
  "use strict";

  var AUTH_REQUEST_TYPE = "iems:portal-auth:request";
  var AUTH_RESPONSE_TYPE = "iems:portal-auth:response";
  var AUTH_PROTOCOL_VERSION = 1;
  var TIMEOUT_MS = 15000;

  var cfg = window.__IEMS_PORTAL_AUTH__ || {};
  var nextPath = typeof cfg.next === "string" && cfg.next.charAt(0) === "/" ? cfg.next : "/";
  var portalOrigins = Array.isArray(cfg.portalOrigins) ? cfg.portalOrigins : [];
  var statusEl = document.getElementById("embed-status");
  var errorEl = document.getElementById("embed-error");

  function setStatus(message) {
    if (statusEl) statusEl.textContent = message;
  }

  function showError(message) {
    if (errorEl) {
      errorEl.style.display = "block";
      errorEl.textContent = message;
    }
    setStatus("Sign-in could not be completed.");
  }

  function randomRequestId() {
    if (window.crypto && window.crypto.randomUUID) {
      return window.crypto.randomUUID();
    }
    var bytes = new Uint8Array(16);
    if (window.crypto && window.crypto.getRandomValues) {
      window.crypto.getRandomValues(bytes);
    }
    return Array.from(bytes, function (b) {
      return b.toString(16).padStart(2, "0");
    }).join("");
  }

  function isAllowedParentOrigin(origin) {
    return portalOrigins.indexOf(origin) !== -1;
  }

  function isPortalAuthResponse(value, requestId) {
    return (
      value &&
      typeof value === "object" &&
      value.type === AUTH_RESPONSE_TYPE &&
      value.version === AUTH_PROTOCOL_VERSION &&
      value.requestId === requestId &&
      typeof value.accessToken === "string" &&
      value.accessToken.length > 32
    );
  }

  function establishSession(accessToken) {
    return fetch("/auth/portal-session", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ access_token: accessToken, next: nextPath }),
    }).then(function (resp) {
      return resp.json().then(function (data) {
        if (!resp.ok) {
          throw new Error((data && data.error) || "Session creation failed");
        }
        return data;
      });
    });
  }

  function requestPortalToken() {
    if (window.self === window.top) {
      window.location.replace("/auth/start?next=" + encodeURIComponent(nextPath));
      return;
    }

    var requestId = randomRequestId();
    var settled = false;

    function cleanup(listener) {
      window.removeEventListener("message", listener);
      clearTimeout(timer);
    }

    function onMessage(event) {
      if (settled || !isAllowedParentOrigin(event.origin)) {
        return;
      }
      if (event.source !== window.parent) {
        return;
      }
      if (!isPortalAuthResponse(event.data, requestId)) {
        return;
      }

      settled = true;
      cleanup(onMessage);
      setStatus("Creating your session…");

      establishSession(event.data.accessToken)
        .then(function (data) {
          var target = data && data.next ? data.next : nextPath;
          window.location.replace(target);
        })
        .catch(function (err) {
          showError(err.message || "Unable to create session.");
        });
    }

    var timer = window.setTimeout(function () {
      if (settled) return;
      settled = true;
      cleanup(onMessage);
      showError(
        "Timed out waiting for Tapis UI. Open this service from the ICICLE portal or sign in directly."
      );
    }, TIMEOUT_MS);

    window.addEventListener("message", onMessage);
    setStatus("Requesting your Tapis session from the portal…");

    var request = {
      type: AUTH_REQUEST_TYPE,
      version: AUTH_PROTOCOL_VERSION,
      requestId: requestId,
    };

    portalOrigins.forEach(function (origin) {
      window.parent.postMessage(request, origin);
    });
  }

  requestPortalToken();
})();
