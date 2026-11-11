"""Job server (runs on the DGX). Stdlib only; one JSON file per job.

  python -m scanbot.server --config config.toml

Endpoints (header X-Token if [server].token is set; all JSON except downloads):
  GET  /health                   no token needed -> {"ok": true}
  POST /jobs                     create a scan request            -> job
  GET  /jobs                     list jobs
  GET  /jobs/<id>                job details / status
  POST /jobs/<id>                update state/message/fields; {"state": "cancelled"} cancels. A cancelled job
                                 ignores further state changes unless the body has "retry": true
  POST /jobs/claim               {"from","to","worker"} take oldest job in a state -> job | 204
  GET  /jobs/<id>/bundle         download <id>.tar (all results), supports Range for resuming
  GET  /jobs/<id>/files          list files that can be downloaded one by one (?detail=1: path, size, mtime);
                                 available while the job runs, so clients can fetch results as they appear
  GET  /jobs/<id>/files/<path>   download one file, e.g. videos/nerf_orbit.mp4

Result files are read from <results_dir>/<id>/ and <results_dir>/<id>.tar, where results_dir defaults
to [worker].data_dir (the worker writes them there).
"""
import argparse
import hmac
import json
import re
import shutil
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .api import STATES
from .config import load_config

# Turntable footage: min_motion must be 0 (the motion filter rejects every frame when only the object moves).
DEFAULT_SPEC = {
    "object": "unnamed",
    "capture": {"rotations": 1},
    "extract": {"target_fps": 5.0, "min_motion": 0.0, "max_dim": 0},
    "recon": {"method": "nerfacto"},   # + max_iterations (unset = recon script default)
}
# Top-level entries of a job's result dir that may be downloaded (same as what goes into the bundle).
RESULT_ITEMS = ["frames", "video", "proc", "colmap", "nerf", "videos", "job.json", "recon.log", "MANIFEST.txt"]
# Never listed: scratch files (COLMAP database, partial writes). Same idea as [worker].bundle_exclude.
SKIP_SUFFIXES = (".db", ".part", ".tmp")


class Conflict(Exception):
    pass


class Store:
    def __init__(self, root):
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def _path(self, job_id):
        return self.root / f"{job_id}.json"

    def _write(self, job):
        tmp = self._path(job["id"]).with_suffix(".tmp")
        tmp.write_text(json.dumps(job, indent=2))
        tmp.replace(self._path(job["id"]))

    def all(self):
        return sorted((json.loads(p.read_text()) for p in self.root.glob("*.json")),
                      key=lambda j: j["created"])

    def get(self, job_id):
        p = self._path(job_id)
        return json.loads(p.read_text()) if p.exists() else None

    def create(self, spec):
        with self.lock:
            merged = {k: (dict(v, **spec.get(k, {})) if isinstance(v, dict) else spec.get(k, v))
                      for k, v in DEFAULT_SPEC.items()}
            slug = re.sub(r"[^a-z0-9]+", "-", str(merged["object"]).lower()).strip("-")[:30] or "scan"
            job_id = base = f"{datetime.now():%Y%m%d-%H%M%S}-{slug}"
            n = 1
            while self._path(job_id).exists():  # two requests in the same second
                n += 1
                job_id = f"{base}-{n}"
            now = time.time()
            job = {"id": job_id, "state": "queued", "created": now, "updated": now,
                   "history": [{"state": "queued", "t": now}], **merged}
            self._write(job)
            return job

    def update(self, job_id, body):
        with self.lock:
            job = self.get(job_id)
            if job is None:
                return None
            if "state" in body and body["state"] not in STATES:
                raise ValueError(f"unknown state {body['state']}")
            # A cancelled job stays cancelled: late updates from the agent/worker must not revive it.
            if job["state"] == "cancelled" and body.get("state") not in (None, "cancelled") and not body.get("retry"):
                raise Conflict(f"job {job_id} is cancelled")
            if body.get("state") == "cancelled" and job["state"] == "done":
                raise Conflict(f"job {job_id} is already {job['state']}")
            body.pop("retry", None)
            now = time.time()
            if body.get("state") and body["state"] != job["state"]:
                job["history"].append({"state": body["state"], "t": now, "message": body.get("message")})
            job.update(body)
            job["updated"] = now
            self._write(job)
            return job

    def claim(self, from_state, to_state, worker):
        with self.lock:
            for job in self.all():
                if job["state"] == from_state:
                    now = time.time()
                    job.update(state=to_state, worker=worker, updated=now)
                    job["history"].append({"state": to_state, "t": now, "worker": worker})
                    self._write(job)
                    return job
        return None


