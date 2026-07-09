"""Single CSI camera mode (original behaviour)."""

from __future__ import annotations

import gc
import os
import sys
import time
from typing import Optional

import cv2
import numpy as np
import tensorrt as trt

from . import config
from .csi_camera import build_capture_pipeline

TRT_LOGGER = trt.Logger(trt.Logger.WARNING)


def _log_cuda_mem(label: str) -> None:
    import torch
    if not torch.cuda.is_available():
        print("GPU mem [%s]: CUDA not available" % label)
        return
    free, total = torch.cuda.mem_get_info()
    print(
        "GPU mem [%s]: %d MiB free / %d MiB total"
        % (label, free // (1024 * 1024), total // (1024 * 1024))
    )


def load_engine(engine_path: str):
    if not os.path.exists(engine_path):
        parent_dir = os.path.dirname(engine_path)
        print("ERROR: Engine file not found at %s" % engine_path)
        if os.path.exists(parent_dir):
            print("Directory %s contains: %s" % (parent_dir, os.listdir(parent_dir)))
        sys.exit(1)

    last_err: Optional[Exception] = None
    for attempt in range(1, config.ENGINE_LOAD_RETRIES + 1):
        try:
            print("Loading engine (attempt %d): %s" % (attempt, engine_path))
            with open(engine_path, "rb") as f, trt.Runtime(TRT_LOGGER) as runtime:
                engine = runtime.deserialize_cuda_engine(f.read())
            if engine is None:
                raise RuntimeError("deserialize_cuda_engine returned None")
            print("Engine loaded successfully.")
            return engine
        except Exception as exc:
            last_err = exc
            print("Engine load attempt %d failed: %s" % (attempt, exc))
            gc.collect()
            if attempt < config.ENGINE_LOAD_RETRIES:
                time.sleep(config.ENGINE_LOAD_RETRY_DELAY)

    raise RuntimeError(
        "Failed to deserialize TensorRT engine after %d attempts: %s"
        % (config.ENGINE_LOAD_RETRIES, last_err)
    )


def trt_dtype_to_torch(dtype, torch):
    mapping = {
        trt.DataType.FLOAT: torch.float32,
        trt.DataType.HALF: torch.float16,
        trt.DataType.INT32: torch.int32,
        trt.DataType.INT8: torch.int8,
        trt.DataType.BOOL: torch.bool,
    }
    if dtype in mapping:
        return mapping[dtype]
    raise RuntimeError("Unsupported TensorRT dtype: %s" % dtype)


def preprocess(frame, h, w):
    resized = cv2.resize(frame, (w, h), interpolation=cv2.INTER_LINEAR)
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    rgb = (rgb - mean) / std
    chw = np.transpose(rgb, (2, 0, 1))
    return np.ascontiguousarray(np.expand_dims(chw, axis=0), dtype=np.float32)


def open_stream_writer(width: int, height: int) -> Optional[cv2.VideoWriter]:
    if not config.STREAM_INGEST_URL:
        return None
    out_w = config.STREAM_WIDTH if config.STREAM_WIDTH > 0 else width
    out_h = config.STREAM_HEIGHT if config.STREAM_HEIGHT > 0 else height
    from .stream_writer import build_stream_writer_pipeline

    pipeline = build_stream_writer_pipeline(
        out_w, out_h, config.STREAM_FPS, config.STREAM_BITRATE_KBPS, config.STREAM_INGEST_URL,
    )
    print("Live stream: opening RTSP push (%dx%d @ %d fps)" % (out_w, out_h, config.STREAM_FPS))
    writer = cv2.VideoWriter(
        pipeline, cv2.CAP_GSTREAMER, 0, float(config.STREAM_FPS), (out_w, out_h), True,
    )
    if not writer.isOpened():
        print("WARNING: Live stream writer failed to open — continuing without stream.")
        return None
    print("Live stream: RTSP push active")
    return writer


def run():
    print("--- UNet++ TensorRT + optional live stream (single CSI) ---")
    print("ENGINE_PATH:         %s" % config.ENGINE_PATH)
    print("SHOW_WINDOW:         %s" % config.SHOW_WINDOW)
    print("STREAM_INGEST_URL:   %s" % ("(set)" if config.STREAM_INGEST_URL else "(not set)"))

    engine = load_engine(config.ENGINE_PATH)

    import torch

    if not torch.cuda.is_available():
        print("CRITICAL ERROR: CUDA not available. Use --runtime nvidia --gpus all.")
        sys.exit(1)
    _log_cuda_mem("after torch import")

    context = engine.create_execution_context()
    input_name = output_name = None
    for i in range(engine.num_io_tensors):
        name = engine.get_tensor_name(i)
        if engine.get_tensor_mode(name) == trt.TensorIOMode.INPUT:
            input_name = name
        else:
            output_name = name
    if not input_name or not output_name:
        raise RuntimeError("Could not find input/output tensors in engine.")

    input_shape = tuple(engine.get_tensor_shape(input_name))
    _, _c, h, w = input_shape

    sensor_id = config.sensor_ids()[0]
    cap = cv2.VideoCapture(build_capture_pipeline(sensor_id), cv2.CAP_GSTREAMER)
    if not cap.isOpened():
        print("CRITICAL ERROR: Could not open CSI camera sensor-id=%d." % sensor_id)
        sys.exit(1)
    print("Camera opened successfully (sensor-id=%d)." % sensor_id)

    stream_writer: Optional[cv2.VideoWriter] = None
    frame_count = 0
    cuda_stream = torch.cuda.Stream()
    last_stream_ts = 0.0
    stream_interval = 1.0 / config.STREAM_FPS
    stream_out_w = config.STREAM_WIDTH if config.STREAM_WIDTH > 0 else None
    stream_out_h = config.STREAM_HEIGHT if config.STREAM_HEIGHT > 0 else None

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                print("Failed to read frame from camera.")
                break

            orig_h, orig_w = frame.shape[:2]

            if stream_writer is None and config.STREAM_INGEST_URL:
                stream_writer = open_stream_writer(orig_w, orig_h)

            inp = preprocess(frame, h, w)
            input_tensor = torch.from_numpy(inp).to("cuda")
            context.set_input_shape(input_name, tuple(input_tensor.shape))
            context.set_tensor_address(input_name, input_tensor.data_ptr())

            out_shape = tuple(context.get_tensor_shape(output_name))
            out_dtype = trt_dtype_to_torch(engine.get_tensor_dtype(output_name), torch)
            output_tensor = torch.empty(size=out_shape, dtype=out_dtype, device="cuda")
            context.set_tensor_address(output_name, output_tensor.data_ptr())

            with torch.cuda.stream(cuda_stream):
                if not context.execute_async_v3(cuda_stream.cuda_stream):
                    raise RuntimeError("TensorRT inference failed.")
            cuda_stream.synchronize()

            prob = output_tensor.detach().cpu().numpy()[0, 0]
            mask = (prob >= config.THRESHOLD).astype(np.uint8)
            mask_resized = cv2.resize(mask, (orig_w, orig_h), interpolation=cv2.INTER_NEAREST)
            color_mask = np.zeros((orig_h, orig_w, 3), dtype=np.uint8)
            color_mask[mask_resized == 1] = (0, 255, 0)
            overlay = cv2.addWeighted(frame, 0.7, color_mask, 0.3, 0)

            if stream_writer is not None and stream_writer.isOpened():
                now = time.time()
                if now - last_stream_ts >= stream_interval:
                    out = overlay
                    if stream_out_w and stream_out_h:
                        out = cv2.resize(overlay, (stream_out_w, stream_out_h))
                    stream_writer.write(out)
                    last_stream_ts = now

            frame_count += 1
            if frame_count % 30 == 0:
                print("Processed %d frames…" % frame_count)

            if config.SHOW_WINDOW:
                cv2.imshow("UNet++ TensorRT Overlay", overlay)
                if cv2.waitKey(1) & 0xFF in (27, ord("q")):
                    break

    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    finally:
        cap.release()
        if stream_writer is not None:
            stream_writer.release()
        cv2.destroyAllWindows()
        print("Cleanup complete.")
