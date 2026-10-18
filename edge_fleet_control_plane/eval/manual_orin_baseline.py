#!/usr/bin/env python3
"""Time the same UNet++ live container by hand (SSH + Docker), Python 3.6-safe.

Same artifacts and done criterion as IEMS RUNNING: docker inspect State.Running
after a short grace period. Does not go through MQTT or the portal.

  # On the Orin, engine already on disk (use the IEMS deployment copy):
  python3 manual_orin_baseline.py --mode cold \\
      --engine /opt/icicle-edge/deployments/<uid>/model/unetpp_teacher_512x768_fp16.engine

  python3 manual_orin_baseline.py --mode warm \\
      --engine /opt/icicle-edge/deployments/<uid>/model/unetpp_teacher_512x768_fp16.engine

Cold: docker rmi (if present) + docker pull + docker run.
Warm: docker rm -f + docker run, no pull (IEMS Restart).

Prints one CSV line to stdout. Append to a file:

  python3 manual_orin_baseline.py --mode cold --engine ... >> baseline.csv
"""

import argparse
import csv
import os
import subprocess
import sys
import time
from io import StringIO


DEFAULT_IMAGE = "habg21/unet-trt-infer-live:latest"
DEFAULT_NAME = "unetpp_infer_live_baseline"
CONFIRM_S = 2.5


def run(cmd, **kwargs):
    kwargs.setdefault("universal_newlines", True)
    return subprocess.run(cmd, **kwargs)


def docker_running(name):
    result = run(
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip().lower() == "true"


def confirm_running(name, timeout_s=CONFIRM_S):
    deadline = time.monotonic() + timeout_s
    saw = False
    while time.monotonic() < deadline:
        state = docker_running(name)
        if state is True:
            saw = True
        elif state is False:
            raise RuntimeError("container exited immediately: %s" % name)
        time.sleep(0.35)
    if not saw or docker_running(name) is not True:
        raise RuntimeError("container not running after start: %s" % name)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--mode", choices=("cold", "warm"), required=True)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--name", default=DEFAULT_NAME)
    parser.add_argument("--engine", required=True, help="Host path to the .engine file")
    parser.add_argument(
        "--ingest",
        default="",
        help="Optional STREAM_INGEST_URL (omit if you only care about inspect Running)",
    )
    args = parser.parse_args()
    engine = os.path.abspath(args.engine)
    if not os.path.isfile(engine):
        sys.exit("engine not found: %s" % engine)
    model_dir = os.path.dirname(engine)

    run(["docker", "rm", "-f", args.name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    pull_s = None
    if args.mode == "cold":
        run(["docker", "rmi", "-f", args.image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        began = time.monotonic()
        pull = run(["docker", "pull", args.image])
        pull_s = round(time.monotonic() - began, 4)
        if pull.returncode != 0:
            sys.exit("docker pull failed")
    else:
        inspect = run(
            ["docker", "image", "inspect", args.image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if inspect.returncode != 0:
            sys.exit("warm run needs the image already present; pull once or use --mode cold")

    cmd = [
        "docker", "run", "-d", "--name", args.name,
        "--runtime", "nvidia", "--gpus", "all",
        "--network", "host", "--ipc", "host", "--privileged",
        "-e", "NVIDIA_DRIVER_CAPABILITIES=all",
        "-e", "SHOW_WINDOW=0",
        "-e", "ENGINE_PATH=/workspace/models/unetpp_teacher_512x768_fp16.engine",
        "-e", "DETECT_EVERY_N_FRAMES=4",
        "-e", "STREAM_FPS=8",
        "-e", "STREAM_WIDTH=960",
        "-e", "STREAM_HEIGHT=540",
        "--mount", "type=bind,source=/tmp/argus_socket,target=/tmp/argus_socket",
        "-v", "%s:/workspace/models:ro" % model_dir,
    ]
    if args.ingest:
        cmd += ["-e", "STREAM_INGEST_URL=%s" % args.ingest]
    cmd.append(args.image)

    began = time.monotonic()
    started = run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if started.returncode != 0:
        sys.exit((started.stderr or started.stdout or "docker run failed").strip()[:500])
    confirm_running(args.name)
    run_s = round(time.monotonic() - began, 4)
    total_s = round((pull_s or 0.0) + run_s, 4)

    row = {
        "mode": args.mode,
        "path": "manual_docker",
        "image": args.image,
        "engine": engine,
        "pull_s": pull_s if pull_s is not None else "",
        "run_s": run_s,
        "total_s": total_s,
        "image_cached": args.mode == "warm",
        "done": "inspect_running",
    }
    buf = StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(row.keys()))
    if sys.stdout.isatty():
        writer.writeheader()
    writer.writerow(row)
    sys.stdout.write(buf.getvalue())
    sys.stderr.write(
        "container %s is running. Stop it with: docker rm -f %s\n" % (args.name, args.name)
    )


if __name__ == "__main__":
    main()
