# Testing Model

This document defines how a change is verified before it is merged and before it is
deployed. It is written to be adopted unchanged across our repositories: the level
definitions, the merge and deployment criteria, the user-test rules, and the adoption
notes are intended to be copied verbatim.

Three sections are repo-specific and must be rewritten when this document is copied
elsewhere: **Level 0** (the local commands), **Level 1** (the workflow inventory), and
**Level 3** (the deployment commands and checklist). Everything else carries over.

## Current position on unit tests

This repository has **no pytest unit-test suite**. That is a deliberate decision rather
than an oversight. Covering the FastAPI control plane, Jinja dashboards, MQTT agent
protocol, and device-side live apps with isolated unit tests is a large up-front cost
that the project has chosen to defer. Unit tests will be added later, and when they
are, they become a blocking CI gate like any other.

What exists today is an **end-to-end smoke test**, not a unit suite.
`edge_fleet_control_plane/tests/smoke_test.py` drives the FastAPI app through
`TestClient` (groups, devices, model cards, installer, deployments) against SQLite
with `LOCAL_DEV_AUTH`. CI runs it. It does not cover the browser UI, Tapis OAuth,
MQTT, GPU inference containers, or a live PowerEdge/Jetson agent.

Until a unit suite exists:

- Keep logic in small pure functions, separated from FastAPI route handlers, Jinja
  templates, and agent scripts. Code shaped this way is cheap to test later; code that
  is not will have to be restructured before it can be tested at all.
- Do not introduce a test framework as part of an unrelated pull request. Adding a
  runner, its configuration, and its CI wiring is its own change and needs its own
  review.
- Volunteered tests are welcome. Ask a maintainer where they should go before writing
  them, so the layout does not have to be redone when the suite is set up properly.
- Record verification gaps in the pull request. If you could not exercise a path, say
  which one and why.
- A passing smoke test does **not** replace a user test document for a behavioural
  change. The smoke test is one automated check; Level 2 is still the evidence that
  the change works for a person using the dashboard or an agent.

The tooling below is the intended future direction. **None of it is in effect today**
except the smoke test already wired in CI — none of these *unit-test* tools are
installed, configured, or run by CI, and no configuration file for any of them exists
in this repository.

| Area | Language | Intended tooling | Status |
|---|---|---|---|
| `edge_fleet_control_plane/app/` | Python 3.12 | pytest + FastAPI `TestClient` for route coverage | Smoke test only; no pytest suite |
| `edge_fleet_control_plane/app/agent_package/` | Python | pytest against agent command handling | Not yet in effect |
| `edge_fleet_control_plane/smart_docker_apps/` | Python | pytest / container smoke on amd64 CUDA where hardware exists | Not yet in effect |
| End-to-end | — | Browser flow against a locally running control plane | Not yet in effect |
| Linting | Python | Ruff | Not yet in effect |

## Levels of verification

| Level | What it is | Who runs it | When | Blocks merge |
|---|---|---|---|---|
| 0 | Local pre-submit checks — the same build and typecheck commands CI runs, executed on your machine before you push | Contributor | Before opening or updating a pull request | No, but a failure here fails Level 1 |
| 1 | Automated CI gates — the GitHub Actions workflows in `.github/workflows/` | GitHub Actions | On every pull request, and on push to `main` | Yes |
| 2 | Manual functional verification — exercising the change by hand, recorded as a user test document in `docs/user-tests/` | Contributor | Before requesting review | Yes, when the change is behavioural |
| 3 | Deployment verification — building the container images and running the deployment | Contributor or release manager | Before a release, and for any change to the build or deployment surface | Yes for deployment; not for an ordinary merge |

## Which levels apply to my change?

| Change type | Level 0 | Level 1 | Level 2 | Level 3 |
|---|---|---|---|---|
| Documentation only | Not required | Automatic | Not required | Not required |
| Refactor with no behaviour change | Required | Automatic | Required — regression check on the refactored path | Not required |
| Behaviour change | Required | Automatic | Required — full user test document | Not required unless deployment-visible |
| Dependency change | Required | Automatic | Required — exercise the code paths using the dependency | Required if the dependency is installed in an image |
| Build, container, or deployment change | Required | Automatic | Required if user-visible | Required |
| Release | Required | Automatic | Required — link the user test documents covering the release | Required |

