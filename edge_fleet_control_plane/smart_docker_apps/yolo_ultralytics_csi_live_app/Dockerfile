FROM nvcr.io/nvidia/pytorch:25.03-py3-igpu

# Keep the Jetson OpenCV (GStreamer) and the NGC torch. A normal
# `pip install ultralytics` would replace both.
RUN pip uninstall -y opencv opencv-python opencv-python-headless || true

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
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
RUN mkdir -p /workspace/models /workspace/app /tmp/Ultralytics \
    && chmod 777 /workspace/models /tmp/Ultralytics

COPY requirements.txt /workspace/requirements.txt
# ultralytics --no-deps so pip cannot replace the NGC torch/torchvision.
# The other packages are installed normally so their own dependencies resolve.
RUN python3 -m pip install --no-cache-dir --upgrade pip \
    && python3 -m pip install --no-cache-dir --no-deps "ultralytics==8.3.253" \
    && python3 -m pip install --no-cache-dir \
        matplotlib pandas seaborn scipy tqdm pyyaml requests psutil \
        py-cpuinfo polars ultralytics-thop pillow \
    && python3 -c "import torch, torchvision, cv2; print('torch', torch.__version__, 'torchvision', torchvision.__version__, 'cv2', cv2.__version__)"

COPY app/ /workspace/app/
COPY run_live.py /workspace/run_live.py

ENV ULTRALYTICS_SETTINGS_DIR=/tmp/Ultralytics
ENV MODEL_PATH=/workspace/models/model.pt
ENV CONFIDENCE=0.25
ENV IOU=0.45
ENV IMAGE_SIZE=0
ENV DEVICE=0
ENV HALF=0
ENV SHOW_WINDOW=0
ENV STREAM_INGEST_URL=
ENV STREAM_FPS=10
ENV STREAM_BITRATE_KBPS=1500
ENV STREAM_WIDTH=960
ENV STREAM_HEIGHT=540
ENV CAM_WIDTH=1280
ENV CAM_HEIGHT=720
ENV CAM_FPS=30
ENV DETECT_EVERY_N_FRAMES=2
ENV DISPLAY_WIDTH=960
ENV QT_QPA_PLATFORM=offscreen

CMD ["python3", "-u", "/workspace/run_live.py"]
