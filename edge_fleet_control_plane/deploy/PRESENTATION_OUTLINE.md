============================================================
ICICLE EDGE FLEET CONTROL PLANE — PRESENTATION CONTENT
============================================================

Audience: mixed (leadership, researchers, students, engineers).
Style: story first, plain language, technical depth layered in.
Format: each "SLIDE" = one slide. "On-slide" = bullets to show.
        "Say" = speaker notes / talking points (don't put on slide).
Suggested length: 15–18 slides, ~15–20 minute talk.


------------------------------------------------------------
SLIDE 1 — TITLE
------------------------------------------------------------
On-slide:
  • ICICLE Edge Fleet Control Plane
  • Deploy and manage AI models on edge devices — from one dashboard
  • [Your name / team / date]

Say:
  This is the story of how we turned a manual, error-prone process of
  putting AI models onto field devices into a few clicks in a browser.


------------------------------------------------------------
SLIDE 2 — THE PROBLEM (no jargon)
------------------------------------------------------------
On-slide:
  • AI models need to run OUT in the field — on cameras, sensors,
    farm equipment — not just in the cloud
  • Each device is a small computer (e.g. NVIDIA Jetson) sitting
    on a farm, in a lab, or in the wild
  • Getting a model onto each device meant: log in by hand, copy files,
    run commands, hope nothing breaks — for every single device

Say:
  Imagine you have 50 devices spread across different farms. Updating
  the AI model on each one by hand is slow, easy to get wrong, and
  impossible to track. That's the pain we set out to remove.


------------------------------------------------------------
SLIDE 3 — THE ANALOGY (make it click for everyone)
------------------------------------------------------------
On-slide:
  • Think of it like an "app store + remote control" for field devices
  • Pick a model  →  choose which devices  →  press Deploy
  • The devices do the rest, and report back their status live

Say:
  If you've ever pushed an app to your phone from a web account, or
  used "Find My Device" — that's the feel. We made AI model deployment
  that simple, but for a fleet of edge computers.


------------------------------------------------------------
SLIDE 4 — WHAT WE BUILT (the one-liner)
------------------------------------------------------------
On-slide:
  • A web control plane (dashboard) to:
      – Publish AI "model cards" (a model + how to run it)
      – Deploy them to one device, a group, or a whole device type
      – Monitor, stop, restart, or remove deployments — live
  • Part of the NSF ICICLE AI Institute edge tooling

Say:
  "Control plane" is just the brain/dashboard that coordinates
  everything. The devices are the "hands" doing the work.


------------------------------------------------------------
SLIDE 5 — WHO USES IT (audience hooks)
------------------------------------------------------------
On-slide:
  • Researchers — publish a model once, run it anywhere in the fleet
  • Operators — deploy & monitor devices without touching a terminal
  • Leadership — one place to see what's running, where, and its health
  • Devices/Agents — receive commands and report back automatically

Say:
  Everyone gets a view that fits them — no one needs to be a DevOps
  expert to put a model in the field.


------------------------------------------------------------
SLIDE 6 — HOW IT WORKS (the big picture, 1 diagram)
------------------------------------------------------------
On-slide (draw as 3 boxes left-to-right):
   [ Browser / User ]  →  [ Control Plane (cloud) ]  ⇄  [ Edge Devices ]
                                   |
                            [ Database + Model Registry ]

  • User acts in the browser
  • Control plane sends commands out and collects status back
  • Devices run the models and "phone home" with heartbeats

Say:
  Two communication paths: commands go OUT to devices over a lightweight
  messaging system; devices send health "heartbeats" and confirmations
  back. The dashboard always reflects reality.


------------------------------------------------------------
SLIDE 7 — THE DEPLOYMENT JOURNEY (lifecycle)
------------------------------------------------------------
On-slide:
  • Publish a model card (what to run + which devices it fits)
  • Deploy → command sent → device downloads & starts the model
  • Live status: Delivering → Running
  • Manage it: Stop (pause), Restart (resume), Delete (full cleanup)

Say:
  A key design choice: "Stop" keeps the model files cached so you can
  restart instantly, while "Delete" fully cleans the device. Small
  detail, big time-saver in the field.


------------------------------------------------------------
SLIDE 8 — WHAT MAKES IT TRUSTWORTHY
------------------------------------------------------------
On-slide:
  • Single sign-on with Tapis (institutional login) — no new passwords
  • Role-aware: admins vs. operators see the right controls
  • Live health monitoring — devices flagged offline automatically
  • Every action is recorded (who deployed what, when)

Say:
  Security and accountability matter when you're touching real hardware.
  We use the same login the rest of the ICICLE platform uses, so access
  is centrally managed.


------------------------------------------------------------
SLIDE 9 — INTEGRATIONS (it doesn't live alone)
------------------------------------------------------------
On-slide:
  • Tapis  — authentication + cloud hosting (Pods)
  • Patra  — model card / provenance backend
  • MQTT broker — fast, lightweight command delivery to devices
  • PostgreSQL — durable record of devices, models, deployments

Say:
  We plugged into the existing ICICLE ecosystem instead of reinventing
  it — login, model provenance, messaging, and storage are all shared
  infrastructure.


------------------------------------------------------------
SLIDE 10 — UNDER THE HOOD (for the technical folks)
------------------------------------------------------------
On-slide:
  • Backend: Python + FastAPI (REST + server-rendered UI)
  • Messaging: MQTT (commands) + HTTPS (heartbeats / ACKs)
  • Storage: SQLite for local dev → PostgreSQL in production
  • Auth: Tapis OAuth2 (authorization-code flow)
  • Packaged as a Docker container, runs on Tapis Pods (Kubernetes)

Say:
  Built to be portable: same codebase runs on a laptop with SQLite or
  in production with Postgres, just by changing environment variables.


------------------------------------------------------------
SLIDE 11 — THE DEVICE AGENT (the other half)
------------------------------------------------------------
On-slide:
  • A small program installed on each Jetson device
  • Listens for commands (deploy / stop / restart / delete)
  • Runs models as Docker containers on the device
  • Sends heartbeats: CPU, memory, temperature, what's running
  • Handles real-world needs (e.g. camera/display access)

Say:
  The agent is what makes the magic feel automatic. It does the heavy
  lifting on the device so the user never has to SSH in.


------------------------------------------------------------
SLIDE 12 — FROM LAPTOP TO PRODUCTION (the journey we took)
------------------------------------------------------------
On-slide:
  • Started: local development with ngrok + SQLite
  • Containerized the app (Docker) for consistent deployment
  • Migrated storage to managed PostgreSQL
  • Deployed to Tapis Pods at a stable public URL
  • Hardened it: real-world networking, login, health checks

Say:
  Going from "works on my machine" to "running 24/7 in the cloud" is
  its own project. This slide is honest about that path.


------------------------------------------------------------
SLIDE 13 — LESSONS LEARNED (relatable, builds credibility)
------------------------------------------------------------
On-slide:
  • Match the environment: build for the right CPU architecture
  • Networking in the cloud ≠ networking on a laptop (ports matter)
  • Small config mistakes can hide big symptoms — good logs save hours
  • Make destructive actions deliberate (Stop vs. Delete)
  • Reuse shared infrastructure instead of rebuilding it

Say:
  These are the kinds of practical lessons any team deploying real
  systems will recognize — worth sharing so others avoid the potholes.


------------------------------------------------------------
SLIDE 14 — IMPACT / VALUE
------------------------------------------------------------
On-slide:
  • Minutes instead of hours to deploy a model fleet-wide
  • No terminal expertise required — accessible to more of the team
  • Central visibility: what's running, where, and is it healthy
  • Repeatable & auditable — fewer mistakes, clear history

Say:
  The headline: we lowered the barrier to running AI in the field and
  made it observable and reliable.


------------------------------------------------------------
SLIDE 15 — WHAT'S NEXT
------------------------------------------------------------
On-slide:
  • Broader device support and larger fleets
  • Richer monitoring & alerts
  • Tighter model provenance (Patra) integration
  • Self-service onboarding for new devices

Say:
  We have a solid foundation; the roadmap is about scale, insight, and
  making onboarding even easier.


------------------------------------------------------------
SLIDE 16 — CLOSING
------------------------------------------------------------
On-slide:
  • One dashboard. Many devices. AI in the field — made simple.
  • Thank you  •  Questions?
  • [Contact / repo / demo link]

Say:
  Happy to give a live demo or go deeper on any part — the device
  agent, the cloud deployment, or the security model.


============================================================
APPENDIX — OPTIONAL BACKUP SLIDES (if asked)
============================================================

A1 — GLOSSARY (for non-technical audiences)
  • Edge device: a small computer in the field (e.g. NVIDIA Jetson)
  • Model card: a packaged AI model + instructions to run it
  • Control plane: the central dashboard/brain that coordinates devices
  • Agent: the helper program running on each device
  • Container (Docker): a sealed box holding the app and everything it
    needs, so it runs the same everywhere
  • MQTT: a lightweight messaging system, like text messages for machines
  • Heartbeat: a periodic "I'm alive and here's my status" message
  • OAuth / SSO: logging in with an existing institutional account

A2 — DEPLOYMENT STATES (for operators)
  • DELIVERING — command sent, device is fetching/starting
  • RUNNING — model is live on the device
  • STOPPED — paused, files kept (fast restart)
  • DELETED/CANCELLED — fully cleaned off the device

A3 — DEMO SCRIPT (2 minutes, if doing it live)
  1. Log in with Tapis
  2. Show the device fleet + live health
  3. Pick a model card → Deploy to a device
  4. Watch status flip Delivering → Running (live, no refresh)
  5. Stop it, then Restart it to show the cached-image speed
  6. Show the action log / history

A4 — SUGGESTED VISUALS
  • Slide 6: the 3-box architecture diagram (the anchor visual)
  • Slide 7: a horizontal lifecycle arrow (Publish→Deploy→Run→Manage)
  • A real screenshot of the dashboard with a couple of devices
  • A photo of an actual Jetson device in the field (humanizes it)
