"""Render a smooth 360 orbit from a trained nerfstudio model.

  python scripts/nerf_orbit_video.py CONFIG.yml OUT.mp4 [--frames 180] [--width 960] [--fps 30] [--full-scene]

By default only the object is rendered: density outside a box around the COLMAP sparse points
(sparse_pc.ply next to transforms.json, all on the object because COLMAP ran with masks) is ignored and
the result is composited on --bg (dark grey, so white objects stay visible). The background was masked out of training, so rendering it
only shows floaters. --full-scene renders everything.

The orbit follows the training cameras: a circle fitted to their centers at the same radius and height,
looking at the circle's center (the turntable axis).

Checkpoints are loaded with torch's safe weights_only loader. Only the numpy scalar/dtype types that
nerfstudio's optimizer state contains are allowlisted (torch>=2.6 rejects them by default).
"""
import argparse
import json
import subprocess
from pathlib import Path

import numpy as np
import torch
from numpy._core.multiarray import scalar as np_scalar

SAFE = [np_scalar, np.dtype, *{type(np.dtype(t)) for t in ("float64", "float32", "int64", "int32", "bool")}]


def look_at(eye, target, up):
    f = target - eye
    f /= np.linalg.norm(f)
    r = np.cross(f, up)
    r /= np.linalg.norm(r)
    u = np.cross(r, f)
    # nerfstudio/OpenGL camera: x right, y up, looking down -z
    return np.c_[r, u, -f, eye]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("out")
    ap.add_argument("--frames", type=int, default=180)
    ap.add_argument("--width", type=int, default=960)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--full-scene", action="store_true")
    ap.add_argument("--pad", type=float, default=0.10, help="box padding, fraction of its size")
    ap.add_argument("--drop", type=float, default=0.35,
                    help="extend the box down by this fraction of its height (plain pots/bases have few points)")
    ap.add_argument("--bg", type=float, nargs=3, default=[0.13, 0.14, 0.15])
    ap.add_argument("--zoom", type=float, default=1.3, help="focal length multiplier vs the training cameras")
    args = ap.parse_args()

    from nerfstudio.cameras.cameras import Cameras, CameraType
    from nerfstudio.data.scene_box import OrientedBox
    from nerfstudio.utils.eval_utils import eval_setup

    with torch.serialization.safe_globals(SAFE):
        cfg, pipeline, _, _ = eval_setup(Path(args.config), test_mode="inference")
    model = pipeline.model.eval()
    cams = pipeline.datamanager.train_dataset.cameras
    c2w = cams.camera_to_worlds.cpu().numpy()
    C = c2w[:, :, 3]
    up_cam = c2w[:, :, 1].mean(0)

    # Circle fit: plane via SVD, center = mean of the centers projected onto that plane.
    mu = C.mean(0)
    _, _, vt = np.linalg.svd(C - mu)
    n = vt[2] if vt[2] @ up_cam > 0 else -vt[2]
    e1, e2 = vt[0], np.cross(n, vt[0])
    P = (C - mu) @ np.c_[e1, e2]
    radius = np.linalg.norm(P, axis=1).mean()
    a0 = np.arctan2(P[0, 1], P[0, 0])
    # Look at the point nearest to all training optical axes (least squares): where the real cameras aimed.
    d = -c2w[:, :, 2] / np.linalg.norm(c2w[:, :, 2], axis=1, keepdims=True)
    A = sum(np.eye(3) - np.outer(v, v) for v in d)
    b = sum((np.eye(3) - np.outer(v, v)) @ c for v, c in zip(d, C))
    target = np.linalg.solve(A, b)

    obb = None
    if not args.full_scene:
        data_dir = Path(cfg.pipeline.datamanager.data)
        ply = data_dir / "sparse_pc.ply"
        tf = json.loads((Path(args.config).parent / "dataparser_transforms.json").read_text())
        lines = ply.read_text().splitlines()
        n_pts = int(next(l for l in lines if l.startswith("element vertex")).split()[-1])
        start = lines.index("end_header") + 1
        xyz = np.array([l.split()[:3] for l in lines[start:start + n_pts]], float)
        T = np.array(tf["transform"])
        xyz = (xyz @ T[:, :3].T + T[:, 3]) * tf["scale"]  # into the training frame
        lo, hi = np.percentile(xyz, 3, axis=0), np.percentile(xyz, 97, axis=0)
        pad = (hi - lo) * args.pad
        lo, hi = lo - pad, hi + pad
        lo[2] -= (hi[2] - lo[2]) * args.drop  # training frame is z-up (orientation-method up)
        size, center = hi - lo, (lo + hi) / 2
        dev = model.device
        obb = OrientedBox(R=torch.eye(3, device=dev), T=torch.tensor(center, dtype=torch.float32, device=dev),
                          S=torch.tensor(size, dtype=torch.float32, device=dev))
        target = center
        # Orbit around the object, not the turntable axis: an off-center object would swing across the frame.
        off = center - mu
        mu = mu + off - (off @ n) * n
        print(f"object box: center {np.round(center, 3)}, size {np.round(size, 3)} ({n_pts} sparse points)")

    fx = float(cams.fx[0]) * args.width / float(cams.width[0]) * args.zoom
    H = int(round(float(cams.height[0]) * args.width / float(cams.width[0]))) // 2 * 2
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                           "-s", f"{args.width}x{H}", "-r", str(args.fps), "-i", "-",
                           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", str(out)], stdin=subprocess.PIPE)
    for i in range(args.frames):
        a = a0 + 2 * np.pi * i / args.frames
        eye = mu + radius * (np.cos(a) * e1 + np.sin(a) * e2)
        cam = Cameras(camera_to_worlds=torch.tensor(look_at(eye, target, n)[None], dtype=torch.float32),
                      fx=fx, fy=fx, cx=args.width / 2, cy=H / 2, width=args.width, height=H,
                      camera_type=CameraType.PERSPECTIVE).to(model.device)
        with torch.no_grad():
            out_ = model.get_outputs_for_camera(cam, obb_box=obb)
        rgb = out_["rgb"]
        if obb is not None:  # "random" background renders premultiplied (on black): composite onto bg
            rgb = rgb + (1 - out_["accumulation"]) * torch.tensor(args.bg, device=rgb.device)
        ff.stdin.write((rgb.clamp(0, 1).cpu().numpy() * 255).astype(np.uint8).tobytes())
        if i % 30 == 0:
            print(f"  rendered {i}/{args.frames}", flush=True)
    ff.stdin.close()
    ff.wait()
    webm = out.with_suffix(".webm")  # VS Code's built-in player can't decode H.264; VP9 plays there
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(out), "-c:v", "libvpx-vp9", "-b:v", "0",
                    "-crf", "34", "-row-mt", "1", str(webm)], check=False)
    print(f"{args.frames} frames -> {out} (+ {webm.name})")


if __name__ == "__main__":
    main()
