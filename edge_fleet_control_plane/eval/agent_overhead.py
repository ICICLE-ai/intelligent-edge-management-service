#!/usr/bin/env python3
"""Measure the edge agent's own CPU and memory on a Jetson.

Run on the board (Python 3.6+, stdlib only, reads /proc):

    python3 agent_overhead.py --label idle --seconds 600
    python3 agent_overhead.py --label busy --seconds 600

Samples the ``icicle-edge-agent`` process once per second. CPU includes the
short-lived ``docker`` commands the agent launches (their time is added to the
agent's cutime/cstime when it waits for them, and live ones are summed too).
CPU percent is of one core; ``cores`` is printed so it can be scaled.

Prints one CSV summary line (header + row) at the end. ``--samples FILE``
also writes the per-second samples.

Impact mode (needs sudo; stops and starts the agent):

    sudo python3 agent_overhead.py --impact --container <name> --block 300 --cycles 2

With the app running, alternates the agent on / off in ``--block``-second
blocks (on, off, on, off, ...), skipping ``--settle`` seconds after each switch.
For each block it reads the app's ``grab FPS: X | infer: Y ms`` lines from
``docker logs`` and samples board CPU and temperature. The agent is always
started again at the end. The container keeps running throughout (Docker owns
it); the portal shows the device OFFLINE while the agent is stopped.
"""

from __future__ import print_function

import argparse
import csv
import glob
import os
import re
import statistics
import subprocess
import sys
import time

FPS_LINE = re.compile(r"grab FPS: ([\d.]+) \| infer: ([\d.]+) ms")

AGENT_UNIT = "icicle-edge-agent"
AGENT_MARKER = "icicle-edge/agent/main.py"
CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_KB = os.sysconf("SC_PAGE_SIZE") // 1024


def agent_pid():
    try:
        out = subprocess.check_output(
            ["systemctl", "show", "-p", "MainPID", AGENT_UNIT],
            universal_newlines=True,
        ).strip()
        pid = int(out.split("=", 1)[1])
        if pid > 0:
            return pid
    except (OSError, subprocess.CalledProcessError, ValueError, IndexError):
        pass
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % name, "rb") as f:
                if AGENT_MARKER.encode() in f.read():
                    return int(name)
        except (IOError, OSError):
            continue
    return None


def read_stat(pid):
    """Return (ppid, cpu_ticks incl. reaped children, rss_kb) or None."""
    try:
        with open("/proc/%d/stat" % pid) as f:
            data = f.read()
    except (IOError, OSError):
        return None
    fields = data[data.rindex(")") + 2:].split()
    ppid = int(fields[1])
    ticks = sum(int(x) for x in fields[11:15])
    rss_kb = int(fields[21]) * PAGE_KB
    return ppid, ticks, rss_kb


def descendants(root):
    children = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        st = read_stat(int(name))
        if st:
            children.setdefault(st[0], []).append(int(name))
    out, stack = [], [root]
    while stack:
        for c in children.get(stack.pop(), []):
            out.append(c)
            stack.append(c)
    return out


def sample(pid):
    main = read_stat(pid)
    if main is None:
        return None
    ticks, rss_main, rss_total = main[1], main[2], main[2]
    for c in descendants(pid):
        st = read_stat(c)
        if st:
            ticks += st[1]
            rss_total += st[2]
    return ticks, rss_main, rss_total


def system_cpu():
    with open("/proc/stat") as f:
        vals = [int(x) for x in f.readline().split()[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals), idle


def mem_used_mb():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0])
    return (info["MemTotal"] - info.get("MemAvailable", info["MemFree"])) / 1024.0


def running_containers():
    try:
        out = subprocess.check_output(
            ["docker", "ps", "--format", "{{.Names}}"], universal_newlines=True,
            stderr=subprocess.DEVNULL,
        )
        return " ".join(out.split()) or "-"
    except (OSError, subprocess.CalledProcessError):
        return "?"


def pct(values, q):
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


