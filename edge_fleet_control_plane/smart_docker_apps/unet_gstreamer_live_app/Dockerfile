FROM nvcr.io/nvidia/pytorch:25.03-py3-igpu

# Remove any pre-installed OpenCV to avoid conflicts
RUN pip uninstall -y opencv || true

# System deps + GStreamer (capture, encode, RTSP push) + X11 preview
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake git pkg-config \
    libjpeg-dev libtiff-dev libpng-dev \
    libavcodec-dev libavformat-dev libswscale-dev \
    libv4l-dev libxvidcore-dev libx264-dev \
    libgtk-3-dev libatlas-base-dev gfortran \
    python3-dev python3-numpy \
    libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-rtsp \
    python3-opencv \
    curl ca-certificates \
    libx11-dev libxext-dev libxrender-dev \
    x11-apps \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --no-cache-dir requests pillow numpy

# pycuda for TensorRT inference (no PyTorch CUDA context — Orin memory friendly).
RUN python3 -m pip install --no-cache-dir \
    six Mako appdirs pytools \
    && python3 -m pip install --no-build-isolation --no-cache-dir pycuda

WORKDIR /workspace
RUN mkdir -p /workspace/models /workspace/app && chmod 777 /workspace/models

COPY app/ /workspace/app/
COPY run_live.py /workspace/run_live.py

ENV DISPLAY=:0
ENV SHOW_WINDOW=1
ENV ENGINE_PATH=/workspace/models/unetpp_teacher_512x768_fp16.engine
ENV THRESHOLD=0.3
ENV STREAM_INGEST_URL=
ENV STREAM_FPS=10
ENV STREAM_BITRATE_KBPS=1500
ENV STREAM_WIDTH=960
ENV STREAM_HEIGHT=540
ENV CAM_WIDTH=1280
ENV CAM_HEIGHT=720
ENV CAM_FPS=30
ENV DETECT_EVERY_N_FRAMES=4
ENV DISPLAY_WIDTH=960

CMD ["python3", "-u", "/workspace/run_live.py"]
