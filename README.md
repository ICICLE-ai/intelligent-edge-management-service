# Intelligent Edge Management Service

The Intelligent Edge Management Service (IEMS) is ICICLE's fleet control plane for deploying and operating AI workloads on edge devices. It provides a browser dashboard and REST API to enroll devices, publish model cards, deploy containers to Jetsons and similar hardware, monitor heartbeats, and manage live camera streams. The service integrates with Tapis for authentication and hosting, Patra for model provenance, and the ICICLE TapisUI extension for portal access.

**Tags:** Software, CI4AI, AI4CI

For guidance on what to include in Tutorials, How-To Guides, Explanation, and Reference, see [Diátaxis](https://diataxis.fr/).

### License

[![License](https://img.shields.io/badge/License-BSD--3--Clause-yellow.svg)](https://github.com/ICICLE-ai/intelligent-edge-management-service?tab=BSD-3-Clause-1-ov-file)

> **Note:** Add a `LICENSE` file at the repository root if one is not already present. Update the badge URL to match the chosen license.

## References

- [ICICLE Edge Fleet Control Plane — System Overview](edge_fleet_control_plane/deploy/SYSTEM_OVERVIEW.md)
- [Deploy on Tapis Pods](edge_fleet_control_plane/deploy/DEPLOY.md)
- [Tapis OAuth integration guide](edge_fleet_control_plane/deploy/TAPIS_OAUTH_GUIDE.md)
- [Live streaming architecture](edge_fleet_control_plane/deploy/STREAMING.md)
- [ICICLE TapisUI](https://github.com/tapis-project/tapis-ui) and [ICICLE extension](https://github.com/ICICLE-ai/tapisui-extension-icicle)
- [Tapis documentation](https://tapis.readthedocs.io/en/latest/contents.html)
- Production control plane: `https://edgecontrolplane.pods.icicleai.tapis.io`
- ICICLE portal (TapisUI): `https://icicleai.tapis.io`

## Acknowledgements

*National Science Foundation (NSF) funded AI institute for Intelligent Cyberinfrastructure with Computational Learning in the Environment (ICICLE) (OAC 2112606)*

## Issue reporting

Open a GitHub issue at [ICICLE-ai/intelligent-edge-management-service/issues](https://github.com/ICICLE-ai/intelligent-edge-management-service/issues).

---

# Tutorials

## Tutorial: Run the control plane locally

This walkthrough starts the Edge Fleet Control Plane on your laptop for development and smoke testing.

### Prerequisites

- Python 3.12+
- `git`
- (Optional) Docker, for building images or running edge model containers

### Steps

1. **Clone the repository**

   ```bash
   git clone https://github.com/ICICLE-ai/intelligent-edge-management-service.git
   cd intelligent-edge-management-service/edge_fleet_control_plane
   ```

2. **Create a virtual environment and install dependencies**

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Configure local development**

   ```bash
   cp .env.example .env
   ```

   For local dev, keep `LOCAL_DEV_AUTH=true` (default in `.env.example`). This bypasses Tapis OAuth and logs you in as `local_tapis_user`.

4. **Start the server**

   ```bash
   uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
   ```

5. **Open the dashboard**

   Visit [http://localhost:8000](http://localhost:8000). You should see the ICICLE Edge Control Plane dashboard.

6. **Run the smoke test (optional)**

   ```bash
   python tests/smoke_test.py
   ```

### Expected result

The dashboard loads, you can browse devices, model cards, and deployments. SQLite stores data under `data/edge_control_plane.db`.

---

## Tutorial: Access IEMS from the ICICLE portal (TapisUI)

The ICICLE TapisUI extension embeds the control plane in an iframe and passes the user's Tapis JWT via a `postMessage` handshake.

### Prerequisites

- A running TapisUI dev instance or access to `https://icicleai.tapis.io`
- Valid ICICLE tenant credentials
- Control plane deployed with `TAPIS_PORTAL_ORIGINS` including your TapisUI origin

### Steps

1. Log in to TapisUI on an ICICLE tenant (`https://icicleai.tapis.io` or local dev with `VITE_TAPIS_BASE_URL` set to an ICICLE URL).
2. Open the sidebar entry **Intelligent Edge Management Service**.
3. The portal iframe loads `https://edgecontrolplane.pods.icicleai.tapis.io` and exchanges a signed auth token with the parent window.
4. You are signed in to the control plane without a separate login prompt.

### Expected result

The full control plane UI appears inside TapisUI. If you see "Invalid JWT", log out of TapisUI and log back in to refresh the token.

---

# How-To Guides

## How to deploy a new control-plane release on Tapis Pods

1. **Build and push the Docker image** for `linux/amd64` (required on Tapis Pods):

   ```bash
   cd edge_fleet_control_plane
   export IMAGE=YOUR_DOCKERHUB_USER/edge-control-plane:vNEXT
   chmod +x deploy/build-and-push.sh
   PUSH=true ./deploy/build-and-push.sh
   ```

2. **Allowlist the image** in Tapis Pods → Images for your tenant.

3. **Update secrets** in `deploy/env.production` (copy from `deploy/env.production.example`). Never commit real secrets.

4. **Update the pod spec** in `deploy/pod-edgecontrolplane.json`:
   - Set `image` to your new tag
   - Copy environment variables from `deploy/env.production`

5. **Register the OAuth callback** on the Tapis OAuth client `icicle-edge-control-plane`:

   ```text
   https://edgecontrolplane.pods.icicleai.tapis.io/auth/callback
   ```

6. **Create or update the pod** via the Tapis Pods UI or API.

7. **Verify** health at `https://edgecontrolplane.pods.icicleai.tapis.io/api/health`.

See [edge_fleet_control_plane/deploy/DEPLOY.md](edge_fleet_control_plane/deploy/DEPLOY.md) for troubleshooting (image allowlist, env var length limits, empty `command` arrays).

---

## How to enroll an edge device

1. In the control plane UI, go to **Devices → Register device**.
2. Follow the setup wizard to install the device agent on the Jetson (or supported hardware).
3. The agent enrolls over HTTPS, receives its identity, and subscribes to MQTT command topics.
4. Confirm the device appears online on the dashboard (heartbeats within `DEVICE_OFFLINE_AFTER_SECONDS`).

Agent install scripts live under `edge_fleet_control_plane/app/agent_package/`.

---

## How to deploy a model to a device

1. **Publish a model card** — define the container image, environment variables, mounts, ports, and compatible device generations.
2. **Select a deployment target** — a single device, a device group, or a device generation.
3. **Click Deploy** — the control plane records the deployment, publishes an MQTT command, and tracks status (`DELIVERING` → `RUNNING`).
4. **Monitor** the deployment detail page; status polls update automatically.
5. **Stop, restart, or delete** as needed. *Stop* keeps cached images on the device; *Delete* removes containers and portal records.

Example smart Docker apps (UNet live inference) are under `edge_fleet_control_plane/smart_docker_apps/`.

---

## How to configure portal iframe authentication

Set these environment variables on the control plane pod:

| Variable | Purpose |
|---|---|
| `TAPIS_PORTAL_ORIGINS` | Comma-separated parent origins allowed to embed IEMS (e.g. `http://localhost:3000,https://icicleai.tapis.io`) |
| `SESSION_SAME_SITE` | Set to `none` for cross-site iframe cookies in HTTPS production |
| `TAPIS_BASE_URL` | ICICLE tenant base URL |
| `TAPIS_CLIENT_ID` / `TAPIS_CLIENT_KEY` | OAuth client credentials |

The portal and control plane exchange messages using protocol version `1`:

- Request: `iems:portal-auth:request`
- Response: `iems:portal-auth:response` (includes Tapis `accessToken`)

Implementation: `edge_fleet_control_plane/app/static/js/portal-auth.js` (control plane) and `tapis-ui/packages/icicle-tapisui-extension/src/pages/IntelligentEdgeManagementService/` (portal).

---

# Explanation

## What IEMS is

IEMS solves fleet-scale edge AI operations. Without a control plane, deploying models to many field devices requires manual SSH access, file copies, and per-device Docker commands — slow, error-prone, and hard to audit.

IEMS replaces that with:

- A **central dashboard** (FastAPI + server-rendered HTML)
- A **device agent** on each edge node that executes commands
- **MQTT** for outbound commands (deploy, stop, restart, delete)
- **HTTPS** for inbound heartbeats and acknowledgements
- **PostgreSQL** (production) or **SQLite** (local dev) as the source of truth

## Architecture

```text
 Browser / TapisUI iframe
         │  HTTPS (Tapis OAuth or portal SSO)
         ▼
 ┌──────────────────────────┐       ┌─────────────┐
 │  Edge Control Plane      │◄─────►│ PostgreSQL  │
 │  (FastAPI, port 8765)    │       └─────────────┘
 └──────────────────────────┘
         │ MQTT commands
         ▼
 ┌──────────────────────────┐
 │  Edge devices + agents   │
 │  (Docker model containers)│
 └──────────────────────────┘
```

### Key concepts

| Term | Meaning |
|---|---|
| **Model card** | A deployable AI model plus its container spec (image, env, mounts, compatible device generations) |
| **Deployment** | One model card sent to a target (device, group, or generation) |
| **Device agent** | On-device program that runs containers and reports heartbeats |
| **Heartbeat** | Periodic device health message (CPU, memory, temperature, running containers) |

### Integrations

- **Tapis** — OAuth login, Pods hosting, tenant identity
- **Patra** — model card and artifact provenance (`PATRA_BASE_URL`)
- **MQTT broker** — command delivery to devices
- **MediaMTX / relay streaming** — live camera preview and inference streams (see `deploy/STREAMING.md`)
- **ICICLE TapisUI extension** — embeds the control plane in the ICICLE portal sidebar

### Repository layout

```text
intelligent-edge-management-service/
├── README.md                    # This file (ICICLE catalog + Diátaxis docs)
├── component-info.yaml          # ICICLE component catalog metadata
├── RELEASE.md                   # Release notes
└── edge_fleet_control_plane/    # Main application
    ├── app/                     # FastAPI app, routes, services, agent package
    ├── config/                  # Seed data (models, device generations)
    ├── deploy/                  # Pod specs, env templates, deployment guides
    ├── smart_docker_apps/       # Example edge inference containers
    ├── Dockerfile
    └── requirements.txt
```

For a longer-form briefing, read [edge_fleet_control_plane/deploy/SYSTEM_OVERVIEW.md](edge_fleet_control_plane/deploy/SYSTEM_OVERVIEW.md).
