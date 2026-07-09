FROM nvcr.io/nvidia/l4t-base:r32.5.0

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

# pycuda runtime/build deps (same pins as yolo_trt_docker on JP4).
RUN pip3 install \
    "six" \
    "Mako==1.1.6" \
    "appdirs==1.4.4" \
    "pytools==2020.4.4"

# pycuda for TensorRT inference (no PyTorch — JP4 friendly).
RUN pip3 install --no-build-isolation --no-cache-dir "pycuda==2020.1"

WORKDIR /workspace
RUN mkdir -p /workspace/models /workspace/app /data/camera_images && chmod 777 /workspace/models

COPY app/ /workspace/app/
COPY run_multicam.py /workspace/run_multicam.py

ENV ENGINE_PATH=/workspace/models/unetpp_teacher_512x768_fp16.engine
ENV THRESHOLD=0.3
ENV CAMERA_INDICES=0,1,2
ENV STREAM_FPS=10
ENV STREAM_BITRATE_KBPS=1500
ENV STREAM_WIDTH=960
ENV STREAM_HEIGHT=540
ENV DISPLAY_WIDTH=960
ENV DETECT_EVERY_N_FRAMES=3
ENV ENABLE_IMAGE_SAVE=false
ENV QT_QPA_PLATFORM=offscreen

CMD ["python3", "-u", "/workspace/run_multicam.py"]