def max_temp_c():
    temps = []
    for path in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        try:
            with open(path, "rb") as f:
                v = int(f.read().strip())
        except Exception:
            # Some Jetson zones (e.g. offline sensors) fail on read in odd ways.
            continue
        if 0 < v < 150000:
            temps.append(v / 1000.0)
    return max(temps) if temps else None


def set_agent(running):
    subprocess.check_call(["systemctl", "start" if running else "stop", AGENT_UNIT])


def app_metrics(container, since, until):
    out = subprocess.check_output(
        ["docker", "logs", "--since", str(int(since)), "--until", str(int(until)), container],
        stderr=subprocess.STDOUT, universal_newlines=True,
    )
    fps, infer = [], []
    for m in FPS_LINE.finditer(out):
        fps.append(float(m.group(1)))
        if float(m.group(2)) > 0:
            infer.append(float(m.group(2)))
    return fps, infer


def run_block(container, state, block_s):
    start = time.time()
    prev_sys = system_cpu()
    sys_pcts, temps = [], []
    while time.time() - start < block_s:
        time.sleep(5.0)
        cur_sys = system_cpu()
        d_total = cur_sys[0] - prev_sys[0]
        if d_total:
            sys_pcts.append(100.0 * (1 - (cur_sys[1] - prev_sys[1]) / float(d_total)))
        prev_sys = cur_sys
        t = max_temp_c()
        if t is not None:
            temps.append(t)
    end = time.time()
    fps, infer = app_metrics(container, start, end)

    def med(v):
        return round(statistics.median(v), 2) if v else None

    return {
        "state": state,
        "start_unix": int(start),
        "seconds": int(end - start),
        "fps_lines": len(fps),
        "grab_fps_median": med(fps),
        "infer_ms_median": med(infer),
        "infer_ms_p90": round(pct(infer, 0.9), 2) if infer else None,
        "system_cpu_avg_pct": round(sum(sys_pcts) / len(sys_pcts), 1) if sys_pcts else None,
        "max_temp_c": round(max(temps), 1) if temps else None,
    }, fps, infer


def impact(args):
    if os.geteuid() != 0:
        sys.exit("impact mode stops/starts the agent: run with sudo")
    running = subprocess.check_output(
        ["docker", "inspect", "-f", "{{.State.Running}}", args.container],
        universal_newlines=True,
    ).strip()
    if running != "true":
        sys.exit("container %s is not running" % args.container)

    blocks, pooled = [], {"on": ([], []), "off": ([], [])}
    order = ["on", "off"] * args.cycles
    total_min = len(order) * (args.block + args.settle) / 60.0
    print("impact: %d blocks of %d s (+%d s settle), ~%.0f min" % (
        len(order), args.block, args.settle, total_min), file=sys.stderr)
    try:
        for i, state in enumerate(order, 1):
            set_agent(state == "on")
            print("[%d/%d] agent %s; settling %d s" % (i, len(order), state, args.settle),
                  file=sys.stderr)
            time.sleep(args.settle)
            row, fps, infer = run_block(args.container, state, args.block)
            blocks.append(row)
            pooled[state][0].extend(fps)
            pooled[state][1].extend(infer)
            print("      fps=%s infer_ms=%s (lines=%d) cpu=%s%% temp=%sC" % (
                row["grab_fps_median"], row["infer_ms_median"], row["fps_lines"],
                row["system_cpu_avg_pct"], row["max_temp_c"]), file=sys.stderr)
    finally:
        set_agent(True)
        print("agent started again", file=sys.stderr)

    if any(b["fps_lines"] == 0 for b in blocks):
        print("WARNING: some blocks had no 'grab FPS' lines in docker logs", file=sys.stderr)

    w = csv.writer(sys.stdout)
    keys = list(blocks[0].keys())
    w.writerow(["block"] + keys)
    for i, b in enumerate(blocks, 1):
        w.writerow([i] + [b[k] for k in keys])
    print()
    w.writerow(["agent", "fps_lines", "grab_fps_median", "infer_ms_median", "infer_ms_p90"])
    for state in ("on", "off"):
        fps, infer = pooled[state]
        w.writerow([state, len(fps),
                    round(statistics.median(fps), 2) if fps else None,
                    round(statistics.median(infer), 2) if infer else None,
                    round(pct(infer, 0.9), 2) if infer else None])


