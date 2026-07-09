"""End-to-end smoke test for the rebuilt control plane.

Run with:
    ALLOW_LOCALHOST_INSTALLER=true APP_BASE_URL=http://testserver MQTT_ENABLED=false \\
        python tests/smoke_test.py
"""

import json
import os
import sys
import tarfile
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_PATH", str(ROOT / "data" / "smoke_test.db"))
os.environ.setdefault("LOCAL_DEV_AUTH", "true")
os.environ.setdefault("LOCAL_DEV_USERNAME", "smoke_user")
os.environ.setdefault("APP_BASE_URL", "http://testserver")
os.environ.setdefault("MQTT_ENABLED", "false")

db_path = Path(os.environ["DATABASE_PATH"])
if db_path.exists():
    db_path.unlink()

from app.config import get_settings  # noqa: E402
get_settings.cache_clear()  # respect overrides
from app.db.migrations import run_migrations  # noqa: E402
from app.db.session import fetch_one, execute  # noqa: E402
from app.main import app  # noqa: E402
from app.services.seed_service import seed_everything  # noqa: E402
from app.services import model_service  # noqa: E402
from app.repositories import users as users_repo  # noqa: E402

# Run startup work explicitly so TestClient construction doesn't depend on
# the ASGI lifespan firing.
run_migrations()
seed_everything()

client = TestClient(app)

# --------------------------------------------------------------------------- #
# Reference data is seeded automatically on startup.                          #
# --------------------------------------------------------------------------- #
r = client.get("/api/device-generations")
assert r.status_code == 200, r.text
gens = r.json()
assert any(g["generation_uid"] == "jetson-orin-nano-v1" for g in gens), gens

# Seeded model card present and published
r = client.get("/api/models")
assert r.status_code == 200, r.text
published_models = r.json()
assert any(m["model_card_uid"] == "mc_seed_unetpp" for m in published_models), published_models

# --------------------------------------------------------------------------- #
# Group + Device CRUD                                                         #
# --------------------------------------------------------------------------- #
r = client.post("/groups", data={"group_name": "farm-a", "site_name": "farm",
                                 "description": "test", "color_tag": "emerald"},
                follow_redirects=False)
assert r.status_code in (303, 307), r.text
g = fetch_one("SELECT * FROM device_groups WHERE owner_tapis_username=?", ("smoke_user",))
assert g and g["group_uid"].startswith("dg_"), dict(g)

r = client.post("/devices", data={
    "device_name": "Jetson Smoke",
    "device_alias": "smoke-001",
    "generation_uid": "jetson-orin-nano-v1",
    "group_uid": g["group_uid"],
    "site_name": "farm",
}, follow_redirects=False)
assert r.status_code in (303, 307), r.text
d = fetch_one("SELECT * FROM devices WHERE owner_tapis_username=?", ("smoke_user",))
assert d and d["device_uid"].startswith("dev_"), dict(d)

# --------------------------------------------------------------------------- #
# Installer + enrollment                                                      #
# --------------------------------------------------------------------------- #
r = client.get(f"/devices/{d['device_uid']}/installer")
assert r.status_code == 200, r.text
content = r.content
assert len(content) > 0

with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "installer.tar.gz"
    p.write_bytes(content)
    with tarfile.open(p, "r:gz") as tf:
        names = tf.getnames()
        assert "icicle-edge-agent/config/enrollment.json" in names
        f = tf.extractfile("icicle-edge-agent/config/enrollment.json")
        enrollment = json.loads(f.read())
        token = enrollment["enrollment_token"]
        assert "icicle-edge-agent/agent/main.py" in names

r = client.post("/api/agent/enroll", json={
    "enrollment_token": token,
    "hostname": "smoke-host",
    "ip_address": "10.0.0.5",
    "agent_version": "test"
})
assert r.status_code == 200, r.text
enroll_body = r.json()
cfg = enroll_body["device_config"]
device_api_key = enroll_body.get("device_api_key")
assert device_api_key and device_api_key.startswith("dkey_"), enroll_body
assert cfg["device_id"] == d["device_uid"]

agent_headers = {"Authorization": f"Bearer {device_api_key}"}

r = client.post("/api/agent/heartbeat", json={
    "device_id": d["device_uid"],
    "status": "ONLINE",
    "cpu_percent": 17.5,
    "memory_used_mb": 2048,
    "active_containers": [],
})
assert r.status_code == 401, "heartbeat without API key should be rejected"

