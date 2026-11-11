"""COLMAP poses video: each frame on the left; on the right a 3D view of the sparse points and all
camera frustums, with the current camera highlighted and the orbit drawn so far.

  python scripts/colmap_pose_video.py SPARSE_TXT_DIR IMAGES_DIR OUT.mp4 [--fps 10] [--masks DIR]

SPARSE_TXT_DIR holds cameras.txt / images.txt / points3D.txt (colmap model_converter --output_type TXT).
In this view the object is still and the cameras orbit it, which is how COLMAP sees a turntable scan.
"""
import argparse
import subprocess
from pathlib import Path

import cv2
import matplotlib
import numpy as np
from scipy.spatial.transform import Rotation as R

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from mpl_toolkits.mplot3d.art3d import Line3DCollection  # noqa: E402


def read_model(d):
    d = Path(d)
    cam = [l.split() for l in open(d / "cameras.txt") if not l.startswith("#")][0]
    w, h, f = int(cam[2]), int(cam[3]), float(cam[4])
    lines = [l.split() for l in open(d / "images.txt") if not l.startswith("#")][0::2]
    poses = {}
    for t in lines:
        Rcw = R.from_quat([*map(float, t[2:5]), float(t[1])]).as_matrix()
        tcw = np.array(list(map(float, t[5:8])))
        poses[t[9]] = (Rcw.T, -Rcw.T @ tcw)  # camera-to-world rotation, camera center
    pts, cols = [], []
    for l in open(d / "points3D.txt"):
        if l.startswith("#"):
            continue
        t = l.split()
        pts.append(list(map(float, t[1:4])))
        cols.append([int(c) / 255 for c in t[4:7]])
    return (w, h, f), poses, np.array(pts), np.array(cols)


def frustum(Rwc, C, w, h, f, s):
    corners = np.array([[0, 0], [w, 0], [w, h], [0, h]], float)
    rays = np.c_[(corners - [w / 2, h / 2]) / f, np.ones(4)] * s
    P = (Rwc @ rays.T).T + C
    return [[C, P[i]] for i in range(4)] + [[P[i], P[(i + 1) % 4]] for i in range(4)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("images")
    ap.add_argument("out")
    ap.add_argument("--fps", type=float, default=10)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--masks", help="optional mask dir: dims masked-out background in the left panel")
    args = ap.parse_args()

    (w, h, f), poses, pts, cols = read_model(args.model)
    names = sorted(poses)
    C = np.array([poses[n][1] for n in names])

    # Drop far outlier points, then look at the scene with the orbit-plane normal as "up".
    center = np.median(pts, axis=0)
    r = np.linalg.norm(pts - center, axis=1)
    keep = r < np.percentile(r, 95)
    pts, cols = pts[keep], cols[keep]
    if len(pts) > 15000:
        idx = np.random.default_rng(0).choice(len(pts), 15000, replace=False)
        pts, cols = pts[idx], cols[idx]
    n = np.linalg.svd(C - C.mean(0))[2][2]
    ups = np.array([poses[k][0] @ [0, -1, 0] for k in names]).mean(0)  # COLMAP camera "up" is -y
    if n @ ups < 0:
        n = -n
    # Rotate the world so n -> +z (matplotlib's up).
    z = n
    x = np.cross([0, 1, 0] if abs(z[1]) < 0.9 else [1, 0, 0], z)
    x /= np.linalg.norm(x)
    Rw = np.stack([x, np.cross(z, x), z])
    origin = C.mean(0)
    tf = lambda P: (np.asarray(P) - origin) @ Rw.T
    pts_t, C_t = tf(pts), tf(C)
    radius = np.linalg.norm(C_t[:, :2], axis=1).mean()
    fs = 0.22 * radius
    frust = {k: [[tf(a), tf(b)] for a, b in frustum(*poses[k], w, h, f, fs)] for k in names}

    H = args.height
    W_img = int(round(H * w / h))
    W_plot = H
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
                           "-s", f"{W_img + W_plot}x{H}", "-r", str(args.fps), "-i", "-",
                           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", str(out)],
                          stdin=subprocess.PIPE)
    dpi = 100
    fig = plt.figure(figsize=(W_plot / dpi, H / dpi), dpi=dpi)
    ax = fig.add_axes([0, 0, 1, 1], projection="3d")
    lim = 1.05 * radius

    for i, k in enumerate(names):
        ax.cla()
        ax.set_axis_off()
        ax.set_facecolor("white")
        ax.scatter(*pts_t.T, c=cols, s=2.0, depthshade=False, linewidths=0)
        # Every 6th camera in grey (all of them overlap into a solid fence), the current one in red.
        others = [seg for kk in names[::6] for seg in frust[kk]]
        ax.add_collection3d(Line3DCollection(others, colors=(0.35, 0.4, 0.55, 0.7), linewidths=0.9))
        ax.plot(*C_t[: i + 1].T, color=(0.1, 0.45, 0.9), lw=1.5)
        ax.add_collection3d(Line3DCollection(frust[k], colors="red", linewidths=2.5))
        ax.plot(*np.array([C_t[i], C_t[i] * 0.15]).T, color="red", lw=0.8, ls="--")  # current camera's line to center
        ax.set_xlim(-lim, lim); ax.set_ylim(-lim, lim); ax.set_zlim(-lim * 0.6, lim * 0.6)
        ax.set_box_aspect((1, 1, 0.6))
        ax.view_init(elev=28, azim=-60 + 0.5 * i)  # slow drift so depth reads
        ax.text2D(0.03, 0.95, f"COLMAP  {i + 1}/{len(names)} cameras   {len(pts)} pts", transform=ax.transAxes,
                  fontsize=10, color="0.2")
        fig.canvas.draw()
        plot = cv2.cvtColor(np.asarray(fig.canvas.buffer_rgba())[..., :3], cv2.COLOR_RGB2BGR)
        plot = cv2.resize(plot, (W_plot, H))

        img = cv2.imread(str(Path(args.images) / k))
        if args.masks:
            m = cv2.imread(str(Path(args.masks) / (Path(k).stem + ".png")), 0)
            if m is not None:
                img[m == 0] = (img[m == 0] * 0.35).astype(np.uint8)
        img = cv2.resize(img, (W_img, H), interpolation=cv2.INTER_AREA)
        cv2.putText(img, k, (14, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2, cv2.LINE_AA)
        ff.stdin.write(np.hstack([img, plot]).tobytes())
    ff.stdin.close()
    ff.wait()
    webm = out.with_suffix(".webm")  # VS Code's built-in player can't decode H.264; VP9 plays there
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(out), "-c:v", "libvpx-vp9", "-b:v", "0",
                    "-crf", "34", "-row-mt", "1", str(webm)], check=False)
    print(f"{len(names)} frames -> {out} (+ {webm.name})")


if __name__ == "__main__":
    main()
