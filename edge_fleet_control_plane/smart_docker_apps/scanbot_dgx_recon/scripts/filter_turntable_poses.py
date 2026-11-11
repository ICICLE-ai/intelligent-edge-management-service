"""Drop bad and duplicate poses from a turntable COLMAP model (TXT format).

A turntable spins at a near-constant rate, so the rotation angle is a smooth function of frame time.
  * Outliers: frames whose angle is more than --max-dev deg from a running median of their time
    neighbors (e.g. a short run of frames COLMAP registered at a mirrored/ambiguous pose).
  * Duplicates: while the table is still (lead-in/out), only the first frame of each still run is kept.
Writes the names to drop to OUT_LIST, one per line (for `colmap image_deleter`).

  python scripts/filter_turntable_poses.py MODEL_TXT_DIR FRAMES_CSV OUT_LIST [--max-dev 10]
"""
import argparse
import csv
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation as R


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("frames_csv")
    ap.add_argument("out_list")
    ap.add_argument("--max-dev", type=float, default=10.0)
    ap.add_argument("--still-deg", type=float, default=0.5, help="step below this = table not moving")
    args = ap.parse_args()

    lines = [l.split() for l in open(Path(args.model) / "images.txt") if not l.startswith("#")][0::2]
    rot = {t[9]: R.from_quat([*map(float, t[2:5]), float(t[1])]) for t in lines}
    times = {Path(r["file"]).name: float(r["time_s"]) for r in csv.DictReader(open(args.frames_csv))}
    names = sorted(rot, key=lambda n: times.get(n, 0.0))
    t = np.array([times.get(n, i) for i, n in enumerate(names)])

    # Signed angle about the turntable axis (median direction of the per-step rotation vectors).
    steps = np.array([(rot[b] * rot[a].inv()).as_rotvec() for a, b in zip(names, names[1:])])
    norms = np.linalg.norm(steps, axis=1)
    moving_steps = steps[norms > np.radians(args.still_deg)]
    axis = np.median(moving_steps / np.linalg.norm(moving_steps, axis=1, keepdims=True), axis=0)
    axis /= np.linalg.norm(axis)
    ang = np.concatenate([[0.0], np.cumsum(np.degrees(steps @ axis))])

    # Still runs at the start and end (before the table starts / after it stops). Mid-rotation frames
    # can also have tiny steps when two kept frames are close in time, so only the ends count.
    still = np.radians(args.still_deg)
    first = next((i for i in range(len(norms)) if norms[i] >= still), 0)
    last = next((i for i in range(len(norms) - 1, -1, -1) if norms[i] >= still), len(norms) - 1) + 1
    drop = set(names[1:first]) | set(names[last + 1:])  # keep one frame of each still run
    # Outliers among the moving frames: residual from a linear angle(t) fit vs a running median.
    keep = list(range(first, last + 1))
    p = np.polyfit(t[keep], ang[keep], 1)
    res = ang - np.polyval(p, t)
    for j, i in enumerate(keep):
        nb = [res[k] for k in keep[max(0, j - 6):j + 7]]
        if abs(res[i] - np.median(nb)) > args.max_dev:
            drop.add(names[i])

    Path(args.out_list).write_text("".join(n + "\n" for n in sorted(drop)))
    print(f"{len(names)} posed, rate {p[0]:.1f} deg/s, dropping {len(drop)}: {sorted(drop)}")


if __name__ == "__main__":
    main()
