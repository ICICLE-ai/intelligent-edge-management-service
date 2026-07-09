============================================================
ICICLE EDGE FLEET CONTROL PLANE — SYSTEM OVERVIEW
A study guide / briefing document
============================================================

Purpose of this document:
  A self-contained explainer you can read on your own to understand
  what this system is, why it exists, how the pieces fit together, and
  how a model actually gets deployed to a device. It moves from the big
  picture down to the technical details. No prior context needed.


------------------------------------------------------------
1. THE ONE-PARAGRAPH SUMMARY
------------------------------------------------------------
The Edge Fleet Control Plane is a web application that lets people
deploy and manage AI models on edge devices (small field computers like
NVIDIA Jetsons) from a single dashboard. Instead of logging into each
device by hand, a user publishes a "model card," picks target devices,
and clicks Deploy. The control plane sends the command to the devices,
the devices run the model inside a container, and they continuously
report their status back. It is part of the NSF ICICLE AI Institute's
edge tooling and integrates with the Tapis platform for login, hosting,
and model provenance.


------------------------------------------------------------
2. WHY IT EXISTS (the problem)
------------------------------------------------------------
Edge AI means running models where the data is created — on cameras,
sensors, farm equipment — rather than sending everything to the cloud.

Without a control plane, deploying a model to a fleet means:
  • Manually connecting to each device (SSH)
  • Copying model files and config
  • Running container commands by hand
  • No central view of what's running or whether it's healthy
  • Mistakes scale with the number of devices

This is slow, error-prone, and unauditable. The control plane replaces
all of that with a browser dashboard and an automated device agent.


------------------------------------------------------------
3. KEY CONCEPTS / VOCABULARY
------------------------------------------------------------
  • Edge device      — a small computer in the field (e.g. Jetson).
  • Device generation— a "type" of device (hardware family). Models can
                       be marked compatible with specific generations.
  • Device group     — a user-defined collection of devices.
  • Model card       — a packaged AI model PLUS the instructions to run
                       it (container image, env vars, mounts, ports,
                       which device types it fits). The unit you deploy.
  • Control plane    — the central web app/dashboard (the "brain").
  • Agent            — a small program on each device that executes
                       commands and reports status (the "hands").
  • Deployment       — one instance of a model card sent to a target
                       (a device, a group, or a generation).
  • Heartbeat        — a periodic status message from a device.
  • Container (Docker)— a sealed package with the app + its dependencies
                       so it runs identically anywhere.
  • MQTT             — a lightweight publish/subscribe messaging system,
                       used to deliver commands to devices.
  • Tapis            — the platform providing login (OAuth), cloud
                       hosting (Pods), and related services.
  • Patra            — the model card / provenance backend.


------------------------------------------------------------
4. THE BIG PICTURE (architecture)
------------------------------------------------------------

        +------------------+
        |   Browser/User   |
        +------------------+
                 |  (HTTPS, logged in via Tapis)
                 v
        +---------------------------+        +------------------+
        |   CONTROL PLANE (cloud)   |  <-->  |   PostgreSQL DB  |
        |   FastAPI web app         |        |  devices, models |
        |   - UI + REST API         |        |  deployments,    |
        |   - auth, orchestration   |        |  heartbeats      |
        +---------------------------+        +------------------+
            |   ^                                   
   commands |   | heartbeats / ACKs (HTTPS)         
   (MQTT)   v   |                                   
        +---------------------------+
        |   EDGE DEVICES + AGENTS   |
        |   run models as Docker    |
        |   containers              |
        +---------------------------+

Two communication directions:
  • OUTBOUND (control plane → devices): commands via MQTT
    (deploy, stop, restart, delete).
  • INBOUND (devices → control plane): heartbeats and command
    acknowledgements via HTTPS.

Why two channels? MQTT is great for pushing small command messages to
many devices quickly; HTTPS is simple and reliable for devices to report
status and confirm actions. The dashboard always reflects real state.


------------------------------------------------------------
5. THE MAIN ACTORS
------------------------------------------------------------
A) The Control Plane (this app)
   • Serves the web UI and a REST API.
   • Authenticates users via Tapis OAuth.
   • Stores the source of truth (devices, model cards, deployments).
   • Publishes commands to devices and ingests their status.

B) The Device Agent (installed on each Jetson)
   • Enrolls with the control plane (gets identity + config).
   • Subscribes to its command topic over MQTT.
   • Executes commands by running/stopping Docker containers.
   • Sends heartbeats (CPU, memory, temperature, running containers).
   • Handles real-world device needs (e.g. camera/display permissions).

C) Supporting services
   • Tapis  — login + cloud hosting (Pods).
   • Patra  — model provenance/card backend.
   • MQTT broker — message delivery.
   • PostgreSQL — durable storage.


------------------------------------------------------------
6. THE DEPLOYMENT LIFECYCLE (the core workflow)
------------------------------------------------------------
Step 1 — Publish a model card
   A researcher defines: the container image to run, environment
   variables, mounts/ports, and which device generations it supports.

Step 2 — Deploy
   The user selects a target (a device, a group, or a generation) and
   clicks Deploy. The control plane:
     a) records the deployment in the database,
     b) builds a self-contained command payload,
     c) publishes it to the device(s) over MQTT.

Step 3 — Device executes
   The agent receives the command, pulls/starts the model container,
   and sends back an acknowledgement.

