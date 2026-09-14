#!/usr/bin/env python3
"""Run YOLO on a live video source and publish annotated video over RTSP/RTSPS."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import cv2
import numpy as np
import torch
from ultralytics import YOLO

from app import config


STOP = threading.Event()


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_source(value: str) -> str | int:
    value = value.strip()
    return int(value) if value.isdigit() else value


def redact_url(value: str) -> str:
    return re.sub(r"(?<=//)[^/@]+@", "***@", value)


def parse_classes(value: str) -> list[int] | None:
    if not value.strip():
        return None
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detect weeds in a live stream and publish annotated RTSPS video."
    )
    parser.add_argument(
        "--model",
        default=config.MODEL_PATH,
    )
    parser.add_argument(
        "--source",
        default=config.SOURCE,
        help="Input camera index, file, RTSP/RTSPS/HTTP URL, or SDP file",
    )
    parser.add_argument(
        "--publish-url",
        default=config.ingest_url(),
        help="RTSPS ingest URL. IEMS injects STREAM_INGEST_URL on deploy.",
    )
    parser.add_argument("--device", default=os.getenv("DEVICE", "0"))
    parser.add_argument(
        "--confidence", type=float, default=float(os.getenv("CONFIDENCE", "0.25"))
    )
    parser.add_argument("--iou", type=float, default=float(os.getenv("IOU", "0.45")))
    parser.add_argument(
        "--image-size", type=int, default=int(os.getenv("IMAGE_SIZE", "640"))
    )
    parser.add_argument(
        "--classes",
        default=os.getenv("CLASSES", ""),
        help="Comma-separated class IDs; empty publishes all model classes",
    )
    parser.add_argument(
        "--half",
        action=argparse.BooleanOptionalAction,
        default=env_bool("HALF", True),
    )
    parser.add_argument(
        "--publish-fps", type=float, default=float(config.STREAM_FPS)
    )
    parser.add_argument(
        "--output-width", type=int, default=int(config.STREAM_WIDTH)
    )
    parser.add_argument(
        "--output-height", type=int, default=int(config.STREAM_HEIGHT)
    )
    parser.add_argument(
        "--input-width",
        type=int,
        default=int(config.INPUT_WIDTH),
        help="Decoded width for an SDP/RTP input",
    )
    parser.add_argument(
        "--input-height",
        type=int,
        default=int(config.INPUT_HEIGHT),
        help="Decoded height for an SDP/RTP input",
    )
    parser.add_argument(
        "--bitrate",
        default=config.stream_bitrate(),
        help="FFmpeg H.264 target bitrate, for example 1800k or 3M",
    )
    parser.add_argument(
        "--preset", default=os.getenv("X264_PRESET", "veryfast")
    )
    parser.add_argument(
        "--input-reconnect-delay",
        type=float,
        default=float(os.getenv("INPUT_RECONNECT_DELAY", "2")),
    )
    parser.add_argument(
        "--log-every", type=int, default=int(os.getenv("LOG_EVERY", "100"))
    )
    parser.add_argument(
        "--ffmpeg-loglevel", default=os.getenv("FFMPEG_LOGLEVEL", "warning")
    )
    return parser


def validate(args: argparse.Namespace) -> None:
    if not Path(args.model).is_file():
        raise ValueError(f"Model not found: {args.model}")
    if shutil.which("ffmpeg") is None:
        raise ValueError("ffmpeg was not found in PATH")
    if not 0 <= args.confidence <= 1 or not 0 <= args.iou <= 1:
        raise ValueError("confidence and IoU must be between 0 and 1")
    if args.publish_fps <= 0 or args.image_size <= 0:
        raise ValueError("publish FPS and image size must be greater than zero")
    if (args.output_width == 0) != (args.output_height == 0):
        raise ValueError("set both output width and output height, or neither")
    if args.output_width < 0 or args.output_height < 0:
        raise ValueError("output dimensions cannot be negative")
    if args.input_width <= 0 or args.input_height <= 0:
        raise ValueError("input dimensions must be greater than zero")
    if args.publish_url:
        scheme = urlparse(args.publish_url).scheme.lower()
        if scheme not in {"rtsp", "rtsps"}:
            raise ValueError("publish URL must use rtsp:// or rtsps://")
    if str(args.device).lower() not in {"cpu", "mps"} and not torch.cuda.is_available():
        raise ValueError("CUDA requested but unavailable; use --device cpu or enable the GPU")


class LatestFrameSource:
    """Continuously read a source, retaining only its newest decoded frame."""

    def __init__(
        self,
        source: str | int,
        reconnect_delay: float,
        input_width: int,
        input_height: int,
        ffmpeg_loglevel: str,
    ) -> None:
        self.source = source
        self.reconnect_delay = reconnect_delay
        self.input_width = input_width
        self.input_height = input_height
        self.ffmpeg_loglevel = ffmpeg_loglevel
        self.condition = threading.Condition()
        self.frame: Any | None = None
        self.sequence = 0
        self.capture: cv2.VideoCapture | None = None
        self.decoder: subprocess.Popen[bytes] | None = None
        self.thread = threading.Thread(target=self._run, name="input-reader", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _publish_frame(self, frame: Any) -> None:
        with self.condition:
            self.frame = frame
            self.sequence += 1
            self.condition.notify_all()

    def _read_exact(self, size: int) -> bytes | None:
        if self.decoder is None or self.decoder.stdout is None:
            return None
        data = bytearray()
        while len(data) < size and not STOP.is_set():
            chunk = self.decoder.stdout.read(size - len(data))
            if not chunk:
                return None
            data.extend(chunk)
        return bytes(data) if len(data) == size else None

    def _run_sdp_once(self) -> None:
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", self.ffmpeg_loglevel,
            "-nostdin", "-protocol_whitelist", "file,udp,rtp,crypto,data",
            "-fflags", "nobuffer", "-flags", "low_delay",
            "-analyzeduration", "0", "-probesize", "32768",
            "-i", str(self.source), "-an",
            "-vf", f"scale={self.input_width}:{self.input_height}",
            "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1",
        ]
        print(
            f"Starting FFmpeg RTP decoder for {self.source}; waiting for UDP packets...",
            flush=True,
        )
        self.decoder = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, bufsize=0
        )
        frame_size = self.input_width * self.input_height * 3
        connected = False
        while not STOP.is_set():
            payload = self._read_exact(frame_size)
            if payload is None:
                break
            if not connected:
                print("Input connected", flush=True)
                connected = True
            frame = np.frombuffer(payload, dtype=np.uint8).reshape(
                self.input_height, self.input_width, 3
            )
            self._publish_frame(frame)

        decoder, self.decoder = self.decoder, None
        if decoder is not None:
            if decoder.poll() is None:
                decoder.terminate()
            try:
                decoder.wait(timeout=2)
            except subprocess.TimeoutExpired:
                decoder.kill()

    def _run_opencv_once(self) -> None:
        print(f"Opening input: {self.source}", flush=True)
        capture = cv2.VideoCapture(self.source)
        self.capture = capture
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not capture.isOpened():
            print("Input open failed; reconnecting...", file=sys.stderr, flush=True)
            capture.release()
            self.capture = None
            return

        print("Input connected", flush=True)
        while not STOP.is_set():
            ok, frame = capture.read()
            if not ok:
                print("Input ended or disconnected; reconnecting...", flush=True)
                break
            self._publish_frame(frame)
        capture.release()
        self.capture = None

    def _run(self) -> None:
        is_sdp = isinstance(self.source, str) and self.source.lower().endswith(".sdp")
        while not STOP.is_set():
            if is_sdp:
                self._run_sdp_once()
            else:
                self._run_opencv_once()
            if not STOP.is_set():
                print("Input disconnected; reconnecting...", flush=True)
                STOP.wait(self.reconnect_delay)

    def get_after(self, previous_sequence: int, timeout: float = 1.0) -> tuple[int, Any] | None:
        with self.condition:
            self.condition.wait_for(
                lambda: self.sequence > previous_sequence or STOP.is_set(), timeout=timeout
            )
            if self.sequence <= previous_sequence or self.frame is None:
                return None
            return self.sequence, self.frame

    def close(self) -> None:
        if self.capture is not None:
            self.capture.release()
        if self.decoder is not None and self.decoder.poll() is None:
            self.decoder.terminate()
        with self.condition:
            self.condition.notify_all()
        self.thread.join(timeout=3)


class FFmpegPublisher:
    """Publish the newest annotated frame at a constant rate, duplicating as needed."""

    def __init__(
        self,
        url: str,
        width: int,
        height: int,
        fps: float,
        bitrate: str,
        preset: str,
        loglevel: str,
    ) -> None:
        self.url = url
        self.width = width
        self.height = height
        self.fps = fps
        self.bitrate = bitrate
        self.preset = preset
        self.loglevel = loglevel
        self.condition = threading.Condition()
        self.latest_frame: Any | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self.thread = threading.Thread(target=self._run, name="rtsp-publisher", daemon=True)

    def _command(self) -> list[str]:
        keyframe_interval = max(1, round(self.fps * 2))
        return [
            "ffmpeg", "-hide_banner", "-loglevel", self.loglevel, "-nostdin",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-video_size", f"{self.width}x{self.height}",
            "-framerate", str(self.fps), "-i", "pipe:0", "-an",
            "-c:v", "libx264", "-preset", self.preset, "-tune", "zerolatency",
            "-pix_fmt", "yuv420p", "-b:v", self.bitrate,
            "-maxrate", self.bitrate, "-bufsize", self.bitrate,
            "-g", str(keyframe_interval), "-keyint_min", str(keyframe_interval),
            "-bf", "0", "-sc_threshold", "0", "-flush_packets", "1",
            "-f", "rtsp", "-rtsp_transport", "tcp", self.url,
        ]

    def _start_process(self) -> None:
        print(f"Connecting publisher to {redact_url(self.url)}", flush=True)
        self.process = subprocess.Popen(
            self._command(), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, bufsize=0
        )

    def _close_process(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

    def _write(self, frame: Any) -> None:
        if self.process is None or self.process.poll() is not None:
            self._close_process()
            self._start_process()
        assert self.process is not None and self.process.stdin is not None
        payload = memoryview(frame).cast("B")
        offset = 0
        while offset < len(payload):
            written = self.process.stdin.write(payload[offset:])
            if written is None or written <= 0:
                raise BrokenPipeError("FFmpeg publisher closed its input")
            offset += written

    def _run(self) -> None:
        interval = 1.0 / self.fps
        deadline = time.monotonic()
        while not STOP.is_set():
            with self.condition:
                self.condition.wait_for(
                    lambda: self.latest_frame is not None or STOP.is_set(), timeout=1
                )
                frame = self.latest_frame
            if frame is None:
                continue
            delay = deadline - time.monotonic()
            if delay > 0 and STOP.wait(delay):
                break
            try:
                self._write(frame)
            except (BrokenPipeError, OSError) as error:
                print(f"Publisher disconnected ({error}); retrying...", file=sys.stderr, flush=True)
                self._close_process()
                STOP.wait(2)
                deadline = time.monotonic()
                continue
            deadline += interval
            if deadline < time.monotonic() - interval:
                deadline = time.monotonic()
        self._close_process()

    def start(self) -> None:
        self.thread.start()

    def update(self, frame: Any) -> None:
        if frame.shape[1] != self.width or frame.shape[0] != self.height:
            frame = cv2.resize(frame, (self.width, self.height), interpolation=cv2.INTER_AREA)
        if not frame.flags["C_CONTIGUOUS"]:
            frame = frame.copy()
        with self.condition:
            self.latest_frame = frame
            self.condition.notify_all()

    def close(self) -> None:
        with self.condition:
            self.condition.notify_all()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
        self.thread.join(timeout=5)
        self._close_process()


def request_stop(signum: int, _frame: Any) -> None:
    print(f"Received signal {signum}; stopping...", flush=True)
    STOP.set()


def main() -> int:
    args = build_parser().parse_args()
    try:
        validate(args)
        classes = parse_classes(args.classes)
        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)

        print("--- YOLO CNW live stream ---", flush=True)
        print(f"MODEL_PATH:         {args.model}", flush=True)
        print(f"SOURCE:             {redact_url(str(args.source))}", flush=True)
        print(
            f"STREAM_INGEST_URL:  {redact_url(args.publish_url) if args.publish_url else '(not set)'}",
            flush=True,
        )

        print(f"Loading YOLO model: {args.model}", flush=True)
        model = YOLO(args.model)
        print(f"Model classes: {model.names}", flush=True)

        source = LatestFrameSource(
            parse_source(str(args.source)),
            args.input_reconnect_delay,
            args.input_width,
            args.input_height,
            args.ffmpeg_loglevel,
        )
        source.start()
        publisher: FFmpegPublisher | None = None
        last_sequence = 0
        processed = 0
        total_detections = 0
        started = time.monotonic()
        if not args.publish_url:
            print(
                "No STREAM_INGEST_URL / PUBLISH_URL — running inference without RTSPS push",
                flush=True,
            )

        try:
            while not STOP.is_set():
                item = source.get_after(last_sequence)
                if item is None:
                    continue
                last_sequence, frame = item
                result = model.predict(
                    source=frame,
                    conf=args.confidence,
                    iou=args.iou,
                    imgsz=args.image_size,
                    classes=classes,
                    device=args.device,
                    half=bool(args.half and torch.cuda.is_available()),
                    verbose=False,
                )[0]
                annotated = result.plot()
                detections = 0 if result.boxes is None else len(result.boxes)
                processed += 1
                total_detections += detections
                inference_fps = processed / max(time.monotonic() - started, 1e-9)
                cv2.putText(
                    annotated,
                    f"Detections: {detections} | Inference: {inference_fps:.1f} FPS",
                    (12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.75,
                    (0, 255, 0), 2, cv2.LINE_AA,
                )

                if args.publish_url:
                    if publisher is None:
                        source_height, source_width = annotated.shape[:2]
                        width = args.output_width or source_width
                        height = args.output_height or source_height
                        width -= width % 2
                        height -= height % 2
                        publisher = FFmpegPublisher(
                            args.publish_url, width, height, args.publish_fps,
                            args.bitrate, args.preset, args.ffmpeg_loglevel,
                        )
                        publisher.start()
                        print(
                            f"Publishing {width}x{height} at {args.publish_fps:g} FPS, "
                            f"bitrate={args.bitrate}", flush=True,
                        )
                    publisher.update(annotated)

                if args.log_every and processed % args.log_every == 0:
                    print(
                        f"processed={processed} detections={total_detections} "
                        f"inference_fps={inference_fps:.2f}", flush=True,
                    )
        finally:
            STOP.set()
            source.close()
            if publisher is not None:
                publisher.close()

        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
