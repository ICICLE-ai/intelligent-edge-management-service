"""Per-camera GStreamer RTSP push (MediaMTX ingest).

Backend priority on JP4:
  1. PyGObject appsrc  (reliable; same elements as working videotestsrc test)
  2. OpenCV VideoWriter + appsrc
  3. ffmpeg rawvideo pipe -> libx264 -> RTSP
"""

import os
import subprocess
import threading
import time

import cv2
import numpy as np

from . import config

try:
    from queue import Full, Queue
except ImportError:
    from Queue import Full, Queue

_gst_inited = False


def _ensure_gst():
    global _gst_inited
    if _gst_inited:
        return True
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        _gst_inited = True
        return True
    except Exception as exc:
        print("WARNING: GStreamer Python bindings unavailable: %s" % exc)
        return False


def build_gst_pipeline_string(width, height, fps, bitrate_kbps, ingest_url):
    """Match the manual videotestsrc pipeline that worked on ingest."""
    key_int = max(int(fps) * 2, 10)
    return (
        "appsrc name=src is-live=true format=time do-timestamp=true "
        "caps=video/x-raw,format=BGR,width=%d,height=%d,framerate=%d/1 ! "
        "videoconvert ! video/x-raw,format=I420 ! "
        "x264enc tune=zerolatency speed-preset=ultrafast bitrate=%d "
        "key-int-max=%d ! "
        "h264parse ! "
        "rtspclientsink location=%s protocols=tcp tls-validation-flags=0"
        % (width, height, fps, bitrate_kbps, key_int, ingest_url)
    )


def open_gstreamer_writer(pipeline, fps, width, height):
    """Open cv2 VideoWriter for a GStreamer pipeline (OpenCV 3 JP4 vs OpenCV 4)."""
    size = (int(width), int(height))
    fps_int = int(fps)
    fps_val = float(fps)

    attempts = [
        ("jp4-fourcc0", lambda: cv2.VideoWriter(pipeline, 0, fps_val, size)),
        ("jp4-fourcc0-intfps", lambda: cv2.VideoWriter(pipeline, 0, fps_int, size)),
        ("opencv-gst", lambda: cv2.VideoWriter(
            pipeline, cv2.CAP_GSTREAMER, 0, fps_val, size)),
    ]
    for name, factory in attempts:
        try:
            writer = factory()
            if writer is not None and writer.isOpened():
                print("VideoWriter opened (%s)" % name)
                return writer
            print("VideoWriter %s: isOpened() returned false" % name)
        except Exception as exc:
            print("VideoWriter %s failed: %s" % (name, exc))
    return None


def _write_all_bytes(stdin_pipe, data):
    fd = stdin_pipe.fileno()
    offset = 0
    total = len(data)
    while offset < total:
        nbytes = os.write(fd, data[offset:])
        if nbytes <= 0:
            raise IOError("pipe write returned %d" % nbytes)
        offset += nbytes


class GstPythonAppsrcWriter(object):
    """Push BGR frames through GStreamer appsrc via PyGObject (JP4 reliable path)."""

    def __init__(self, ingest_url, width, height, fps, bitrate_kbps, label):
        from gi.repository import Gst

        self.label = label
        self.width = int(width)
        self.height = int(height)
        self.fps = int(fps)
        self._frames_written = 0
        self._pts = 0
        self._duration = Gst.SECOND // max(1, self.fps)
        self._stop = threading.Event()
        self._failed = False

        pipeline_str = build_gst_pipeline_string(
            self.width, self.height, self.fps, bitrate_kbps, ingest_url
        )
        print("%s: starting PyGObject appsrc pipeline %dx%d @ %d fps" % (
            label, self.width, self.height, self.fps,
        ))
        try:
            self.pipeline = Gst.parse_launch(pipeline_str)
            self.appsrc = self.pipeline.get_by_name("src")
            if self.appsrc is None:
                raise RuntimeError("appsrc element not found in pipeline")
        except Exception as exc:
            print("WARNING %s: failed to parse GStreamer pipeline: %s" % (label, exc))
            self.pipeline = None
            self.appsrc = None
            return

        self._bus_thread = threading.Thread(
            target=self._bus_loop, name="gst-bus-%s" % label
        )
        self._bus_thread.daemon = True
        self._bus_thread.start()

        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            print("WARNING %s: pipeline failed to reach PLAYING" % label)
            self.pipeline = None
            self.appsrc = None
            return

        time.sleep(0.5)

    def _bus_loop(self):
        from gi.repository import Gst
        if self.pipeline is None:
            return
        bus = self.pipeline.get_bus()
        while not self._stop.is_set():
            msg = bus.timed_pop(200 * Gst.MSECOND)
            if msg is None:
                continue
            if msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                print("WARNING %s: GStreamer ERROR: %s | %s" % (
                    self.label, err, debug,
                ))
                self._failed = True
                break
            if msg.type == Gst.MessageType.EOS:
                break

    def is_active(self):
        return (
            not self._failed
            and self.pipeline is not None
            and self.appsrc is not None
        )

    def frames_written(self):
        return self._frames_written

    def write(self, bgr):
        from gi.repository import Gst
        if not self.is_active():
            return False
        h, w = bgr.shape[:2]
        if (w, h) != (self.width, self.height):
            bgr = cv2.resize(bgr, (self.width, self.height))
        if not bgr.flags["C_CONTIGUOUS"]:
            bgr = np.ascontiguousarray(bgr)
        data = bgr.tobytes()
        buf = Gst.Buffer.new_allocate(None, len(data), None)
        buf.fill(0, data)
        buf.pts = self._pts
        buf.duration = self._duration
        buf.offset = self._frames_written
        self._pts += self._duration
        flow = self.appsrc.emit("push-buffer", buf)
        if flow != Gst.FlowReturn.OK:
            print("WARNING %s: push-buffer returned %s" % (self.label, flow))
            self._failed = True
            return False
        self._frames_written += 1
        if self._frames_written in (1, 30, 100, 300):
            print("%s: wrote %d frames via PyGObject appsrc" % (
                self.label, self._frames_written,
            ))
        return True

    def release(self):
        from gi.repository import Gst
        self._stop.set()
        if self._bus_thread is not None:
            self._bus_thread.join(timeout=2.0)
        if self.pipeline is not None:
            try:
                if self.appsrc is not None:
                    self.appsrc.emit("end-of-stream")
                self.pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass
        print("%s: total frames via PyGObject appsrc: %d" % (
            self.label, self._frames_written,
        ))


