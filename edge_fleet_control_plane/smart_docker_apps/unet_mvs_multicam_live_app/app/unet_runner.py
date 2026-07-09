"""UNet++ TensorRT segmentation (pycuda, no PyTorch - JP4 / AGX Xavier friendly).

Supports both TensorRT 7 (bindings + execute_async_v2) and TRT 8+ (IO tensors API).
"""

import gc
import threading
import time

import cv2
import numpy as np
import pycuda.autoinit  # noqa: F401
import pycuda.driver as cuda
import tensorrt as trt

from . import config

TRT_LOGGER = trt.Logger(trt.Logger.WARNING)


def preprocess(bgr, h, w):
    resized = cv2.resize(bgr, (w, h), interpolation=cv2.INTER_LINEAR)
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    rgb = (rgb - mean) / std
    chw = np.transpose(rgb, (2, 0, 1))
    return np.ascontiguousarray(np.expand_dims(chw, axis=0), dtype=np.float32)


def overlay_mask(bgr, prob, threshold):
    orig_h, orig_w = bgr.shape[:2]
    mask = (prob >= threshold).astype(np.uint8)
    mask_resized = cv2.resize(mask, (orig_w, orig_h), interpolation=cv2.INTER_NEAREST)
    color_mask = np.zeros((orig_h, orig_w, 3), dtype=np.uint8)
    color_mask[mask_resized == 1] = (0, 255, 0)
    return cv2.addWeighted(bgr, 0.7, color_mask, 0.3, 0)


class _NullContext(object):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _CudaContext(object):
    def __init__(self, ctx):
        self.ctx = ctx

    def __enter__(self):
        self.ctx.push()
        return self

    def __exit__(self, *args):
        self.ctx.pop()