r = client.post("/api/agent/heartbeat", json={
    "device_id": d["device_uid"],
    "status": "ONLINE",
    "cpu_percent": 17.5,
    "memory_used_mb": 2048,
    "active_containers": [],
}, headers=agent_headers)
assert r.status_code == 200, r.text

# --------------------------------------------------------------------------- #
# Publish a fresh app via the web form
# --------------------------------------------------------------------------- #
form = {
    "display_name": "Smoke Detector",
    "version": "0.1.0",
    "visibility": "private",
    "status": "PUBLISHED",
    "task_type": "classification",
    "framework": "onnx",
    "description": "Test model.",
    "license": "MIT",
    "tags": "test, smoke",
    "patra_model_card_uuid": "11111111-2222-3333-4444-555555555555",
    "raw_docker_command": (
        "docker run -d --name smoke_test_container --runtime nvidia --gpus all "
        "-e MODEL_PATH=/models/smoke.onnx -e FOO=bar "
        "-v /local/models:/models:ro library/hello-world:latest"
    ),
    "artifact_filename": "smoke.onnx",
    "artifact_container_path": "/models/smoke.onnx",
    "artifact_source_type": "url",
    "artifact_download_url": "http://example.com/smoke.onnx",
    "spec_image_repository": "library/hello-world",
    "spec_image_tag": "latest",
    "spec_container_name": "smoke_test_container",
    "spec_pull_policy": "if_not_present",
    "spec_restart_policy": "no",
    "spec_model_env_var": "MODEL_PATH",
    "env_key": ["FOO", "MODEL_PATH"],
    "env_value": ["bar", "/models/smoke.onnx"],
    # The "wrong" host path will be rewritten to ${DEPLOYMENT_DIR}/model
    # because the target /models is a prefix of /models/smoke.onnx.
    "mount_source": ["/local/models"],
    "mount_target": ["/models"],
    "mount_style": ["volume"],
    "mount_type": ["bind"],
    "mount_mode": ["ro"],
    "compat_generation_uid": ["jetson-orin-nano-v1"],
}
r = client.post("/apps", data=form, follow_redirects=False)
assert r.status_code in (303, 307), r.text
created = fetch_one("SELECT * FROM model_cards WHERE display_name='Smoke Detector' AND version='0.1.0'")
assert created and created["status"] == "PUBLISHED", dict(created or {})
assert created["app_id"] and created["app_id"].startswith("app_"), dict(created)
assert created["patra_model_card_uuid"] == "11111111-2222-3333-4444-555555555555", dict(created)
assert "docker run" in (created["raw_docker_command"] or ""), dict(created)

# The model mount on the device must be the templated path the agent expects.
created_mount = fetch_one(
    """
    SELECT m.* FROM container_spec_mounts m
      JOIN container_specs cs ON cs.spec_uid = m.spec_uid
     WHERE cs.model_card_uid = ?
       AND m.target = ?
    """,
    (created["model_card_uid"], "/models"),
)
assert created_mount, "Model mount missing for the new card"
assert created_mount["source"] == "${DEPLOYMENT_DIR}/model", dict(created_mount)
assert created_mount["mode"] == "ro", dict(created_mount)

# Multiple apps may share the same Patra UUID (different container images).
dup_form = dict(form)
dup_form["display_name"] = "Smoke Detector Alt"
dup_form["version"] = "0.2.0"
dup_form["spec_container_name"] = "smoke_test_container_2"
r = client.post("/apps", data=dup_form, follow_redirects=False)
assert r.status_code in (303, 307), f"Same Patra UUID should be allowed: {r.status_code}: {r.text[:300]}"
dup = fetch_one("SELECT * FROM model_cards WHERE display_name='Smoke Detector Alt'")
assert dup and dup["patra_model_card_uuid"] == "11111111-2222-3333-4444-555555555555", dict(dup or {})
assert dup["app_id"] != created["app_id"], "Each app gets its own app_id"

