"""Per-camera GStreamer RTSP push (MediaMTX ingest).

Tries Jetson hardware H.264 (nvv4l2h264enc) first, falls back to software x264enc.
Both paths use a small leaky queue to avoid frame backlog on live streams.
"""

import time

import cv2

from . import config


def _appsrc_prefix(width, height, fps):
    return (
        "appsrc is-live=true format=time do-timestamp=true "
        "caps=video/x-raw,format=BGR,width=%d,height=%d,framerate=%d/1 ! "
        "queue max-size-buffers=2 leaky=downstream ! "
        % (int(width), int(height), int(fps))
    )


def build_hw_pipeline(width, height, fps, bitrate_kbps, ingest_url):
    """Jetson V4L2 hardware encoder (Orin Nano / Orin NX)."""
    key_int = max(int(fps) * 2, 10)
    bitrate_bps = int(bitrate_kbps) * 1000
    return (
        _appsrc_prefix(width, height, fps)
        + "videoconvert ! video/x-raw,format=NV12 ! "
        "nvvidconv ! video/x-raw(memory:NVMM),format=NV12 ! "
        "nvv4l2h264enc bitrate=%d control-rate=1 preset-level=1 "
        "insert-sps-pps=true iframeinterval=%d maxperf-enable=true ! "
        "h264parse ! "
        "rtspclientsink location=%s protocols=tcp tls-validation-flags=0"
        % (bitrate_bps, key_int, ingest_url)
    )


def build_sw_pipeline(width, height, fps, bitrate_kbps, ingest_url):
    """Software x264 — portable fallback when HW encode fails in Docker."""
    key_int = max(int(fps) * 2, 10)
    return (
        _appsrc_prefix(width, height, fps)
        + "videoconvert ! video/x-raw,format=I420 ! "
        "x264enc tune=zerolatency speed-preset=ultrafast bitrate=%d "
        "key-int-max=%d ! "
        "h264parse ! "
        "rtspclientsink location=%s protocols=tcp tls-validation-flags=0"
        % (int(bitrate_kbps), key_int, ingest_url)
    )


def _open_video_writer(pipeline, fps, width, height):
    size = (int(width), int(height))
    writer = cv2.VideoWriter(
        pipeline, cv2.CAP_GSTREAMER, 0, float(fps), size, True,
    )
    if writer is not None and writer.isOpened():
        return writer
    if writer is not None:
        writer.release()
    return None


class StreamWriter(object):
    def __init__(self, ingest_url, label):
        self.ingest_url = ingest_url.strip()
        self.label = label
        self.writer = None
        self.backend = None
        self.out_w = config.STREAM_WIDTH if config.STREAM_WIDTH > 0 else 0
        self.out_h = config.STREAM_HEIGHT if config.STREAM_HEIGHT > 0 else 0
        self.interval = 1.0 / config.STREAM_FPS
        self.last_write_ts = 0.0
        self._open_failed = False
        self._frames_written = 0

    def _open(self, frame_w, frame_h):
        if not self.ingest_url or self.writer is not None or self._open_failed:
            return
        out_w = self.out_w or frame_w
        out_h = self.out_h or frame_h
        fps = config.STREAM_FPS
        bitrate = config.STREAM_BITRATE_KBPS
        suffix = self.ingest_url.rsplit("/", 1)[-1]
        print(
            "%s: opening RTSP push %dx%d @ %d fps -> .../%s (encoder=%s)"
            % (self.label, out_w, out_h, fps, suffix, config.STREAM_ENCODER)
        )

        pref = config.STREAM_ENCODER
        candidates = []
        if pref == "hw":
            candidates = [("nvv4l2h264enc", build_hw_pipeline)]
        elif pref == "sw":
            candidates = [("x264enc", build_sw_pipeline)]
        else:
            candidates = [
                ("nvv4l2h264enc", build_hw_pipeline),
                ("x264enc", build_sw_pipeline),
            ]

        for backend_name, builder in candidates:
            try:
                pipeline = builder(out_w, out_h, fps, bitrate, self.ingest_url)
            except Exception as exc:
                print("WARNING %s: %s pipeline build failed: %s" % (
                    self.label, backend_name, exc,
                ))
                continue
            writer = _open_video_writer(pipeline, fps, out_w, out_h)
            if writer is not None:
                self.writer = writer
                self.backend = backend_name
                print("%s: RTSP push active (%s)" % (self.label, backend_name))
                return
            print("WARNING %s: %s pipeline failed to open" % (self.label, backend_name))

        self._open_failed = True
        print("WARNING %s: RTSP writer failed (all encoders)" % self.label)

    def write_paced(self, bgr):
        if not self.ingest_url:
            return
        h, w = bgr.shape[:2]
        self._open(w, h)
        if self.writer is None or not self.writer.isOpened():
            return
        now = time.time()
        if now - self.last_write_ts < self.interval:
            return
        out_w = self.out_w or w
        out_h = self.out_h or h
        out = cv2.resize(bgr, (out_w, out_h)) if (w, h) != (out_w, out_h) else bgr
        self.writer.write(out)
        self.last_write_ts = now
        self._frames_written += 1

    def frames_written(self):
        return self._frames_written

    def release(self):
        if self.writer is not None:
            self.writer.release()
            self.writer = None
        if self.backend and self._frames_written:
            print(
                "%s: released (%s, %d frames)"
                % (self.label, self.backend, self._frames_written)
            )
