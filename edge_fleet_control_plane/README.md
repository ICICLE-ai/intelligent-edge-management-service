# ICICLE Edge Control Plane — v2

A production-shaped FastAPI service that lets a researcher publish a **Model
Card** and lets an operator deploy that model to a Jetson device or a group of
Jetsons with a single click. No release manifests, no OTA bundle versioning —
each deploy command is self-contained, and stopping a deployment cleans up the
container, image, and downloaded artifact.

```
[Researcher UI]  ──▶  Model Card (artifact + container spec + compatibility)
                                                │
                                                ▼
[Operator UI]   ──▶  Deployment   ──MQTT──▶  Jetson Agent  ──▶  Download → docker run
       ▲                                                          │
       └────────────── Heartbeat / Ack (HTTP) ───────────────────┘
```

## Highlights

* **Two interfaces in one app:** researcher (publish model cards) + operator
  (fleet management).
* **Researcher-first publish UX:** paste a Patra UUID and a raw `docker run`
  command, pick the supported devices as checkbox tiles, and watch the form
  auto-fill plus a live preview of the command we'll run on the device. The
  model mount is automatically rewritten to the agent's standard
  `${DEPLOYMENT_DIR}/model` path.
* **Self-contained MQTT commands:** the agent doesn't need any pre-staged
  manifest. The control plane sends artifact source, image, container spec,
  env, mounts, args, and ports in one payload.
* **One-button cleanup:** `stop_deployment` with `purge: true` removes the
  container, image, and on-disk artifact directory. By default, **Stop** only
  halts the container so you can **Restart** quickly without re-downloading.
* **Normalised SQLite schema with migrations:** model cards (with a UNIQUE
  `patra_model_card_uuid` natural key on top of the surrogate `model_card_uid`
  PK), artifacts, container specs (env / mounts / args / ports as child
  tables), device compatibility, deployments, per-device materialisation,
  command audit, events, and raw MQTT audit.
* **Modern UI** with a categorised accordion sidebar and a clean design
  system (single CSS file, no build step).
* **Modular code layout:** core / db / repositories / services / routes/web /
  routes/api / agent_package — each layer has a single responsibility.

## Publishing a model card

Researchers usually have two things on hand: a Patra model UUID and the
`docker run` command they use locally. The publish form is built around
that:

1. Paste the Patra UUID. It's stored as a UNIQUE field on `model_cards`, so a
   single Patra model can have at most one card (a friendly error pops up if
   you try to publish a duplicate). Internally, the card's primary key
   remains the prefixed `model_card_uid` so every foreign-key relationship in
   the schema stays stable even if the natural key changes shape later.
2. Type the in-container path of the model file
   (e.g. `/workspace/models/unetpp.engine`). The agent always downloads the
   artifact into `/opt/icicle-edge/deployments/<deployment_uid>/model/` and
   mounts that directory at the matching point in your `docker run` command.
3. Paste the raw `docker run` command. The form parses it live and fills in
   every structured field below — image, container name, env vars, mounts,
   ports, runtime flags. The mount whose container-side target encloses the
   model path is rewritten so its host side becomes
   `${DEPLOYMENT_DIR}/model`. A pre block below the textarea always shows
   the canonical command we'll execute on the device — it updates as you
   edit any field.
4. Check the compatible Jetson generations (multi-select tiles). The portal
   will refuse to deploy to incompatible devices.

If JavaScript is disabled or the user wants to publish via the API, the same
parsing runs server-side: `POST /api/models/parse-command` exposes it
directly, and `POST /models` falls back to it whenever the structured spec
fields are empty.

## Repo layout

```
app/
├── config.py                # AppSettings, env loading
├── auth.py                  # current_username() — local dev or Tapis session
├── main.py                  # FastAPI bootstrap + error handlers + startup
├── core/                    # ids, time, templates, errors, logging
├── db/
│   ├── session.py           # sqlite3 helpers (BEGIN/COMMIT/ROLLBACK)
│   ├── migrations.py        # numbered .sql files, schema_migrations table
│   └── sql/001_init.sql     # initial normalised schema
├── repositories/            # one file per aggregate
├── services/                # business logic
├── routes/
│   ├── web/                 # HTML routes (Jinja)
│   └── api/                 # JSON routes (agent + public)
├── static/                  # design system CSS + vanilla JS
├── templates/               # Jinja templates (layout + pages)
└── agent_package/           # Jetson installer bundle
    ├── install.sh
    ├── uninstall.sh
    ├── requirements.txt
    ├── systemd/icicle-edge-agent.service
    └── agent/main.py        # rewritten agent v2 (HTTP heartbeat + MQTT cmds)
config/
├── device_generations.json  # seeded device catalog
└── seed_models.json         # seeded model card for first-run UX
data/                        # SQLite database lives here
tests/smoke_test.py          # end-to-end test
```

## Quick start

