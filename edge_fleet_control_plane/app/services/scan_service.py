"""Turntable scans on a scan station.

The station runs the Scanbot job server on 127.0.0.1:8800; nothing reaches
it from outside. The portal sends ``scanbot_*`` commands over MQTT (and the
heartbeat as a fallback), the station's agent relays them to the job server,
and it reports the job list back every few seconds. Preview videos are
uploaded when a scan finishes. The full result bundle is uploaded only when
someone asks for it.

    Portal --(MQTT scanbot_submit)--> Agent --(POST /jobs)--> job server
    Agent  --(POST /api/agent/scanbot/report)--> Portal (job list)
    Agent  --(POST /api/agent/scanbot/file|bundle)--> Portal (results)
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import DATA_DIR, get_settings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.ids import gen_request_id, gen_uid
from app.core.logging import get_logger
from app.core.time import now_iso
from app.repositories import commands as commands_repo
from app.repositories import devices as devices_repo
from app.repositories import mqtt_audit as audit_repo
from app.repositories import scan_jobs as scan_repo
from app.services import deployment_service, device_capabilities, device_service, event_service, mqtt_service

log = get_logger("scan")

# Same wording as Iowa's GUI so operators see familiar steps.
LABELS = {
    "queued": "Waiting in line",
    "capturing": "Filming on the turntable",
    "extracting": "Picking sharp frames",
    "uploading_frames": "Sending frames",
    "frames_ready": "Waiting for the GPU",
    "pulling_frames": "Copying frames to the GPU",
    "reconstructing": "Building the 3D model",
    "packaging": "Packing the results",
    "exporting": "Exporting",
    "uploading_results": "Uploading results",
    "done": "Done",
    "failed": "Failed",
    "cancelled": "Cancelled",
}
TERMINAL = {"done", "failed", "cancelled"}
# Steps shown on the progress bar, in order.
STEPS = [
    ("filming", "Filming", {"queued", "capturing"}),
    ("frames", "Picking frames", {"extracting", "uploading_frames", "frames_ready", "pulling_frames"}),
    ("model", "Building the model", {"reconstructing", "exporting"}),
    ("packing", "Packing", {"packaging", "uploading_results"}),
    ("done", "Done", {"done"}),
]
CHECKLIST = [
    ("chk_object", "The object is centered on the turntable"),
    ("chk_gopro", "The GoPro is turned on and plugged in"),
    ("chk_hypersmooth", "HyperSmooth (stabilization) is off"),
    ("chk_lens", "The lens is set to Wide"),
]
PREVIEWS = {
    "nerf_orbit.mp4": ("videos/nerf_orbit.mp4", "3D model orbit"),
    "colmap_poses.mp4": ("videos/colmap_poses.mp4", "Camera positions"),
}
PREVIEW_MAX_BYTES = 120 * 1024 * 1024
BUNDLE_CHUNK_MAX_BYTES = 48 * 1024 * 1024
DEFAULT_ITERATIONS = 30000
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][\w.-]{0,119}$")
_NAME_RE = re.compile(r"^[\w .,()'-]{1,60}$")
# A submit that has not reached the station by then is shown as not delivered.
SUBMIT_TTL_S = 600


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def scans_root() -> Path:
    return Path(os.getenv("SCAN_DATA_DIR") or (DATA_DIR / "scans"))


def _check_job_id(job_id: str) -> str:
    if not job_id or not _JOB_ID_RE.match(job_id):
        raise ValidationError("Invalid scan id.")
    return job_id


def job_dir(device_uid: str, job_id: str) -> Path:
    if not re.match(r"^[\w-]{1,80}$", device_uid or ""):
        raise ValidationError("Invalid device id.")
    return scans_root() / device_uid / _check_job_id(job_id)


def preview_path(device_uid: str, job_id: str, name: str) -> Optional[Path]:
    if name not in PREVIEWS:
        return None
    p = job_dir(device_uid, job_id) / name
    return p if p.is_file() else None


def bundle_path(device_uid: str, job_id: str) -> Path:
    return job_dir(device_uid, job_id) / "bundle.tar"


def _bundle_status_path(device_uid: str, job_id: str) -> Path:
    return job_dir(device_uid, job_id) / "bundle.json"


def bundle_status(device_uid: str, job_id: str) -> Dict[str, Any]:
    p = _bundle_status_path(device_uid, job_id)
    status: Dict[str, Any] = {"state": "none"}
    if p.is_file():
        try:
            status = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    tar = bundle_path(device_uid, job_id)
    if tar.is_file():
        status["state"] = "ready"
        status["bytes"] = tar.stat().st_size
    elif status.get("state") == "uploading":
        part = tar.with_suffix(".tar.part")
        status["bytes"] = part.stat().st_size if part.is_file() else 0
    return status


def _write_bundle_status(device_uid: str, job_id: str, status: Dict[str, Any]) -> None:
    p = _bundle_status_path(device_uid, job_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(dict(status, updated_at=now_iso())))
    tmp.replace(p)


# ---------------------------------------------------------------------------
# Station checks
# ---------------------------------------------------------------------------

def get_station(device_uid: str, owner: str) -> dict:
    device = device_service.get(device_uid, owner)
    if not device_capabilities.scan_station(device):
        raise NotFoundError("This device is not a scan station.")
    return device


def active_service(device_uid: str, owner: str) -> Optional[dict]:
    dep = deployment_service.get_active_device_deployment(device_uid, owner)
    if not dep or ((dep.get("card") or {}).get("app_kind") != "scanbot"):
        return None
    return dep


def _require_running_service(device_uid: str, owner: str) -> dict:
    dep = active_service(device_uid, owner)
    if not dep:
        raise ValidationError("The scan service is not running on this station. Start it from the device page.")
    if dep.get("effective_status") != "RUNNING":
        raise ValidationError(
            "The scan service is still starting (%s). Try again when it shows Running."
            % (dep.get("effective_status") or "unknown").lower()
        )
    return dep


def _require_job(device_uid: str, job_id: str) -> dict:
    row = scan_repo.get(device_uid, _check_job_id(job_id))
    if not row:
        raise NotFoundError("Scan not found.")
    return row


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _send(*, owner: str, device_uid: str, deployment_uid: str, operation: str,
          body: Dict[str, Any]) -> str:
    """Record the command row, then publish it. The heartbeat redelivers it if MQTT is down."""
    request_id = gen_request_id()
    topic = mqtt_service.topic_for("DEVICE", device_uid)
    payload = {
        "operation": operation,
        "request_id": request_id,
        "deployment_uid": deployment_uid,
        "issued_at": now_iso(),
        **body,
    }
    commands_repo.insert(
        command_uid=gen_uid("cmd"),
        owner=owner,
        deployment_uid=None,
        target_type="DEVICE",
        target_uid=device_uid,
        device_uid=device_uid,
        operation=operation,
        request_id=request_id,
        topic=topic,
        payload=payload,
        status="RECORDED",
    )
    audit_repo.insert(owner=owner, topic=topic, direction="OUTBOUND", request_id=request_id,
                      device_uid=device_uid, payload=payload)
    if get_settings().mqtt.enabled:
        try:
            mqtt_service.publish(topic, payload)
            commands_repo.update_send_status(request_id, status="MQTT_SENT", sent_at=now_iso())
        except Exception as e:  # pragma: no cover - network failure path
            log.warning("scan command publish failed, heartbeat will redeliver: %s", e)
            commands_repo.update_send_status(request_id, status="MQTT_FAILED", error=str(e))
    return request_id


def submit(*, device_uid: str, owner: str, object_name: str, rotations: float,
           iterations: int, target_fps: float, checklist: List[str]) -> str:
    get_station(device_uid, owner)
    dep = _require_running_service(device_uid, owner)
    name = (object_name or "").strip()
    if not _NAME_RE.match(name):
        raise ValidationError("Object name: 1 to 60 letters, numbers, spaces or - _ . , ( ) '.")
    missing = [text for key, text in CHECKLIST if key not in set(checklist or [])]
    if missing:
        raise ValidationError("Check the rig first: %s." % "; ".join(missing))
    if not 0.25 <= rotations <= 5:
        raise ValidationError("Rotations must be between 0.25 and 5.")
    if not 1000 <= iterations <= 100000:
        raise ValidationError("Training iterations must be between 1,000 and 100,000.")
    if not 1 <= target_fps <= 15:
        raise ValidationError("Frames per second must be between 1 and 15.")
    spec = {
        "object": name,
        "capture": {"rotations": rotations},
        "extract": {"target_fps": target_fps},
        "recon": {"method": "nerfacto", "max_iterations": int(iterations)},
    }
    request_id = _send(owner=owner, device_uid=device_uid, deployment_uid=dep["deployment_uid"],
                       operation="scanbot_submit", body={"scan": spec})
    event_service.record("SCAN_SUBMITTED", "Scan requested: %s" % name, owner=owner,
                         device_uid=device_uid, deployment_uid=dep["deployment_uid"],
                         payload={"request_id": request_id, "scan": spec})
    return request_id


def cancel(*, device_uid: str, owner: str, job_id: str) -> str:
    get_station(device_uid, owner)
    dep = _require_running_service(device_uid, owner)
    row = _require_job(device_uid, job_id)
    if row["state"] in TERMINAL:
        raise ConflictError("This scan is already %s." % LABELS.get(row["state"], row["state"]).lower())
    request_id = _send(owner=owner, device_uid=device_uid, deployment_uid=dep["deployment_uid"],
                       operation="scanbot_cancel", body={"job_id": job_id})
    event_service.record("SCAN_CANCEL_REQUESTED", "Cancel requested for scan %s" % job_id,
                         owner=owner, device_uid=device_uid, deployment_uid=dep["deployment_uid"])
    return request_id


def _had_frames(job: dict) -> bool:
    return any(h.get("state") == "frames_ready" for h in job.get("history") or [])


def retry(*, device_uid: str, owner: str, job_id: str) -> str:
    get_station(device_uid, owner)
    dep = _require_running_service(device_uid, owner)
    row = _require_job(device_uid, job_id)
    if row["state"] not in ("failed", "cancelled"):
        raise ConflictError("Only failed or cancelled scans can be retried.")
    if not _had_frames(row["job"]):
        raise ConflictError("This scan never got its frames. Start a new scan instead.")
    request_id = _send(owner=owner, device_uid=device_uid, deployment_uid=dep["deployment_uid"],
                       operation="scanbot_retry", body={"job_id": job_id})
    event_service.record("SCAN_RETRY_REQUESTED", "Rebuild requested for scan %s" % job_id,
                         owner=owner, device_uid=device_uid, deployment_uid=dep["deployment_uid"])
    return request_id


def fetch_bundle(*, device_uid: str, owner: str, job_id: str) -> str:
    get_station(device_uid, owner)
    dep = _require_running_service(device_uid, owner)
    row = _require_job(device_uid, job_id)
    if row["state"] != "done":
        raise ConflictError("Results can be fetched once the scan is done.")
    status = bundle_status(device_uid, job_id)
    if status["state"] == "ready":
        raise ConflictError("The results are already here.")
    if status["state"] in ("requested", "uploading") and not _stale(status):
        raise ConflictError("The results are already on their way.")
    request_id = _send(owner=owner, device_uid=device_uid, deployment_uid=dep["deployment_uid"],
                       operation="scanbot_fetch_bundle", body={"job_id": job_id})
    _write_bundle_status(device_uid, job_id, {"state": "requested", "request_id": request_id})
    return request_id


def delete_bundle(*, device_uid: str, owner: str, job_id: str) -> None:
    get_station(device_uid, owner)
    _require_job(device_uid, job_id)
    d = job_dir(device_uid, job_id)
    for name in ("bundle.tar", "bundle.tar.part", "bundle.json"):
        try:
            (d / name).unlink()
        except FileNotFoundError:
            pass


def _stale(status: Dict[str, Any], seconds: int = 600) -> bool:
    try:
        then = datetime.fromisoformat(status.get("updated_at") or "")
    except ValueError:
        return True
    return datetime.now(timezone.utc) - then > timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# Agent uploads
# ---------------------------------------------------------------------------

def _station_owner(device_uid: str) -> str:
    device = devices_repo.get_any(device_uid)
    if not device or not device_capabilities.scan_station(device):
        raise NotFoundError("Not a scan station.")
    return device["owner_tapis_username"]


def record_report(device_uid: str, payload: Dict[str, Any]) -> dict:
    owner = _station_owner(device_uid)
    deployment_uid = payload.get("deployment_uid")
    scan_repo.set_status(device_uid=device_uid, deployment_uid=deployment_uid,
                         reachable=bool(payload.get("reachable")),
                         error=(str(payload.get("error"))[:500] if payload.get("error") else None))
    jobs = payload.get("jobs")
    stored = 0
    if isinstance(jobs, list):
        for job in jobs[:500]:
            if not isinstance(job, dict) or not _JOB_ID_RE.match(str(job.get("id") or "")):
                continue
            before = scan_repo.get(device_uid, job["id"])
            scan_repo.upsert_job(device_uid=device_uid, owner=owner,
                                 deployment_uid=deployment_uid, job=job)
            stored += 1
            state = job.get("state")
            if before and before["state"] != state and state in TERMINAL:
                event_service.record(
                    "SCAN_%s" % state.upper(),
                    "Scan %s %s" % (job.get("object") or job["id"], LABELS.get(state, state).lower()),
                    severity="ERROR" if state == "failed" else "INFO",
                    owner=owner, device_uid=device_uid, deployment_uid=deployment_uid,
                    payload={"job_id": job["id"], "message": job.get("message")},
                )
    want = []
    for row in scan_repo.list_for_device(device_uid, limit=50):
        if row["state"] != "done":
            continue
        for name in PREVIEWS:
            if not (job_dir(device_uid, row["job_id"]) / name).is_file():
                want.append({"job_id": row["job_id"], "name": name, "path": PREVIEWS[name][0]})
    return {"ok": True, "stored": stored, "want_previews": want}


def store_preview(device_uid: str, job_id: str, name: str, body: bytes) -> dict:
    _station_owner(device_uid)
    if name not in PREVIEWS:
        raise ValidationError("Unknown preview file.")
    if not scan_repo.get(device_uid, _check_job_id(job_id)):
        raise NotFoundError("Unknown scan.")
    if len(body) > PREVIEW_MAX_BYTES:
        raise ValidationError("Preview is too large.")
    d = job_dir(device_uid, job_id)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (name + ".tmp")
    tmp.write_bytes(body)
    tmp.replace(d / name)
    return {"ok": True, "bytes": len(body)}


def store_bundle_chunk(device_uid: str, job_id: str, *, offset: int, total: int,
                       final: bool, body: bytes) -> dict:
    """Append one chunk. The agent resumes from ``bytes`` if an offset does not match."""
    owner = _station_owner(device_uid)
    if not scan_repo.get(device_uid, _check_job_id(job_id)):
        raise NotFoundError("Unknown scan.")
    if len(body) > BUNDLE_CHUNK_MAX_BYTES:
        raise ValidationError("Chunk is too large.")
    d = job_dir(device_uid, job_id)
    d.mkdir(parents=True, exist_ok=True)
    part = d / "bundle.tar.part"
    have = part.stat().st_size if part.is_file() else 0
    if offset == 0 and have:
        part.unlink()
        have = 0
    if offset != have:
        return {"ok": False, "bytes": have}
    if offset == 0:
        free = shutil.disk_usage(d).free
        if total and free < total * 1.1 + 512 * 1024 * 1024:
            _write_bundle_status(device_uid, job_id, {"state": "failed",
                                                      "error": "Not enough disk space on the portal."})
            raise ValidationError("Not enough disk space on the portal for this bundle.")
    with open(part, "ab") as f:
        f.write(body)
    have += len(body)
    if final:
        if total and have != total:
            _write_bundle_status(device_uid, job_id, {"state": "failed",
                                                      "error": "Upload ended at %d of %d bytes." % (have, total)})
            part.unlink()
            raise ValidationError("Bundle size does not match.")
        part.replace(bundle_path(device_uid, job_id))
        _write_bundle_status(device_uid, job_id, {"state": "ready", "total": have})
        event_service.record("SCAN_BUNDLE_READY", "Results for scan %s are on the portal" % job_id,
                             owner=owner, device_uid=device_uid, payload={"job_id": job_id, "bytes": have})
    else:
        _write_bundle_status(device_uid, job_id, {"state": "uploading", "total": total})
    return {"ok": True, "bytes": have}


def bundle_failed(device_uid: str, job_id: str, error: str) -> None:
    _station_owner(device_uid)
    if not scan_repo.get(device_uid, _check_job_id(job_id)):
        raise NotFoundError("Unknown scan.")
    _write_bundle_status(device_uid, job_id, {"state": "failed", "error": (error or "")[:300]})


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def _step_index(state: str, job: dict) -> int:
    if state in ("failed", "cancelled"):
        for h in reversed(job.get("history") or []):
            s = h.get("state")
            if s not in TERMINAL:
                state = s
                break
        else:
            return 0
    for i, (_, _, states) in enumerate(STEPS):
        if state in states:
            return i
    return 0


def _job_view(device_uid: str, row: dict) -> dict:
    job = row["job"]
    state = row["state"]
    history = job.get("history") or []
    started = job.get("created")
    ended = history[-1].get("t") if (history and state in TERMINAL) else None
    previews = [
        {"name": name, "title": title, "url": "/scans/%s/jobs/%s/files/%s" % (device_uid, row["job_id"], name)}
        for name, (_, title) in PREVIEWS.items()
        if (job_dir(device_uid, row["job_id"]) / name).is_file()
    ]
    bundle = job.get("bundle") or {}
    progress = job.get("progress")
    return {
        "id": row["job_id"],
        "object": job.get("object") or row["job_id"],
        "state": state,
        "label": LABELS.get(state, state.replace("_", " ").capitalize()),
        "terminal": state in TERMINAL,
        "step": _step_index(state, job),
        "progress": float(progress) if isinstance(progress, (int, float)) and state == "reconstructing" else None,
        "message": job.get("message"),
        "error": job.get("error"),
        "log_tail": job.get("log_tail"),
        "created": started,
        "updated": job.get("updated"),
        "ended": ended,
        "rotations": (job.get("capture") or {}).get("rotations"),
        "iterations": (job.get("recon") or {}).get("max_iterations") or DEFAULT_ITERATIONS,
        "history": [
            {"state": h.get("state"), "label": LABELS.get(h.get("state"), h.get("state")),
             "t": h.get("t"), "message": h.get("message")}
            for h in history[-12:]
        ],
        "previews": previews,
        "station_bundle_mb": bundle.get("mb"),
        "bundle": bundle_status(device_uid, row["job_id"]),
        "download_url": "/scans/%s/jobs/%s/bundle" % (device_uid, row["job_id"]),
        "can_cancel": state not in TERMINAL,
        "can_retry": state in ("failed", "cancelled") and _had_frames(job),
        "can_fetch": state == "done",
    }


def _pending_submissions(device_uid: str, known_ids: set) -> List[dict]:
    """Submits that have not shown up as a job yet (or failed on the station)."""
    out = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    for cmd in commands_repo.list_scan_commands(device_uid, "scanbot_submit", limit=20):
        created = datetime.fromisoformat(cmd["created_at"])
        if created < cutoff:
            continue
        try:
            payload = json.loads(cmd.get("payload_json") or "{}")
            response = json.loads(cmd.get("response_json") or "{}") if cmd.get("response_json") else {}
        except json.JSONDecodeError:
            continue
        result = response.get("result") or {}
        if result.get("job_id") and result["job_id"] in known_ids:
            continue
        status = (response.get("status") or "").upper()
        if status in ("OK", "SUCCESS") and result.get("job_id"):
            label, failed = "Accepted by the station", False
        elif status == "FAILED":
            label, failed = "The station could not start it: %s" % (response.get("error") or "unknown error"), True
        elif datetime.now(timezone.utc) - created > timedelta(seconds=SUBMIT_TTL_S):
            label, failed = "Never reached the station. Is it online?", True
        else:
            label, failed = "Sending to the station…", False
        out.append({
            "request_id": cmd["request_id"],
            "object": ((payload.get("scan") or {}).get("object")) or "scan",
            "label": label,
            "failed": failed,
            "created_at": cmd["created_at"],
        })
    return out


def list_jobs(device_uid: str, owner: str) -> dict:
    get_station(device_uid, owner)
    rows = scan_repo.list_for_device(device_uid)
    jobs = [_job_view(device_uid, r) for r in rows]
    known = {j["id"] for j in jobs}
    return {
        "jobs": jobs,
        "pending": _pending_submissions(device_uid, known),
        "service": service_summary(device_uid, owner),
        "steps": [label for _, label, _ in STEPS],
    }


def service_summary(device_uid: str, owner: str) -> dict:
    dep = active_service(device_uid, owner)
    status = scan_repo.get_status(device_uid)
    device = devices_repo.get(device_uid, owner) or {}
    online = (device.get("status") or "").upper() in ("ONLINE", "RUNNING")
    summary = {
        "deployment_uid": (dep or {}).get("deployment_uid"),
        "deployment_status": (dep or {}).get("effective_status"),
        "device_online": online,
        "reachable": bool(status and status["reachable"]),
        "error": (status or {}).get("error"),
        "reported_at": (status or {}).get("reported_at"),
    }
    if not dep:
        summary.update(ready=False, headline="Scan service not running",
                       detail="Start it from the station's device page.")
    elif dep.get("effective_status") != "RUNNING":
        summary.update(ready=False, headline="Scan service starting",
                       detail="Status: %s. The first start downloads a large image." % dep.get("effective_status"))
    elif not online:
        summary.update(ready=False, headline="Station offline",
                       detail="The station's agent has not checked in recently.")
    elif not status or status.get("deployment_uid") != dep["deployment_uid"]:
        summary.update(ready=False, headline="Waiting for the station",
                       detail="The scan service is up; waiting for its first report.")
    elif not status["reachable"]:
        summary.update(ready=False, headline="Job server not answering",
                       detail=status.get("error") or "The agent cannot reach the job server.")
    else:
        summary.update(ready=True, headline="Ready", detail="Job server is answering.")
    return summary


def list_stations(owner: str) -> List[dict]:
    out = []
    for device in devices_repo.list_for_owner(owner):
        station = device_capabilities.scan_station(device)
        if not station:
            continue
        counts = scan_repo.counts_by_state(device["device_uid"])
        out.append({
            "device": device,
            "capture_host": station["capture_host"],
            "service": service_summary(device["device_uid"], owner),
            "active": sum(c for s, c in counts.items() if s not in TERMINAL),
            "done": counts.get("done", 0),
            "failed": counts.get("failed", 0),
        })
    return out
