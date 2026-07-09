"""Jetson CSI cameras via nvarguscamerasrc (one capture thread per sensor-id)."""

import time

import cv2

from . import config


def resize_keep_aspect(image, target_width):
    height, width = image.shape[:2]
    if width <= target_width:
        return image
    scale = float(target_width) / float(width)
    new_height = int(height * scale)
    return cv2.resize(image, (target_width, new_height), interpolation=cv2.INTER_AREA)


def build_capture_pipeline(sensor_id):
    return (
        "nvarguscamerasrc sensor-id=%d ! "
        "video/x-raw(memory:NVMM), width=%d, height=%d, "
        "format=NV12, framerate=%d/1 ! "
        "nvvidconv ! video/x-raw, format=BGRx ! "
        "videoconvert ! video/x-raw, format=BGR ! "
        "appsink drop=true max-buffers=1"
        % (sensor_id, config.CAM_WIDTH, config.CAM_HEIGHT, config.CAM_FPS)
    )


def open_camera(sensor_id):
    pipeline = build_capture_pipeline(sensor_id)
    print("Opening CSI sensor-id=%d (%dx%d @ %d fps)" % (
        sensor_id, config.CAM_WIDTH, config.CAM_HEIGHT, config.CAM_FPS,
    ))
    cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
    if not cap.isOpened():
        raise RuntimeError("Could not open CSI sensor-id=%d via GStreamer" % sensor_id)
    return cap


def camera_loop(camera_id, cap, frame_store, stop_event):
    frame_count = 0
    fps_start_time = time.time()
    current_fps = 0.0

    try:
        while not stop_event.is_set():
            ret, bgr = cap.read()
            if not ret or bgr is None:
                time.sleep(0.01)
                continue

            frame_count += 1
            now = time.time()
            elapsed = now - fps_start_time
            if elapsed >= 1.0:
                current_fps = frame_count / elapsed
                st = frame_store.get_status(camera_id)
                infer_ms = float(st.get("infer_ms", 0.0))
                trt_status = st.get("trt_status", "TRT WAITING")
                print(
                    "cam %d grab FPS: %.2f | infer: %.1f ms | %s"
                    % (camera_id, current_fps, infer_ms, trt_status)
                )
                frame_count = 0
                fps_start_time = now

            raw_frame = resize_keep_aspect(bgr, config.DISPLAY_WIDTH)
            preview = raw_frame.copy()
            cv2.putText(
                preview,
                "Cam %d grab %.1f fps" % (camera_id, current_fps),
                (20, 40),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 255, 0),
                2,
            )
            frame_store.update_raw(camera_id, raw_frame, preview, {"fps": current_fps})
    finally:
        cap.release()


def close_camera(cap, camera_id):
    try:
        cap.release()
    except Exception:
        pass
    print("CSI sensor-id=%d released" % camera_id)
