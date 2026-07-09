# UNet++ MVS Multi-Camera Live Stream (AGX Xavier / JetPack 4)

GigE multi-camera capture via **Hikrobot MVS SDK**, shared **UNet++ TensorRT** segmentation (pycuda, no PyTorch), and **per-camera RTSPS push** to MediaMTX (Option B).

Designed for **Jetson AGX Xavier** with JetPack 4 (`l4t-base:r32.5.0`). MVS SDK is **not** baked into the image — mount host `/opt/MVS` at runtime.

## Architecture

```
Cam 0 grab thread ──┐
Cam 1 grab thread ──┼──► FrameStore (raw) ──► inference worker (UNet TRT)
                    │                              │
                    └──► stream threads ◄── processed overlays
                              │
                              ├── STREAM_INGEST_URL_0 -> MediaMTX
                              └── STREAM_INGEST_URL_1 -> MediaMTX
```

Grab, inference, and RTSP push run in **separate threads** (same pattern as `yolo_trt_docker`).

## Prerequisites (on Xavier host)

1. **MVS SDK** at `/opt/MVS` (already installed on your Jetsons).
2. **UNet++ TRT engine** built on this Xavier, e.g.:
   `/path/to/unetpp_teacher_512x768_fp16.engine`
3. GigE cameras reachable (same subnet; jumbo frames optional).
4. MediaMTX ingest/HLS pods running (see `deploy/mediamtx/MEDIAMTX_TWO_POD.md`).

## Build (on Xavier)

```bash
cd edge_fleet_control_plane/smart_docker_apps/unet_mvs_multicam_live_app
docker build -t habg21/unet-mvs-unetpp-live:latest .
```

## Run (manual validation)

```bash
docker run --rm -it \
  --runtime nvidia --gpus all \
  --network host \
  --privileged \
  -v /opt/MVS:/opt/MVS:ro \
  -v /path/to/models:/workspace/models:ro \
  -e ENGINE_PATH=/workspace/models/unetpp_teacher_512x768_fp16.engine \
  -e CAMERA_INDICES=0,1 \
  -e STREAM_FPS=10 \
  -e STREAM_WIDTH=960 \
  -e STREAM_HEIGHT=540 \
  -e STREAM_INGEST_URL_0='rtsps://edgemediaingest.pods.icicleai.tapis.io:443/cam-dev_test-0' \
  -e STREAM_INGEST_URL_1='rtsps://edgemediaingest.pods.icicleai.tapis.io:443/cam-dev_test-1' \
  habg21/unet-mvs-unetpp-live:latest
```

**Notes**

- `--network host` is required for GigE camera discovery and low-latency RTSP.
- `--privileged` matches the YOLO reference container for MVS device access.
- Replace `dev_test` with your device UID when testing portal paths later.

### Verify HLS

After the container is pushing:

```text
https://edgemediahls.pods.icicleai.tapis.io/cam-dev_test-0/index.m3u8
https://edgemediahls.pods.icicleai.tapis.io/cam-dev_test-1/index.m3u8
```

Open in VLC or the portal HLS player once Phase 2 is wired.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ENGINE_PATH` | `/workspace/models/unetpp_teacher_512x768_fp16.engine` | UNet++ TRT engine |
| `CAMERA_INDICES` | `0,1` | MVS device indices (`all` = every enumerated camera) |
| `STREAM_INGEST_URL_0`, `_1`, … | — | Per-camera RTSPS ingest (Option B) |
| `STREAM_INGEST_URLS` | — | Comma-separated fallback if indexed vars omitted |
| `STREAM_FPS` | `10` | Output stream frame rate |
| `STREAM_BITRATE_KBPS` | `1500` | x264 bitrate |
| `STREAM_WIDTH` / `STREAM_HEIGHT` | `960` / `540` | Downscale for RTSP push |
| `DISPLAY_WIDTH` | `960` | Resize before inference (aspect preserved) |
| `DETECT_EVERY_N_FRAMES` | `1` | Run TRT every N grabs (raise to save GPU) |
| `THRESHOLD` | `0.3` | Segmentation mask threshold |
| `ENABLE_AUTO_ADJUSTMENT` | `true` | MVS auto exposure/gain/WB |
| `AUTO_ADJUST_MODE` | `once` | `off` / `once` / `continuous` |
| `ENABLE_IMAGE_SAVE` | `false` | Save annotated JPEGs under `/data/camera_images` |

## Phase 2 (portal)

Not included in this image — planned follow-up:

- Inject `STREAM_INGEST_URL_{i}` on deploy for GigE device generation
- Nested camera grid on deployment/group pages
- Model card with MVS mount, `network_mode: host`, `privileged`

## Troubleshooting

| Symptom | Check |
|---------|--------|
| `enum devices fail` | Cameras powered, same L2 network, firewall |
| Engine deserialize fails | Engine must be built on Xavier; free GPU mem (`docker rm` old containers) |
| RTSP writer failed | `gstreamer1.0-rtsp`, TLS reachability to ingest pod |
| Low FPS with 2 cams | Set `DETECT_EVERY_N_FRAMES=2` or lower `DISPLAY_WIDTH` |