Step 4 — Live status
   The deployment moves through states the UI shows in real time:
     DELIVERING  → command sent, device working on it
     RUNNING     → model is live on the device
   The UI polls a lightweight status endpoint so it updates without a
   manual page refresh.

Step 5 — Manage it
   • STOP    — removes the running container but KEEPS the image and
               model files on the device, so a restart is fast.
   • RESTART — launches a new container from the cached image.
   • DELETE  — full cleanup on the device AND removes it from the portal.
   • DISMISS — portal-only cleanup for a stuck record (no device call).

Design rationale: separating "Stop" (pause, keep cache) from "Delete"
(full wipe) is a deliberate, real-world convenience — restarting a model
shouldn't require re-downloading everything.


------------------------------------------------------------
7. SECURITY & ACCESS
------------------------------------------------------------
  • Login: Tapis OAuth2 (authorization-code flow). Users sign in with
    their existing institutional account — no separate password store.
  • Sessions: a signed, HTTP-only cookie keeps the user logged in.
  • Roles: admins and operators; the UI shows controls appropriate to
    the role. Admins are configured via an env var (list of usernames).
  • Auditability: actions and events are recorded, so there's a history
    of who deployed/stopped/deleted what and when.
  • Access gate: unauthenticated browser users are redirected to login;
    API calls without a session get a 401.


------------------------------------------------------------
8. TECHNOLOGY CHOICES (and why)
------------------------------------------------------------
  • Python + FastAPI
      Fast to build, great for both REST APIs and server-rendered HTML.
  • Server-rendered UI + small JS for live polling
      Simple, robust, no heavy front-end framework needed.
  • MQTT for commands
      Lightweight, designed for many devices and intermittent networks.
  • HTTPS for heartbeats/ACKs
      Simple and firewall-friendly from the device side.
  • SQLite (dev) → PostgreSQL (prod)
      Same code, different database via configuration. Easy local dev,
      durable production storage.
  • Docker + Tapis Pods (Kubernetes)
      Package once, run consistently in the cloud at a stable URL.
  • Configuration via environment variables
      The same image behaves correctly in dev or prod without code
      changes (database, auth, messaging all configurable).


------------------------------------------------------------
9. DATA MODEL (what the database tracks)
------------------------------------------------------------
At a high level, the system stores:
  • Users          — identity + role (operator/admin).
  • Device         — each enrolled edge device + its last-seen status.
  • Device groups  — collections of devices.
  • Device         — generations (hardware types) used for compatibility.
  • Model cards    — the deployable units, plus their container spec
                     (image, env, mounts, ports) and compatibility list.
  • Deployments    — each deploy action, its target, and current status.
  • Heartbeats     — device health over time (optionally sampled to
                     avoid flooding the database).
  • Events/logs    — an audit trail of actions.


------------------------------------------------------------
10. FROM LAPTOP TO PRODUCTION (the deployment story)
------------------------------------------------------------
The app was taken from local development to a 24/7 cloud service:
  1. Local dev: ran with SQLite; exposed temporarily via ngrok for
     device testing.
  2. Containerized: wrote a Dockerfile so the app runs identically
     anywhere (built for the correct CPU architecture — important!).
  3. Storage: migrated to managed PostgreSQL for durability and scale;
     connection details split into small env vars to fit platform
     limits.
  4. Hosting: deployed to Tapis Pods at a stable public HTTPS URL.
  5. Hardening: handled cloud networking (service ports), startup
     health checks, clear startup logging, and a database connectivity
     pre-check so failures are obvious instead of silent.


------------------------------------------------------------
11. LESSONS LEARNED (practical takeaways)
------------------------------------------------------------
  • Build for the target CPU architecture. An image built on a laptop
    (ARM) won't run on a cloud node (x86) — it fails with "exec format
    error". Always build for the deployment platform.
  • Cloud networking differs from local. Inter-service ports in a
    managed platform may not be the same as the raw database port.
  • Don't override the container start command with an empty value —
    it can make the container exit immediately with no logs.
  • Good logs save hours. A startup banner + a database pre-check turned
    silent crashes into one-line, obvious errors.
  • Match the OAuth callback URL exactly. The single most common login
    error is a redirect_uri that doesn't match the registered callback.
  • Reuse shared infrastructure (login, messaging, storage) rather than
    rebuilding it.
  • Make destructive actions deliberate (Stop vs. Delete).


------------------------------------------------------------
12. GLOSSARY QUICK REFERENCE
------------------------------------------------------------
  Control plane  : the central dashboard/brain.
  Agent          : the helper program on each device.
  Model card     : a model + how to run it.
  Deployment     : one model card sent to a target.
  Heartbeat      : periodic device status message.
  MQTT           : lightweight machine-to-machine messaging.
  OAuth/SSO      : logging in with an existing account.
  Container      : a sealed, portable app package (Docker).
  Pods           : Tapis's container hosting (on Kubernetes).


------------------------------------------------------------
13. IF YOU WANT TO GO DEEPER
------------------------------------------------------------
  • Deployment details ............ deploy/DEPLOY.md
  • Adding Tapis OAuth to an app ... deploy/TAPIS_OAUTH_GUIDE.md
  • Presentation outline ........... deploy/PRESENTATION_OUTLINE.md

Suggested study path:
  1. Read sections 1–6 here for the mental model.
  2. Read section 6 (lifecycle) again slowly — it's the heart of it.
  3. Skim the OAuth guide to understand the login flow.
  4. Skim DEPLOY.md to see how it actually runs in the cloud.