class CvAppsrcWriter(object):
    """OpenCV VideoWriter feeding appsrc -> x264 -> rtspclientsink."""

    def __init__(self, ingest_url, width, height, fps, bitrate_kbps, label):
        self.label = label
        self.width = int(width)
        self.height = int(height)
        self._frames_written = 0
        pipeline = build_gst_pipeline_string(
            self.width, self.height, fps, bitrate_kbps, ingest_url
        )
        print("%s: trying OpenCV VideoWriter appsrc %dx%d @ %s fps" % (
            label, self.width, self.height, fps,
        ))
        self.writer = open_gstreamer_writer(
            pipeline, fps, self.width, self.height
        )

    def is_active(self):
        return self.writer is not None and self.writer.isOpened()

    def frames_written(self):
        return self._frames_written

    def write(self, bgr):
        if not self.is_active():
            return False
        h, w = bgr.shape[:2]
        if (w, h) != (self.width, self.height):
            bgr = cv2.resize(bgr, (self.width, self.height))
        self.writer.write(bgr)
        self._frames_written += 1
        if self._frames_written in (1, 30, 100, 300):
            print("%s: wrote %d frames via OpenCV appsrc" % (
                self.label, self._frames_written,
            ))
        return True

    def release(self):
        if self.writer is not None:
            self.writer.release()
            self.writer = None
        print("%s: total frames via OpenCV appsrc: %d" % (
            self.label, self._frames_written,
        ))


