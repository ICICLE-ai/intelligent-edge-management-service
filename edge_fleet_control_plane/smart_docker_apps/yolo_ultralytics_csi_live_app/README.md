# Ultralytics YOLO — CSI live stream

One image for Ultralytics YOLO **detection** and **segmentation**. The weights
file chooses the task. `results[0].plot()` draws boxes or masks.

Camera capture and the RTSPS push match `unet_gstreamer_live_app` (CSI via
`nvarguscamerasrc`, portal-injected `STREAM_INGEST_URL`).

## What you can swap on the model card

Keep this image. Change only the artifact the card downloads and `MODEL_PATH`
(the filename inside `/workspace/models`).

| Weights | Works on this image |
|---|---|
| Ultralytics `.pt` for detect or segment (YOLO11, v8, and other checkpoints this Ultralytics version can load) | Yes. Task, class names, and default input size come from the file. |
| `.engine` exported by Ultralytics **on this same Jetson and TensorRT** | Yes. |
| `.engine` built on another device, or a raw `trtexec` engine with no Ultralytics metadata | No. |
| A UNet++ engine, or a YOLOv5 `.pt` from the old PyTorch repo | No. |

`IMAGE_SIZE=0` (the default) uses the size stored in the weights. Set it only when you want to override that.

## Build

Build on the Jetson, or with `buildx` for `linux/arm64`. The base image is the
same NGC iGPU PyTorch image as the UNet live app (JetPack 6 / Orin). It does
not run on JetPack 4.

```bash
cd smart_docker_apps/yolo_ultralytics_csi_live_app
docker buildx build --platform linux/arm64 \
  -t docker.io/<youruser>/yolo-ultralytics-csi-live:latest --push .
```

The Dockerfile installs Ultralytics with `--no-deps` so pip does not replace
the Jetson `torch` / `torchvision`. The build fails if those imports are missing
from the base image.

## Model card

- Image: `yolo-ultralytics-csi-live:latest`
- Model env var: `MODEL_PATH`
- Container path: `/workspace/models/<your-file>.pt` (or `.engine`)
- Mount: `${DEPLOYMENT_DIR}/model` → `/workspace/models`
- Same CSI mounts as the UNet live card: `/tmp/argus_socket`
- Leave `STREAM_INGEST_URL` off the card. The portal injects it on deploy.
- Check **Ask for a model URL at deploy time**. The person launching the app pastes a download link, including a Tapis postit redeem URL. The Jetson downloads that URL and saves the bytes at the card's `MODEL_PATH`. The link does not need a filename in it. Set `MODEL_PATH` to a `.pt` path when the file is a checkpoint, or `.engine` when it is an Ultralytics engine.

## Manual test

```bash
docker run --rm -it \
  --runtime nvidia --gpus all \
  --network host --ipc host --privileged \
  -e NVIDIA_DRIVER_CAPABILITIES=all \
  -e MODEL_PATH=/workspace/models/yolo11n-seg.pt \
  -e STREAM_INGEST_URL=rtsps://edgemediaingest.pods.icicleai.tapis.io:443/cam-<device_uid> \
  -e CONFIDENCE=0.25 \
  -e DETECT_EVERY_N_FRAMES=2 \
  --mount type=bind,source=/tmp/argus_socket,target=/tmp/argus_socket \
  -v /opt/models/yolo:/workspace/models:ro \
  docker.io/<youruser>/yolo-ultralytics-csi-live:latest
```