"Automatic" means CI runs it whether you want it or not; you do not get to opt out, but
you also do not have to do anything to trigger it.

## Level 0 — local pre-submit checks

Run these from the repository root, in this order. It is the same work CI runs, so a
failure here is a failure in CI.

```bash
# 1. Control-plane dependencies, byte-compile, smoke test.
python -m pip install --disable-pip-version-check -r edge_fleet_control_plane/requirements.txt
python -m compileall -q \
  edge_fleet_control_plane/app \
  edge_fleet_control_plane/tests \
  edge_fleet_control_plane/smart_docker_apps

cd edge_fleet_control_plane
mkdir -p data
ALLOW_LOCALHOST_INSTALLER=true \
APP_BASE_URL=http://testserver \
MQTT_ENABLED=false \
LOCAL_DEV_AUTH=true \
LOCAL_DEV_USERNAME=smoke_user \
python tests/smoke_test.py
cd ..
```

`compileall` proves every listed module parses. It does not import them, so it will not
catch a bad import or a missing dependency. The smoke test *does* import the FastAPI
app and hit routes. It uses SQLite under `edge_fleet_control_plane/data/` (gitignored)
and does not need Postgres, MQTT, Tapis, or a GPU.

```bash
# 2. Repository-wide YAML and shell syntax (the manifests job).
python -m pip install --disable-pip-version-check pyyaml
git ls-files '*.yml' '*.yaml' -z | xargs -0 python -c '
import sys, yaml
failed = False
for path in sys.argv[1:]:
    try:
        with open(path) as handle:
            list(yaml.safe_load_all(handle))
    except yaml.YAMLError as error:
        print(f"Invalid YAML in {path}: {error}")
        failed = True
sys.exit(1 if failed else 0)
'
git ls-files '*.sh' -z | xargs -0 -r -n1 bash -n
```

On macOS, GNU `xargs -r` may be missing. If that flag errors, drop `-r` or run
`bash -n` on each `git ls-files '*.sh'` path by hand.

### Toolchain versions

| Toolchain | Version CI uses | Where it is pinned |
|---|---|---|
| Python | 3.12 | `env.PYTHON_VERSION` in [.github/workflows/build.yml](../.github/workflows/build.yml), matching `FROM python:3.12-slim-bookworm` in [edge_fleet_control_plane/Dockerfile](../edge_fleet_control_plane/Dockerfile) |
| pip packages | Pinned in `requirements.txt` | [edge_fleet_control_plane/requirements.txt](../edge_fleet_control_plane/requirements.txt) |
| Gitleaks | 8.30.1 | `GITLEAKS_VERSION` in [.github/workflows/secret-scan.yml](../.github/workflows/secret-scan.yml) |
| TruffleHog | 3.97.0 | `version` / commit pin in [.github/workflows/secret-scan.yml](../.github/workflows/secret-scan.yml) |

Dependencies are installed with `pip install -r edge_fleet_control_plane/requirements.txt`.
There is no lockfile. A local Python other than 3.12 can hide a 3.12-only failure.

## Level 1 — automated CI gates

| Workflow | File | Triggers | What it proves |
|---|---|---|---|
| Build | [.github/workflows/build.yml](../.github/workflows/build.yml) | `push` to `main`, `pull_request` (any base branch), `workflow_dispatch` | Control-plane deps install, listed Python trees parse, smoke test passes, every tracked YAML file parses, every tracked shell script parses |
| Repository health | [.github/workflows/repository-health.yml](../.github/workflows/repository-health.yml) | `pull_request` (any base branch), `push` to `main` | A fixed list of contribution-readiness files exists |
| Secret Scan | [.github/workflows/secret-scan.yml](../.github/workflows/secret-scan.yml) | `push` to `main`, `pull_request` (any base branch), weekly `schedule` (`0 6 * * 1`), `workflow_dispatch` | No detectable secrets in the changed commits, or in the full history on non-PR runs |

All three declare `permissions: contents: read`. `Build` and `Secret Scan` set a
concurrency group keyed on workflow and ref, cancelling in-progress runs for pull
requests only. `Repository health` sets no concurrency group, so its runs are never
cancelled and can pile up on a rapidly updated branch.

