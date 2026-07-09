# Release notes — Intelligent Edge Management Service

## Version 0.1.0

**Component ID:** `IntelligentEdgeManagementService:0.1.0`  
**Status:** BetaRelease  
**Production URL:** https://edgecontrolplane.pods.icicleai.tapis.io

### Summary

Initial ICICLE catalog release of the Edge Fleet Control Plane with TapisUI portal integration, live streaming support, and production deployment on Tapis Pods.

### Features

- **Fleet dashboard** — enroll devices, organize groups and generations, view online/offline status
- **Model card lifecycle** — publish, deploy, stop, restart, and delete AI workloads on edge devices
- **Device agent** — MQTT command channel and HTTPS heartbeats/ACKs
- **Tapis OAuth** — authorization-code login for standalone access
- **Portal iframe SSO** — `postMessage` auth handshake with ICICLE TapisUI (`iems:portal-auth:*` protocol v1)
- **Patra integration** — resolve model artifacts from the Patra backend
- **Live streaming** — relay (MJPEG) and MediaMTX (RTSPS/HLS) modes for camera preview and inference streams
- **Example edge apps** — UNet GStreamer (CSI) and MVS multicam smart Docker applications

### Portal integration

| Item | Value |
|---|---|
| TapisUI route | `#/intelligent-edge-management-service` |
| Extension package | `@icicle/tapisui-extension` |
| Iframe origin | `https://edgecontrolplane.pods.icicleai.tapis.io` |
| Allowed portal origins | `http://localhost:3000`, `https://icicleai.tapis.io` |

### Deployment artifacts

| File | Purpose |
|---|---|
| `edge_fleet_control_plane/deploy/pod-edgecontrolplane.json` | Tapis Pod spec |
| `edge_fleet_control_plane/deploy/env.production.example` | Production environment template |
| `edge_fleet_control_plane/deploy/DEPLOY.md` | Step-by-step deployment guide |
| `edge_fleet_control_plane/Dockerfile` | Container image (`linux/amd64` for Pods) |

### Upgrade checklist

1. Build and push a new Docker image tag (`linux/amd64`).
2. Allowlist the image in Tapis Pods.
3. Update `deploy/pod-edgecontrolplane.json` (`image` + env vars).
4. Confirm OAuth callback URL matches `APP_BASE_URL/auth/callback`.
5. Confirm `TAPIS_PORTAL_ORIGINS` includes all TapisUI origins.
6. Restart the pod and verify `/api/health`.

### Known limitations

- `tapisui-api` ML Hub SDK types may fail to build on some `tapis-ui` branches (unrelated to IEMS backend).
- Portal SSO requires a valid Tapis JWT; expired tokens show "Invalid JWT" in the iframe.
- Tapis Pod environment values are limited to 128 characters — use split `DB_*` vars instead of a long `DATABASE_URL`.

### Documentation

- [README.md](README.md) — ICICLE-standard project documentation (Diátaxis)
- [component-info.yaml](component-info.yaml) — ICICLE component catalog entry
- [edge_fleet_control_plane/deploy/SYSTEM_OVERVIEW.md](edge_fleet_control_plane/deploy/SYSTEM_OVERVIEW.md) — architecture briefing

---

## Preparing the next release

1. Bump `componentVersion` in `component-info.yaml` and match the `id` suffix.
2. Update the FastAPI `version` in `edge_fleet_control_plane/app/main.py`.
3. Add a new section to this file.
4. Rebuild and redeploy the Docker image per `deploy/DEPLOY.md`.
5. After training-catalog deployment, add `trainingTutorialsUrl` to `component-info.yaml` if required by the catalog pipeline.
