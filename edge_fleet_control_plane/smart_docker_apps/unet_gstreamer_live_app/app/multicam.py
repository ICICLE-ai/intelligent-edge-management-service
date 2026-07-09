"""CSI live stream: grab + shared inference + per-camera RTSP push (1 or N cameras)."""

import os
import signal
import sys
import threading

from . import config
from . import csi_camera
from .frame_store import FrameStore
from .inference_worker import inference_loop
from .stream_worker import stream_loop
from .stream_writer import StreamWriter
from .unet_runner import SharedUnetRunner


def _ingest_url_for_camera(cam_idx, position):
    """Resolve RTSPS ingest URL for a camera (portal single or multi inject)."""
    by_sensor = (os.environ.get("STREAM_INGEST_URL_%d" % cam_idx) or "").strip()
    if by_sensor:
        return by_sensor
    by_pos = (os.environ.get("STREAM_INGEST_URL_%d" % position) or "").strip()
    if by_pos:
        return by_pos
    urls = config.STREAM_INGEST_URLS
    if position < len(urls):
        return urls[position]
    if config.STREAM_INGEST_URL and position == 0:
        return config.STREAM_INGEST_URL
    return None


def run():
    indices = config.sensor_ids()
    if not indices:
        print("CRITICAL: no CSI sensor indices configured (set CAMERA_INDICES or use default 0).")
        sys.exit(1)

    runner = SharedUnetRunner(config.ENGINE_PATH)
    try:
        runner.load()
    except Exception as exc:
        print("CRITICAL: UNet engine load failed: %s" % exc)
        sys.exit(1)

    frame_store = FrameStore()
    stop_event = threading.Event()
    threads = []
    cameras = []
    stream_writers = {}

    def _handle_signal(signum, _frame):
        print("Signal %d - shutting down..." % signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        for pos, cam_idx in enumerate(indices):
            ingest = _ingest_url_for_camera(cam_idx, pos)
            label = "cam-%d" % cam_idx
            print("Opening sensor-id=%d ingest=%s" % (cam_idx, "(set)" if ingest else "(none)"))
            cap = csi_camera.open_camera(cam_idx)
            cameras.append((cam_idx, cap))
            stream_writers[cam_idx] = StreamWriter(ingest or "", label) if ingest else None

        infer_thread = threading.Thread(
            target=inference_loop,
            args=(frame_store, runner, stop_event),
            name="inference-worker",
            daemon=True,
        )
        infer_thread.start()
        threads.append(infer_thread)

        for cam_idx, cap in cameras:
            grab_thread = threading.Thread(
                target=csi_camera.camera_loop,
                args=(cam_idx, cap, frame_store, stop_event),
                name="camera-%d" % cam_idx,
                daemon=True,
            )
            grab_thread.start()
            threads.append(grab_thread)

            stream_thread = threading.Thread(
                target=stream_loop,
                args=(cam_idx, frame_store, stream_writers.get(cam_idx), stop_event),
                name="stream-%d" % cam_idx,
                daemon=True,
            )
            stream_thread.start()
            threads.append(stream_thread)

        print(
            "Running %d CSI camera(s): 1 inference worker + %d grab + %d stream threads."
            % (len(cameras), len(cameras), len(cameras))
        )

        heartbeat = 0
        while not stop_event.is_set():
            stop_event.wait(timeout=5.0)
            heartbeat += 1
            alive = sum(1 for t in threads if t.is_alive())
            stream_counts = []
            for cam_idx in sorted(stream_writers.keys()):
                sw = stream_writers.get(cam_idx)
                if sw is not None and sw.writer is not None:
                    stream_counts.append("%d:%d" % (cam_idx, sw.frames_written()))
            print(
                "heartbeat %d: %d/%d threads alive | cams: %s | gst frames: %s | %s"
                % (
                    heartbeat,
                    alive,
                    len(threads),
                    [c[0] for c in cameras],
                    stream_counts or ["none"],
                    runner.status,
                )
            )

    finally:
        stop_event.set()
        for t in threads:
            t.join(timeout=5.0)
        for cam_idx, cap in cameras:
            csi_camera.close_camera(cap, cam_idx)
        print("Shutdown complete.")
