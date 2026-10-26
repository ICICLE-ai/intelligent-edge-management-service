"""Per-camera RTSP push thread (reads processed frames from FrameStore)."""

import time

from . import config


def stream_loop(camera_id, frame_store, stream_writer, stop_event):
    label = "cam-%d" % camera_id
    if stream_writer is None:
        print("%s: no ingest URL - stream thread idle" % label)
        while not stop_event.is_set():
            time.sleep(0.5)
        return

    while not stop_event.is_set():
        frame = frame_store.get(camera_id, "processed")
        if frame is not None:
            stream_writer.write_paced(frame)
        time.sleep(max(0.005, 1.0 / max(1, config.STREAM_FPS)))

    stream_writer.release()
