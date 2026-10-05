"""YOLO detection through TensorRT + pycuda.

JetPack 4 is Python 3.6, which rules out ultralytics (3.8+) and therefore any way
to unpickle a YOLO .pt, so letterboxing, decoding and NMS all happen here against
an engine built on the target device.
"""

import gc
import threading
import time

import cv2
import numpy as np

from . import config

ENGINE_SUFFIXES = (".engine", ".plan", ".trt")

# BGR, one entry per class in CLASS_NAMES order. Neighbouring colour-classes
# (red vs red-green, purple vs purple-green) get hues that stay distinct after
# the 1920 -> 960 stream downscale.
CLASS_COLORS = (
    (36, 220, 36),     # green_broadleaf
    (50, 50, 255),     # red_swordleaf
    (0, 140, 255),     # red_green_swordleaf
    (0, 255, 210),     # bright_green_grass
    (240, 240, 240),   # white_flower_spike
    (220, 60, 200),    # purple_flower_spike
    (160, 255, 40),    # purple_green_swordleaf
)


def letterbox(bgr, target_h, target_w, pad_value=114):
    """Resize into target_h x target_w keeping aspect ratio, padding the rest."""
    height, width = bgr.shape[:2]
    ratio = min(float(target_h) / float(height), float(target_w) / float(width))
    new_w = max(1, int(round(width * ratio)))
    new_h = max(1, int(round(height * ratio)))
    resized = cv2.resize(bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((target_h, target_w, 3), pad_value, dtype=np.uint8)
    pad_x = (target_w - new_w) // 2
    pad_y = (target_h - new_h) // 2
    canvas[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = resized
    return canvas, ratio, pad_x, pad_y


def to_blob(canvas):
    rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    chw = np.transpose(rgb, (2, 0, 1))
    return np.ascontiguousarray(np.expand_dims(chw, axis=0), dtype=np.float32)


def nms(x1, y1, x2, y2, confidences, iou_threshold, limit):
    """Greedy NMS in numpy, returning kept indices in descending score order.

    Hand-rolled rather than cv2.dnn.NMSBoxes because the JetPack 4 python3-opencv
    package is built without the dnn module, so that call raises AttributeError on
    the target device.
    """
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = np.argsort(confidences)[::-1]
    # Bound the O(n^2) loop: at 1920 the head emits ~76k anchors, and a low
    # CONFIDENCE on a busy frame can let thousands through.
    order = order[: max(limit * 10, 1000)]

    keep = []
    while order.size > 0 and len(keep) < limit:
        i = order[0]
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        inter_w = np.maximum(0.0, np.minimum(x2[i], x2[rest]) - np.maximum(x1[i], x1[rest]))
        inter_h = np.maximum(0.0, np.minimum(y2[i], y2[rest]) - np.maximum(y1[i], y1[rest]))
        inter = inter_w * inter_h
        union = areas[i] + areas[rest] - inter
        iou = np.where(union > 0.0, inter / np.maximum(union, 1e-9), 0.0)
        order = rest[iou <= iou_threshold]

    return np.asarray(keep, dtype=np.int64)


def decode_detections(raw, ratio, pad_x, pad_y, frame_w, frame_h):
    """Decode an ultralytics detect head into frame-space boxes.

    Expects (1, 4 + num_classes, num_anchors) with cx/cy/w/h in letterboxed
    pixels and per-class scores — the v8/v11 layout, which has no separate
    objectness column.
    """
    pred = np.asarray(raw, dtype=np.float32)
    if pred.ndim == 3:
        pred = pred[0]
    if pred.ndim != 2:
        raise RuntimeError("Unexpected YOLO output shape %s" % (np.shape(raw),))
    # Anchors always outnumber channels, so the longer axis is the anchor axis.
    if pred.shape[0] < pred.shape[1]:
        pred = pred.T
    if pred.shape[1] < 5:
        raise RuntimeError(
            "YOLO output has %d columns; expected 4 + num_classes" % pred.shape[1]
        )

    scores = pred[:, 4:]
    class_ids = np.argmax(scores, axis=1)
    confidences = scores[np.arange(scores.shape[0]), class_ids]

    keep = confidences >= config.CONFIDENCE
    if not np.any(keep):
        return []

    boxes = pred[keep, :4]
    class_ids = class_ids[keep]
    confidences = confidences[keep]

    half_w = boxes[:, 2] / 2.0
    half_h = boxes[:, 3] / 2.0
    x1 = np.clip((boxes[:, 0] - half_w - pad_x) / ratio, 0, frame_w - 1)
    y1 = np.clip((boxes[:, 1] - half_h - pad_y) / ratio, 0, frame_h - 1)
    x2 = np.clip((boxes[:, 0] + half_w - pad_x) / ratio, 0, frame_w - 1)
    y2 = np.clip((boxes[:, 1] + half_h - pad_y) / ratio, 0, frame_h - 1)

    # NMS is class-agnostic, so shift each class into its own coordinate band to
    # stop overlapping boxes of different classes suppressing each other.
    band = float(frame_w + 1)
    offsets = class_ids.astype(np.float32) * band
    order = nms(
        x1 + offsets,
        y1,
        x2 + offsets,
        y2,
        confidences,
        config.IOU,
        config.MAX_DETECTIONS,
    )
    if order.size == 0:
        return []

    detections = []
    for i in order:
        detections.append(
            (
                int(x1[i]), int(y1[i]), int(x2[i]), int(y2[i]),
                float(confidences[i]), int(class_ids[i]),
            )
        )
    return detections


def color_for_class(class_id):
    return CLASS_COLORS[int(class_id) % len(CLASS_COLORS)]


def label_text_color(bgr_color):
    """Black on bright fills, white on dark — keeps the class name readable."""
    b, g, r = bgr_color
    luminance = 0.114 * b + 0.587 * g + 0.299 * r
    return (0, 0, 0) if luminance >= 140 else (255, 255, 255)


def draw_metrics(frame_w):
    """Scale glyphs for the streamed frame, not the infer frame.

    Boxes are drawn on DISPLAY_WIDTH (1920) and then downscaled to STREAM_WIDTH
    (960). A 0.5 OpenCV scale that looks fine at 1920 becomes unreadably small
    on the HLS tile, so we size as if the viewer is looking at ~960 px.
    """
    font_scale = max(1.1, frame_w / 960.0 * 0.85)
    text_thickness = max(2, int(round(frame_w / 640.0)))
    box_thickness = max(3, int(round(frame_w / 480.0)))
    return font_scale, text_thickness, box_thickness


def draw_detections(bgr, detections):
    out = bgr.copy()
    font_scale, text_thickness, box_thickness = draw_metrics(out.shape[1])
    for x1, y1, x2, y2, confidence, class_id in detections:
        color = color_for_class(class_id)
        cv2.rectangle(out, (x1, y1), (x2, y2), color, box_thickness)
        label = "%s %.2f" % (config.class_name(class_id), confidence)
        (text_w, text_h), baseline = cv2.getTextSize(
            label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, text_thickness
        )
        pad = max(4, int(round(font_scale * 4)))
        top = max(y1, text_h + baseline + pad)
        cv2.rectangle(
            out,
            (x1, top - text_h - baseline - pad),
            (x1 + text_w + pad, top),
            color,
            -1,
        )
        cv2.putText(
            out, label, (x1 + pad // 2, top - baseline - pad // 2),
            cv2.FONT_HERSHEY_SIMPLEX, font_scale, label_text_color(color),
            text_thickness,
        )
    return out


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


class TensorRTBackend(object):
    name = "tensorrt"

    def __init__(self, engine_path):
        self.engine_path = engine_path
        self.trt = None
        self.cuda = None
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
        self._cuda_ctx = None

    def load(self):
        import pycuda.autoinit  # noqa: F401  (creates the primary CUDA context)
        import pycuda.driver as cuda
        import tensorrt as trt

        self.cuda = cuda
        self.trt = trt

        with open(self.engine_path, "rb") as handle:
            runtime = trt.Runtime(trt.Logger(trt.Logger.WARNING))
            self.engine = runtime.deserialize_cuda_engine(handle.read())
        if self.engine is None:
            raise RuntimeError("deserialize_cuda_engine returned None")

        self._ensure_cuda_context()
        with self._with_cuda_context():
            self._allocate()

    def describe(self):
        api = "TRT 8+ tensors" if self.use_tensor_api else "TRT 7 bindings"
        return "%s, input %s, output %s" % (api, self.input_shape, self.out_shape)

    def _ensure_cuda_context(self):
        cuda = self.cuda
        if self._cuda_ctx is None:
            self._cuda_ctx = cuda.Context.get_current()
        if self._cuda_ctx is None:
            cuda.init()
            self._cuda_ctx = cuda.Device(0).make_context()

    def _with_cuda_context(self):
        if self._cuda_ctx is None:
            return _NullContext()
        return _CudaContext(self._cuda_ctx)

    def _allocate(self):
        self.context = self.engine.create_execution_context()
        self.stream = self.cuda.Stream()
        if hasattr(self.engine, "num_io_tensors"):
            self._allocate_tensor_api()
        elif hasattr(self.engine, "num_bindings"):
            self._allocate_bindings_api()
        else:
            raise RuntimeError("Unsupported TensorRT engine API.")

    def _allocate_tensor_api(self):
        cuda, trt = self.cuda, self.trt
        self.use_tensor_api = True

        outputs = []
        for i in range(self.engine.num_io_tensors):
            name = self.engine.get_tensor_name(i)
            if self.engine.get_tensor_mode(name) == trt.TensorIOMode.INPUT:
                self.input_name = name
            else:
                outputs.append(name)
        if not self.input_name or not outputs:
            raise RuntimeError("Could not resolve input/output tensor names.")
        self.output_name = outputs[0]
        if len(outputs) > 1:
            print(
                "WARNING: engine has %d outputs; decoding %s"
                % (len(outputs), self.output_name)
            )

        self.input_shape = tuple(self.engine.get_tensor_shape(self.input_name))
        if any(dim < 0 for dim in self.input_shape):
            raise RuntimeError("Dynamic input shape not supported: %s" % (self.input_shape,))
        self.context.set_input_shape(self.input_name, self.input_shape)
        self.out_shape = tuple(self.context.get_tensor_shape(self.output_name))
        out_dtype = trt.nptype(self.engine.get_tensor_dtype(self.output_name))

        self.host_in = cuda.pagelocked_empty(int(np.prod(self.input_shape)), np.float32)
        self.device_in = cuda.mem_alloc(self.host_in.nbytes)
        self.host_out = cuda.pagelocked_empty(int(np.prod(self.out_shape)), out_dtype)
        self.device_out = cuda.mem_alloc(self.host_out.nbytes)

        self.context.set_tensor_address(self.input_name, int(self.device_in))
        self.context.set_tensor_address(self.output_name, int(self.device_out))

    def _allocate_bindings_api(self):
        cuda, trt = self.cuda, self.trt
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
            print(
                "WARNING: engine has %d outputs; decoding binding %d"
                % (len(output_idxs), self.output_binding_idx)
            )

        input_shape = tuple(self.engine.get_binding_shape(self.input_binding_idx))
        if any(dim < 0 for dim in input_shape):
            raise RuntimeError("Dynamic input shape not supported: %s" % (input_shape,))
        self.input_shape = input_shape

        for i in range(self.engine.num_bindings):
            shape = tuple(self.engine.get_binding_shape(i))
            if any(dim < 0 for dim in shape):
                raise RuntimeError("Unresolved binding shape for %d: %s" % (i, shape))
            dtype = trt.nptype(self.engine.get_binding_dtype(i))
            host_mem = cuda.pagelocked_empty(int(np.prod(shape)), dtype)
            device_mem = cuda.mem_alloc(host_mem.nbytes)
            self.bindings[i] = int(device_mem)

            if i == self.input_binding_idx:
                self.host_in = host_mem
                self.device_in = device_mem
            elif i == self.output_binding_idx:
                self.out_shape = shape
                self.host_out = host_mem
                self.device_out = device_mem

    def _execute(self):
        if self.use_tensor_api:
            if not self.context.execute_async_v3(self.stream.handle):
                raise RuntimeError("TensorRT execute_async_v3 failed.")
        elif hasattr(self.context, "execute_async_v2"):
            self.context.execute_async_v2(
                bindings=self.bindings, stream_handle=self.stream.handle
            )
        else:
            self.context.execute_async(
                batch_size=1, bindings=self.bindings, stream_handle=self.stream.handle
            )

    def infer_raw(self, bgr):
        """Run the engine and return its undecoded output plus letterbox geometry.

        Split out from infer_annotated so tools/probe_image.py can look at the raw
        scores: a silent zero-detection failure looks identical whether the engine
        is wrong or the scene simply has nothing in it.
        """
        cuda = self.cuda
        _, _, in_h, in_w = self.input_shape
        canvas, ratio, pad_x, pad_y = letterbox(bgr, in_h, in_w)
        blob = to_blob(canvas)

        with self._with_cuda_context():
            start = time.time()
            np.copyto(self.host_in, blob.ravel())
            cuda.memcpy_htod_async(self.device_in, self.host_in, self.stream)
            self._execute()
            cuda.memcpy_dtoh_async(self.host_out, self.device_out, self.stream)
            self.stream.synchronize()
            infer_ms = (time.time() - start) * 1000.0
            raw = np.array(self.host_out, copy=True).reshape(self.out_shape)

        return raw, ratio, pad_x, pad_y, infer_ms

    def infer_annotated(self, bgr):
        frame_h, frame_w = bgr.shape[:2]
        raw, ratio, pad_x, pad_y, infer_ms = self.infer_raw(bgr)
        detections = decode_detections(raw, ratio, pad_x, pad_y, frame_w, frame_h)
        return draw_detections(bgr, detections), infer_ms, len(detections)


def build_backend(model_path):
    if not model_path.lower().endswith(ENGINE_SUFFIXES):
        raise RuntimeError(
            "MODEL_PATH must be a TensorRT engine (%s), got %s. JetPack 4 cannot "
            "run a .pt checkpoint; export it to an engine on this device first "
            "(see README.md)." % ("/".join(ENGINE_SUFFIXES), model_path)
        )
    return TensorRTBackend(model_path)


class SharedYoloRunner(object):
    """Thread-safe YOLO runner - one engine, serial inference across cameras."""

    def __init__(self, model_path):
        self.model_path = model_path
        self.lock = threading.Lock()
        self.status = "MODEL NOT LOADED"
        self.backend = None

    @property
    def loaded(self):
        return self.backend is not None

    def load(self):
        last_err = None
        for attempt in range(1, config.MODEL_LOAD_RETRIES + 1):
            backend = build_backend(self.model_path)
            try:
                print("Loading engine (attempt %d): %s" % (attempt, self.model_path))
                backend.load()
                self.backend = backend
                self.status = "MODEL READY (%s)" % backend.name
                print("Model loaded: %s" % backend.describe())
                return
            except Exception as exc:
                last_err = exc
                print("Model load failed: %s" % exc)
                gc.collect()
                if attempt < config.MODEL_LOAD_RETRIES:
                    time.sleep(config.MODEL_LOAD_RETRY_DELAY)
        raise RuntimeError("Failed to load %s: %s" % (self.model_path, last_err))

    def infer_annotated(self, bgr):
        if self.backend is None:
            return bgr, 0.0, 0
        with self.lock:
            return self.backend.infer_annotated(bgr)
