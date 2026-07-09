# UNet++ TensorRT — inference + live annotated stream

Runs UNet++ segmentation on the Jetson CSI camera and optionally pushes the
**annotated overlay** (same view as the local popup) to MediaMTX over RTSPS.

## Build & push

```bash
cd smart_docker_apps/unet_gstreamer_live_app
docker buildx build --platform linux/arm64 \
  -t docker.io/<youruser>/unet-trt-infer-live:latest --push .
```

Use `linux/arm64` on the Jetson; use `linux/amd64` only for CI — not for device runs.

## Runtime environment

| Variable | Default | Purpose |
|---|---|---|
| `ENGINE_PATH` | `/workspace/models/...engine` | TensorRT engine file |
| `SHOW_WINDOW` | `1` | Local X11 preview (`cv2.imshow`) |
| `THRESHOLD` | `0.3` | Segmentation mask threshold |
| `STREAM_INGEST_URL` | *(empty)* | **Single camera** — RTSPS URL injected on deploy |
| `STREAM_INGEST_URL_0` … `STREAM_INGEST_URL_N` | *(portal injects)* | **Multi-CSI** — one ingest URL per camera |
| `CAMERA_INDICES` | *(portal injects)* | CSI `sensor-id` list, e.g. `0,1` |
| `DETECT_EVERY_N_FRAMES` | `4` | Run UNet every Nth frame per camera (reduces GPU load) |
| `STREAM_FPS` | `15` | Stream frame rate (encode/push) |
| `STREAM_BITRATE_KBPS` | `2000` | x264 bitrate (software encode; Orin Nano has no NVENC) |

When `STREAM_INGEST_URL` is set, the container pushes annotated frames. The portal
plays them from:

`https://edgemediahls.pods.icicleai.tapis.io/cam-<device_uid>/index.m3u8`

## Manual test on Jetson

```bash
docker run --rm -it \
  --runtime nvidia --gpus all \
  --network host --ipc host --privileged \
  -e NVIDIA_DRIVER_CAPABILITIES=all \
  -e SHOW_WINDOW=1 \
  -e DISPLAY=$DISPLAY \
  -e ENGINE_PATH=/workspace/models/unetpp_teacher_512x768_fp16.engine \
  -e STREAM_INGEST_URL=rtsps://edgemediaingest.pods.icicleai.tapis.io:443/cam-<device_uid> \
  -e STREAM_FPS=15 \
  --mount type=bind,source=/tmp/argus_socket,target=/tmp/argus_socket \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  -v /opt/models/unetpp:/workspace/models:ro \
  docker.io/<youruser>/unet-trt-infer-live:latest
```

Open the HLS URL in a browser while the container runs.

## Multi-CSI on Orin Nano

Set device **camera count ≥ 2** and CSI sensor indices in the portal hardware setup, then deploy this app.
The control plane injects `STREAM_INGEST_URL_0`, `STREAM_INGEST_URL_1`, … and `CAMERA_INDICES`.
The container auto-selects multi-camera mode (shared UNet inference + per-camera RTSP push).

Manual test with two sensors:

```bash
docker run --rm -it \
  --runtime nvidia --gpus all \
  --network host --ipc host --privileged \
  -e SHOW_WINDOW=0 \
  -e CAMERA_INDICES=0,1 \
  -e STREAM_INGEST_URL_0=rtsps://edgemediaingest.pods.icicleai.tapis.io:443/cam-dev_test-0 \
  -e STREAM_INGEST_URL_1=rtsps://edgemediaingest.pods.icicleai.tapis.io:443/cam-dev_test-1 \
  -e DETECT_EVERY_N_FRAMES=4 \
  --mount type=bind,source=/tmp/argus_socket,target=/tmp/argus_socket \
  -v /opt/models/unetpp:/workspace/models:ro \
  docker.io/<youruser>/unet-trt-infer-live:latest
```

## Model card / portal deploy

Point the model card image at `unet-trt-infer-live:latest` and add env:

```
STREAM_INGEST_URL=rtsps://edgemediaingest.pods.icicleai.tapis.io:443/cam-${DEVICE_UID}
```

*(The control plane can inject this automatically on deploy — wire-up pending.)*

Keep the same mounts as the existing UNet card: `argus_socket`, `.X11-unix`, model volume.

## Troubleshooting: TensorRT engine OOM / deserialize failed

If you see `OutOfMemory` or `Failed to deserialize TensorRT engine` (~98 MiB):

1. **Stop leftover containers** — GPU memory is often held by a previous run:
   ```bash
   docker ps -a
   docker rm -f unetpp_infer   # or whatever name was used
   ```
2. **Retry** — the app retries engine load 3 times by default (`ENGINE_LOAD_RETRIES=3`).
3. **Engine / device match** — TRT warns if the `.engine` was built on a different
   Jetson SKU. Rebuild the engine on the same Orin Nano you deploy to.
4. **Watch memory** — on the host: `sudo tegrastats` while starting the container.
5. **Inference backend** — this image uses **pycuda** for TensorRT (not PyTorch) to
   avoid PyTorch grabbing unified GPU memory before TRT context allocation on Orin Nano.

Optional env tuning:

```
-e ENGINE_LOAD_RETRIES=5
-e ENGINE_LOAD_RETRY_DELAY=3
```