def make_handler(store, token, results_dir):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, obj=None):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if obj is not None:
                self.wfile.write(json.dumps(obj).encode())

        def _auth(self):
            if token and not hmac.compare_digest(self.headers.get("X-Token", ""), token):
                self._send(401, {"error": "bad token"})
                return False
            return True

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _send_file(self, path):
            size = path.stat().st_size
            start = 0
            m = re.fullmatch(r"bytes=(\d+)-", self.headers.get("Range", ""))
            if m:
                start = int(m.group(1))
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
            self.send_response(206 if start else 200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size - start))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            if start:
                self.send_header("Content-Range", f"bytes {start}-{size - 1}/{size}")
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                shutil.copyfileobj(f, self.wfile, 1 << 20)

        def _result_file(self, job_id, rel):
            """Resolve <results_dir>/<job_id>/<rel>, refusing anything outside the allowed result items."""
            root = (results_dir / job_id).resolve()
            p = (root / rel).resolve()
            if not p.is_relative_to(root) or p == root or p.relative_to(root).parts[0] not in RESULT_ITEMS:
                return None
            return p if p.is_file() else None

        def do_GET(self):
            url = urlsplit(self.path)
            path = unquote(url.path)
            if path == "/health":
                return self._send(200, {"ok": True})
            if not self._auth():
                return
            if path == "/jobs":
                return self._send(200, store.all())
            m = re.fullmatch(r"/jobs/([\w.-]+)(?:/(bundle|files)(?:/(.+))?)?", path)
            job = m and store.get(m.group(1))
            if not job:
                return self._send(404, {"error": "not found"})
            jid, what, rel = m.groups()
            if what is None:
                return self._send(200, job)
            if what == "bundle":
                tar = results_dir / f"{jid}.tar"
                if not tar.is_file():
                    return self._send(404, {"error": f"no bundle yet (state {job['state']})"})
                return self._send_file(tar)
            if rel is None:
                root = results_dir / jid
                files = sorted((f for item in RESULT_ITEMS if (root / item).exists()
                                for f in ([root / item] if (root / item).is_file() else (root / item).rglob("*"))
                                if f.is_file() and not f.name.endswith(SKIP_SUFFIXES)), key=str)
                if "detail=1" in url.query:
                    out = []
                    for f in files:
                        try:
                            st = f.stat()
                        except OSError:  # deleted while listing (e.g. an old checkpoint)
                            continue
                        out.append({"path": str(f.relative_to(root)), "size": st.st_size, "mtime": st.st_mtime})
                    return self._send(200, out)
                return self._send(200, [str(f.relative_to(root)) for f in files])
            f = self._result_file(jid, rel)
            return self._send_file(f) if f else self._send(404, {"error": f"no such file {rel}"})

        def do_POST(self):
            if not self._auth():
                return
            try:
                body = self._body()
                if self.path == "/jobs":
                    return self._send(201, store.create(body))
                if self.path == "/jobs/claim":
                    job = store.claim(body["from"], body["to"], body.get("worker", "?"))
                    return self._send(200, job) if job else self._send(204)
                m = re.fullmatch(r"/jobs/([\w.-]+)", self.path)
                job = m and store.update(m.group(1), body)
                return self._send(200, job) if job else self._send(404, {"error": "not found"})
            except Conflict as e:
                return self._send(409, {"error": str(e)})
            except (ValueError, KeyError) as e:
                return self._send(400, {"error": str(e)})

        def log_message(self, fmt, *args):
            print(f"[server] {self.address_string()} {fmt % args}", flush=True)

    return H


def main():
    ap = argparse.ArgumentParser(description="scanbot job server")
    ap.add_argument("--config")
    args = ap.parse_args()
    full = load_config(args.config)
    cfg = full["server"]
    store = Store(cfg.get("jobs_dir", "~/scanbot/jobs"))
    results = Path(cfg.get("results_dir") or full.get("worker", {}).get("data_dir", "~/scanbot/data")).expanduser()
    host, port = cfg.get("host", "0.0.0.0"), int(cfg.get("port", 8800))
    if not cfg.get("token") or cfg.get("token") == "change-me":
        print("WARNING: [server].token is empty or 'change-me'; anyone on the network can submit jobs")
    print(f"scanbot server on http://{host}:{port}, jobs in {store.root}, results in {results}", flush=True)
    ThreadingHTTPServer((host, port), make_handler(store, cfg.get("token", ""), results)).serve_forever()


if __name__ == "__main__":
    main()
