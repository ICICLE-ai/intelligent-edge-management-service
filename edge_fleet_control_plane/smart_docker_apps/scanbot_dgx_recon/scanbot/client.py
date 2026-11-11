"""Remote user's tool: request a scan, follow it, download the results.

  python -m scanbot.client submit "coffee mug" [--rotations 1 --iterations 30000 --wait --download DEST]
                                                        # --download: results arrive in DEST as they are produced
  python -m scanbot.client list                         # all jobs
  python -m scanbot.client status JOB_ID                # one job, with history
  python -m scanbot.client wait JOB_ID
  python -m scanbot.client files JOB_ID                 # what can be downloaded
  python -m scanbot.client download JOB_ID [--dest results/]          # whole bundle, unpacked
  python -m scanbot.client download JOB_ID --follow                   # fetch each result as it is produced
  python -m scanbot.client cancel JOB_ID                # stop a queued or running job
  python -m scanbot.client download JOB_ID --file videos/nerf_orbit.mp4
  python -m scanbot.client retry JOB_ID                 # re-run reconstruction on the same frames
  python -m scanbot.client export results/JOB_ID        # ns-export pointcloud locally (needs nerfstudio + GPU)

Server address/token: [server] url + token from --config / $SCANBOT_CONFIG / ./config.toml, or
--url/--token, or $SCANBOT_URL / $SCANBOT_TOKEN (no config file needed). Needs only `requests`.
"""
import argparse
import json
import os
import subprocess
import sys
import tarfile
import time
from datetime import datetime
from pathlib import Path

from .api import FINAL, JobAPI
from .config import load_config


def show(job):
    t = datetime.fromtimestamp(job["updated"]).strftime("%m-%d %H:%M:%S")
    msg = job.get("message") or job["history"][-1].get("message") or ""
    print(f"{job['id']:36} {job['state']:16} {t}  {msg}")


# Rewritten in place during training (every few thousand steps, older ones deleted): fetch once the job is done.
LATE = ("nerfstudio_models/", "events.out.tfevents", "recon.log", "job.json", "MANIFEST.txt")


def follow(api, job_id, dest, poll=10):
    """Mirror a job's result files into dest/<job_id>/ while it runs.

    A file is fetched once its size and mtime are unchanged between two polls, so nothing half-written is
    copied. Checkpoints, logs and job.json are fetched when the job finishes. Returns the final job.
    """
    root = Path(dest) / job_id
    seen, got, last_state = {}, {}, None
    while True:
        job = api.get(job_id)
        final = job["state"] in FINAL
        if (job["state"], job.get("message")) != last_state:
            show(job)
            last_state = (job["state"], job.get("message"))
        try:
            files = api.files(job_id, detail=True)
        except Exception as e:  # no results dir yet, or a network blip
            files = []
            if "404" not in str(e):
                print(f"  (listing failed: {e})")
        new = []
        for f in files:
            sig = (f["size"], f["mtime"])
            stable = seen.get(f["path"]) == sig
            seen[f["path"]] = sig
            if got.get(f["path"]) == sig or not (stable or final):
                continue
            if not final and any(x in f["path"] for x in LATE):
                continue
            new.append(f)
        for f in new:
            out = root / f["path"]
            out.parent.mkdir(parents=True, exist_ok=True)
            try:
                api.download(job_id, out, path=f["path"], progress=f["size"] > 50 * 2**20)
                got[f["path"]] = (f["size"], f["mtime"])
            except Exception as e:  # e.g. a checkpoint replaced by a newer one mid-download
                print(f"  skipped {f['path']}: {e}")
        if new:
            groups = {}
            for f in new:
                groups[f["path"].split("/")[0]] = groups.get(f["path"].split("/")[0], 0) + 1
            print("  received " + ", ".join(f"{k}/ ({n})" if n > 1 else k for k, n in groups.items())
                  + f"  -> {root}")
        if final:
            return job
        time.sleep(poll)


