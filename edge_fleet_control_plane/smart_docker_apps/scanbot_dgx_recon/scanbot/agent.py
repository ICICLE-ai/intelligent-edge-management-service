"""Jetson capture agent: pulls scan jobs from the DGX job server and runs them.

  python -m scanbot.agent --config config.toml          # loop forever
  python -m scanbot.agent --config config.toml --once   # handle one job and exit

Per job: rotate turntable + record -> fetch video -> extract good frames -> hand frames + video to the DGX
(push: rsync to it; pull: the DGX worker rsyncs from us) -> frames_ready.
The agent only makes outgoing HTTP connections (polling); in pull mode the DGX also needs SSH to the Jetson.
"""
import argparse
import json
import shlex
import socket
import subprocess
import time
import traceback
from pathlib import Path

from .api import JobAPI
from .camera import make_camera
from .config import load_config
from .frames import extract_frames
from .turntable import make_turntable


def capture(job, cfg, workdir):
    turntable = make_turntable(cfg["turntable"])
    camera = make_camera(cfg["camera"])
    rotations = float(job["capture"].get("rotations", 1))
    lead = float(cfg["camera"].get("lead_in_s", 1))
    try:
        camera.prepare()
        camera.start()
        time.sleep(lead)
        print(f"[agent] rotating {rotations} rotation(s) while recording")
        turntable.rotate(rotations, job["capture"].get("seconds"))
        time.sleep(lead)
        camera.stop()
        return camera.fetch(workdir)
    finally:
        try:
            turntable.stop()
        except Exception:
            pass
        turntable.close()
        camera.finish()  # e.g. switch WiFi back to the campus network


def rsync(src, dest):
    """rsync a file or a dir's contents; dest may be 'user@host:path' or a local path."""
    if ":" in dest:
        host, path = dest.split(":", 1)
        subprocess.run(["ssh", "-o", "BatchMode=yes", host, f"mkdir -p {shlex.quote(path)}"], check=True)
    else:
        dest = str(Path(dest).expanduser())
        Path(dest).mkdir(parents=True, exist_ok=True)
    src = f"{src}/" if Path(src).is_dir() else str(src)
    subprocess.run(["rsync", "-a", "--partial", src, dest.rstrip("/") + "/"], check=True)


def send_frames(workdir, video, job_id, cfg):
    """[transfer].mode = "push": rsync to <data_dir>/<job_id>/{frames,video}/ (needs SSH Jetson -> DGX).
    "pull" (default): leave everything in work_dir; the DGX worker rsyncs it from here (SSH DGX -> Jetson)."""
    tr = cfg.get("transfer", {})
    if tr.get("mode", "pull") == "pull":
        return {"mode": "pull", "path": str(workdir.resolve()), "video": Path(video).name}
    target = f"{tr['data_dir'].rstrip('/')}/{job_id}"
    rsync(workdir / "images", f"{target}/frames")
    rsync(video, f"{target}/video")
    return {"mode": "push", "path": target, "video": Path(video).name}


def run_job(job, cfg, api):
    jid = job["id"]
    workdir = Path(cfg["agent"].get("work_dir", "~/scanbot/work")).expanduser() / jid
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "job.json").write_text(json.dumps(job, indent=2))

    video = capture(job, cfg, workdir)
    api.update(jid, "extracting", f"video {Path(video).name} ({Path(video).stat().st_size / 1e6:.0f} MB)")

    # Turntable footage: the default motion filter rejects everything, so the job spec sends min_motion = 0.
    ex = job.get("extract", {})
    stats = extract_frames(video, workdir / "images", target_fps=ex.get("target_fps", 5.0),
                           min_motion=ex.get("min_motion", 0.0), blur_ratio=ex.get("blur_ratio", 0.4),
                           max_dim=ex.get("max_dim", 0), progress=False)
    if stats["kept"] < int(cfg["agent"].get("min_frames", 20)):
        raise RuntimeError(f"only {stats['kept']} good frames; check lighting/motion or settings")

    api.update(jid, "uploading_frames", f"{stats['kept']} frames", frames=stats)
    transfer = send_frames(workdir, video, jid, cfg)
    api.update(jid, "frames_ready", f"{stats['kept']} frames ({transfer['mode']}: {transfer['path']})",
               transfer=transfer)
    if not cfg["agent"].get("keep_video", True) and transfer["mode"] == "push":
        Path(video).unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser(description="scanbot Jetson capture agent")
    ap.add_argument("--config")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config)
    api = JobAPI(cfg["server"]["url"], cfg["server"].get("token", ""))
    worker = cfg["agent"].get("name", socket.gethostname())
    poll = float(cfg["agent"].get("poll_s", 5))
    print(f"[agent] {worker} polling {cfg['server']['url']}")
    while True:
        try:
            job = api.claim("queued", "capturing", worker)
        except Exception as e:  # server down / network blip: keep trying
            print(f"[agent] server unreachable: {e}")
            job = None
        if job:
            print(f"[agent] job {job['id']}: {job['object']}")
            try:
                run_job(job, cfg, api)
                print(f"[agent] job {job['id']} frames sent")
            except Exception as e:
                traceback.print_exc()
                try:
                    api.update(job["id"], "failed", f"capture: {e}")
                except Exception:
                    pass
            if args.once:
                return
        elif args.once:
            print("[agent] no queued job")
            return
        else:
            time.sleep(poll)


if __name__ == "__main__":
    main()
