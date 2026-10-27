"""Ultralytics YOLO runner. The weights file selects detect vs segment.

Accepts an Ultralytics checkpoint (.pt) or an engine that Ultralytics itself
exported on this same Jetson. plot() draws boxes for detection and masks for
segmentation, so the rest of the app does not branch on the task.
"""

import os
import sys
import threading
import time

from . import config

_PYTORCH_SUFFIXES = (".pt", ".pth")


class SharedYoloRunner(object):
    """One loaded model, serial inference across cameras."""

    def __init__(self, model_path):
        self.model_path = model_path
        self.model = None
        self.task = None
        self.lock = threading.Lock()
        self.status = "YOLO NOT LOADED"

    def load(self):
        if not os.path.exists(self.model_path):
            print("CRITICAL: model not found at %s" % self.model_path)
            sys.exit(1)

        from ultralytics import YOLO

        print("Loading YOLO weights: %s" % self.model_path)
        self.model = YOLO(self.model_path)
        self.task = getattr(self.model, "task", None) or "unknown"
        self.status = "YOLO READY (%s)" % self.task
        names = getattr(self.model, "names", None)
        print("YOLO loaded. task=%s classes=%s" % (self.task, names))
        if self.task not in ("detect", "segment"):
            print(
                "WARNING: task '%s' is not detect or segment. "
                "plot() will still run, but this image is meant for those two."
                % self.task
            )

    def infer_overlay(self, bgr):
        if self.model is None:
            return bgr, 0.0

        kwargs = {
            "conf": config.CONFIDENCE,
            "iou": config.IOU,
            "verbose": False,
            "device": config.DEVICE,
        }
        if config.IMAGE_SIZE > 0:
            kwargs["imgsz"] = config.IMAGE_SIZE
        # half=True is a PyTorch flag. An exported engine already has its precision.
        if config.HALF and self.model_path.lower().endswith(_PYTORCH_SUFFIXES):
            kwargs["half"] = True

        with self.lock:
            t0 = time.time()
            results = self.model.predict(bgr, **kwargs)
            infer_ms = (time.time() - t0) * 1000.0
            if not results:
                return bgr, infer_ms
            # Default plot() draws the box and the mask together on a segmentation model.
            plotted = results[0].plot(
                boxes=self.task != "segment",
                masks=self.task == "segment",
            )
            if plotted is None:
                return bgr, infer_ms
            return plotted, infer_ms
