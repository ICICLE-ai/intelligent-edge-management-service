"""DGX reconstruction worker: frames_ready -> [pull frames from Jetson] -> recon script -> downloadable bundle.

  source ~/ai-testbed/activate.sh
  python -m scanbot.worker --config config.toml           # loop forever
  python -m scanbot.worker --config config.toml --once    # handle one job and exit

All reconstruction happens in ONE script ([worker].recon_cmd, default ~/ai-testbed/scripts/recon_turntable.sh):
  recon_turntable.sh <raw_frames_dir> <out_dir>
run with cwd = out_dir = <data_dir>/<job_id>/. Expected outputs (relative to out_dir):
  proc/{images,masks,masks_hull}  colmap/sparse/0  nerf/**/config.yml + nerfstudio_models/*.ckpt
  videos/colmap_poses.mp4  videos/nerf_orbit.mp4
Extra env for the script: SCANBOT_JOB_ID, SCANBOT_JOB_JSON, and MAX_ITERS if the job sets recon.max_iterations.

Job dir <data_dir>/<job_id>/: frames/ (from Jetson, + frames.csv), video/ (raw MP4), the outputs above,
recon.log, job.json, MANIFEST.txt. Bundle: <data_dir>/<job_id>.tar, served by the job server.
"""
import argparse
import json
import os
import shlex
import signal
import socket
import subprocess
import time
import traceback
from pathlib import Path

from .api import JobAPI
from .config import load_config
from .server import RESULT_ITEMS

# (label, glob relative to the job dir) in pipeline order; used for progress and the final check.
OUTPUTS = [("masks", "proc/masks/*"), ("colmap", "colmap/sparse/0/images.*"),
           ("nerf config", "nerf/**/config.yml"), ("nerf checkpoint", "nerf/**/nerfstudio_models/*.ckpt"),
           ("poses video", "videos/colmap_poses.mp4"), ("nerf video", "videos/nerf_orbit.mp4")]


class Cancelled(Exception):
    pass


def check_cancel(api, jid):
    try:
        if api.get(jid)["state"] == "cancelled":
            raise Cancelled(jid)
    except Cancelled:
        raise
    except Exception as e:  # server hiccup: keep going
        print(f"[worker] cancel check failed: {e}")


