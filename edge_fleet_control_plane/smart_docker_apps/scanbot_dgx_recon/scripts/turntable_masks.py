"""Masks for fixed-camera turntable captures: keep only the rotating object, drop the static background.

The camera never moves, so anything that matches the per-pixel median over all frames is background.
Moving pixels are cleaned up, joined with a convex hull, limited to the area the object sweeps out,
and dilated a little.

  python scripts/turntable_masks.py SRC_IMAGES OUT_DIR [--max-dim 1920] [--thresh 30]

Writes OUT_DIR/images/*.jpg (downscaled frames), OUT_DIR/masks/*.png (tight, for COLMAP) and
OUT_DIR/masks_hull/*.png (convex hull, no holes, for NeRF). Masks are white = keep and
black = ignore, the convention both COLMAP and nerfstudio use. It also writes OUT_DIR/masks_preview.jpg.
"""
import argparse
from pathlib import Path

import cv2
import numpy as np


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--max-dim", type=int, default=1920)
    ap.add_argument("--thresh", type=float, default=30, help="per-pixel color distance from median (0-255)")
    ap.add_argument("--dilate", type=int, default=25, help="px at output resolution")
    args = ap.parse_args()

    src, out = Path(args.src), Path(args.out)
    (out / "images").mkdir(parents=True, exist_ok=True)
    (out / "masks").mkdir(parents=True, exist_ok=True)
    (out / "masks_hull").mkdir(parents=True, exist_ok=True)
    paths = sorted(src.glob("*.jpg"))
    imgs = []
    for p in paths:
        im = cv2.imread(str(p))
        s = args.max_dim / max(im.shape[:2])
        if s < 1:
            im = cv2.resize(im, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(out / "images" / p.name), im, [cv2.IMWRITE_JPEG_QUALITY, 95])
        imgs.append(im)
    stack = np.stack(imgs)
    median = np.median(stack, axis=0).astype(np.float32)
    h, w = median.shape[:2]

    k_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (31, 31))
    raw = []
    for im in imgs:
        d = np.linalg.norm(cv2.GaussianBlur(im, (5, 5), 0).astype(np.float32) - median, axis=2)
        m = (d > args.thresh).astype(np.uint8)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, k_open)
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k_close)
        raw.append(m)

    # Area the object sweeps: pixels that move in a meaningful fraction of frames. Drops one-off noise
    # (lighting flicker, the fan, people walking by).
    freq = np.mean(raw, axis=0)
    sweep = (freq > 0.05).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(sweep)
    if n > 1:
        big = 1 + np.argmax(stats[1:, cv2.CC_STAT_AREA])
        sweep = (lab == big).astype(np.uint8)
    sweep = cv2.dilate(sweep, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (61, 61)))

    k_dil = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * args.dilate + 1,) * 2)
    previews = []
    for p, im, m in zip(paths, imgs, raw):
        m = m & sweep
        # Filled outer contours (not a convex hull, which spills onto static background beside concave
        # objects such as plants). Small blobs are noise.
        mask = np.zeros((h, w), np.uint8)
        cs, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cs = [c for c in cs if cv2.contourArea(c) > 0.0005 * h * w]
        cv2.drawContours(mask, cs, -1, 1, cv2.FILLED)
        mask = cv2.dilate(mask, k_dil) & sweep
        cv2.imwrite(str(out / "masks" / (p.stem + ".png")), mask * 255)
        # Convex hull: no holes in low-texture parts (e.g. a white pot), some spill onto background.
        # Better for NeRF supervision; the tight mask above is better for COLMAP features.
        hull = np.zeros((h, w), np.uint8)
        if cs:
            cv2.fillConvexPoly(hull, cv2.convexHull(np.vstack(cs)), 1)
        cv2.imwrite(str(out / "masks_hull" / (p.stem + ".png")), (cv2.dilate(hull, k_dil) & sweep) * 255)
        if len(previews) < 8 and (paths.index(p) % max(1, len(paths) // 8) == 0):
            v = im.copy()
            v[mask == 0] = (v[mask == 0] * 0.25).astype(np.uint8)
            previews.append(cv2.resize(v, (w // 4, h // 4)))
    while len(previews) % 4:
        previews.append(np.zeros_like(previews[0]))
    rows = [np.hstack(previews[i:i + 4]) for i in range(0, len(previews), 4)]
    cv2.imwrite(str(out / "masks_preview.jpg"), np.vstack(rows))
    print(f"{len(paths)} frames -> {out}/images, {out}/masks ({w}x{h}); sweep area {sweep.mean():.1%} of frame")


if __name__ == "__main__":
    main()