def server_args(args):
    url = args.url or os.environ.get("SCANBOT_URL")
    token = args.token or os.environ.get("SCANBOT_TOKEN", "")
    if not url:
        srv = load_config(args.config)["server"]
        url, token = srv["url"], token or srv.get("token", "")
    return url, token


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--url", help="job server, e.g. http://localhost:8800")
    ap.add_argument("--token")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit")
    s.add_argument("object")
    s.add_argument("--rotations", type=float, default=1)
    s.add_argument("--target-fps", type=float, default=5.0)
    s.add_argument("--method", default="nerfacto")
    s.add_argument("--iterations", type=int, help="nerfacto iterations (default: the recon script's)")
    s.add_argument("--wait", action="store_true")
    s.add_argument("--download", metavar="DEST", help="with --wait: download the bundle here when done")
    sub.add_parser("list")
    sub.add_parser("status").add_argument("job_id", nargs="?")
    sub.add_parser("wait").add_argument("job_id")
    sub.add_parser("files").add_argument("job_id")
    sub.add_parser("retry").add_argument("job_id")
    sub.add_parser("cancel").add_argument("job_id")
    d = sub.add_parser("download")
    d.add_argument("job_id")
    d.add_argument("--dest", default="results")
    d.add_argument("--file", help="one file of the bundle (see `files`), instead of the whole bundle")
    d.add_argument("--keep-tar", action="store_true", help="keep <job>.tar instead of unpacking it")
    d.add_argument("--follow", action="store_true", help="fetch result files as they are produced, until the job ends")
    e = sub.add_parser("export")
    e.add_argument("job_dir")
    e.add_argument("--num-points", type=int, default=1000000)
    args = ap.parse_args()

    if args.cmd == "export":  # local only, no server needed
        job_dir = Path(args.job_dir)
        configs = sorted(job_dir.glob("nerf/**/config.yml"))
        if not configs:
            sys.exit(f"no nerf/**/config.yml in {job_dir}")
        cmd = ["ns-export", "pointcloud", "--load-config", str(configs[-1].relative_to(job_dir)),
               "--output-dir", "exports_local", "--num-points", str(args.num_points),
               "--remove-outliers", "True", "--normal-method", "open3d"]
        print("$", " ".join(cmd), f"(in {job_dir})")
        sys.exit(subprocess.call(cmd, cwd=job_dir))

    api = JobAPI(*server_args(args))

    if args.cmd == "submit":
        job = api.create({"object": args.object,
                          "capture": {"rotations": args.rotations},
                          "extract": {"target_fps": args.target_fps},
                          "recon": {"method": args.method,
                                    **({"max_iterations": args.iterations} if args.iterations else {})}})
        print(f"submitted {job['id']}")
        if not args.wait:
            return
        args.job_id = job["id"]
        args.cmd = "wait"

    if args.cmd == "list" or (args.cmd == "status" and not args.job_id):
        for job in api.list():
            show(job)
    elif args.cmd == "status":
        job = api.get(args.job_id)
        show(job)
        for h in job["history"]:
            print(f"   {datetime.fromtimestamp(h['t']):%H:%M:%S}  {h['state']:16} {h.get('message') or h.get('worker') or ''}")
        for k in ("progress", "log_tail", "bundle", "missing"):
            if job.get(k) not in (None, [], ""):
                print(f"   {k}: {json.dumps(job[k]) if not isinstance(job[k], str) else job[k]}")
    elif args.cmd == "wait" and getattr(args, "download", None):
        job = follow(api, args.job_id, args.download)
        print(f"job {job['state']}: results in {Path(args.download) / job['id']}")
        sys.exit(0 if job["state"] == "done" else 1)
    elif args.cmd == "wait":
        last = None
        while True:
            job = api.get(args.job_id)
            now = (job["state"], job.get("message"))
            if now != last:
                show(job)
                last = now
            if job["state"] in FINAL:
                if job["state"] != "done":
                    sys.exit(1)
                print(f"download with: python -m scanbot.client download {job['id']}")
                return
            time.sleep(10)

    if args.cmd == "files":
        for f in api.files(args.job_id):
            print(f)
    elif args.cmd == "cancel":
        job = api.cancel(args.job_id)
        print(f"{job['id']} -> {job['state']} (a running reconstruction stops within ~15 s; "
              f"a turntable capture in progress finishes its rotation first)")
    elif args.cmd == "retry":
        job = api.update(args.job_id, "frames_ready", "retry requested by user", retry=True)
        print(f"{job['id']} -> {job['state']} (the worker will pick it up)")
    elif args.cmd == "download":
        dest = Path(args.dest)
        dest.mkdir(parents=True, exist_ok=True)
        if args.follow:
            job = follow(api, args.job_id, dest)
            print(f"job {job['state']}: results in {dest / job['id']}")
            return
        if args.file:
            out = dest / args.job_id / args.file
            out.parent.mkdir(parents=True, exist_ok=True)
            print(f"downloaded {api.download(args.job_id, out, path=args.file)}")
            return
        job = api.get(args.job_id)
        if job["state"] != "done":
            print(f"warning: job is {job['state']}, not done; trying anyway")
        tar = api.download(args.job_id, dest / f"{args.job_id}.tar")
        if args.keep_tar:
            print(f"downloaded {tar}")
            return
        with tarfile.open(tar) as t:
            try:
                t.extractall(dest, filter="data")
            except TypeError:  # Python < 3.11.4 has no extraction filters
                t.extractall(dest)
        tar.unlink()
        root = dest / args.job_id
        print(f"unpacked to {root}")
        manifest = root / "MANIFEST.txt"
        if manifest.exists():
            print("  " + manifest.read_text().splitlines()[1])
        for label, pat in [("frames", "frames/*.jpg"), ("raw video", "video/*"), ("COLMAP model", "colmap/sparse/0"),
                           ("masks", "proc/masks"), ("nerf config", "nerf/**/config.yml"),
                           ("checkpoint", "nerf/**/nerfstudio_models/*.ckpt"),
                           ("poses video", "videos/colmap_poses.mp4"), ("nerf video", "videos/nerf_orbit.mp4")]:
            hits = sorted(root.glob(pat))
            print(f"  {label:13} {hits[-1].relative_to(dest) if hits else '-- missing --'}"
                  + (f"  ({len(hits)} files)" if len(hits) > 1 else ""))


if __name__ == "__main__":
    main()
