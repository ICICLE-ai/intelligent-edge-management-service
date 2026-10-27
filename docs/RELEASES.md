# Releases

This file is the source of truth for **GitHub Release** notes. When you tag a release,
create a GitHub Release with the same title and paste that section’s body.

Do not edit a past section after it is published. Add a new dated heading.

## How to publish a GitHub Release

1. Complete [RELEASE_CHECKLIST.md](./RELEASE_CHECKLIST.md) and [TESTING.md](./TESTING.md)
   Level 3.
2. Add a new section at the top of this file (`Release YYYY-MM`) and commit it on `main`.
3. On GitHub: **Releases → Draft a new release**.
4. **Choose a tag:** create `v0.1.0` (or the next version) on the release commit.
5. **Release title:** match the heading, for example `Release 2026-09`.
6. Paste the section body (everything under the heading, not the heading itself).
7. Publish. Leave pre-release checked only while the catalog status is still Beta.

Catalog / ICICLE metadata for the current release also lives in [RELEASE.md](../RELEASE.md). Prefer
this file for what changed for operators and contributors.

---

## Release 2026-10

**Tag:** `v0.2.0` (create this tag when you publish the GitHub Release)  
**Previous tag:** `v0.1.0`  
**Catalog status:** BetaRelease  
**Production:** https://edgecontrolplane.pods.icicleai.tapis.io  
**Image:** `habg21/edge-control-plane:v13` built from commit `e485f49`  
**Agent:** `edge-agent-v2.5.1` (reinstall devices after the portal upgrade)

Deploy-time checks, deployment verification, and recovery for fleets that mix Jetson platforms. Also adds the Faux Weed YOLO11 app for AGX Xavier (JetPack 4), a deployer-supplied weights URL, and operator tools for timing and fleet rollout.

### Added

- **Deploy-time compatibility.** A deployment is refused before anything is sent when the device generation is not on the app, when reported memory or free disk is below the app's minimum, or when an engine build target does not match the device. Engine checks are exact GPU compute capability and a dotted-prefix match on TensorRT and L4T. The app owner sets memory, free disk, CUDA, and notes per generation on the app form.
- **Device platform report.** The agent reports GPU compute capability, L4T, host TensorRT, memory, free disk, and whether the NVIDIA container runtime is present. Compute capability is taken from the Tegra chip (`tegra194` is sm 7.2), so a JetPack 4 AGX Xavier that identifies itself as `Jetson-AGX` is recognized.
- **Deployment verification.** On RUNNING, the agent reports the artifact sha256 and the image id and registry digest of the image the container is actually running. The portal compares those with the deploy command and records MATCHED, PARTIAL, DRIFT, INVALID, UNVERIFIED, UNDECLARED, or UNKNOWN. MATCHED means every declared digest was observed and equal.
- **Recovery.** After reboot the agent starts containers that should still be running. A heartbeat that no longer sees one marks the deployment DEGRADED. A deploy replayed after stop does not start the container again. An acknowledgement for a command that a newer deploy, stop, or restart has replaced is ignored.
- **Weights URL at launch.** Apps that opt in ask the deployer for a model URL. The seeded Ultralytics CSI live app uses that path and saves the file as `model.pt`.
- **Faux Weed YOLO11 (live stream).** Catalog app and `smart_docker_apps/yolo_faux_weed_mvs_live_app` for AGX Xavier on JetPack 4 (`habg21/yolo-faux-weed-mvs-live:jp4`). The engine is downloaded from Hugging Face.
- **Theater view** for inference stream tiles.
- **One broker connection per group deploy.** Per-device MQTT commands for a group rollout are published on a single connection.
- **Per-phase timings.** Download, pull, and start times are stored on the command and can be exported with `edge_fleet_control_plane/eval/export_deployment_timings.py`.
- **Operator tools** in `edge_fleet_control_plane/eval/`: a fleet rollout simulator, a manual container timer (optional timed engine download), and an on-device sampler for agent CPU, memory, and the effect on a running app's inference time.

### Changed

- Seeded catalog apps are owned by `habg21`. On startup, apps still owned by `system` are reassigned to that account so the publisher can edit compatibility minimums.
- Agent state files are flushed to disk before they are replaced.
- OpenAPI `version` is `0.2.0`, the same identifier as this tag.

### Verification

- Automated: `tests/smoke_test.py` and `tests/provenance_test.py` (FastAPI `TestClient`, SQLite, `LOCAL_DEV_AUTH`). `tests/compatibility_test.py` covers accept and reject through `POST /deployments`. There is no pytest unit suite.
- By hand, 26 October 2026, portal image `habg21/edge-control-plane:v13`, agent `edge-agent-v2.5.1`, on an AGX Xavier running JetPack 4 (L4T 32.5.1, TensorRT 7.1.3.0, sm 7.2). Recorded in [docs/user-tests/2026-10-26-compatibility-and-recovery.md](./user-tests/2026-10-26-compatibility-and-recovery.md).
- Not covered by CI: browser UI, Tapis OAuth, MQTT, and GPU containers.

### Known limitations

