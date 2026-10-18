"""Simulate a fleet of agents against a real control-plane process.

Starts the portal (uvicorn) on a throwaway SQLite database, registers and
enrolls N fake Orin Nano devices in one group through the real HTTP API, then
issues one group Deploy and lets N agent threads do what agent v2.4 does on the
wire: pick up ``deploy_model`` from ``pending_commands`` on heartbeat, ack
DOWNLOADING / PULLING / STARTING / RUNNING, then heartbeat with the container.

Measured per run (control plane only; no Docker, no GPU, no camera):

* ``dispatch_s``      — POST /deployments wall time: compatibility check for all
                        N devices, row creation, and the per-device publish loop.
* ``all_running_s``   — Deploy click until the last device's RUNNING ack is in.
* ``overhead_s``      — ``all_running_s`` minus the simulated device work.
* ack latency, portal CPU seconds, and portal peak RSS during the run.

Device work is a sleep (``--device-seconds``). The default 3.8 s is the median
manual ``docker run`` on the Orin (warm, 2026-10-17). The modeled SSH columns
are arithmetic, not measured: a sequential SSH loop pays the device work once
per board; an ideal parallel SSH run pays it once.

Agents poll ``pending_commands`` every ``--poll-seconds`` (default 1 s) instead
of the real 30 s heartbeat, so pickup delay does not swamp the control-plane
cost. With ``--mqtt`` the portal also publishes to the broker configured in
``.env``; agents still read the command over HTTP.

With ``--mqtt-agents`` (implies ``--mqtt``) each agent instead holds its own
TLS connection to the broker, subscribes to ``commands/device/{uid}`` at QoS 1
like the real agent, and takes the command from the MQTT push. Heartbeat
polling is only a fallback after ``--mqtt-fallback-seconds`` without a message.
Pickup times then include broker delivery. Sessions are clean, so the broker
keeps no state for the fake devices afterwards.

Simulator results support control-plane scaling only, not fleet health.

    python eval/simulate_fleet.py                       # 10, 50, 100 x 3 repeats
    python eval/simulate_fleet.py --sizes 10 --repeats 1 --device-seconds 0
    python eval/simulate_fleet.py --mqtt
    python eval/simulate_fleet.py --mqtt-agents
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import random
import resource
import socket
import statistics
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

import psutil
import requests

ROOT = Path(__file__).resolve().parents[1]
CARD = "mc_seed_unetpp_live"
GENERATION = "jetson-orin-nano-v1"
OWNER = "fleet_sim"
ORIN_JP6 = {
    "machine": "aarch64",
    "jetson_model": "NVIDIA Jetson Orin Nano Engineering Reference Developer Kit Super",
    "gpu_compute_capability": "8.7",
    "l4t_version": "36.4.7",
    "tensorrt_version": "10.3.0.30",
    "memory_mb": 7620,
    "disk_free_gb": 40.0,
    "nvidia_runtime": True,
}

COLUMNS = [
    "n", "repeat", "mqtt", "mqtt_agents", "device_seconds", "poll_seconds",
    "dispatch_s", "first_pickup_s", "last_pickup_s", "all_running_s", "overhead_s",
    "picked_up_via_mqtt", "mqtt_connect_failures",
    "running_rows", "timeouts", "ack_p50_ms", "ack_p90_ms", "ack_max_ms",
    "server_cpu_s", "server_peak_rss_mb",
    "modeled_sequential_ssh_s", "modeled_parallel_ssh_s",
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pct(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


def start_portal(db_path: Path, port: int, mqtt: bool, log_file) -> subprocess.Popen:
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env.update({
        "DATABASE_PATH": str(db_path),
        "APP_DEBUG": "false",
        "DATABASE_URL": "",
        "DB_HOST": "",
        "LOCAL_DEV_AUTH": "true",
        "LOCAL_DEV_USERNAME": OWNER,
        "APP_BASE_URL": base,
        "ALLOW_LOCALHOST_INSTALLER": "true",
        "MQTT_ENABLED": "true" if mqtt else "false",
        "DEVICE_OFFLINE_AFTER_SECONDS": "3600",
        "HEARTBEAT_HISTORY_MODE": "sample",
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning", "--no-access-log"],
        cwd=str(ROOT), env=env, stdout=log_file, stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("portal exited during startup; see the portal log")
        try:
            if requests.get(base + "/api/health", timeout=1).ok:
                return proc
        except requests.RequestException:
            pass
        time.sleep(0.3)
    proc.terminate()
    raise RuntimeError("portal did not become healthy within 60 s")


def _location_uid(resp: requests.Response, prefix: str) -> str:
    loc = resp.headers.get("location") or ""
    if resp.status_code not in (303, 307) or prefix not in loc:
        raise RuntimeError(f"unexpected response {resp.status_code}: {loc or resp.text[:300]}")
    return loc.split(prefix, 1)[1].split("?", 1)[0].split("/", 1)[0]


def create_group(base: str, name: str) -> str:
    r = requests.post(base + "/groups", data={"group_name": name, "site_name": "sim"},
                      allow_redirects=False, timeout=30)
    return _location_uid(r, "/groups/")


def register_device(base: str, alias: str, group_uid: str) -> Dict[str, str]:
    r = requests.post(base + "/devices", data={
        "device_name": alias, "device_alias": alias, "generation_uid": GENERATION,
        "group_uid": group_uid, "camera_bus": "csi", "camera_count": 1,
    }, allow_redirects=False, timeout=30)
    uid = _location_uid(r, "/devices/")
    r = requests.get(f"{base}/devices/{uid}/installer", timeout=30)
    r.raise_for_status()
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tf:
        token = json.loads(
            tf.extractfile("icicle-edge-agent/config/enrollment.json").read()
        )["enrollment_token"]
    r = requests.post(base + "/api/agent/enroll", json={
        "enrollment_token": token, "hostname": alias, "agent_version": "fleet-sim",
    }, timeout=30)
    r.raise_for_status()
    key = r.json()["device_api_key"]
    r = requests.post(base + "/api/agent/heartbeat", json={
        "device_id": uid, "status": "ONLINE", "active_containers": [],
        "docker_running": True, "platform": ORIN_JP6, "agent_version": "fleet-sim",
    }, headers={"Authorization": f"Bearer {key}"}, timeout=30)
    r.raise_for_status()
    return {"uid": uid, "key": key}


class FakeAgent(threading.Thread):
    def __init__(self, base: str, device: Dict[str, str], *, go: threading.Event,
                 poll_s: float, device_s: float, timeout_s: float,
                 mqtt_cfg=None, mqtt_fallback_s: float = 30.0) -> None:
        super().__init__(daemon=True)
        self.base = base
        self.uid = device["uid"]
        self.headers = {"Authorization": f"Bearer {device['key']}"}
        self.go = go
        self.poll_s = poll_s
        self.device_s = device_s
        self.timeout_s = timeout_s
        self.mqtt_cfg = mqtt_cfg
        self.mqtt_fallback_s = mqtt_fallback_s
        self.ready = threading.Event()
        self.mqtt_failed = False
        self._client = None
        self._pushed: Optional[dict] = None
        self._pushed_event = threading.Event()
        self.t0 = 0.0
        self.pickup_s: Optional[float] = None
        self.via_mqtt = False
        self.running_s: Optional[float] = None
        self.ack_ms: List[float] = []
        self.error: Optional[str] = None

    def _connect_mqtt(self) -> None:
        import paho.mqtt.client as mqtt

        mc = self.mqtt_cfg
        topic = f"{mc.base_topic.rstrip('/')}/commands/device/{self.uid}"
        subscribed = threading.Event()
        client = mqtt.Client(client_id=f"fleet-sim-{self.uid}", clean_session=True,
                             callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        if mc.tls:
            client.tls_set()
        if mc.username:
            client.username_pw_set(mc.username, mc.password)

        def on_connect(c, userdata, flags, reason_code, properties):
            if not reason_code.is_failure:
                c.subscribe(topic, qos=1)

        def on_subscribe(c, userdata, mid, reason_codes, properties):
            if not any(rc.is_failure for rc in reason_codes):
                subscribed.set()

        def on_message(c, userdata, msg):
            try:
                payload = json.loads(msg.payload.decode())
            except ValueError:
                return
            if payload.get("operation") == "deploy_model" and not self._pushed_event.is_set():
                self._pushed = payload
                self._pushed_event.set()

        client.on_connect = on_connect
        client.on_subscribe = on_subscribe
        client.on_message = on_message
        client.connect(mc.host, mc.port, keepalive=60)
        client.loop_start()
        self._client = client
        if not subscribed.wait(60):
            raise RuntimeError("no SUBACK within 60 s")

    def _close_mqtt(self) -> None:
        if self._client is not None:
            self._client.disconnect()
            self._client.loop_stop()
            self._client = None

    def _wait_for_command(self, sess: requests.Session, deadline: float) -> Optional[dict]:
        if self._client is not None:
            fallback_at = min(deadline, self.t0 + self.mqtt_fallback_s)
            if self._pushed_event.wait(max(0.0, fallback_at - time.monotonic())):
                self.via_mqtt = True
                return self._pushed
        else:
            time.sleep(random.uniform(0, self.poll_s))
        while time.monotonic() < deadline:
            for c in self._heartbeat(sess, []).get("pending_commands") or []:
                if c.get("operation") == "deploy_model":
                    return c
            time.sleep(self.poll_s)
        return None

    def _heartbeat(self, sess: requests.Session, containers: List[dict]) -> dict:
        r = sess.post(self.base + "/api/agent/heartbeat", json={
            "device_id": self.uid, "status": "ONLINE", "active_containers": containers,
            "docker_running": True, "platform": ORIN_JP6, "agent_version": "fleet-sim",
        }, headers=self.headers, timeout=60)
        r.raise_for_status()
        return r.json()

    def _ack(self, sess: requests.Session, cmd: dict, status: str, **extra) -> None:
        began = time.monotonic()
        r = sess.post(self.base + "/api/agent/ack", json={
            "device_id": self.uid,
            "request_id": cmd["request_id"],
            "deployment_uid": cmd["deployment_uid"],
            "operation": "deploy_model",
            "status": status,
            **extra,
        }, headers=self.headers, timeout=60)
        self.ack_ms.append((time.monotonic() - began) * 1000.0)
        r.raise_for_status()

    def run(self) -> None:
        sess = requests.Session()
        if self.mqtt_cfg is not None:
            try:
                self._connect_mqtt()
            except Exception as e:
                self.mqtt_failed = True
                self._close_mqtt()
                print(f"  mqtt connect failed for {self.uid}: {e}", file=sys.stderr)
        self.ready.set()
        self.go.wait()
        deadline = self.t0 + self.timeout_s
        try:
            cmd = self._wait_for_command(sess, deadline)
            if cmd is None:
                self.error = "timeout waiting for deploy_model"
                return
            self.pickup_s = time.monotonic() - self.t0
            cname = (cmd.get("container") or {}).get("container_name") or "sim"
            step = self.device_s / 3.0
            for status in ("DOWNLOADING", "PULLING", "STARTING"):
                self._ack(sess, cmd, status)
                time.sleep(step)
            self._ack(
                sess, cmd, "RUNNING",
                container_id="sim%012d" % random.randint(0, 10 ** 12),
                container_name=cname,
                timings={"agent_version": "fleet-sim", "total_s": self.device_s},
            )
            self.running_s = time.monotonic() - self.t0
            self._heartbeat(sess, [{"name": cname}])
        except Exception as e:
            self.error = str(e)
        finally:
            self._close_mqtt()


class ResourceSampler(threading.Thread):
    def __init__(self, pid: int) -> None:
        super().__init__(daemon=True)
        self.proc = psutil.Process(pid)
        self.stop = threading.Event()
        self.peak_rss = 0

    def _procs(self) -> List[psutil.Process]:
        return [self.proc] + self.proc.children(recursive=True)

    def cpu_seconds(self) -> float:
        total = 0.0
        for p in self._procs():
            try:
                t = p.cpu_times()
                total += t.user + t.system
            except psutil.Error:
                pass
        return total

    def run(self) -> None:
        while not self.stop.is_set():
            rss = 0
            for p in self._procs():
                try:
                    rss += p.memory_info().rss
                except psutil.Error:
                    pass
            self.peak_rss = max(self.peak_rss, rss)
            time.sleep(0.1)


def run_once(base: str, pid: int, n: int, repeat: int, args) -> dict:
    group_uid = create_group(base, f"sim-{n}-{repeat}")
    devices = [register_device(base, f"sim-{n}-{repeat}-{i:03d}", group_uid) for i in range(n)]

    go = threading.Event()
    mqtt_cfg = None
    if args.mqtt_agents:
        from app.config import get_settings
        mqtt_cfg = get_settings().mqtt
    agents = [
        FakeAgent(base, d, go=go, poll_s=args.poll_seconds,
                  device_s=args.device_seconds, timeout_s=args.timeout,
                  mqtt_cfg=mqtt_cfg, mqtt_fallback_s=args.mqtt_fallback_seconds)
        for d in devices
    ]
    for a in agents:
        a.start()
    for a in agents:
        a.ready.wait(120)

    sampler = ResourceSampler(pid)
    sampler.start()
    cpu0 = sampler.cpu_seconds()
    t0 = time.monotonic()
    for a in agents:
        a.t0 = t0
    go.set()
    r = requests.post(base + "/deployments", data={
        "model_card_uid": CARD, "target_type": "GROUP", "target_uid": group_uid,
    }, allow_redirects=False, timeout=600)
    dispatch_s = time.monotonic() - t0
    deployment_uid = _location_uid(r, "/deployments/")
    for a in agents:
        a.join(args.timeout + 30)
    cpu_s = sampler.cpu_seconds() - cpu0
    sampler.stop.set()
    sampler.join()

    poll = requests.get(f"{base}/api/deployments/{deployment_uid}/poll", timeout=60).json()
    running_rows = sum(1 for d in poll.get("devices") or [] if d.get("status") == "RUNNING")

    pickups = [a.pickup_s for a in agents if a.pickup_s is not None]
    finished = [a.running_s for a in agents if a.running_s is not None]
    acks = [ms for a in agents for ms in a.ack_ms]
    errors = [a.error for a in agents if a.error]
    if errors:
        print(f"  {len(errors)} agent error(s), first: {errors[0]}", file=sys.stderr)
    all_running = max(finished) if len(finished) == n else None

    def r4(v):
        return round(v, 4) if v is not None else None

    return {
        "n": n,
        "repeat": repeat,
        "mqtt": args.mqtt,
        "mqtt_agents": args.mqtt_agents,
        "device_seconds": args.device_seconds,
        "poll_seconds": args.poll_seconds,
        "dispatch_s": r4(dispatch_s),
        "first_pickup_s": r4(min(pickups) if pickups else None),
        "last_pickup_s": r4(max(pickups) if pickups else None),
        "all_running_s": r4(all_running),
        "overhead_s": r4(all_running - args.device_seconds if all_running is not None else None),
        "picked_up_via_mqtt": sum(1 for a in agents if a.via_mqtt),
        "mqtt_connect_failures": sum(1 for a in agents if a.mqtt_failed),
        "running_rows": running_rows,
        "timeouts": n - len(finished),
        "ack_p50_ms": r4(_pct(acks, 0.5)),
        "ack_p90_ms": r4(_pct(acks, 0.9)),
        "ack_max_ms": r4(max(acks) if acks else None),
        "server_cpu_s": r4(cpu_s),
        "server_peak_rss_mb": round(sampler.peak_rss / 1e6, 1),
        "modeled_sequential_ssh_s": r4(n * args.device_seconds),
        "modeled_parallel_ssh_s": r4(args.device_seconds),
    }


def print_summary(rows: List[dict]) -> None:
    def med(rs, key):
        vals = [r[key] for r in rs if isinstance(r[key], (int, float))]
        return f"{statistics.median(vals):10.2f}" if vals else f"{'-':>10}"

    print("\nmedians across repeats (seconds unless noted)")
    header = (f"{'n':>4} {'runs':>4} {'dispatch':>10} {'last_pick':>10} {'all_run':>10} "
              f"{'overhead':>10} {'ack_p90ms':>10} {'cpu_s':>10} {'rss_mb':>10} "
              f"{'via_mqtt':>10} {'seq_ssh':>10} {'par_ssh':>10}")
    print(header)
    print("-" * len(header))
    for n in sorted({r["n"] for r in rows}):
        rs = [r for r in rows if r["n"] == n]
        print(f"{n:>4} {len(rs):>4} {med(rs, 'dispatch_s')} {med(rs, 'last_pickup_s')} "
              f"{med(rs, 'all_running_s')} {med(rs, 'overhead_s')} {med(rs, 'ack_p90_ms')} "
              f"{med(rs, 'server_cpu_s')} {med(rs, 'server_peak_rss_mb')} "
              f"{med(rs, 'picked_up_via_mqtt')} {med(rs, 'modeled_sequential_ssh_s')} "
              f"{med(rs, 'modeled_parallel_ssh_s')}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sizes", type=int, nargs="+", default=[10, 50, 100])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--device-seconds", type=float, default=3.8)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--mqtt", action="store_true",
                        help="Portal publishes to the broker in .env")
    parser.add_argument("--mqtt-agents", action="store_true",
                        help="Agents subscribe on the broker and take commands from the push")
    parser.add_argument("--mqtt-fallback-seconds", type=float, default=30.0)
    parser.add_argument("--out", default=str(
        ROOT / "eval" / "results" / f"fleet_sim_{date.today().isoformat()}.csv"))
    args = parser.parse_args()
    if args.mqtt_agents:
        args.mqtt = True
        sys.path.insert(0, str(ROOT))
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = 10240 if hard == resource.RLIM_INFINITY else min(hard, 10240)
        if soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    log_path = out.with_suffix(".portal.log")
    rows: List[dict] = []
    with tempfile.TemporaryDirectory() as td, log_path.open("w") as log_file:
        port = _free_port()
        base = f"http://127.0.0.1:{port}"
        proc = start_portal(Path(td) / "fleet_sim.db", port, args.mqtt, log_file)
        try:
            for n in args.sizes:
                for repeat in range(1, args.repeats + 1):
                    row = run_once(base, proc.pid, n, repeat, args)
                    rows.append(row)
                    print(f"n={n:<4} repeat={repeat} dispatch={row['dispatch_s']}s "
                          f"last_pickup={row['last_pickup_s']}s "
                          f"all_running={row['all_running_s']}s running_rows={row['running_rows']}/{n} "
                          f"via_mqtt={row['picked_up_via_mqtt']}/{n} "
                          f"cpu={row['server_cpu_s']}s rss={row['server_peak_rss_mb']}MB", flush=True)
        finally:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()

    with out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {out}")
    print_summary(rows)


if __name__ == "__main__":
    main()
