ARG BASE_IMAGE=pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime
FROM ${BASE_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    ULTRALYTICS_SETTINGS_DIR=/tmp/Ultralytics \
    MODEL_PATH=/workspace/models/Yolo-cnw.pt \
    SOURCE=/workspace/input-h264.sdp \
    STREAM_INGEST_URL= \
    DEVICE=0 \
    HALF=1 \
    CONFIDENCE=0.25 \
    IOU=0.45 \
    IMAGE_SIZE=640 \
    STREAM_FPS=15 \
    STREAM_BITRATE_KBPS=1800 \
    STREAM_WIDTH=480 \
    STREAM_HEIGHT=320 \
    INPUT_WIDTH=480 \
    INPUT_HEIGHT=320

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        libglib2.0-0 \
        libgl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
RUN mkdir -p /workspace/models /workspace/app /tmp/Ultralytics \
    && chmod 777 /workspace/models \
    && chmod -R a+rX /tmp/Ultralytics

COPY requirements.txt /workspace/requirements.txt
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install -r /workspace/requirements.txt

COPY app/ /workspace/app/
COPY run_live.py /workspace/run_live.py
COPY input-h264.sdp /workspace/input-h264.sdp
RUN chmod -R a+rX /workspace

ENTRYPOINT ["python", "-u", "/workspace/run_live.py"]
