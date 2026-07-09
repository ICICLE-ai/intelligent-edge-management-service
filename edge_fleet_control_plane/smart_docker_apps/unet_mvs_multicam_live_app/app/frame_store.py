"""Thread-safe latest-frame store (raw + processed per camera)."""

import threading


class FrameStore(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.raw_frames = {}
        self.processed_frames = {}
        self.status = {}
        self.raw_seq = {}

    def update(self, camera_id, raw_frame, processed_frame, status):
        with self.lock:
            self.raw_seq[camera_id] = self.raw_seq.get(camera_id, 0) + 1
            self.raw_frames[camera_id] = raw_frame.copy()
            self.processed_frames[camera_id] = processed_frame.copy()
            self.status[camera_id] = dict(status)

    def update_raw(self, camera_id, raw_frame, fallback_processed_frame, status_updates):
        with self.lock:
            self.raw_seq[camera_id] = self.raw_seq.get(camera_id, 0) + 1
            self.raw_frames[camera_id] = raw_frame.copy()
            if camera_id not in self.processed_frames:
                self.processed_frames[camera_id] = fallback_processed_frame.copy()
            status = self.status.setdefault(camera_id, {})
            status.update(status_updates)

    def update_processed(self, camera_id, processed_frame, status_updates):
        with self.lock:
            self.processed_frames[camera_id] = processed_frame.copy()
            status = self.status.setdefault(camera_id, {})
            status.update(status_updates)

    def raw_generation(self, camera_id):
        with self.lock:
            return self.raw_seq.get(camera_id, 0)

    def camera_ids(self):
        with self.lock:
            return sorted(set(self.raw_frames.keys()) | set(self.processed_frames.keys()))

    def get_status(self, camera_id):
        with self.lock:
            return dict(self.status.get(camera_id, {}))

    def get(self, camera_id, stream_type):
        with self.lock:
            frames = self.raw_frames if stream_type == "raw" else self.processed_frames
            frame = frames.get(camera_id)
            if frame is None:
                return None
            return frame.copy()
