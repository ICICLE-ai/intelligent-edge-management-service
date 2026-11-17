"""Agent command handling: duplicates, stale deploys, restart after stop, local state writes.

These cases exercise the agent's own decision code (``handle_command``,
``_command_already_applied``, ``_stale_deploy_after_stop``, ``save_json``) with
Docker and the command runner replaced by stubs, so they need no device and no
network. The portal side of the same behaviors (pending list, late acks) is
covered by tests/smoke_test.py.

Run with:
    python tests/agent_commands_test.py
"""

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="agent_cmd_test_"))
os.environ["ICICLE_BASE_DIR"] = str(TMP)

spec = importlib.util.spec_from_file_location(
    "icicle_agent", str(ROOT / "app" / "agent_package" / "agent" / "main.py"))
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

UID = "dpl_test0001"
CFG = {"device_id": "dev_test"}

RealThread = threading.Thread
ran = []
running = {"value": True}


def _fake_run(cfg, payload, received_mono=None):
    ran.append((payload.get("operation"), payload.get("request_id")))


agent._run_command = _fake_run
agent._container_running_state = lambda name: running["value"]


class _InlineThread(object):
    """Run the command synchronously so the test can count calls."""

    def __init__(self, target=None, args=(), **kwargs):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


agent.threading.Thread = _InlineThread


def reset(state=None):
    del ran[:]
    agent._seen_request_ids.clear()
    agent._portal_deployment_status.clear()
    running["value"] = True
    d = agent.deployment_dir(UID)
    if d.exists():
        for p in d.iterdir():
            p.unlink()
    else:
        d.mkdir(parents=True)
    if state is not None:
        (d / "state.json").write_text(json.dumps(state))


def cmd(op, rid, intent_seq=None):
    out = {"operation": op, "request_id": rid, "deployment_uid": UID}
    if intent_seq is not None:
        out["intent_seq"] = intent_seq
    return out


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        check.failed += 1


check.failed = 0

# 1. The same request arrives twice in one agent process.
reset()
agent.handle_command(CFG, cmd("deploy_model", "req-1"))
agent.handle_command(CFG, cmd("deploy_model", "req-1"))
check("same request twice, one process: executed once", len(ran) == 1)

# 1b. The agent restarts (memory cleared); the request arrives again after it was applied.
reset({"deployment_uid": UID, "container_name": "c1", "desired": "running",
       "last_request_id": "req-1"})
agent.handle_command(CFG, cmd("deploy_model", "req-1"))
check("same request after agent restart, container running: not re-executed", len(ran) == 0)

# 1c. Same, but the container is gone: the command is allowed to run again.
reset({"deployment_uid": UID, "container_name": "c1", "desired": "running",
       "last_request_id": "req-1"})
running["value"] = False
agent.handle_command(CFG, cmd("deploy_model", "req-1"))
check("same request after agent restart, container missing: executed", len(ran) == 1)

# 2. One request arrives through MQTT and the heartbeat at the same moment.
reset()
agent.threading.Thread = RealThread
orig_run = agent._run_command
gate = threading.Event()
count = []


def _slow_run(cfg, payload, received_mono=None):
    count.append(payload.get("request_id"))
    gate.wait(0.2)


agent._run_command = _slow_run
t1 = RealThread(target=agent.handle_command, args=(CFG, cmd("deploy_model", "req-2")))
t2 = RealThread(target=agent.handle_command, args=(CFG, cmd("deploy_model", "req-2")))
t1.start(); t2.start(); t1.join(); t2.join()
gate.set()
import time as _t  # noqa: E402
_t.sleep(0.4)
check("MQTT and heartbeat delivery of one request: executed once", len(count) == 1)
agent._run_command = orig_run
agent.threading.Thread = _InlineThread

# 3. An old deploy arrives after the deployment was stopped.
reset({"deployment_uid": UID, "container_name": "c1", "desired": "stopped",
       "last_request_id": "stop-1"})