# /api/models/parse-command parses a raw command and returns the rewritten
# model mount source.
r = client.post("/api/models/parse-command", json={
    "command": (
        "docker run -d --name infer --runtime nvidia --gpus all "
        "-e ENGINE_PATH=/workspace/models/m.engine "
        "-v /local/path:/workspace/models:ro org/img:latest"
    ),
    "model_container_path": "/workspace/models/m.engine",
    "model_env_var": "ENGINE_PATH",
})
assert r.status_code == 200, r.text
parsed = r.json()
mounts = parsed["spec"]["mounts"]
model_mounts = [m for m in mounts if m["target"] == "/workspace/models"]
assert model_mounts and model_mounts[0]["source"] == "${DEPLOYMENT_DIR}/model", mounts
assert "docker run" in parsed["preview_command"], parsed["preview_command"]

# --------------------------------------------------------------------------- #
# App visibility + ownership (multi-user authorization)                       #
# --------------------------------------------------------------------------- #
users_repo.ensure("other_user")
_other_form = dict(form)
_other_form.update({
    "display_name": "Other User Private",
    "version": "9.9.9",
    "visibility": "private",
    "status": "PUBLISHED",
    "spec_container_name": "other_private_container",
})
other_private = model_service.create_from_payload(
    "other_user", model_service.build_payload_from_form(_other_form),
)
_other_public_form = dict(form)
_other_public_form.update({
    "display_name": "Other User Public",
    "version": "9.9.8",
    "visibility": "public",
    "status": "PUBLISHED",
    "spec_container_name": "other_public_container",
})
other_public = model_service.create_from_payload(
    "other_user", model_service.build_payload_from_form(_other_public_form),
)

visible_uids = {m["model_card_uid"] for m in client.get("/api/apps").json()}
assert other_public["model_card_uid"] in visible_uids, "public app from another user should be listed"
assert other_private["model_card_uid"] not in visible_uids, "private app from another user should be hidden"

r = client.get(f"/api/apps/{other_private['model_card_uid']}")
assert r.status_code == 403, f"private app detail should be forbidden, got {r.status_code}"

r = client.get(f"/api/apps/{other_public['model_card_uid']}")
assert r.status_code == 200, r.text

r = client.post(f"/apps/{other_private['model_card_uid']}/delete", follow_redirects=False)
assert r.status_code == 403, "cannot delete another user's app"

# --------------------------------------------------------------------------- #
# Deploy the seeded model card to the device                                  #
# --------------------------------------------------------------------------- #
r = client.post("/deployments", data={
    "model_card_uid": "mc_seed_unetpp",
    "target_type": "DEVICE",
    "target_uid": d["device_uid"],
    "notes": "smoke trial",
}, follow_redirects=False)
assert r.status_code in (303, 307), r.text
dep = fetch_one("SELECT * FROM deployments WHERE owner_tapis_username=? ORDER BY created_at DESC", ("smoke_user",))
assert dep and dep["status"] in {"PENDING", "RECORDED", "DELIVERING"}, dict(dep)

cmd = fetch_one("SELECT * FROM device_commands WHERE deployment_uid=? AND operation='deploy_model'", (dep["deployment_uid"],))
assert cmd, "deploy_model command not recorded"
payload = json.loads(cmd["payload_json"])
assert payload["operation"] == "deploy_model"
assert payload["artifact"]["filename"] == "unetpp_teacher_512x768_fp16.engine"
assert payload["container"]["container_name"] == "unetpp_infer"

# Group deploy
r = client.post("/deployments", data={
    "model_card_uid": "mc_seed_unetpp",
    "target_type": "GROUP",
    "target_uid": g["group_uid"],
}, follow_redirects=False)
assert r.status_code in (303, 307), r.text

# --------------------------------------------------------------------------- #
# Agent ACK back -> device-level row updated, deployment marked RUNNING       #
# --------------------------------------------------------------------------- #
r = client.post("/api/agent/ack", json={
    "device_id": d["device_uid"],
    "request_id": cmd["request_id"],
    "deployment_uid": dep["deployment_uid"],
    "operation": "deploy_model",
    "status": "RUNNING",
    "container_id": "abcd1234",
    "container_name": payload["container"]["container_name"],
}, headers=agent_headers)
assert r.status_code == 200, r.text
dd = fetch_one("SELECT * FROM device_deployments WHERE deployment_uid=? AND device_uid=?",
               (dep["deployment_uid"], d["device_uid"]))
assert dd and dd["status"] == "RUNNING", dict(dd or {})

