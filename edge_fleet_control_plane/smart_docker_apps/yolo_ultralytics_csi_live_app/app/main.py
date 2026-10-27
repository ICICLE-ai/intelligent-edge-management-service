"""Ultralytics YOLO CSI live stream — single or multi-camera."""

import os

os.environ.setdefault("CUDA_MODULE_LOADING", "LAZY")
os.environ.setdefault("ULTRALYTICS_SETTINGS_DIR", "/tmp/Ultralytics")
if os.environ.get("SHOW_WINDOW", "0") != "1":
    os.environ.pop("DISPLAY", None)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from . import config
from . import multicam


def main():
    indices = config.sensor_ids()
    mode = "multi-CSI" if len(indices) > 1 else "single-CSI"
    print("--- Ultralytics YOLO CSI live stream (%s, %d camera(s)) ---" % (mode, len(indices)))
    print("MODEL_PATH:           %s" % config.MODEL_PATH)
    print("CAMERA_INDICES:       %s" % indices)
    print("STREAM_FPS:           %d" % config.STREAM_FPS)
    print("DETECT_EVERY_N:       %d" % config.DETECT_EVERY_N_FRAMES)
    print("CONFIDENCE:           %s" % config.CONFIDENCE)
    print("IMAGE_SIZE:           %s" % (config.IMAGE_SIZE or "model default"))
    print("STREAM_INGEST_URL:    %s" % ("(set)" if config.STREAM_INGEST_URL else "(not set)"))
    multicam.run()


if __name__ == "__main__":
    main()
