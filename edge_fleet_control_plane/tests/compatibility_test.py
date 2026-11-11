"""Deploy-time compatibility: compatible requests are accepted, incompatible ones rejected.

Each scenario registers and enrolls a device through the real API, has it
report its platform by heartbeat (as agent v2.2.0 does), then attempts a
deployment through ``POST /deployments``. A rejected request must leave no
deployment or command behind.

Run with:
    ALLOW_LOCALHOST_INSTALLER=true APP_BASE_URL=http://testserver MQTT_ENABLED=false \\
        python tests/compatibility_test.py
"""

import json
import os
import sys
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_PATH", str(ROOT / "data" / "compatibility_test.db"))
os.environ.setdefault("LOCAL_DEV_AUTH", "true")
os.environ.setdefault("LOCAL_DEV_USERNAME", "compat_user")
os.environ.setdefault("APP_BASE_URL", "http://testserver")
os.environ.setdefault("MQTT_ENABLED", "false")

db_path = Path(os.environ["DATABASE_PATH"])
if db_path.exists():
    db_path.unlink()

from app.config import get_settings  # noqa: E402
get_settings.cache_clear()
from app.db.migrations import run_migrations  # noqa: E402
from app.db.session import fetch_one  # noqa: E402
from app.main import app  # noqa: E402
from app.services import model_service  # noqa: E402
from app.services.seed_service import seed_everything  # noqa: E402

run_migrations()
seed_everything()
client = TestClient(app)
OWNER = os.environ["LOCAL_DEV_USERNAME"]

UNETPP = "mc_seed_unetpp"                    # Orin engine: sm 8.7, TensorRT 10.3, L4T 36.4
FAUX_WEED = "mc_seed_yolo_faux_weed_mvs_live"  # Xavier JP4 engine: sm 7.2, TensorRT 7, L4T 32
SCANBOT = "mc_seed_scanbot_dgx_recon"          # DGX Spark service: no weights file, no engine target

ORIN_JP5 = {"machine": "aarch64", "jetson_model": "NVIDIA Jetson Orin Nano Developer Kit",
            "gpu_compute_capability": "8.7", "l4t_version": "35.4.1",
            "tensorrt_version": "8.5.2.2", "memory_mb": 7620, "disk_free_gb": 40.0,
            "nvidia_runtime": True}
ORIN_JP6 = {**ORIN_JP5, "jetson_model": "NVIDIA Jetson Orin Nano Engineering Reference Developer Kit Super",
            "l4t_version": "36.4.7", "tensorrt_version": "10.3.0.30"}
XAVIER_JP4 = {"machine": "aarch64", "jetson_model": "NVIDIA Jetson AGX Xavier Developer Kit",
              "gpu_compute_capability": "7.2", "l4t_version": "32.7.1",
              "tensorrt_version": "7.1.3.0", "memory_mb": 31002, "disk_free_gb": 120.0,
              "nvidia_runtime": True}
SPARK = {"machine": "aarch64", "gpu_compute_capability": "12.1",
         "memory_mb": 128000, "disk_free_gb": 800.0, "nvidia_runtime": True}


def make_device(alias, generation, platform=None, camera_bus="csi", group_uid=""):
    r = client.post("/devices", data={
        "device_name": alias, "device_alias": alias, "generation_uid": generation,
        "group_uid": group_uid, "camera_bus": camera_bus, "camera_count": 1,
    }, follow_redirects=False)
    assert r.status_code in (303, 307), r.text
    device = fetch_one("SELECT * FROM devices WHERE device_alias = ?", (alias,))
    r = client.get(f"/devices/{device['device_uid']}/installer")
    assert r.status_code == 200, r.text
    with tempfile.TemporaryDirectory() as td:
        bundle = Path(td) / "installer.tar.gz"
        bundle.write_bytes(r.content)
        with tarfile.open(bundle, "r:gz") as tf:
            token = json.loads(tf.extractfile("icicle-edge-agent/config/enrollment.json").read())["enrollment_token"]
    r = client.post("/api/agent/enroll", json={"enrollment_token": token, "hostname": alias})
    assert r.status_code == 200, r.text
    heartbeat = {"device_id": device["device_uid"], "status": "ONLINE", "active_containers": []}
    if platform is not None:
        heartbeat["platform"] = platform
    r = client.post("/api/agent/heartbeat", json=heartbeat,
                    headers={"Authorization": f"Bearer {r.json()['device_api_key']}"})
    assert r.status_code == 200, r.text
    return device["device_uid"]


def counts():
    deployments = fetch_one("SELECT COUNT(*) AS c FROM deployments")["c"]
    commands = fetch_one("SELECT COUNT(*) AS c FROM device_commands")["c"]
    return deployments, commands