# Soft stop — keeps image and artifacts on device
r = client.post(f"/deployments/{dep['deployment_uid']}/stop", follow_redirects=False)
assert r.status_code in (303, 307), r.text
stop_cmd = fetch_one(
    "SELECT * FROM device_commands WHERE deployment_uid=? AND operation='stop_deployment' ORDER BY created_at DESC LIMIT 1",
    (dep["deployment_uid"],),
)
assert stop_cmd, "stop_deployment command not recorded"
stop_payload = json.loads(stop_cmd["payload_json"])
assert stop_payload.get("purge") is False, stop_payload

r = client.post("/api/agent/ack", json={
    "device_id": d["device_uid"],
    "request_id": stop_cmd["request_id"],
    "deployment_uid": dep["deployment_uid"],
    "operation": "stop_deployment",
    "status": "STOPPED",
}, headers=agent_headers)
assert r.status_code == 200, r.text
dep_after = fetch_one("SELECT * FROM deployments WHERE deployment_uid=?", (dep["deployment_uid"],))
assert dep_after and dep_after["status"] == "STOPPED", dict(dep_after or {})

# Restart from cached artifacts
r = client.post(f"/deployments/{dep['deployment_uid']}/restart", follow_redirects=False)
assert r.status_code in (303, 307), r.text
restart_cmd = fetch_one(
    "SELECT * FROM device_commands WHERE deployment_uid=? AND operation='restart_deployment' ORDER BY created_at DESC LIMIT 1",
    (dep["deployment_uid"],),
)
assert restart_cmd, "restart_deployment command not recorded"

r = client.post("/api/agent/ack", json={
    "device_id": d["device_uid"],
    "request_id": restart_cmd["request_id"],
    "deployment_uid": dep["deployment_uid"],
    "operation": "restart_deployment",
    "status": "RUNNING",
    "container_id": "abcd1234",
    "container_name": payload["container"]["container_name"],
}, headers=agent_headers)
assert r.status_code == 200, r.text
dep_running = fetch_one("SELECT * FROM deployments WHERE deployment_uid=?", (dep["deployment_uid"],))
assert dep_running and dep_running["status"] == "RUNNING", dict(dep_running or {})

# Purge — full cleanup
r = client.post(f"/deployments/{dep['deployment_uid']}/stop", data={"purge": "on"}, follow_redirects=False)
assert r.status_code in (303, 307), r.text
purge_cmd = fetch_one(
    "SELECT * FROM device_commands WHERE deployment_uid=? AND operation='stop_deployment' ORDER BY created_at DESC LIMIT 1",
    (dep["deployment_uid"],),
)
assert purge_cmd, "purge stop_deployment command not recorded"
purge_payload = json.loads(purge_cmd["payload_json"])
assert purge_payload.get("purge") is True, purge_payload

# Agent acks the purge
r = client.post("/api/agent/ack", json={
    "device_id": d["device_uid"],
    "request_id": purge_cmd["request_id"],
    "deployment_uid": dep["deployment_uid"],
    "operation": "stop_deployment",
    "status": "STOPPED",
}, headers=agent_headers)
assert r.status_code == 200, r.text
dep_purged = fetch_one("SELECT * FROM deployments WHERE deployment_uid=?", (dep["deployment_uid"],))
assert dep_purged and dep_purged["status"] == "STOPPED", dict(dep_purged or {})

# Stuck STOPPING — retry stop, dismiss, and remove
r = client.post("/deployments", data={
    "model_card_uid": "mc_seed_unetpp",
    "target_type": "DEVICE",
    "target_uid": d["device_uid"],
}, follow_redirects=False)
assert r.status_code in (303, 307), r.text
dep2 = fetch_one(
    "SELECT * FROM deployments WHERE owner_tapis_username=? ORDER BY created_at DESC LIMIT 1",
    ("smoke_user",),
)
deploy_cmd = fetch_one(
    "SELECT * FROM device_commands WHERE deployment_uid=? AND operation='deploy_model' ORDER BY created_at DESC LIMIT 1",
    (dep2["deployment_uid"],),
)
client.post("/api/agent/ack", json={
    "device_id": d["device_uid"],
    "request_id": deploy_cmd["request_id"],
    "deployment_uid": dep2["deployment_uid"],
    "operation": "deploy_model",
    "status": "RUNNING",
    "container_id": "abcd1234",
    "container_name": "unetpp_infer",
}, headers=agent_headers)
client.post(f"/deployments/{dep2['deployment_uid']}/stop", follow_redirects=False)
execute(
    "UPDATE deployments SET status='STOPPING' WHERE deployment_uid=?",
    (dep2["deployment_uid"],),
)
execute(
    "UPDATE device_deployments SET status='STOPPING' WHERE deployment_uid=?",
    (dep2["deployment_uid"],),
)
dep_stopping = fetch_one("SELECT status FROM deployments WHERE deployment_uid=?", (dep2["deployment_uid"],))
assert dep_stopping and dep_stopping["status"] == "STOPPING", dict(dep_stopping)

