"""UNet++ TensorRT CSI live stream — single or multi-camera."""

import os

os.environ.setdefault("CUDA_MODULE_LOADING", "LAZY")
if os.environ.get("SHOW_WINDOW", "1") != "1":
    os.environ.pop("DISPLAY", None)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from . import config
from . import multicam


def main():
    indices = config.sensor_ids()
    mode = "multi-CSI" if len(indices) > 1 else "single-CSI"
    print("--- UNet++ CSI live stream (%s, %d camera(s)) ---" % (mode, len(indices)))
    print("ENGINE_PATH:          %s" % config.ENGINE_PATH)
    print("CAMERA_INDICES:       %s" % indices)
    print("STREAM_FPS:           %d" % config.STREAM_FPS)
    print("DETECT_EVERY_N:       %d" % config.DETECT_EVERY_N_FRAMES)
    print("STREAM_INGEST_URL:    %s" % ("(set)" if config.STREAM_INGEST_URL else "(not set)"))
    print("Architecture:         grab threads + inference worker + stream threads")
    multicam.run()


if __name__ == "__main__":
    main()