- The TensorRT check reads the host package. An application image that ships its own TensorRT can differ. On a Jetson Orin Nano running `habg21/unet-trt-infer-live`, the host reported 10.3 and the container reported 10.9. The check does not load the engine file.
- Memory and free-disk minimums are values on the app. A minimum that is higher than the device has free will refuse a deploy that used to start. Change it on the app form.
- Verification compares declared digests with the file the agent hashed and the image id of the running container. It does not inspect what the process inside the container loaded.
- Tapis Pod environment values are at most 128 characters. Use split `DB_*` variables.
- The control-plane image must be `linux/amd64`. Reusing an image tag does not by itself make a running pod pull the new build. Restart, and recreate the pod if the new behavior is missing.

### Upgrade

1. Build and push `linux/amd64` (`edge_fleet_control_plane/deploy/build-and-push.sh`). Use a new tag, or recreate the pod if you push over a tag the pod already pulled.
2. Allowlist the image on Tapis Pods if the tag is new.
3. Point the `edgecontrolplane` pod at the new image. Leave `command` and `arguments` null. Do not set `TAPIS_PODS_IMAGEPULLSECRET` for this public image.
4. Confirm `GET /api/health` returns `{"status":"ok"}`.
5. Re-download and reinstall the device agent so heartbeats report `edge-agent-v2.5.1`.

### Rollback

Redeploy the image built from tag `v0.1.0` and reinstall the agent from that portal. Compatibility columns and provenance columns added by later migrations can stay; `v0.1.0` does not read them.

---

## Release 2026-09

**Tag:** `v0.1.0` (create this tag when you publish the GitHub Release)  
**Catalog status:** BetaRelease  
**Production:** https://edgecontrolplane.pods.icicleai.tapis.io

First public release notes for the Intelligent Edge Management Service (IEMS): a fleet
control plane for enrolling edge devices, publishing model cards, deploying containers,
and viewing live inference streams. Tapis OAuth and ICICLE TapisUI iframe SSO are
supported. This write-up also covers work landed after the first catalog draft: Dell
PowerEdge GPU devices, the crop/weed YOLO live app, and open-source CI.

### Added

- **Fleet dashboard and API.** Enroll devices, groups, and hardware generations; watch
  online/offline from heartbeats; publish model cards; deploy, stop, restart, and delete
  workloads on the device agent (MQTT commands, HTTPS heartbeat and ack).
- **Dell PowerEdge (GPU) generation.** x86_64 CUDA servers can be registered without a
  Jetson-style local camera. The wizard includes **Wireless video (no local camera)** so
  inbound RTSP stays inside the container; IEMS injects `STREAM_INGEST_URL` for annotated
  RTSPS to MediaMTX / portal HLS.
- **Crop Weed YOLO CNW (live stream).** Catalog seed card and `smart_docker_apps/ara_yolo_cnw_live_app`
  for `habg21/ara-yolo-cnw-live:latest`. Weights are mounted at deploy time (Patra /
  `MODEL_PATH`), not baked into the control-plane image.
- **UNet live examples.** GStreamer CSI and MVS multicam smart Docker apps remain in
  the catalog for Jetson-class devices.
- **CI for contributions.** `Build` (install, `compileall`, smoke test, YAML and shell
  parse), `Repository health` (required community files), and `Secret Scan` (Gitleaks +
  TruffleHog). [TESTING.md](./TESTING.md) defines verification levels and user-test
  documents under `docs/user-tests/`.
- **Citation authors.** `CITATION.cff` lists Harikesh Byrandurga Gopinath, Qifeng Chen,
  and Hari Subramoni.

### Changed

- Secrets and local databases are gitignored. A filled Tapis pod update spec is not
  tracked; use `deploy/pod-edgecontrolplane.json` with `REPLACE_*` placeholders in git.
- Repository community files: license, code of conduct, security policy, contributing
  guide, maintainer roles, and release checklist.

### Security

- Rotate Tapis pod `APP_SECRET`, database password, and Tapis client key if they ever
  appeared in a committed pod spec or `.env`. Removing a file from the current tree
  does **not** remove it from git history. Secret Scan on pull requests and pushes
  checks **new** commits; a weekly full-history Gitleaks run is informational until
  history is cleaned.

### Verification

- Automated: `edge_fleet_control_plane/tests/smoke_test.py` in the Build workflow
  (FastAPI `TestClient`, SQLite, `LOCAL_DEV_AUTH`). There is no pytest unit suite yet.
- Not covered by CI: browser UI, Tapis OAuth, MQTT, GPU containers, or a live agent.
  Behavioural changes still need a user test document (see TESTING.md).

### Known limitations

- Tapis Pod environment values are at most 128 characters; use split `DB_*` variables
  instead of a long `DATABASE_URL`.
- Portal SSO needs a valid Tapis JWT; an expired token shows as an invalid JWT in the
  iframe.
- Control-plane image must be `linux/amd64` or the Tapis pod fails with `exec format error`.
- FastAPI reports `2.0.0` in OpenAPI; catalog / CITATION version is `0.1.0`. Use the
  GitHub tag as the release identifier.

### Upgrade

1. Build and push `linux/amd64` (`edge_fleet_control_plane/deploy/build-and-push.sh`).
2. Allowlist the image on Tapis Pods.
3. Update the pod image tag from placeholders in `deploy/pod-edgecontrolplane.json`.
4. Confirm OAuth callback is `APP_BASE_URL/auth/callback`.
5. Restart the pod; `GET /api/health` returns `{"status":"ok"}`.
6. Re-download device installers if `APP_BASE_URL` changed.