r = client.post("/groups", data={"group_name": "mixed-orin", "site_name": "lab"}, follow_redirects=False)
assert r.status_code in (303, 307), r.text
mixed_group = fetch_one("SELECT group_uid FROM device_groups WHERE group_name = 'mixed-orin'")["group_uid"]

orin_jp5 = make_device("orin-jp5", "jetson-orin-nano-v1", ORIN_JP5)
orin_jp6 = make_device("orin-jp6", "jetson-orin-nano-v1", ORIN_JP6)
orin_silent = make_device("orin-no-platform", "jetson-orin-nano-v1", None)
orin_full = make_device("orin-disk-full", "jetson-orin-nano-v1", {**ORIN_JP6, "disk_free_gb": 2.0})
xavier_nx = make_device("xavier-nx-jp5", "jetson-xavier-nx-v1",
                        {**ORIN_JP5, "jetson_model": "NVIDIA Jetson Xavier NX", "gpu_compute_capability": "7.2"})
agx_xavier = make_device("agx-xavier-gige", "jetson-agx-xavier-jp4-v1", XAVIER_JP4, camera_bus="gige-mvs")
agx_xavier_csi = make_device("agx-xavier-csi", "jetson-agx-xavier-jp4-v1", XAVIER_JP4, camera_bus="csi")
agx_no_runtime = make_device("agx-xavier-no-runtime", "jetson-agx-xavier-jp4-v1",
                             {**XAVIER_JP4, "nvidia_runtime": False}, camera_bus="gige-mvs")
make_device("group-orin-jp5", "jetson-orin-nano-v1", ORIN_JP5, group_uid=mixed_group)
make_device("group-orin-jp6", "jetson-orin-nano-v1", ORIN_JP6, group_uid=mixed_group)
spark = make_device("spark-gb10", "dgx-spark-gb10-v1", SPARK)
spark_low_disk = make_device("spark-disk-full", "dgx-spark-gb10-v1", {**SPARK, "disk_free_gb": 4.0})
spark_no_runtime = make_device("spark-no-runtime", "dgx-spark-gb10-v1", {**SPARK, "nvidia_runtime": False})

# The scan token and capture host belong to the station, not the app card.
station_env_on_card = fetch_one(
    """
    SELECT COUNT(*) AS c
      FROM container_spec_env e
      JOIN container_specs s ON s.spec_uid = e.spec_uid
     WHERE s.model_card_uid = ? AND e.var_key IN ('SCANBOT_TOKEN', 'SCANBOT_PULL_HOST')
    """,
    (SCANBOT,),
)
assert station_env_on_card["c"] == 0, station_env_on_card
assert fetch_one("SELECT app_kind FROM model_cards WHERE model_card_uid = ?", (SCANBOT,))["app_kind"] == "scanbot"
spark_caps = json.loads(fetch_one("SELECT capabilities_json FROM devices WHERE device_uid = ?", (spark,))["capabilities_json"])
assert spark_caps["scan_station"]["capture_host"] == "jetson", spark_caps
assert len(spark_caps["scan_station"]["token"]) >= 16, spark_caps
assert spark_caps["camera_count"] == 0, spark_caps
scanbot_artifact = fetch_one(
    "SELECT source_type, download_url FROM model_artifacts WHERE model_card_uid = ?",
    (SCANBOT,),
)
assert scanbot_artifact["source_type"] == "none" and not scanbot_artifact["download_url"], scanbot_artifact

no_compat_card = model_service.create_from_payload(OWNER, model_service.build_payload_from_form({
    "display_name": "No Compatibility Declared", "version": "0.1.0", "visibility": "private",
    "status": "PUBLISHED", "framework": "onnx",
    "artifact_filename": "m.onnx", "artifact_container_path": "/models/m.onnx",
    "artifact_source_type": "url", "artifact_download_url": "http://example.com/m.onnx",
    "spec_image_repository": "library/hello-world", "spec_image_tag": "latest",
    "spec_container_name": "no_compat", "spec_pull_policy": "if_not_present",
    "spec_restart_policy": "no",
}))["model_card_uid"]