```bash
python3 -m venv edgeopsenv
source edgeopsenv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

```env
APP_BASE_URL=https://<your-ngrok-or-public-url>
MQTT_ENABLED=true
MQTT_HOST=icicleedgemqttbroker.pods.icicleai.tapis.io
MQTT_PORT=443
MQTT_TLS=true
ALLOW_LOCALHOST_INSTALLER=false
```

Run:

```bash
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open <http://localhost:8000>. JSON docs are at `/api/docs`.

## Smoke test

```bash
ALLOW_LOCALHOST_INSTALLER=true \
APP_BASE_URL=http://testserver \
MQTT_ENABLED=false \
LOCAL_DEV_USERNAME=smoke_user \
python tests/smoke_test.py
```

Expected:

```text
SMOKE TEST PASSED
```

The smoke test exercises group + device CRUD, installer minting, enrollment,
heartbeat, model-card publishing via the web form, deployment dispatch,
agent-ack, stop+purge, and incompatible-target rejection.

## MQTT contract

| Topic                                       | Direction | Payload type        |
|---------------------------------------------|-----------|---------------------|
| `icicle/v1/commands/device/<device_uid>`    | control→  | `deploy_model` / `stop_deployment` |
| `icicle/v1/commands/device-group/<gid>`     | control→  | same                |
| `icicle/v1/commands/generation/<gen_uid>`   | control→  | reserved            |

### `deploy_model`

```jsonc
{
  "operation": "deploy_model",
  "request_id": "req_…",
  "deployment_uid": "dpl_…",
  "model": { "model_card_uid": "...", "display_name": "...", "version": "..." },
  "artifact": {
    "filename": "model.engine",
    "container_path": "/workspace/models/model.engine",
    "source_type": "patra | url",
    "patra_model_card_uuid": "…",     // when source_type=patra
    "download_url": "…",              // when source_type=url
    "sha256": "…",
    "size_bytes": 0
  },
  "container": {
    "image": "habg21/unet-trt-infer:latest",
    "container_name": "unetpp_infer",
    "pull_policy": "if_not_present",
    "runtime": "nvidia", "gpus": "all",
    "network_mode": "host", "ipc_mode": "host", "privileged": true
  },
  "runtime": {
    "model_env_var": "ENGINE_PATH",
    "environment": [{ "key": "NVIDIA_DRIVER_CAPABILITIES", "value": "all" }],
    "mounts": [{ "source": "${DEPLOYMENT_DIR}/model", "target": "/workspace/models",
                 "style": "volume", "type": "bind", "mode": "ro" }],
    "docker_args": [],
    "ports": []
  }
}
```

The agent stages the artifact under
`/opt/icicle-edge/deployments/<deployment_uid>/model/<filename>`, pulls the
image, and runs the container. Mount templates `${DEPLOYMENT_DIR}`,
`${MODEL_DIR}`, and `${MODEL_FILE}` are substituted at run time.

### `stop_deployment`

```jsonc
{
  "operation": "stop_deployment",
  "request_id": "req_…",
  "deployment_uid": "dpl_…",
  "container_name": "unetpp_infer",
  "image": "habg21/unet-trt-infer:latest",
  "artifact_filename": "model.engine",
  "purge": false   // default — docker stop only; true removes container + image + files
}
```

When `purge` is `false` (the default), the agent runs `docker rm -f` on the
container and keeps the image and artifact directory under
`/opt/icicle-edge/deployments/<uid>/` so a later `restart_deployment` can
launch a fresh container quickly.

When `purge` is `true`, the agent also removes the image and deletes the
deployment directory.

### `restart_deployment`

```jsonc
{
  "operation": "restart_deployment",
  "request_id": "req_…",
  "deployment_uid": "dpl_…",
  "container_name": "unetpp_infer"
}
```

The agent loads the saved `deploy_payload.json`, verifies the image is present
locally (`pull_policy: never`), and runs `docker run` — a new container from
the cached image and model files. No artifact re-download.

### Agent ACKs

The agent posts to `POST /api/agent/ack` for every state transition (DOWNLOADING,
PULLING, STARTING, RUNNING, STOPPED, FAILED) and the control plane updates
both the per-device deployment row and the parent deployment status.

## Database

The schema lives in `app/db/sql/001_init.sql` and is applied once via the
migration runner. To add a new column or table, create
`app/db/sql/002_…sql`; on next startup it will be applied and recorded in
`schema_migrations`.

## What changed vs v1

* `releases`, `release_assignments`, and the embedded
  `manifest_json` field are gone. They were the OTA-style manifest layer.
* `runtime_commands` became `device_commands`, scoped to a `deployment_uid`
  and a real `topic` audit field.
* New `model_cards`, `model_artifacts`, `container_specs`,
  `container_spec_env|mounts|docker_args|ports`, and `model_compatibility`
  tables — fully normalised.
* New `deployments` + `device_deployments` tables to track per-device
  delivery status.
* The agent's `deploy_model` command is now self-contained — no
  pre-staged manifest lookup.
