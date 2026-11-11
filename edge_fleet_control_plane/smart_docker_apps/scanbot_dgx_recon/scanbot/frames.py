"""Pick good frames from a video for 3D reconstruction.

Single streaming pass, low memory:
  1. Split the video into windows of `fps / target_fps` frames.
  2. In each window, keep the sharpest frame (variance of Laplacian).
  3. Reject frames that are badly exposed or much blurrier than the video's typical sharpness.
  4. Reject frames with too little camera motion vs. the last kept frame (no parallax = useless for SfM).
"""
import csv
from pathlib import Path

import cv2
import numpy as np
from tqdm import tqdm

ANALYSIS_WIDTH = 640  # frames are scored at this width for speed


def small_gray(frame):
    h, w = frame.shape[:2]
    scale = ANALYSIS_WIDTH / w if w > ANALYSIS_WIDTH else 1.0
    if scale != 1.0:
        frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


def sharpness(gray):
    return cv2.Laplacian(gray, cv2.CV_64F).var()


def exposure_ok(gray, dark=0.25, bright=0.25):
    """Reject frames where too many pixels are crushed black or blown out."""
    n = gray.size
    return (gray < 10).sum() / n < dark and (gray > 245).sum() / n < bright


def motion(prev_gray, gray):
    """Median feature displacement (pixels at analysis scale) between two frames."""
    pts = cv2.goodFeaturesToTrack(prev_gray, maxCorners=400, qualityLevel=0.01, minDistance=8)
    if pts is None or len(pts) < 10:
        return float("inf")  # can't tell; treat as enough motion
    nxt, status, _ = cv2.calcOpticalFlowPyrLK(prev_gray, gray, pts, None)
    good = status.ravel() == 1
    if good.sum() < 10:
        return float("inf")  # tracking lost => big change
    return float(np.median(np.linalg.norm(nxt[good] - pts[good], axis=-1)))


def extract_frames(video, out, target_fps=2.0, blur_ratio=0.4, min_motion=8.0, max_frames=0,
                   max_dim=0, fmt="jpg", quality=95, progress=True):
    """Write selected frames + frames.csv into `out`. Returns a stats dict."""
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or None
    window = max(1, round(fps / target_fps))

    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"{video}: {fps:.2f} fps, {total or '?'} frames -> 1 candidate per {window} frames, saving to {out}")

    sharp_hist = []
    last_gray = None
    kept = 0
    stats = {"blur": 0, "exposure": 0, "static": 0}
    rows = []
    params = [cv2.IMWRITE_JPEG_QUALITY, quality] if fmt == "jpg" else []

    def consider(cand):
        nonlocal last_gray, kept
        score, idx, frame, gray = cand
        sharp_hist.append(score)
        median = float(np.median(sharp_hist[-50:]))
        if score < blur_ratio * median:
            stats["blur"] += 1
            return
        if not exposure_ok(gray):
            stats["exposure"] += 1
            return
        if last_gray is not None and motion(last_gray, gray) < min_motion:
            stats["static"] += 1
            return
        if max_dim:
            h, w = frame.shape[:2]
            s = max_dim / max(h, w)
            if s < 1:
                frame = cv2.resize(frame, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
        name = f"frame_{kept:05d}.{fmt}"
        cv2.imwrite(str(out / name), frame, params)
        rows.append((name, idx, f"{idx / fps:.3f}", f"{score:.1f}"))
        last_gray = gray
        kept += 1

    best = None  # (score, idx, frame, gray) for the current window
    idx = 0
    with tqdm(total=total, unit="f", disable=not progress) as bar:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            gray = small_gray(frame)
            s = sharpness(gray)
            if best is None or s > best[0]:
                best = (s, idx, frame, gray)
            idx += 1
            bar.update(1)
            if idx % window == 0:
                consider(best)
                best = None
                if max_frames and kept >= max_frames:
                    break
    if best is not None and not (max_frames and kept >= max_frames):
        consider(best)
    cap.release()

    with open(out / "frames.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["file", "frame_index", "time_s", "sharpness"])
        w.writerows(rows)

    print(f"Kept {kept} frames. Rejected: {stats['blur']} blurry, {stats['exposure']} bad exposure, "
          f"{stats['static']} too little motion.")
    if kept < 20:
        print("Warning: <20 frames is usually too few for reconstruction; try a higher target_fps "
              "or lower min_motion.")
    return {"kept": kept, "rejected": stats, "video_frames": idx, "fps": fps}
