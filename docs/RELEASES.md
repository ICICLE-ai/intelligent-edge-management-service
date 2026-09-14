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

Catalog / ICICLE metadata for 0.1.0 also lives in [RELEASE.md](../RELEASE.md). Prefer
this file for what changed for operators and contributors.

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