def kill_tree(proc):
    """Stop the recon script and everything it started (ns-train, colmap, ffmpeg): it runs in its own session."""
    for sig, wait in ((signal.SIGTERM, 15), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(wait)
            return
        except subprocess.TimeoutExpired:
            pass


def sh(cmd):
    cmd = list(map(str, cmd))
    print("[worker] $", shlex.join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def found(d):
    return [label for label, pat in OUTPUTS if next(d.glob(pat), None)]


def tail(path, n=5):
    """Last n non-empty lines of the current run (the log is appended to; runs start with '=====')."""
    try:
        lines = Path(path).read_text(errors="replace").split("\n===== ")[-1].splitlines()[1:]
    except OSError:
        return ""
    return " | ".join(line.strip()[-160:] for line in lines[-n:] if line.strip())


def pull_frames(job, d, cfg, api):
    """Get frames + video into d/frames and d/video (pull them from the Jetson if the agent left them there)."""
    tr = job.get("transfer", {})
    if tr.get("mode") == "pull":
        host = cfg["worker"].get("pull_host", "jetson")
        api.update(job["id"], "pulling_frames", f"rsync from {host}:{tr['path']}")
        (d / "frames").mkdir(parents=True, exist_ok=True)
        (d / "video").mkdir(exist_ok=True)
        ssh = "ssh -o BatchMode=yes -o ConnectTimeout=20"
        sh(["rsync", "-a", "--partial", "-e", ssh, f"{host}:{tr['path']}/images/", f"{d}/frames/"])
        if tr.get("video"):
            sh(["rsync", "-a", "--partial", "-e", ssh, f"{host}:{tr['path']}/{tr['video']}", f"{d}/video/"])
    elif not (d / "frames").is_dir() and (d / "images").is_dir():  # older push layout
        (d / "images").rename(d / "frames")
    n = len(list((d / "frames").glob("*.jpg"))) if (d / "frames").is_dir() else 0
    if n == 0:
        raise RuntimeError(f"no frames in {d / 'frames'}")
    return n


def reconstruct(job, d, cfg, api):
    w = cfg["worker"]
    cmd = [str(Path(w.get("recon_cmd", "~/ai-testbed/scripts/recon_turntable.sh")).expanduser()),
           str(d / "frames"), str(d)]
    rc = job.get("recon", {})
    env = dict(os.environ, SCANBOT_JOB_ID=job["id"], SCANBOT_JOB_JSON=str(d / "job.json"))
    if rc.get("max_iterations"):  # otherwise the script's own default applies
        env["MAX_ITERS"] = str(rc["max_iterations"])
    log = d / "recon.log"
    print("[worker] $", shlex.join(cmd), f"(log: {log})", flush=True)
    timeout = float(w.get("recon_timeout_h", 6)) * 3600
    with open(log, "a") as f:
        f.write(f"\n===== {time.ctime()} $ {shlex.join(cmd)}\n")
        f.flush()
        proc = subprocess.Popen(cmd, cwd=d, stdout=f, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        t0, last = time.time(), None
        while proc.poll() is None:
            time.sleep(float(w.get("progress_s", 15)))
            try:
                check_cancel(api, job["id"])
            except Cancelled:
                kill_tree(proc)
                f.write(f"\n===== {time.ctime()} cancelled by user, recon script stopped\n")
                raise
            done = found(d)
            msg = f"{len(done)}/{len(OUTPUTS)} outputs ({', '.join(done) or 'none yet'}), {(time.time() - t0) / 60:.0f} min"
            if msg != last:
                try:
                    api.update(job["id"], message=msg, progress=round(len(done) / len(OUTPUTS), 2),
                               log_tail=tail(log, 2))
                except Exception as e:  # progress is best effort
                    print(f"[worker] progress update failed: {e}")
                last = msg
            if time.time() - t0 > timeout:
                kill_tree(proc)
                raise RuntimeError(f"recon script timed out after {timeout / 3600:.1f} h")
    if proc.returncode:
        raise RuntimeError(f"recon script exit {proc.returncode}: {tail(log)}")


def package(job, d, cfg, api):
    jid = job["id"]
    api.update(jid, "packaging", "building bundle")
    missing = [label for label, _ in OUTPUTS if label not in found(d)]
    if not next((d / "video").glob("*"), None):
        missing.append("raw video")
    (d / "job.json").write_text(json.dumps(api.get(jid), indent=2))
    items = [i for i in RESULT_ITEMS if (d / i).exists() and i != "MANIFEST.txt"]
    files = sorted(str(f.relative_to(d)) for i in items for f in ([d / i] if (d / i).is_file() else (d / i).rglob("*"))
                   if f.is_file())
    (d / "MANIFEST.txt").write_text(f"job {jid}\nmissing: {', '.join(missing) or 'nothing'}\n\n" + "\n".join(files) + "\n")
    tar = d.parent / f"{jid}.tar"
    excludes = [f"--exclude={x}" for x in cfg["worker"].get("bundle_exclude", ["*.db"])]
    # Plain tar, no gzip: JPEGs, MP4s and checkpoints barely compress, and gzip on GBs is slow.
    # --dereference: store files, not symlinks (e.g. colmap/cmasks -> absolute paths, which would dangle on the
    # user's machine and which Python's safe tar extraction refuses).
    sh(["tar", "-cf", f"{tar}.part", "--dereference", *excludes, "-C", d.parent, *[f"{jid}/{i}" for i in items + ["MANIFEST.txt"]]])
    Path(f"{tar}.part").replace(tar)
    return tar, missing, len(files)


def process(job, cfg, api):
    jid = job["id"]
    d = Path(cfg["worker"]["data_dir"]).expanduser() / jid
    d.mkdir(parents=True, exist_ok=True)
    (d.parent / f"{jid}.tar").unlink(missing_ok=True)  # stale bundle from an earlier run (retry)
    (d / "job.json").write_text(json.dumps(job, indent=2))
    n = pull_frames(job, d, cfg, api)
    check_cancel(api, jid)
    api.update(jid, "reconstructing", f"{n} frames -> recon script", progress=0.0)
    reconstruct(job, d, cfg, api)
    tar, missing, nfiles = package(job, d, cfg, api)
    st = cfg.get("storage", {})
    if st.get("url"):  # optional copy to S3 / a shared dir (off by default)
        from .storage import Storage
        api.update(jid, "uploading_results", f"copy to {st['url']}")
        Storage(st).upload_file(tar, tar.name)
    mb = tar.stat().st_size / 2**20
    msg = f"bundle {mb:.0f} MB, {nfiles} files" + (f"; MISSING: {', '.join(missing)}" if missing else "")
    api.update(jid, "done", msg, progress=1.0, bundle={"name": tar.name, "mb": round(mb, 1), "files": nfiles},
               missing=missing)
    print(f"[worker] job {jid} done -> {tar} ({msg})", flush=True)


def main():
    ap = argparse.ArgumentParser(description="scanbot DGX reconstruction worker")
    ap.add_argument("--config")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config)
    api = JobAPI(cfg["server"]["url"], cfg["server"].get("token", ""))
    name = cfg["worker"].get("name", f"{socket.gethostname()}-worker")
    print(f"[worker] {name} polling {cfg['server']['url']}", flush=True)
    while True:
        try:
            job = api.claim("frames_ready", "pulling_frames", name)
        except Exception as e:
            print(f"[worker] server unreachable: {e}")
            job = None
        if job:
            try:
                process(job, cfg, api)
            except Cancelled:
                print(f"[worker] job {job['id']} cancelled, stopped", flush=True)
            except Exception as e:
                traceback.print_exc()
                try:
                    api.update(job["id"], "failed", f"reconstruction: {e}")
                except Exception:
                    pass
        if args.once:
            if not job:
                print("[worker] no job ready")
            return
        if not job:
            time.sleep(float(cfg["worker"].get("poll_s", 10)))


if __name__ == "__main__":
    main()
