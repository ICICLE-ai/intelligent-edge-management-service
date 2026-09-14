# YOLO CNW — inference + live annotated stream

YOLOv8 corn/weed detection with annotated RTSPS push to MediaMTX. Target
hardware is a **Dell PowerEdge GPU server**. Inbound video is handled inside
this container. IEMS only injects `STREAM_INGEST_URL` on fleet deploy (same
contract as the UNet live apps).

## Build & push

```bash
cd edge_fleet_control_plane/smart_docker_apps/ara_yolo_cnw_live_app
docker buildx build --platform linux/amd64 \
  -t docker.io/<youruser>/yolo-cnw-live:latest --push .
```

linux/amd64 CUDA 12.4. PowerEdge needs NVIDIA driver + Container Toolkit.
Weights are mounted at `/workspace/models` (Patra artifact on IEMS deploy).

## Runtime environment

| Variable | Default | Purpose |
|---|---|---|
| `MODEL_PATH` | `/workspace/models/Yolo-cnw.pt` | Weights (IEMS `model_env_var`) |
| `STREAM_INGEST_URL` | *(empty)* | **Injected on deploy** — RTSPS output |
| `STREAM_FPS` | `15` | Annotated stream frame rate |
| `STREAM_BITRATE_KBPS` | `1800` | x264 bitrate |
| `STREAM_WIDTH` / `STREAM_HEIGHT` | `480` / `320` | Output size |

Portal playback:

`https://edgemediahls.pods.icicleai.tapis.io/cam-<device_uid>/index.m3u8`

## Publish on IEMS

Register the PowerEdge as **Dell PowerEdge (GPU)** with **Wireless video (no local camera)**. Paste:

```
docker run -d --name yolo_cnw_live --runtime nvidia --gpus all --network host --ipc host \
  -e NVIDIA_DRIVER_CAPABILITIES=compute,utility \
  -e MODEL_PATH=/workspace/models/Yolo-cnw.pt \
  -e STREAM_FPS=15 -e STREAM_WIDTH=480 -e STREAM_HEIGHT=320 \
  -v /opt/models/yolo-cnw:/workspace/models:ro \
  habg21/ara-yolo-cnw-live:latest
```

Do **not** put `STREAM_INGEST_URL` on the card. Attach Patra card
`b404980f-438a-4760-b358-f6325e6c8f2d` (`Yolo-cnw.pt`,
`container_path=/workspace/models/Yolo-cnw.pt`, `model_env_var=MODEL_PATH`).
Tags: `live-stream`, `rtsp`.