### Build

Two independent jobs, both on `ubuntu-latest`.

**`control-plane` — "Build control plane"**, with `working-directory: edge_fleet_control_plane`
except the compile step, which runs at the repository root.

1. `actions/checkout@v4`.
2. `actions/setup-python@v5` with `python-version: "3.12"`, pip cache keyed on
   `edge_fleet_control_plane/requirements.txt`.
3. `python -m pip install --disable-pip-version-check -r requirements.txt`.
4. `python -m compileall -q` on `edge_fleet_control_plane/app`,
   `edge_fleet_control_plane/tests`, and `edge_fleet_control_plane/smart_docker_apps`.
   Live-app trees are parsed without importing CUDA or camera SDKs.
5. `python tests/smoke_test.py` with `ALLOW_LOCALHOST_INSTALLER=true`,
   `APP_BASE_URL=http://testserver`, `MQTT_ENABLED=false`, `LOCAL_DEV_AUTH=true`,
   `LOCAL_DEV_USERNAME=smoke_user`.

**`manifests` — "Check manifests and scripts"**, running at the repository root.

1. `actions/checkout@v4`.
2. `actions/setup-python@v5` with `python-version: "3.12"`.
3. `python -m pip install --disable-pip-version-check pyyaml`.
4. Validate YAML manifests — `yaml.safe_load_all` over every tracked `*.yml` and
   `*.yaml` (workflows, issue templates, `component.yaml`, compose files, MediaMTX
   config). This proves the files are well-formed YAML. It does not validate them
   against any schema.
5. Check shell scripts parse — `bash -n` over every tracked `*.sh`. Syntax only; the
   scripts are never executed.

### Repository health

A single job, `required-project-files`, on `ubuntu-latest`: checkout, then a shell loop
asserting that each path in a hard-coded `required_files` array exists, exiting non-zero
if any is missing. It checks existence only — not content, not freshness, not whether the
file says anything useful.

The list currently covers `LICENSE`, `README.md`, `CONTRIBUTING.md`,
`CODE_OF_CONDUCT.md`, `SECURITY.md`, `CITATION.cff`, `docs/RELEASE_CHECKLIST.md`,
`docs/MAINTAINER_ROLES.md`, `docs/TESTING.md` and `docs/user-tests/TEMPLATE.md`.

### Secret Scan

Two jobs.

**`gitleaks` — "Detect secrets (gitleaks)"**

1. `actions/checkout@v4` with `fetch-depth: 0`, so the full history is available.
2. Install Gitleaks — downloads the `8.30.1` Linux x64 release tarball, verifies it
   against a pinned SHA-256, extracts the binary to `/usr/local/bin`, prints the version.
   The version and digest are pinned in the job `env` rather than resolved from the
   releases API, which is unauthenticated and rate limited per runner IP.
3. Scan for secrets — on a pull request, `gitleaks git . --log-opts "$BASE_SHA..$HEAD_SHA"
   --redact`, covering only the commits the pull request adds. On any other event,
   `gitleaks git . --redact` over the whole history. The SHAs are passed through the
   environment rather than interpolated into the script body, so a workflow input can
   never become shell syntax.

**`trufflehog` — "Detect secrets (trufflehog)"**

Gated on `if: github.event_name == 'push' || github.event_name == 'pull_request'`.

1. `actions/checkout@v4` with `fetch-depth: 0`.
2. `trufflesecurity/trufflehog`, pinned to the commit tagged `v3.97.0`
   (`bcfcf73aaf4759d4dadc2783177c245a02792318`), with `path: ./` and
   `extra_args: --results=verified,unknown`. `base` and `head` are deliberately unset so
   the action derives the correct commit range per event type. `--results=verified,unknown`
   keeps findings whose verification was inconclusive, which `--only-verified` would
   discard along with any real credential whose verification request happened to fail.

This job does not run on the weekly schedule or on manual dispatch — the action only
derives a commit range for `push` and `pull_request` events, and on other events it emits
a malformed argument pair. The periodic full-history rescan is therefore covered by the
`gitleaks` job alone.

### What CI does not check today

- **No pytest unit suite.** The smoke test exercises a slice of HTTP routes. It does not
  replace isolated tests of parsers, MQTT, streaming, or agent installers.
