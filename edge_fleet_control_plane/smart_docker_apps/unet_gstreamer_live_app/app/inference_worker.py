"""Dedicated UNet++ inference thread (round-robin across cameras)."""

import time
import traceback

import cv2

from . import config


def inference_loop(frame_store, runner, stop_event):
    if runner.engine is None:
        print("Inference worker: skipping - engine not loaded.")
        while not stop_event.is_set():
            time.sleep(0.5)
        return

    print("Inference worker: started.")
    last_seq = {}
    infer_counter = {}
    last_overlay = {}
    last_infer_ms = {}

    while not stop_event.is_set():
        camera_ids = frame_store.camera_ids()
        if not camera_ids:
            time.sleep(0.05)
            continue

        processed_any = False
        for camera_id in camera_ids:
            if stop_event.is_set():
                break

            seq = frame_store.raw_generation(camera_id)
            if seq == last_seq.get(camera_id, -1):
                continue

            last_seq[camera_id] = seq
            processed_any = True

            raw_frame = frame_store.get(camera_id, "raw")
            if raw_frame is None:
                continue

            infer_counter[camera_id] = infer_counter.get(camera_id, 0) + 1
            infer_ms = last_infer_ms.get(camera_id, 0.0)
            trt_status = runner.status
            overlay = last_overlay.get(camera_id, raw_frame)

            if (
                runner.engine is not None
                and infer_counter[camera_id] % config.DETECT_EVERY_N_FRAMES == 0
            ):
                try:
                    overlay, infer_ms = runner.infer_overlay(raw_frame)
                    last_overlay[camera_id] = overlay
                    last_infer_ms[camera_id] = infer_ms
                except Exception as exc:
                    print("camera %d UNet inference failed: %s" % (camera_id, exc))
                    traceback.print_exc()
                    trt_status = "TRT INFER FAILED"

            annotated = overlay.copy()
            cv2.putText(
                annotated,
                "Cam %d | %.1f fps | infer %.0f ms" % (
                    camera_id,
                    float(frame_store.get_status(camera_id).get("fps", 0.0)),
                    infer_ms,
                ),
                (20, 40),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 255, 0),
                2,
            )

            frame_store.update_processed(
                camera_id,
                annotated,
                {"trt_status": trt_status, "infer_ms": infer_ms},
            )

        if not processed_any:
            time.sleep(0.01)