class SharedUnetRunner:
    """Thread-safe UNet++ runner - one engine, serial inference."""

    def __init__(self, engine_path):
        self.engine_path = engine_path
        self.lock = threading.Lock()
        self.status = "TRT NOT LOADED"
        self.engine = None
        self.context = None
        self.stream = None
        self.use_tensor_api = False
        self.input_name = None
        self.output_name = None
        self.input_binding_idx = None
        self.output_binding_idx = None
        self.bindings = None
        self.input_shape = None
        self.out_shape = None
        self.host_in = None
        self.device_in = None
        self.host_out = None
        self.device_out = None
        self.out_dtype = np.float32
        self._cuda_ctx = None

    def load(self):
        last_err = None
        for attempt in range(1, config.ENGINE_LOAD_RETRIES + 1):
            try:
                print("Loading UNet engine (attempt %d): %s" % (attempt, self.engine_path))
                with open(self.engine_path, "rb") as f:
                    runtime = trt.Runtime(TRT_LOGGER)
                    self.engine = runtime.deserialize_cuda_engine(f.read())
                if self.engine is None:
                    raise RuntimeError("deserialize_cuda_engine returned None")
                self._ensure_cuda_context()
                with self._with_cuda_context():
                    self._allocate()
                api = "TRT 8+ tensors" if self.use_tensor_api else "TRT 7 bindings"
                self.status = "TRT READY (%s)" % api
                print("UNet engine loaded (%s)." % api)
                print("Input shape: %s  Output shape: %s" % (self.input_shape, self.out_shape))
                return
            except Exception as exc:
                last_err = exc
                print("Engine load failed: %s" % exc)
                gc.collect()
                if attempt < config.ENGINE_LOAD_RETRIES:
                    time.sleep(config.ENGINE_LOAD_RETRY_DELAY)
        raise RuntimeError("Failed to load engine: %s" % last_err)

    def _binding_shape(self, binding_idx):
        if hasattr(self.context, "get_binding_shape"):
            return tuple(self.context.get_binding_shape(binding_idx))
        return tuple(self.engine.get_binding_shape(binding_idx))

    def _allocate(self):
        self.context = self.engine.create_execution_context()
        self.stream = cuda.Stream()

        if hasattr(self.engine, "num_io_tensors"):
            self._allocate_tensor_api()
        elif hasattr(self.engine, "num_bindings"):
            self._allocate_bindings_api()
        else:
            raise RuntimeError("Unsupported TensorRT engine API.")

    def _ensure_cuda_context(self):
        """Activate a CUDA context before TRT/pycuda GPU allocation."""
        if self._cuda_ctx is None:
            self._cuda_ctx = cuda.Context.get_current()
        if self._cuda_ctx is None:
            cuda.init()
            self._cuda_ctx = cuda.Device(0).make_context()

    def _with_cuda_context(self):
        if self._cuda_ctx is None:
            return _NullContext()
        return _CudaContext(self._cuda_ctx)

    def _allocate_tensor_api(self):
        self.use_tensor_api = True
        for i in range(self.engine.num_io_tensors):
            name = self.engine.get_tensor_name(i)
            if self.engine.get_tensor_mode(name) == trt.TensorIOMode.INPUT:
                self.input_name = name
            else:
                self.output_name = name

        if not self.input_name or not self.output_name:
            raise RuntimeError("Could not resolve input/output tensor names.")

        self.input_shape = tuple(self.engine.get_tensor_shape(self.input_name))
        in_size = int(np.prod(self.input_shape))
        self.out_dtype = trt.nptype(self.engine.get_tensor_dtype(self.output_name))

        self.context.set_input_shape(self.input_name, self.input_shape)
        self.out_shape = tuple(self.context.get_tensor_shape(self.output_name))
        out_size = int(np.prod(self.out_shape))

        self.host_in = cuda.pagelocked_empty(in_size, np.float32)
        self.device_in = cuda.mem_alloc(self.host_in.nbytes)
        self.host_out = cuda.pagelocked_empty(out_size, self.out_dtype)
        self.device_out = cuda.mem_alloc(self.host_out.nbytes)

        self.context.set_tensor_address(self.input_name, int(self.device_in))
        self.context.set_tensor_address(self.output_name, int(self.device_out))

    def _allocate_bindings_api(self):
        self.use_tensor_api = False
        self.bindings = [None] * self.engine.num_bindings
        output_idxs = []

        for i in range(self.engine.num_bindings):
            if self.engine.binding_is_input(i):
                self.input_binding_idx = i
            else:
                output_idxs.append(i)

        if self.input_binding_idx is None or not output_idxs:
            raise RuntimeError("Could not resolve input/output bindings.")

        self.output_binding_idx = output_idxs[0]
        if len(output_idxs) > 1:
            print("WARNING: engine has %d outputs; using binding %d" % (len(output_idxs), self.output_binding_idx))

        input_shape = tuple(self.engine.get_binding_shape(self.input_binding_idx))
        if any(dim < 0 for dim in input_shape):
            raise RuntimeError("Dynamic input shape not supported: %s" % (input_shape,))
        self.input_shape = input_shape

        for i in range(self.engine.num_bindings):
            shape = self._binding_shape(i)
            if any(dim < 0 for dim in shape):
                raise RuntimeError("Unresolved binding shape for binding %d: %s" % (i, shape))
            if i == self.output_binding_idx:
                self.out_shape = shape
                self.out_dtype = trt.nptype(self.engine.get_binding_dtype(i))

            size = int(np.prod(shape))
            dtype = trt.nptype(self.engine.get_binding_dtype(i))
            host_mem = cuda.pagelocked_empty(size, dtype)
            device_mem = cuda.mem_alloc(host_mem.nbytes)
            self.bindings[i] = int(device_mem)

            if i == self.input_binding_idx:
                self.host_in = host_mem
                self.device_in = device_mem
            elif i == self.output_binding_idx:
                self.host_out = host_mem
                self.device_out = device_mem

    def _run_inference(self):
        if self.use_tensor_api:
            if not self.context.execute_async_v3(self.stream.handle):
                raise RuntimeError("TensorRT execute_async_v3 failed.")
        elif hasattr(self.context, "execute_async_v2"):
            self.context.execute_async_v2(bindings=self.bindings, stream_handle=self.stream.handle)
        else:
            self.context.execute_async(
                batch_size=1, bindings=self.bindings, stream_handle=self.stream.handle
            )

    def infer_overlay(self, bgr):
        """Run segmentation and return annotated BGR frame + inference ms."""
        if self.engine is None or self.context is None or self.stream is None:
            return bgr, 0.0

        _, _, h, w = self.input_shape
        inp = preprocess(bgr, h, w)

        with self.lock:
            with self._with_cuda_context():
                t0 = time.time()
                np.copyto(self.host_in, inp.ravel())
                cuda.memcpy_htod_async(self.device_in, self.host_in, self.stream)
                self._run_inference()
                cuda.memcpy_dtoh_async(self.host_out, self.device_out, self.stream)
                self.stream.synchronize()
                infer_ms = (time.time() - t0) * 1000.0

                prob = np.array(self.host_out, copy=False).reshape(self.out_shape)
                if prob.ndim == 4:
                    prob = prob[0, 0]
                elif prob.ndim == 3:
                    prob = prob[0]
                return overlay_mask(bgr, prob, config.THRESHOLD), infer_ms