- **No linting or formatting.** No Ruff, Flake8, or Black configuration is run in CI.
- **No browser or Tapis OAuth check.** `LOCAL_DEV_AUTH` bypasses OAuth in the smoke test.
- **No MQTT, MediaMTX, or GPU container run.** Live apps are byte-compiled only.
- **No control-plane image build.** [edge_fleet_control_plane/Dockerfile](../edge_fleet_control_plane/Dockerfile)
  is not built by any workflow. A change that breaks `docker buildx` on `linux/amd64`
  can still pass CI.
- **No Tapis pod spec schema check.** `deploy/pod-edgecontrolplane.json` is JSON, not YAML,
  so the YAML job does not see it.
- **No dependency vulnerability scanning and no Dependabot.** There is no
  `.github/dependabot.yml` and no `pip-audit` step.
- **No check that a new environment variable was added to every manifest.** That is a
  Level 3 checklist item, done by hand.

## Level 2 — manual functional verification

Because there is no unit-test suite, this is where the evidence that a change works
for a person actually comes from. For any behavioural change, exercise the change by
hand and write it up. The minimum bar is all six of these:

1. **Happy path.** The change doing the thing it was written to do, with ordinary inputs,
   end to end from the user interface — not from a `curl` against one route, unless the
   route *is* the user-facing surface.
2. **At least one edge case.** An empty input, a boundary value, the largest realistic
   input, a slow heartbeat, a second concurrent browser session — whichever is plausible
   for the code you touched.
3. **At least one failure case.** What the user sees when the thing fails: an invalid
   installer, a missing generation, an unpublished model card, an unreachable MQTT
   broker. "It errors" is not a result; record the actual message or state.
4. **A regression check on adjacent behaviour.** Exercise the nearest thing you did not
   intend to change. If you touched the device wizard, check that an existing device
   still heartbeats. If you touched deploy env injection, check that an existing live
   app still starts.
5. **Screenshots for any visual change.** Before and after, in the same viewport, for
   every screen the change affects. Attach them to the pull request and reference them
   from the user test document.
6. **Written up and committed in the same pull request** as `docs/user-tests/YYYY-MM-DD-<slug>.md`,
   from [docs/user-tests/TEMPLATE.md](./user-tests/TEMPLATE.md).

If a step in the bar does not apply, say so in the "Not tested" section of the document
and say why. Omitting it silently is the thing this model exists to prevent.

## Level 3 — deployment verification

The control plane ships one production image, built for **linux/amd64** (Tapis pods).
Apple Silicon builds without `--platform linux/amd64` fail on the pod with
`exec format error`. Follow [edge_fleet_control_plane/deploy/DEPLOY.md](../edge_fleet_control_plane/deploy/DEPLOY.md).

```bash
cd edge_fleet_control_plane
export IMAGE=YOUR_DOCKERHUB_USER/edge-control-plane:<tag>
PUSH=true ./deploy/build-and-push.sh
```

The image must be on the Tapis Pods allowlist before the pod will start. Update the
pod spec from [deploy/pod-edgecontrolplane.json](../edge_fleet_control_plane/deploy/pod-edgecontrolplane.json)
using **placeholders only** in git (`REPLACE_APP_SECRET`, `REPLACE_DB_PASSWORD`,
`REPLACE_TAPIS_CLIENT_KEY`). Do not commit `deploy/env.production` or a filled
`pod-edgecontrolplane-update.json`.

Live-app images (for example YOLO CNW) are separate Docker Hub images. Rebuild and
push those only when the corresponding `smart_docker_apps/` tree or its catalog
entry changed. They are not required for a control-plane-only release.

For local runs without Tapis, follow the root [README.md](../README.md):
`cp edge_fleet_control_plane/.env.example edge_fleet_control_plane/.env` and
`uvicorn` on port 8000.

### Deployment verification checklist

**Images build clean**

- [ ] The control-plane image builds from a clean checkout for `linux/amd64`
      (`PUSH=true ./deploy/build-and-push.sh` or `docker buildx build --platform linux/amd64`).
- [ ] The build emits no new warnings, and no step silently swallows a failure.
- [ ] The image size did not grow unexpectedly.
- [ ] Any live-app image affected by the change was rebuilt for the target
      architecture (amd64 CUDA for PowerEdge; arm64 only when that device is in scope).