agent.handle_command(CFG, cmd("deploy_model", "req-old"))
check("old deploy after stop (local state stopped): ignored", len(ran) == 0)

reset()
agent._remember_portal_deployments([{"deployment_uid": UID, "status": "STOPPED"}])
agent.handle_command(CFG, cmd("deploy_model", "req-old"))
check("old deploy after stop (no local state, portal says STOPPED): ignored", len(ran) == 0)

# 4. A legitimate restart follows a stop.
reset({"deployment_uid": UID, "container_name": "c1", "desired": "stopped",
       "last_request_id": "stop-1"})
agent.handle_command(CFG, cmd("restart_deployment", "req-restart"))
check("restart with a new request id after stop: executed", ran == [("restart_deployment", "req-restart")])

# 5. A stop is not applied twice, and a first stop is applied.
reset({"deployment_uid": UID, "container_name": "c1", "desired": "stopped",
       "last_request_id": "stop-1"})
agent.handle_command(CFG, cmd("stop_deployment", "stop-1"))
check("repeated stop already applied: not re-executed", len(ran) == 0)
reset({"deployment_uid": UID, "container_name": "c1", "desired": "running",
       "last_request_id": "req-1"})
agent.handle_command(CFG, cmd("stop_deployment", "stop-2"))
check("first stop: executed", ran == [("stop_deployment", "stop-2")])

# 6. Power fails while the local state file is being replaced.
reset()
state_path = agent.deployment_dir(UID) / "state.json"
agent.save_json(state_path, {"desired": "running", "last_request_id": "req-1"})
real_replace = Path.replace


def _crash(self, target):
    raise OSError("power lost before rename")


Path.replace = _crash
try:
    try:
        agent.save_json(state_path, {"desired": "stopped", "last_request_id": "stop-9"})
    except OSError:
        pass
finally:
    Path.replace = real_replace
survived = json.loads(state_path.read_text())
check("crash before rename: previous state.json intact and parseable",
      survived == {"desired": "running", "last_request_id": "req-1"})
check("crash before rename: only a .tmp file is left over",
      (agent.deployment_dir(UID) / "state.tmp").exists())

# 6b. A truncated state file is treated as absent, not as a decision to run.
state_path.write_text('{"desired": "runn')
check("truncated state.json: read as absent", agent._load_json_if_exists(state_path) is None)
check("truncated state.json: container is not restored",
      agent._deployment_desired_running(agent._load_json_if_exists(state_path)) is False)

# 6c. Payload saved but state never written (crash before the container was confirmed):
# nothing is restored locally, and the unacknowledged deploy is allowed to run again.
reset()
agent.save_json(agent.deployment_dir(UID) / "deploy_payload.json", cmd("deploy_model", "req-3"))
check("payload without state: not restored locally",
      agent._deployment_desired_running(agent._load_json_if_exists(agent.deployment_dir(UID) / "state.json")) is False)
agent.handle_command(CFG, cmd("deploy_model", "req-3"))
check("payload without state: redelivered deploy executes", len(ran) == 1)

# 7. A delayed stop/restart cannot override a newer intent already applied.
reset({"deployment_uid": UID, "container_name": "c1", "desired": "running",
       "last_request_id": "req-new", "last_intent_seq": 3})
agent.handle_command(CFG, cmd("stop_deployment", "stop-old", intent_seq=2))
check("delayed stop with older intent_seq: ignored", len(ran) == 0)
agent.handle_command(CFG, cmd("restart_deployment", "restart-old", intent_seq=1))
check("delayed restart with older intent_seq: ignored", len(ran) == 0)
agent.handle_command(CFG, cmd("deploy_model", "req-old-seq", intent_seq=3))
check("deploy with same intent_seq already applied: ignored", len(ran) == 0)
agent.handle_command(CFG, cmd("stop_deployment", "stop-new", intent_seq=4))
check("stop with newer intent_seq: executed", ran == [("stop_deployment", "stop-new")])