# (scenario, card, target type, target uid, expected reason or None for accepted)
SCENARIOS = [
    ("Orin engine -> Orin Nano, matching JetPack 6", UNETPP, "DEVICE", orin_jp6, None),
    ("Xavier JP4 engine -> AGX Xavier JP4, GigE", FAUX_WEED, "DEVICE", agx_xavier, None),
    ("Orin engine -> Orin Nano on JetPack 5", UNETPP, "DEVICE", orin_jp5, "TensorRT 10.3, device has TensorRT 8.5"),
    ("Orin engine -> Xavier NX (allowed generation)", UNETPP, "DEVICE", xavier_nx, "GPU sm 8.7, device GPU is sm 7.2"),
    ("Orin engine -> AGX Xavier", UNETPP, "DEVICE", agx_xavier, "is not in the app"),
    ("Xavier JP4 engine -> Orin Nano", FAUX_WEED, "DEVICE", orin_jp5, "is not in the app"),
    ("Orin engine -> device with no platform report", UNETPP, "DEVICE", orin_silent, "has not reported its platform"),
    ("Orin engine -> Orin Nano with 2 GB free", UNETPP, "DEVICE", orin_full, "GB free disk"),
    ("GigE app -> AGX Xavier with CSI camera", FAUX_WEED, "DEVICE", agx_xavier_csi, "GigE (MVS) cameras"),
    ("CUDA app -> device without NVIDIA runtime", FAUX_WEED, "DEVICE", agx_no_runtime, "NVIDIA container runtime"),
    ("App with no compatible generations", no_compat_card, "DEVICE", orin_jp5, "declares no compatible device generations"),
    ("Group: one JetPack 5 + one JetPack 6 Orin", UNETPP, "GROUP", mixed_group, "group-orin-jp5"),
    ("Scanbot service -> DGX Spark", SCANBOT, "DEVICE", spark, None),
    ("Scanbot service -> Orin Nano", SCANBOT, "DEVICE", orin_jp6, "is not in the app"),
    ("Scanbot service -> Spark with 4 GB free", SCANBOT, "DEVICE", spark_low_disk, "GB free disk"),
    ("Scanbot service -> Spark without NVIDIA runtime", SCANBOT, "DEVICE", spark_no_runtime, "NVIDIA container runtime"),
    ("Live camera app -> scan station", no_compat_card, "DEVICE", spark, "declares no compatible device generations"),
]

results = []
for name, card, target_type, target_uid, expected in SCENARIOS:
    before = counts()
    r = client.post("/deployments", data={
        "model_card_uid": card, "target_type": target_type, "target_uid": target_uid,
    }, follow_redirects=False)
    accepted = r.status_code in (303, 307)
    if expected is None:
        assert accepted, f"{name}: expected accept, got {r.status_code}: {r.text[:300]}"
        assert counts()[0] == before[0] + 1, f"{name}: deployment row missing"
        results.append((name, "accepted", ""))
    else:
        assert not accepted and 400 <= r.status_code < 500, f"{name}: expected reject, got {r.status_code}"
        assert expected in r.text, f"{name}: expected {expected!r} in {r.text[:400]}"
        assert counts() == before, f"{name}: rejected request left rows behind"
        results.append((name, "rejected", expected))

width = max(len(n) for n, _, _ in results)
print(f"\n{'scenario':{width}}  outcome   reason")
for name, outcome, reason in results:
    print(f"{name:{width}}  {outcome:8}  {reason}")

# The station's token and capture host are injected at deploy time; no camera env.
deploy_cmd = fetch_one(
    "SELECT payload_json FROM device_commands WHERE device_uid = ? AND operation = 'deploy_model'",
    (spark,),
)
deploy_payload = json.loads(deploy_cmd["payload_json"])
env = {e["key"]: e for e in deploy_payload["runtime"]["environment"]}
assert deploy_payload["model"]["app_kind"] == "scanbot", deploy_payload["model"]
assert env["SCANBOT_TOKEN"]["value"] == spark_caps["scan_station"]["token"] and env["SCANBOT_TOKEN"]["is_secret"]
assert env["SCANBOT_PULL_HOST"]["value"] == "jetson"
assert not any(k.startswith("STREAM_INGEST_URL") for k in env), sorted(env)
ssh_mounts = [m for m in deploy_payload["runtime"]["mounts"] if m["target"] == "/home/scanbot/.ssh-mount"]
assert ssh_mounts == [{"source": spark_caps["scan_station"]["ssh_dir"], "target": "/home/scanbot/.ssh-mount",
                       "style": "volume", "type": "bind", "mode": "ro"}], ssh_mounts
assert env["SCANBOT_SSH_DIR"]["value"] == spark_caps["scan_station"]["ssh_dir"]

# Browser Deploy posts redirect_to; the portal must toast, not return JSON.
before = counts()
r = client.post("/deployments", data={
    "model_card_uid": UNETPP, "target_type": "DEVICE", "target_uid": orin_jp5,
    "redirect_to": f"/devices/{orin_jp5}",
}, follow_redirects=False)
assert r.status_code == 303, r.text
loc = urlparse(r.headers["location"])
qs = parse_qs(loc.query)
assert loc.path == f"/devices/{orin_jp5}"
assert qs.get("level") == ["danger"]
assert "TensorRT 10.3" in unquote(qs["notice"][0])
assert counts() == before

print("\nCOMPATIBILITY TEST PASSED")