**Healthchecks pass**

- [ ] `GET /api/health` returns `{"status":"ok"}`. Production:
      `curl -s https://edgecontrolplane.pods.icicleai.tapis.io/api/health`.
      Local container: port **8765** (image `EXPOSE` / Tapis networking), not 8000.
- [ ] The container log shows architecture, DB host/port, and `database preflight ok`
      (Postgres) or `database: sqlite` (local), printed by
      [docker-entrypoint.sh](../edge_fleet_control_plane/deploy/docker-entrypoint.sh).
- [ ] The container stays up. Watch it long enough to see it is not crash-looping.
- [ ] Tapis OAuth sign-in works against the registered callback
      `https://edgecontrolplane.pods.icicleai.tapis.io/auth/callback`.

**Environment variables**

- [ ] Every new environment variable is documented in
      [edge_fleet_control_plane/.env.example](../edge_fleet_control_plane/.env.example)
      and [deploy/env.production.example](../edge_fleet_control_plane/deploy/env.production.example).
- [ ] Every new variable needed in production is present in
      [deploy/pod-edgecontrolplane.json](../edge_fleet_control_plane/deploy/pod-edgecontrolplane.json)
      as a **placeholder or documented value**, never a real secret.
- [ ] Unset behaviour is sane. Either the code supplies a safe default, or it fails
      immediately with a message that names the variable.
- [ ] Tapis env values are non-empty strings and ≤ 128 characters.

**No secrets in images**

- [ ] `docker history --no-trunc <image>` shows no credential in any build argument or
      command.
- [ ] `docker run --rm <image> env` shows no baked-in credential.
- [ ] No `.env`, key, certificate, or local database file was copied into the image.
      [edge_fleet_control_plane/.dockerignore](../edge_fleet_control_plane/.dockerignore)
      excludes `.env`, `data/`, `*.db`, and `tests/`.
- [ ] The `Secret Scan` workflow passed on the commit being deployed.

**Agents**

- [ ] After a `APP_BASE_URL` change, agents re-download the installer or are pointed at
      `https://edgecontrolplane.pods.icicleai.tapis.io` so heartbeat and ack still work.

## Merge criteria

A reviewer should be able to check every one of these before approving.

- [ ] The pull request describes the problem and links the related issue.
- [ ] All CI gates in Level 1 are green.
- [ ] Level 0 commands relevant to the change were run locally, and the pull request says
      so.
- [ ] A user test document exists for any behavioural change, is committed in this pull
      request, and its result is recorded.
- [ ] The user test document meets the Level 2 minimum bar, or explicitly says which part
      it does not meet and why.
- [ ] Screenshots are attached for every user-visible change.
- [ ] Documentation is updated where behaviour, interfaces, configuration, installation,
      or limitations changed.
- [ ] New environment variables, dependencies, and trust boundaries are identified.
- [ ] Known verification gaps are stated in the reviewer notes.

**A pull request is not blocked for missing unit tests.** Do not request them as a
condition of approval while the suite is deferred.

**A pull request is blocked for a missing user test document when one applies.** The
matrix above decides whether one applies. This is the gate that replaces the missing
unit-test suite, so waiving it leaves the change with no evidence behind it at all.
A green smoke test does not waive this requirement.

## Deployment criteria

Everything under Merge criteria, plus:

- [ ] Level 3 deployment verification was performed and recorded — as a user test document
      with the deployment checklist filled in, or in the release record.
- [ ] The release is traceable to a tag, and the tag identifies the source commit.
- [ ] [docs/RELEASE_CHECKLIST.md](./RELEASE_CHECKLIST.md) is complete.
- [ ] Release notes state the known test gaps, **including the absence of a unit-test
      suite**, and name what was verified by the smoke test and by hand instead.
- [ ] A rollback path is known and written down: the previous image tag, the previous
      release tag, and any schema or configuration change that would have to be undone
      with it.

## User test documentation

User test documents live in [docs/user-tests/](./user-tests/) and are named
`docs/user-tests/YYYY-MM-DD-<slug>.md` — the date the test was run, then a short
hyphenated slug describing the change, for example
`docs/user-tests/2026-09-14-yolo-cnw-poweredge.md`.

