"""Small HTTP client for the job server (scanbot.server)."""
import time
from pathlib import Path

from urllib.parse import quote

import requests

STATES = ["queued", "capturing", "extracting", "uploading_frames", "frames_ready",
          "pulling_frames", "reconstructing", "packaging", "exporting", "uploading_results", "done", "failed", "cancelled"]
FINAL = ("done", "failed", "cancelled")


class JobAPI:
    def __init__(self, url, token="", timeout=30, retries=4):
        self.url = url.rstrip("/")
        self.s = requests.Session()
        if token:
            self.s.headers["X-Token"] = token
        self.timeout = timeout
        self.retries = retries

    def _req(self, method, path, **kw):
        # Retry connection errors: Jetson WiFi power save can drop the first packets after idle.
        for i in range(self.retries):
            try:
                r = self.s.request(method, self.url + path, timeout=self.timeout, **kw)
                break
            except (requests.ConnectionError, requests.Timeout) as e:
                # a timed-out POST may have been applied already, so only GETs retry on timeout
                if i == self.retries - 1 or (isinstance(e, requests.ReadTimeout) and method != "GET"):
                    raise
                time.sleep(2 ** i)
        if r.status_code == 204:
            return None
        if r.status_code >= 400:
            try:
                msg = r.json().get("error", r.text)
            except ValueError:
                msg = r.text
            raise requests.HTTPError(f"{r.status_code} {method} {path}: {msg}", response=r)
        return r.json()

    def create(self, spec):
        return self._req("POST", "/jobs", json=spec)

    def get(self, job_id):
        return self._req("GET", f"/jobs/{job_id}")

    def list(self):
        return self._req("GET", "/jobs")

    def claim(self, from_state, to_state, worker):
        """Atomically take the oldest job in `from_state`; returns the job or None."""
        return self._req("POST", "/jobs/claim", json={"from": from_state, "to": to_state, "worker": worker})

    def update(self, job_id, state=None, message=None, **fields):
        body = {k: v for k, v in {"state": state, "message": message, **fields}.items() if v is not None}
        return self._req("POST", f"/jobs/{job_id}", json=body)

    def files(self, job_id, detail=False):
        """Result files so far: names, or with detail=True dicts {path, size, mtime}."""
        return self._req("GET", f"/jobs/{job_id}/files" + ("?detail=1" if detail else ""))

    def cancel(self, job_id, message="cancelled by user"):
        return self._req("POST", f"/jobs/{job_id}", json={"state": "cancelled", "message": message})

    def download(self, job_id, dest, path=None, progress=True):
        """Stream the job bundle (or one file of it) to `dest`; resumes a partial `dest.part`."""
        dest = Path(dest)
        part = dest.with_name(dest.name + ".part")
        url = self.url + (f"/jobs/{job_id}/files/{quote(path)}" if path else f"/jobs/{job_id}/bundle")
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        with self.s.get(url, stream=True, timeout=self.timeout, headers=headers) as r:
            if r.status_code == 416:  # already complete
                part.replace(dest)
                return dest
            if r.status_code >= 400:
                raise requests.HTTPError(f"{r.status_code} GET {url}: {r.text[:200]}", response=r)
            mode = "ab" if r.status_code == 206 else "wb"
            total = int(r.headers.get("Content-Length", 0)) + (have if mode == "ab" else 0)
            done = have if mode == "ab" else 0
            t0 = time.time()
            with open(part, mode) as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    if progress and time.time() - t0 > 1:
                        print(f"\r  {done / 2**20:.0f} / {total / 2**20:.0f} MB", end="", flush=True)
                        t0 = time.time()
            if progress:
                print(f"\r  {done / 2**20:.0f} / {total / 2**20:.0f} MB")
        part.replace(dest)
        return dest
