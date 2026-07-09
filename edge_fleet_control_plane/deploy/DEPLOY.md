# Deploy ICICLE Edge Control Plane on Tapis Pods

Target URL: **https://edgecontrolplane.pods.icicleai.tapis.io**

Database: **edgetoolsuitedb.pods.icicleai.tapis.io** (Postgres 16)

## 1. Build and push the Docker image

Tapis pods run **linux/amd64**. If you build on an Apple Silicon Mac without
`--platform linux/amd64`, the pod will fail with `exec format error`.

```bash
cd edge_fleet_control_plane
export IMAGE=YOUR_DOCKERHUB_USER/edge-control-plane:latest
chmod +x deploy/build-and-push.sh
PUSH=true ./deploy/build-and-push.sh
```

Or manually:

```bash
docker buildx build --platform linux/amd64 -t habg21/edge-control-plane:latest --load .
docker push habg21/edge-control-plane:latest
```

Get the image whitelisted for Tapis pods (required — see step 1b below).

## 1b. Allowlist the image on Tapis Pods (required)

Tapis will **not spawn** a pod unless the Docker image is registered on the tenant allowlist. Action logs showing `spawner got error when creating pod` with **no container logs** usually mean the image is missing from the allowlist or cannot be pulled.

1. Open **Pods → Images** in the Tapis UI (`https://icicleai.tapis.io/pods/images` or your pods portal).
2. Check whether `habg21/edge-control-plane` (or your tag, e.g. `:v3`) is listed — your working `habg21/icicle-edge-mqttbroker` image should appear here as a reference.
3. If missing, **register the image** (Create Image / Submit Image):
   - **Image:** `habg21/edge-control-plane:v3` (match the tag you push and use in the pod spec)
   - **Description:** ICICLE Edge Fleet Control Plane
   - **Tenants:** your tenant (e.g. `icicleai`)
4. Wait until the image shows as allowed, then create/restart the pod.

Via API (optional):

```bash
curl -X POST "https://icicleai.tapis.io/v3/pods/images" \
  -H "X-Tapis-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"image":"habg21/edge-control-plane:v3","description":"ICICLE Edge Control Plane","tenants":["icicleai"]}'
```

## 2. Register OAuth callback

In the Tapis OAuth client `icicle-edge-control-plane`, add:

```text
https://edgecontrolplane.pods.icicleai.tapis.io/auth/callback
```

Remove or keep the ngrok callback only if you still need local dev.

## 3. Configure secrets

```bash
cp deploy/env.production.example deploy/env.production
# Edit deploy/env.production — set:
#   APP_SECRET          (openssl rand -hex 32)
#   DATABASE_URL        (Postgres password from edgetoolsuitedb pod)
#   TAPIS_CLIENT_KEY
#   MQTT_USERNAME/PASSWORD if your broker requires auth
```

Copy values into `deploy/pod-edgecontrolplane.json` → `environment_variables` and set `image` to your pushed tag.

**Important:** Use only the fields in `deploy/pod-edgecontrolplane.json`. If you copy JSON from the Tapis **Details** tab, remove read-only server fields first — the API rejects them with `Extra inputs are not permitted`:

```text
update_ts, last_status_check_ts, creation_ts, start_instance_ts, time_to_stop_ts,
status, status_container, healthchecks, ready_condition, depends_on, stack_id
```

Also omit `pod_id` when **updating** an existing pod (it goes in the URL, not the body). Omit generated networking fields like `url` unless you know you need them.

**Tapis pods rules:** every env key and value must be a **non-empty** string **≤ 128 characters**. Do not include empty `MQTT_USERNAME`/`MQTT_PASSWORD`. Use `DB_HOST`/`DB_USER`/… instead of a long `DATABASE_URL`.

**Do not commit `deploy/env.production` or real secrets.**

## 4. Create the pod

Using the Tapis CLI/API (adjust to your workflow):

```bash
# Example — replace with your tapis pods create command
tapis pods create --file deploy/pod-edgecontrolplane.json
```

Or paste the JSON into the Tapis pods UI.

**Do not set `"command": []` or `"arguments": []`.** Empty arrays override the image start command and the container exits immediately with no logs. If your pod was created with empty arrays, update with `"command": null, "arguments": null` (see `deploy/pod-edgecontrolplane-update.json`).

**Do not set `TAPIS_PODS_IMAGEPULLSECRET` unless you need a private registry.** Tapis requires **APPROVEDADMIN** on the pod for that env var; pod creators only get **ADMIN** by default, which causes `spawner got error when creating pod` with no container logs. `habg21/edge-control-plane` is public on Docker Hub — omit the pull secret.

## 5. Verify

```bash
curl -s https://edgecontrolplane.pods.icicleai.tapis.io/api/health
# {"status":"ok"}

open https://edgecontrolplane.pods.icicleai.tapis.io
```

Log in with Tapis OAuth. Migrations and seed data run automatically on first startup.

## Troubleshooting: pod stuck in DELETING / no logs

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Action log: `spawner got error when creating pod`, no container logs | Image **not on Pods allowlist** or image pull failed | Register image under **Pods → Images**; verify `TAPIS_PODS_IMAGEPULLSECRET` matches your Docker Hub username secret |
| Same loop, image **is** allowlisted | `TAPIS_PODS_IMAGEPULLSECRET` set but user lacks **APPROVEDADMIN** | Remove `TAPIS_PODS_IMAGEPULLSECRET` for public Docker Hub images |
| Same loop, image allowlisted | Stored pod still has `"command": []` | PUT update with `"command": null, "arguments": null`, then restart |
| `exec format error` | Image built for arm64 (Mac) not amd64 | Rebuild with `PUSH=true ./deploy/build-and-push.sh` (uses `--platform linux/amd64`) |
| No logs, pod dies quickly | Still running old cached image | Push a new tag (e.g. `:v2`) and update pod `image`, or recreate pod to force pull |
| `DB_PASSWORD is unset or still the placeholder` | Pod JSON still has `REPLACE_DB_PASSWORD` | Set `DB_PASSWORD` to the **edgetoolsuitedb** pod `POSTGRES_PASSWORD` |
| `database preflight failed` / timeout | Wrong port from pod-to-pod | Try `DB_PORT=443` (Tapis inter-pod Postgres proxy); if that fails, try `5432` |
| `password authentication failed` | Wrong DB password or user | Match `DB_USER` / `DB_PASSWORD` / `DB_NAME` to the Postgres pod env |

After rebuilding, check **Logs** on the pod — the entrypoint prints architecture, DB host/port, and a DB connectivity preflight before uvicorn starts.

## 6. Update Jetson agents

Re-download installers from the portal or set `APP_BASE_URL` in agent config to:

```text
https://edgecontrolplane.pods.icicleai.tapis.io
```

Agents must reach this URL for heartbeat, ack, and enrollment.

## Environment reference

| Variable | Production value |
|----------|------------------|
| `APP_BASE_URL` | `https://edgecontrolplane.pods.icicleai.tapis.io` |
| `DATABASE_URL` | Postgres on `edgetoolsuitedb.pods.icicleai.tapis.io` |
| `DB_HOST`, `DB_USER`, … | Use split vars in Tapis pod JSON (128-char limit per value) |
| `LOCAL_DEV_AUTH` | `false` |
| `HEARTBEAT_HISTORY_MODE` | `none` (avoids heartbeat spam in Postgres) |

Local dev can keep SQLite (`DATABASE_PATH`) and omit `DATABASE_URL`.
