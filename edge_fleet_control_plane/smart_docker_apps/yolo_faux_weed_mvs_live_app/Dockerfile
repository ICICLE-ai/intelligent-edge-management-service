# YOLO faux-weed detection over Hikrobot GigE (MVS) cameras.
# Target: Jetson AGX Xavier on JetPack 4 (L4T r32.x) - the same base as
# unet_mvs_multicam_live_app.
#
# JetPack 4 is Python 3.6 and ultralytics needs 3.8+. A CUDA PyTorch wheel does
# exist for 3.6, but a YOLO .pt is a pickled ultralytics object and cannot be
# loaded without that package. Inference therefore runs a TensorRT engine through
# pycuda, and MODEL_PATH must point at an .engine built on this device.
# See README.md for the .pt -> ONNX -> .engine export.
#
# The MVS SDK is not baked in - mount the host /opt/MVS at runtime.
ARG BASE_IMAGE=nvcr.io/nvidia/l4t-base:r32.5.0
FROM ${BASE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive

ENV MVCAM_COMMON_RUNENV=/opt/MVS/lib
ENV CUDA_HOME=/usr/local/cuda
ENV PATH=/usr/local/cuda/bin:${PATH}
ENV LD_LIBRARY_PATH=/usr/local/cuda/lib64:/opt/MVS/lib/aarch64:${LD_LIBRARY_PATH}

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-dev \
    python3-numpy \
    python3-opencv \
    build-essential \
    libboost-python-dev \
    libboost-thread-dev \
    libboost-system-dev \
    libboost-filesystem-dev \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-rtsp \
    libx264-dev \
    python3-gi \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --upgrade "pip<22" "setuptools<60" wheel

# pycuda runtime/build deps (same pins as unet_mvs_multicam_live_app on JP4).
RUN pip3 install \
    "six" \
    "Mako==1.1.6" \
    "appdirs==1.4.4" \
    "pytools==2020.4.4"

RUN pip3 install --no-build-isolation --no-cache-dir "pycuda==2020.1"

WORKDIR /workspace
RUN mkdir -p /workspace/models /workspace/app /data/camera_images \
    && chmod 777 /workspace/models

COPY app/ /workspace/app/
COPY tools/ /workspace/tools/
COPY run_multicam.py /workspace/run_multicam.py

ENV MODEL_PATH=/workspace/models/faux_weed_1x3x1920x1920_agx_xavier_jp4.engine
ENV CONFIDENCE=0.35
# Auto exposure/gain/white balance must keep tracking: "once" locks after 1.5s,
# which latched a near-black frame on the Xavier and suppressed every detection.
# Colour defines several classes here, so a drifting white balance costs accuracy.
ENV AUTO_ADJUST_MODE=continuous
ENV IOU=0.45
ENV CAMERA_INDICES=0
ENV STREAM_FPS=5
ENV STREAM_BITRATE_KBPS=1500
ENV STREAM_WIDTH=960
ENV STREAM_HEIGHT=540
# Matches the 1920 training resolution. Keep at or above the engine's input
# width: the frame is resized to this before inference, so a smaller value would
# upscale into the letterbox and lose detail the camera already captured.
ENV DISPLAY_WIDTH=1920
ENV DETECT_EVERY_N_FRAMES=3
ENV ENABLE_IMAGE_SAVE=false
ENV QT_QPA_PLATFORM=offscreen

CMD ["python3", "-u", "/workspace/run_multicam.py"]