r = client.post(f"/deployments/{dep2['deployment_uid']}/stop", follow_redirects=False)
assert r.status_code in (303, 307), r.text
stop_count = fetch_one(
    "SELECT COUNT(*) c FROM device_commands WHERE deployment_uid=? AND operation='stop_deployment'",
    (dep2["deployment_uid"],),
)
assert stop_count and stop_count["c"] >= 2, "retry stop should record another command"

r = client.post(f"/deployments/{dep2['deployment_uid']}/dismiss", follow_redirects=False)
assert r.status_code in (303, 307), r.text
dep_dismissed = fetch_one("SELECT status FROM deployments WHERE deployment_uid=?", (dep2["deployment_uid"],))
assert dep_dismissed and dep_dismissed["status"] == "STOPPED", dep_dismissed

r = client.post(f"/deployments/{dep2['deployment_uid']}/cancel", follow_redirects=False)
assert r.status_code in (303, 307), r.text
dep_cancelled = fetch_one("SELECT status FROM deployments WHERE deployment_uid=?", (dep2["deployment_uid"],))
assert dep_cancelled and dep_cancelled["status"] == "CANCELLED", dep_cancelled
cancel_purge = fetch_one(
    "SELECT * FROM device_commands WHERE deployment_uid=? AND operation='stop_deployment' ORDER BY created_at DESC LIMIT 1",
    (dep2["deployment_uid"],),
)
assert cancel_purge, "cancel should dispatch a purge command"
assert json.loads(cancel_purge["payload_json"]).get("purge") is True, cancel_purge

# --------------------------------------------------------------------------- #
# Compatibility filtering — incompatible target should be rejected            #
# --------------------------------------------------------------------------- #
client.post("/devices", data={
    "device_name": "Nano Old",
    "device_alias": "smoke-002",
    "generation_uid": "jetson-nano-v1",
    "site_name": "",
}, follow_redirects=False)
old = fetch_one("SELECT * FROM devices WHERE device_alias='smoke-002'")
assert old
r = client.post("/deployments", data={
    "model_card_uid": "mc_seed_unetpp",
    "target_type": "DEVICE",
    "target_uid": old["device_uid"],
}, follow_redirects=False)
assert r.status_code == 422, f"expected 422 for incompatible target, got {r.status_code}: {r.text}"

# --------------------------------------------------------------------------- #
# Edit app (after deploy) + delete device                                     #
# --------------------------------------------------------------------------- #
edit_form = dict(form)
edit_form["display_name"] = "Smoke Detector Updated"
r = client.post(f"/apps/{created['model_card_uid']}/edit", data=edit_form, follow_redirects=False)
assert r.status_code in (303, 307), f"edit app failed: {r.status_code} {r.text[:400]}"
updated = fetch_one("SELECT display_name FROM model_cards WHERE model_card_uid=?", (created["model_card_uid"],))
assert updated and "Updated" in updated["display_name"], dict(updated or {})

r = client.post(f"/devices/{d['device_uid']}/delete", follow_redirects=False)
assert r.status_code in (303, 307), f"delete device failed: {r.status_code} {r.text[:400]}"
assert not fetch_one("SELECT 1 FROM devices WHERE device_uid=?", (d["device_uid"],))

# --------------------------------------------------------------------------- #
# Pages render                                                                 #
# --------------------------------------------------------------------------- #
for path in [
    "/",
    "/devices",
    "/groups",
    f"/groups/{g['group_uid']}",
    "/hardware",
    "/apps",
    "/apps/new",
    "/apps/compatibility",
    "/apps/mc_seed_unetpp",
    "/deployments?scope=all",
    f"/deployments/{dep['deployment_uid']}",
    "/operations/commands",
    "/operations/events",
    "/operations/mqtt",
]:
    r = client.get(path)
    assert r.status_code == 200, f"{path} → {r.status_code}\n{r.text[:400]}"

print("SMOKE TEST PASSED")