# 7b. Commands without intent_seq still follow the stop-guard (backward compatible).
reset({"deployment_uid": UID, "container_name": "c1", "desired": "running",
       "last_request_id": "req-1", "last_intent_seq": 2})
agent.handle_command(CFG, cmd("stop_deployment", "stop-no-seq"))
check("stop without intent_seq: executed", ran == [("stop_deployment", "stop-no-seq")])

# 8. Recovery planned before a newer intent was applied must not act on the old plan.
agent.threading.Thread = RealThread
launched = []
acked = []
agent._wait_for_recover_ready = lambda payload: None
agent._relaunch_from_payload = lambda payload, dpath, state, cname, confirm_s: (
    launched.append(cname) or "cid-1")
agent._ack_recovered = lambda cfg, payload, dpath, **kw: acked.append(kw.get("status"))
agent._container_running_state = lambda name: False
RECOVER_PAYLOAD = {"deployment_uid": UID, "container": {"container_name": "c1"}}


def recover(snapshot):
    del launched[:]
    del acked[:]
    agent._restore_fail_until.clear()
    agent._recover_one(CFG, agent.deployment_dir(UID), RECOVER_PAYLOAD, snapshot, "boot")


running_state = {"deployment_uid": UID, "container_name": "c1", "desired": "running",
                 "last_intent_seq": 3}
reset(running_state)
recover(dict(running_state))
check("recovery with unchanged intent: container launched and acknowledged",
      launched == ["c1"] and acked == ["RUNNING"])

# 8a. A stop was applied after the snapshot was taken.
reset({**running_state, "desired": "stopped", "last_intent_seq": 4})
recover(dict(running_state))
check("recovery after a stop was applied: container not launched, no acknowledgment",
      launched == [] and acked == [])

# 8b. A newer restart advanced the intent sequence while the desired state stayed running.
reset({**running_state, "last_intent_seq": 4})
recover(dict(running_state))
check("recovery after a newer intent_seq was applied: container not launched",
      launched == [] and acked == [])

# 8c. A stop arrives while recovery is launching the container: the stop waits,
# runs after the launch, and is the last writer of both the container and the state.
reset(running_state)
events = []
real_run = agent.subprocess.run
real_send_ack = agent.send_ack
agent._relaunch_from_payload = lambda payload, dpath, state, cname, confirm_s: (
    events.append("launch-start"), time.sleep(0.3), events.append("launch-end"), "cid-1")[-1]
agent._ack_recovered = lambda cfg, payload, dpath, **kw: (
    events.append("recover-ack"),
    agent._merge_state(dpath, desired="running"))
agent.subprocess.run = lambda cmd_, **kw: events.append("remove-container")
agent.send_ack = lambda *a, **kw: events.append("stop-ack")
try:
    rec = RealThread(target=agent._recover_one, args=(
        CFG, agent.deployment_dir(UID), RECOVER_PAYLOAD, dict(running_state), "boot"))
    rec.start()
    time.sleep(0.05)
    agent.stop_deployment(CFG, {"deployment_uid": UID, "request_id": "stop-race",
                                "container_name": "c1", "intent_seq": 4})
    rec.join()
finally:
    agent.subprocess.run = real_run
    agent.send_ack = real_send_ack
final = json.loads((agent.deployment_dir(UID) / "state.json").read_text())
check("stop during recovery: recovery finishes first, then the stop removes the container",
      events == ["launch-start", "launch-end", "recover-ack", "remove-container", "stop-ack"])
check("stop during recovery: the stopped intent is the final state",
      final["desired"] == "stopped" and final["last_intent_seq"] == 4)

print("\n%s" % ("PASS" if not check.failed else "%d FAILED" % check.failed))
sys.exit(1 if check.failed else 0)