Start from [docs/user-tests/TEMPLATE.md](./user-tests/TEMPLATE.md), which carries the
required sections as fill-in tables.

| Section | Required | What goes in it |
|---|---|---|
| Header table | Yes | Date, tester, pull request or issue, commit SHA tested, overall result |
| Environment | Yes | Where it ran — local, container, or Tapis pod — plus Python, browser, and image tag as applicable |
| Scope | Yes | What this document covers, in one or two sentences |
| Preconditions | Yes | Accounts, data, systems, and environment variables needed to reproduce the run |
| Test cases | Yes | The happy path, as steps / expected / actual / pass-fail |
| Edge cases | Yes | At least one, in the same table shape |
| Failure cases | Yes | At least one, in the same table shape |
| Regression check | Yes | The adjacent behaviour you exercised and what it did |
| Evidence | For visual changes | Screenshots, recordings, log excerpts |
| Issues found | Yes | Anything that failed or looked wrong, with an issue link where one was filed. "None" is a valid entry |
| Not tested | Yes | What you did not exercise, and why |

Rules for writing them:

1. Write steps a stranger can follow. Name the page, the button, the field, and the value
   you entered. "Deployed the app" is not a step.
2. Record the expected result before you run the step, not after you see what happened.
   Writing the expectation afterwards turns every run into a pass.
3. Record failures. A document with no failures in it is not a better document; it is
   usually a less careful one. A failed case that is understood and linked to an issue is
   a good outcome.
4. Use public, synthetic, or de-identified data only. No credentials, no tokens, no
   restricted datasets, no sensitive locations, no personal information — not in the
   steps, not in the screenshots, not in the pasted logs. Redact hostnames that identify
   a private system.
5. Keep it to about a page. This is a record of what you did, not a specification. If it
   is running long, the change is probably too large for one document.
6. Never edit a past document. It records what was true on a specific commit on a
   specific date. If something changed, write a new one.
7. Say what you did not test. The "Not tested" section is the most useful part of the
   document for the next person, and leaving it empty to look thorough is the one failure
   mode this model cannot detect.

## Adopting this model in another repository

**Copy verbatim:**

- The "Current position on unit tests" section, minus the tooling table.
- The four-level table and the "Which levels apply to my change?" matrix.
- The Level 2 minimum bar.
- Merge criteria and Deployment criteria.
- The whole "User test documentation" section, including the naming convention, the
  required-sections table, and the seven rules.
- `docs/user-tests/TEMPLATE.md` and `docs/user-tests/README.md`.

**Adapt per repository:**

- Level 0 — the real commands from that repository's manifests, in the order its CI runs
  them, with its own toolchain versions and pinning locations.
- Level 1 — the actual workflow inventory. Rewrite it from the workflow files, not from
  this document.
- Level 3 — the real build and run commands, the real health checks, the real manifests.
- The intended-future-tooling table, for that repository's languages.
- The related-documents list.

**Invariants, whatever the stack:**

- Four levels, in the same order, with the same meanings.
- Deferring unit tests defers automation, never verification.
- A behavioural change without a user test document does not merge.
- Level 0 is exactly what Level 1 runs, so a local pass predicts a CI pass.
- The workflow inventory states what each gate proves *and* what it does not.
- User test documents are immutable once merged.
- A missing unit test never blocks a merge while the suite is deferred.

## Related documents

- [CONTRIBUTING.md](../CONTRIBUTING.md) — how to contribute and what a pull request should
  contain
- [docs/RELEASE_CHECKLIST.md](./RELEASE_CHECKLIST.md) — the checklist to complete before a
  public release
- [docs/MAINTAINER_ROLES.md](./MAINTAINER_ROLES.md) — who owns review, release, and
  security response
- [SECURITY.md](../SECURITY.md) — reporting a vulnerability; contributor security
  expectations
- [docs/user-tests/README.md](./user-tests/README.md) — the user test record
- [docs/user-tests/TEMPLATE.md](./user-tests/TEMPLATE.md) — the user test template
- [README.md](../README.md) — local run and smoke test
- [edge_fleet_control_plane/deploy/DEPLOY.md](../edge_fleet_control_plane/deploy/DEPLOY.md) —
  Tapis pod image build, allowlist, and health check
