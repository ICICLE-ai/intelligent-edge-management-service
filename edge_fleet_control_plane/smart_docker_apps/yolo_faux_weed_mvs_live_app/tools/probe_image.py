#!/usr/bin/env python3
"""Run the engine over still images using the live pipeline's code path.

Zero detections on camera frames is ambiguous: the engine may be broken, or the
scene may simply not look like the training data. Feeding a known-positive frame
through the same preprocessing, engine and decoder separates the two, and the
pre-threshold score summary says which even when nothing survives the threshold.

    python3 tools/probe_image.py frame.jpg [more.jpg ...]

Writes <name>.annotated.jpg beside each input.
"""

import os
import sys

os.environ.pop("DISPLAY", None)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from app import config
from app.hik_camera import resize_keep_aspect
from app.yolo_runner import build_backend, decode_detections, draw_detections


def summarize(raw):
    """Report the decoder's view of the output before any threshold is applied."""
    pred = np.asarray(raw, dtype=np.float32)
    if pred.ndim == 3:
        pred = pred[0]
    if pred.shape[0] < pred.shape[1]:
        pred = pred.T

    print("  output shape:     %s -> %d anchors x %d cols" % (
        np.shape(raw), pred.shape[0], pred.shape[1]))

    bad = int(np.count_nonzero(~np.isfinite(pred)))
    if bad:
        print("  NON-FINITE VALUES: %d (fp16 overflow - rebuild the engine without --fp16)" % bad)
        return

    boxes, scores = pred[:, :4], pred[:, 4:]
    print("  box cx/cy range:  %.1f..%.1f / %.1f..%.1f" % (
        boxes[:, 0].min(), boxes[:, 0].max(), boxes[:, 1].min(), boxes[:, 1].max()))
    print("  box w/h range:    %.1f..%.1f / %.1f..%.1f" % (
        boxes[:, 2].min(), boxes[:, 2].max(), boxes[:, 3].min(), boxes[:, 3].max()))
    print("  score range:      %.4f..%.4f" % (scores.min(), scores.max()))

    best = scores.max(axis=1)
    for thr in (0.50, 0.25, 0.10, 0.05, 0.01):
        print("  anchors >= %.2f:  %d" % (thr, int(np.count_nonzero(best >= thr))))

    print("  best per class:")
    for class_id in range(scores.shape[1]):
        print("    %-24s %.4f" % (config.class_name(class_id), scores[:, class_id].max()))


def main():
    paths = sys.argv[1:]
    if not paths:
        print(__doc__)
        return 1

    print("MODEL_PATH:  %s" % config.MODEL_PATH)
    print("CONFIDENCE:  %.2f   IOU: %.2f" % (config.CONFIDENCE, config.IOU))
    backend = build_backend(config.MODEL_PATH)
    backend.load()
    print("engine:      %s\n" % backend.describe())

    for path in paths:
        image = cv2.imread(path)
        if image is None:
            print("%s: cannot read" % path)
            continue

        # Match the camera path exactly, so the probe cannot pass on a frame the
        # live pipeline would fail.
        frame = resize_keep_aspect(image, config.DISPLAY_WIDTH)
        frame_h, frame_w = frame.shape[:2]
        print("%s: %dx%d -> %dx%d" % (path, image.shape[1], image.shape[0], frame_w, frame_h))

        raw, ratio, pad_x, pad_y, infer_ms = backend.infer_raw(frame)
        print("  infer:            %.1f ms" % infer_ms)
        summarize(raw)

        detections = decode_detections(raw, ratio, pad_x, pad_y, frame_w, frame_h)
        print("  decoded:          %d detection(s) at CONFIDENCE=%.2f" % (
            len(detections), config.CONFIDENCE))
        for x1, y1, x2, y2, confidence, class_id in detections[:10]:
            print("    %-24s %.2f  [%d %d %d %d]" % (
                config.class_name(class_id), confidence, x1, y1, x2, y2))

        out_path = "%s.annotated.jpg" % os.path.splitext(path)[0]
        cv2.imwrite(out_path, draw_detections(frame, detections))
        print("  wrote:            %s\n" % out_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