class FfmpegPipeWriter(object):
    """ffmpeg rawvideo stdin -> libx264 -> RTSP/TLS."""

    QUEUE_SIZE = 2

    def __init__(self, ingest_url, width, height, fps, bitrate_kbps, label):
        self.label = label
        self.width = int(width)
        self.height = int(height)
        self.blocksize = self.width * self.height * 3
        self.proc = None
        self._queue = Queue(maxsize=self.QUEUE_SIZE)
        self._stop = threading.Event()
        self._thread = None
        self._stderr_thread = None
        self._dropped = 0
        self._frames_written = 0

        cmd = [
            "ffmpeg", "-nostdin", "-loglevel", "warning",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", "%dx%d" % (self.width, self.height),
            "-r", str(int(fps)),
            "-i", "pipe:0",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-b:v", "%dk" % int(bitrate_kbps),
            "-pix_fmt", "yuv420p", "-g", str(max(int(fps) * 2, 10)),
            "-f", "rtsp", "-rtsp_transport", "tcp",
            ingest_url,
        ]
        print("%s: starting ffmpeg RTSP push %dx%d @ %s fps" % (
            label, self.width, self.height, fps,
        ))
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except Exception as exc:
            print("WARNING %s: ffmpeg failed to start: %s" % (label, exc))
            self.proc = None
            return

        self._stderr_thread = threading.Thread(
            target=self._stderr_loop, name="ffmpeg-err-%s" % label
        )
        self._stderr_thread.daemon = True
        self._stderr_thread.start()

        time.sleep(1.0)
        if self.proc.poll() is not None:
            print("WARNING %s: ffmpeg exited early (code %s)" % (
                label, self.proc.returncode,
            ))
            self.proc = None
            return

        self._thread = threading.Thread(
            target=self._writer_loop, name="ffmpeg-%s" % label
        )
        self._thread.daemon = True
        self._thread.start()

    def _stderr_loop(self):
        if self.proc is None or self.proc.stderr is None:
            return
        for line in iter(self.proc.stderr.readline, b""):
            text = line.decode("utf-8", "replace").strip()
            if text:
                print("%s ffmpeg: %s" % (self.label, text))

    def is_active(self):
        return (
            self.proc is not None
            and self.proc.poll() is None
            and self._thread is not None
            and self._thread.is_alive()
        )

    def frames_written(self):
        return self._frames_written

    def write(self, bgr):
        if not self.is_active():
            return False
        h, w = bgr.shape[:2]
        if (w, h) != (self.width, self.height):
            bgr = cv2.resize(bgr, (self.width, self.height))
        if not bgr.flags["C_CONTIGUOUS"]:
            bgr = np.ascontiguousarray(bgr)
        try:
            self._queue.put_nowait(bgr)
            return True
        except Full:
            self._dropped += 1
            return False

    def _writer_loop(self):
        while not self._stop.is_set():
            try:
                frame = self._queue.get(timeout=0.5)
            except Exception:
                if self.proc is not None and self.proc.poll() is not None:
                    break
                continue
            try:
                if self.proc is None or self.proc.stdin is None:
                    break
                data = frame.tobytes()
                if len(data) != self.blocksize:
                    continue
                _write_all_bytes(self.proc.stdin, data)
                self._frames_written += 1
                if self._frames_written in (1, 30, 100, 300):
                    print("%s: wrote %d frames via ffmpeg" % (
                        self.label, self._frames_written,
                    ))
            except (BrokenPipeError, IOError, OSError) as exc:
                print("WARNING %s: ffmpeg stdin closed: %s" % (self.label, exc))
                break
            finally:
                self._queue.task_done()

    def release(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=1.0)
        print("%s: total frames via ffmpeg: %d" % (
            self.label, self._frames_written,
        ))
        if self.proc is None:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None


class StreamWriter(object):
    """Lazy-open RTSP writer with frame pacing."""

    def __init__(self, ingest_url, label):
        self.ingest_url = ingest_url.strip()
        self.label = label
        self.backend = None
        self.out_w = config.STREAM_WIDTH if config.STREAM_WIDTH > 0 else 0
        self.out_h = config.STREAM_HEIGHT if config.STREAM_HEIGHT > 0 else 0
        self.interval = 1.0 / config.STREAM_FPS
        self.last_write_ts = 0.0
        self._open_failed = False

    def _open(self, frame_w, frame_h):
        if not self.ingest_url or self.backend is not None or self._open_failed:
            return
        out_w = self.out_w or frame_w
        out_h = self.out_h or frame_h
        path_suffix = self.ingest_url.rsplit("/", 1)[-1]
        print(
            "%s: opening RTSP push %dx%d @ %d fps -> .../%s"
            % (self.label, out_w, out_h, config.STREAM_FPS, path_suffix)
        )

        if _ensure_gst():
            pygst = GstPythonAppsrcWriter(
                self.ingest_url, out_w, out_h,
                config.STREAM_FPS, config.STREAM_BITRATE_KBPS, self.label,
            )
            if pygst.is_active():
                self.backend = pygst
                print("%s: RTSP push active (PyGObject appsrc)" % self.label)
                return

        cvw = CvAppsrcWriter(
            self.ingest_url, out_w, out_h,
            config.STREAM_FPS, config.STREAM_BITRATE_KBPS, self.label,
        )
        if cvw.is_active():
            self.backend = cvw
            print("%s: RTSP push active (OpenCV appsrc)" % self.label)
            return

        ffw = FfmpegPipeWriter(
            self.ingest_url, out_w, out_h,
            config.STREAM_FPS, config.STREAM_BITRATE_KBPS, self.label,
        )
        if ffw.is_active():
            self.backend = ffw
            print("%s: RTSP push active (ffmpeg)" % self.label)
            return

        self._open_failed = True
        print("WARNING %s: RTSP writer failed (all backends)" % self.label)

    def write_paced(self, bgr):
        if not self.ingest_url:
            return
        h, w = bgr.shape[:2]
        self._open(w, h)
        if self.backend is None:
            return
        if hasattr(self.backend, "is_active") and not self.backend.is_active():
            return
        now = time.time()
        if now - self.last_write_ts < self.interval:
            return
        if self.backend.write(bgr):
            self.last_write_ts = now

    @property
    def writer(self):
        return self.backend

    def release(self):
        if self.backend is not None:
            self.backend.release()
            self.backend = None
