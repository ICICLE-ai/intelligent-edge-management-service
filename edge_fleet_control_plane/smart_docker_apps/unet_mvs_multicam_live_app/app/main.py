"""UNet++ multi-GigE live streaming - entry point."""

import os
import signal
import sys
import threading

# Headless container: avoid OpenCV/GUI trying X11 (XOpenDisplay failed).
os.environ.pop("DISPLAY", None)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

os.environ.setdefault("CUDA_MODULE_LOADING", "LAZY")

from . import config
from . import hik_camera
from .frame_store import FrameStore
from .inference_worker import inference_loop
from .stream_worker import stream_loop
from .stream_writer import StreamWriter
from .unet_runner import SharedUnetRunner


def _resolve_ingest_urls(camera_indices):
    urls = config.STREAM_INGEST_URLS
    if not urls:
        return [None] * len(camera_indices)

    by_index = {}
    for key, val in os.environ.items():
        if not key.startswith("STREAM_INGEST_URL_") or key == "STREAM_INGEST_URLS":
            continue
        suffix = key[len("STREAM_INGEST_URL_") :]
        if suffix.isdigit() and val.strip():
            by_index[int(suffix)] = val.strip()

    resolved = []
    for i, cam_idx in enumerate(camera_indices):
        if cam_idx in by_index:
            resolved.append(by_index[cam_idx])
        elif i < len(urls):
            resolved.append(urls[i])
        else:
            resolved.append(None)
    return resolved


def main():
    print("--- UNet++ MVS multi-camera live stream (AGX Xavier / JP4) ---")
    print("ENGINE_PATH:          %s" % config.ENGINE_PATH)
    print("CAMERA_INDICES:       %s" % (config.CAMERA_INDICES or "all"))
    print("STREAM_FPS:           %d" % config.STREAM_FPS)
    print("STREAM_WIDTH/HEIGHT:  %dx%d" % (config.STREAM_WIDTH, config.STREAM_HEIGHT))
    print("DETECT_EVERY_N:       %d" % config.DETECT_EVERY_N_FRAMES)
    print("STREAM_INGEST_URLS:   %d configured" % len(config.STREAM_INGEST_URLS))
    print("Architecture:         grab threads + inference worker + stream threads")

    if not os.path.exists(config.ENGINE_PATH):
        print("CRITICAL: engine not found at %s" % config.ENGINE_PATH)
        sys.exit(1)

    runner = SharedUnetRunner(config.ENGINE_PATH)
    try:
        runner.load()
    except Exception as exc:
        print("CRITICAL: UNet engine load failed: %s" % exc)
        print("Hints:")
        print("  - Engine must be built on this Xavier (TRT 7)")
        print("  - Stop other containers using the GPU: docker ps && docker rm -f ...")
        print("  - Verify mount: -v $HOME/Documents:/workspace/models:ro")
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

    hik_camera.initialize_sdk()

    try:
        device_list = hik_camera.enum_devices()
        hik_camera.print_device_list(device_list)
        indices = hik_camera.selected_indices(device_list)
        if not indices:
            print("CRITICAL: no valid camera indices.")
            sys.exit(1)

        ingest_urls = _resolve_ingest_urls(indices)
        for pos, cam_idx in enumerate(indices):
            ingest = ingest_urls[pos] if pos < len(ingest_urls) else None
            label = "cam-%d" % cam_idx
            print("Opening %s ingest=%s" % (label, "(set)" if ingest else "(none)"))
            cam = hik_camera.open_camera(device_list, cam_idx)
            hik_camera.start_grabbing(cam, cam_idx)
            cameras.append((cam_idx, cam))
            stream_writers[cam_idx] = StreamWriter(ingest or "", label) if ingest else None

        infer_thread = threading.Thread(
            target=inference_loop,
            args=(frame_store, runner, stop_event),
            name="inference-worker",
        )
        infer_thread.daemon = True
        infer_thread.start()
        threads.append(infer_thread)

        for cam_idx, cam in cameras:
            grab_thread = threading.Thread(
                target=hik_camera.camera_loop,
                args=(cam_idx, cam, frame_store, stop_event),
                name="camera-%d" % cam_idx,
            )
            grab_thread.daemon = True
            grab_thread.start()
            threads.append(grab_thread)

            stream_thread = threading.Thread(
                target=stream_loop,
                args=(cam_idx, frame_store, stream_writers.get(cam_idx), stop_event),
                name="stream-%d" % cam_idx,
            )
            stream_thread.daemon = True
            stream_thread.start()
            threads.append(stream_thread)

        print(
            "Running %d camera(s): 1 inference worker + %d grab + %d stream threads."
            % (len(cameras), len(cameras), len(cameras))
        )
        print("Ctrl+C to stop.")

        heartbeat = 0
        while not stop_event.is_set():
            stop_event.wait(timeout=5.0)
            heartbeat += 1
            alive = sum(1 for t in threads if t.is_alive())
            stream_counts = []
            for cam_idx in sorted(stream_writers.keys()):
                sw = stream_writers.get(cam_idx)
                if sw is not None and sw.writer is not None:
                    stream_counts.append(
                        "%d:%d" % (cam_idx, sw.writer.frames_written())
                    )
            print(
                "heartbeat %d: %d/%d worker threads alive | cams: %s | gst frames: %s"
                % (
                    heartbeat,
                    alive,
                    len(threads),
                    [c[0] for c in cameras],
                    stream_counts or ["none"],
                )
            )

    finally:
        stop_event.set()
        for t in threads:
            t.join(timeout=5.0)
        for cam_idx, cam in cameras:
            hik_camera.close_camera(cam, cam_idx)
        hik_camera.finalize_sdk()
        print("Shutdown complete.")


if __name__ == "__main__":
    main()