def main():
    ap = argparse.ArgumentParser(description="Measure the edge agent's overhead.")
    ap.add_argument("--label", help="footprint mode: e.g. idle or busy")
    ap.add_argument("--seconds", type=int, default=600)
    ap.add_argument("--samples", help="footprint mode: optional per-second CSV path")
    ap.add_argument("--impact", action="store_true", help="agent on/off vs app FPS (sudo)")
    ap.add_argument("--container", help="impact mode: app container name")
    ap.add_argument("--block", type=int, default=300)
    ap.add_argument("--cycles", type=int, default=2)
    ap.add_argument("--settle", type=int, default=30)
    args = ap.parse_args()

    if args.impact:
        if not args.container:
            ap.error("--impact needs --container")
        impact(args)
        return
    if not args.label:
        ap.error("--label is required (or use --impact)")

    pid = agent_pid()
    if not pid:
        sys.exit("agent process not found (is %s running?)" % AGENT_UNIT)
    containers = running_containers()
    print("agent pid %d, containers: %s, sampling %d s ..." % (pid, containers, args.seconds),
          file=sys.stderr)

    rows, cpu_pcts, rss_main, rss_total, sys_pcts, mem = [], [], [], [], [], []
    restarts = 0
    prev = sample(pid)
    prev_sys = system_cpu()
    prev_t = time.time()
    t_start = prev_t
    while time.time() - t_start < args.seconds:
        time.sleep(1.0)
        now = time.time()
        cur = sample(pid)
        if cur is None:
            new_pid = agent_pid()
            restarts += 1
            if not new_pid:
                sys.exit("agent process disappeared")
            pid, prev, prev_t = new_pid, sample(new_pid), now
            prev_sys = system_cpu()
            continue
        cur_sys = system_cpu()
        dt = now - prev_t
        cpu = max(0, cur[0] - prev[0]) / float(CLK_TCK) / dt * 100.0
        d_total = cur_sys[0] - prev_sys[0]
        sys_pct = 100.0 * (1 - (cur_sys[1] - prev_sys[1]) / float(d_total)) if d_total else 0.0
        m = mem_used_mb()
        cpu_pcts.append(cpu)
        rss_main.append(cur[1] / 1024.0)
        rss_total.append(cur[2] / 1024.0)
        sys_pcts.append(sys_pct)
        mem.append(m)
        rows.append([round(now - t_start, 1), round(cpu, 2), round(cur[1] / 1024.0, 1),
                     round(cur[2] / 1024.0, 1), round(sys_pct, 1), round(m, 0)])
        prev, prev_sys, prev_t = cur, cur_sys, now

    if args.samples:
        with open(args.samples, "w") as f:
            w = csv.writer(f)
            w.writerow(["t_s", "agent_cpu_pct", "agent_rss_mb", "agent_tree_rss_mb",
                        "system_cpu_pct", "mem_used_mb"])
            w.writerows(rows)

    n = len(cpu_pcts) or 1
    header = ["label", "seconds", "cores", "containers", "agent_restarts",
              "agent_cpu_avg_pct", "agent_cpu_p99_pct", "agent_cpu_max_pct",
              "agent_rss_avg_mb", "agent_rss_max_mb", "agent_tree_rss_max_mb",
              "system_cpu_avg_pct", "mem_used_avg_mb"]
    row = [args.label, len(cpu_pcts), os.cpu_count(), containers, restarts,
           round(sum(cpu_pcts) / n, 3), round(pct(cpu_pcts, 0.99), 2),
           round(max(cpu_pcts or [0]), 2),
           round(sum(rss_main) / n, 1), round(max(rss_main or [0]), 1),
           round(max(rss_total or [0]), 1),
           round(sum(sys_pcts) / n, 1), round(sum(mem) / n, 0)]
    w = csv.writer(sys.stdout)
    w.writerow(header)
    w.writerow(row)


if __name__ == "__main__":
    main()
