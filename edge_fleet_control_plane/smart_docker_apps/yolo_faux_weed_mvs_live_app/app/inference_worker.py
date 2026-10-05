"""Dedicated YOLO inference thread (round-robin across cameras)."""

import time
import traceback

import cv2

from . import config


def inference_loop(frame_store, runner, stop_event):
    if not runner.loaded:
        print("Inference worker: skipping - model not loaded.")
        while not stop_event.is_set():
            time.sleep(0.5)
        return

    print("Inference worker: started.")

    last_seq = {}
    infer_counter = {}
    last_annotated = {}
    last_infer_ms = {}
    last_detections = {}

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
            detections = last_detections.get(camera_id, 0)
            model_status = runner.status
            annotated = last_annotated.get(camera_id, raw_frame)

            if infer_counter[camera_id] % config.DETECT_EVERY_N_FRAMES == 0:
                try:
                    annotated, infer_ms, detections = runner.infer_annotated(raw_frame)
                    last_annotated[camera_id] = annotated
                    last_infer_ms[camera_id] = infer_ms
                    last_detections[camera_id] = detections
                    if infer_counter[camera_id] <= config.DETECT_EVERY_N_FRAMES * 2:
                        print(
                            "camera %d: infer ok %.1f ms, %d detection(s) (%s)"
                            % (camera_id, infer_ms, detections, runner.status)
                        )
                except Exception as exc:
                    print("camera %d YOLO inference failed: %s" % (camera_id, exc))
                    traceback.print_exc()
                    model_status = "MODEL INFER FAILED"

            status = frame_store.get_status(camera_id)
            fps = float(status.get("fps", 0.0))
            overlay = annotated.copy()
            font_scale = max(1.1, overlay.shape[1] / 960.0 * 0.85)
            thickness = max(2, int(round(overlay.shape[1] / 640.0)))
            cv2.putText(
                overlay,
                "Cam %d | %.1f fps | infer %.0f ms | det %d"
                % (camera_id, fps, infer_ms, detections),
                (20, int(48 * font_scale)),
                cv2.FONT_HERSHEY_SIMPLEX,
                font_scale,
                (0, 255, 0),
                thickness,
            )

            frame_store.update_processed(
                camera_id,
                overlay,
                {
                    "model_status": model_status,
                    "infer_ms": infer_ms,
                    "detections": detections,
                },
            )

        if not processed_any:
            time.sleep(0.01)
