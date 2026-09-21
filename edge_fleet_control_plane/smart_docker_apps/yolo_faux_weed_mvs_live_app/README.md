# YOLO Faux Weed Detection — MVS GigE Live Stream (AGX Xavier / JetPack 4)

YOLO detection of faux (artificial) weeds on Hikrobot GigE cameras via the **MVS
SDK**, with per-camera **RTSPS push** to MediaMTX. Same threading model as
`unet_mvs_multicam_live_app`: grab, inference, and RTSP push run in separate
threads and share one engine.

- Patra model card: `c2e4c207-732d-46ae-8fd9-1d0658ae9d95`
- Weights: [habg21/faux_weed_detection](https://huggingface.co/habg21/faux_weed_detection) → `artificial_plants_1000_native_1920_best.pt`

## Architecture

```
Cam 0 grab thread ──┐
Cam 1 grab thread ──┼──► FrameStore (raw) ──► inference worker (YOLO TRT)
                    │                              │
                    └──► stream threads ◄── annotated frames
                              │
                              ├── STREAM_INGEST_URL_0 -> MediaMTX
                              └── STREAM_INGEST_URL_1 -> MediaMTX
```

## Why TensorRT and not the `.pt`

JetPack 4 is Python 3.6 and ultralytics requires 3.8+. NVIDIA does ship a
CUDA-enabled PyTorch wheel for 3.6, but that is not enough on its own: a YOLO
`.pt` is a pickled ultralytics model object, so it cannot be loaded without the
package that defines those classes. Installing ultralytics from source is not an
option either — it uses 3.8+ syntax throughout.

So `MODEL_PATH` must point at a TensorRT engine built **on this Xavier**, the
same arrangement as `unet_mvs_multicam_live_app`, and box decoding plus NMS
happen in `app/yolo_runner.py`.

## Prerequisites (on the Xavier host)

1. **MVS SDK** at `/opt/MVS`.
2. A host directory for the engine, e.g. `/opt/models/faux-weed/`.
3. GigE cameras reachable on the same L2 network.
4. MediaMTX ingest/HLS pods running (see `deploy/mediamtx/MEDIAMTX_TWO_POD.md`).

## 1. Class names

A TensorRT engine carries no class names, so they must be passed in as
`CLASS_NAMES`, indexed by class id. For this model there are 7:

| id | name |
|----|------|
| 0 | `green_broadleaf` |
| 1 | `red_swordleaf` |
| 2 | `red_green_swordleaf` |
| 3 | `bright_green_grass` |
| 4 | `white_flower_spike` |
| 5 | `purple_flower_spike` |
| 6 | `purple_green_swordleaf` |

```text
CLASS_NAMES=green_broadleaf,red_swordleaf,red_green_swordleaf,bright_green_grass,white_flower_spike,purple_flower_spike,purple_green_swordleaf
```

These come from the ONNX metadata, which ultralytics embeds on export. To re-read
them after a retrain:

```bash
python3 -c "import onnx; print({p.key: p.value for p in onnx.load('faux_weed_1920.onnx').metadata_props}['names'])"
```

Note how many pairs differ only by colour — `red_swordleaf` vs
`red_green_swordleaf` vs `purple_green_swordleaf`, and `green_broadleaf` vs
`bright_green_grass`. That is what makes the white balance note below load-bearing
rather than theoretical.

## 2. Export the engine

Do this before building the image. TensorRT engines are tied to the device,
TensorRT version, and precision, so the ONNX export can happen anywhere but the
engine build must run on the Xavier — and whether TensorRT 7 will accept a YOLO11
graph at all is the open question this repo cannot answer for you.

### 2a. ONNX, on the training box

Export twice. The 640 copy is a throwaway used only to find out quickly whether
the graph parses, since a parse failure is resolution-independent and fails in
minutes rather than after an hour of 1920 build time.

```bash
# Anywhere with ultralytics — NOT on the Xavier.
# Do not pass nms=True: the decoder in app/yolo_runner.py expects the raw
# (1, 4 + num_classes, num_anchors) detect head, not an NMS-fused graph.
yolo export model=artificial_plants_1000_native_1920_best.pt \
  format=onnx opset=11 imgsz=1920 simplify=True
mv artificial_plants_1000_native_1920_best.onnx faux_weed_1920.onnx

yolo export model=artificial_plants_1000_native_1920_best.pt \
  format=onnx opset=11 imgsz=640 simplify=True
mv artificial_plants_1000_native_1920_best.onnx faux_weed_probe_640.onnx
```

Ultralytics names the ONNX after the checkpoint, so the renames keep the second
export from overwriting the first.

### 2b. Probe, on the Xavier

```bash
python3 -c "import tensorrt; print(tensorrt.__version__)"   # expect 7.x
/usr/src/tensorrt/bin/trtexec --onnx=faux_weed_probe_640.onnx --explicitBatch --fp16
```

`--explicitBatch` is required on TensorRT 7: it defaults to implicit batch, while
ultralytics exports an explicit batch dimension. Add `--verbose` if it fails, to
see which node the parser rejected.

If this succeeds, note the reported mean latency — scaled by ~9x it estimates the
1920 cost, and answers whether the Xavier is fast enough to be worth targeting at
all. If it fails, stop here and see the fallbacks below rather than building the
1920 engine.

### 2c. Real build, on the Xavier

Tens of minutes at 1920 — run it under `tmux` or `nohup`.

```bash
sudo nvpmodel -m 0 && sudo jetson_clocks          # max clocks first
/usr/src/tensorrt/bin/trtexec \
  --onnx=faux_weed_1920.onnx \
  --saveEngine=/opt/models/faux-weed/faux_weed_1x3x1920x1920_agx_xavier_jp4.engine \
  --explicitBatch --fp16 --workspace=4096
```

`opset=11` is deliberate: JetPack 4 ships TensorRT 7, whose ONNX parser is
unreliable above opset 11. **This model is YOLO11s**, whose blocks are newer than
that parser, so treat a successful build as something to verify early rather than
assume — see the fallbacks below.

### Why `imgsz=1920`

Match the training resolution. The model was trained at `imgsz=1920, rect=False`,
so 1920x1920 letterboxed, and critically with `scale=0.0` and `mosaic=0.0` — no
scale augmentation at all. It has therefore only ever seen objects at one scale,
and dropping to 1280 shrinks them ~1.5x relative to everything it learned. A
lower `imgsz` is the obvious lever if the Xavier is too slow, but expect to lose
recall for it rather than treating it as free.

Keep `DISPLAY_WIDTH` at or above the engine's input width. Frames are resized to
`DISPLAY_WIDTH` after grab and before inference, so a smaller value upscales into
the letterbox and throws away detail the camera already captured. Name the engine
after its shape, as above, so the deployed resolution stays obvious.

### Camera colour matters for this model

Training disabled all colour augmentation (`hsv_h/hsv_s/hsv_v = 0.0`) because the
classes are distinguished by colour — red, purple, and green artificial plants.
That makes inference unusually sensitive to camera colour rendition: auto white
balance drifting between runs can shift a class across the decision boundary in a
way a colour-augmented model would have shrugged off.

The MVS defaults here run auto exposure/gain/white balance once at start-up and
then lock them (`AUTO_ADJUST_MODE=once`, `LOCK_AUTO_ADJUST_AFTER_ONCE=true`),
which keeps colour stable within a run but not necessarily across runs or across
cameras. If classes get confused, pin fixed exposure and white balance on the
cameras in MVS Viewer and set `AUTO_ADJUST_MODE=off` instead of leaving it to the
`once` pass.

### If TensorRT 7 refuses the graph

Run the export first, before anything else here: it is the step most likely to
fail, and finding out early is cheap. If `trtexec` cannot parse the ONNX even at
opset 11 with `simplify=True`, the realistic options are:

- **Reflash the Xavier to JetPack 5** (TensorRT 8.5, Python 3.8). Ultralytics then
  runs `artificial_plants_1000_native_1920_best.pt` directly and this whole export
  step disappears. Highest chance of working, largest one-time cost.
- **Deploy to the Dell PowerEdge instead.** These are *GigE* cameras — they sit on
  the network, not on the Xavier's bus — so the inference host does not have to be
  the Xavier, as long as the PowerEdge is on the same L2 segment as the cameras and
  has the MVS SDK. `ara_yolo_cnw_live_app` already shows the x86 ultralytics
  pattern. Least disruptive, and much faster at 1920.

Performance is worth sizing up before committing either way: YOLO11s at 1920x1920
FP16 on the Xavier's Volta GPU lands in the low single-digit FPS. With
`DETECT_EVERY_N_FRAMES=3` and one camera that still fills a 5 fps output stream,
but three cameras share that same budget round-robin.

## 3. Build and push

Build on the Xavier itself — this is an arm64 L4T base, and cross-building from
an x86 laptop cannot resolve the host CUDA libraries, so the image could not be
smoke-tested there anyway.

```bash
cd edge_fleet_control_plane/smart_docker_apps/yolo_faux_weed_mvs_live_app
docker build -t habg21/yolo-faux-weed-mvs-live:jp4 .
docker push habg21/yolo-faux-weed-mvs-live:jp4
```

## 4. Run (manual validation)

Single camera:

```bash
docker run --rm -it \
  --runtime nvidia --gpus all \
  --network host \
  --privileged \
  -v /opt/MVS:/opt/MVS:ro \
  -v /opt/models/faux-weed:/workspace/models:ro \
  -e MODEL_PATH=/workspace/models/faux_weed_1x3x1920x1920_agx_xavier_jp4.engine \
  -e CLASS_NAMES=green_broadleaf,red_swordleaf,red_green_swordleaf,bright_green_grass,white_flower_spike,purple_flower_spike,purple_green_swordleaf \
  -e CAMERA_INDICES=0 \
  -e DISPLAY_WIDTH=1920 \
  -e DETECT_EVERY_N_FRAMES=3 \
  -e STREAM_FPS=5 \
  -e STREAM_INGEST_URL_0='rtsps://edgemediaingest.pods.icicleai.tapis.io:443/cam-dev_test-0' \
  habg21/yolo-faux-weed-mvs-live:jp4
```

Two cameras — add one `STREAM_INGEST_URL_<index>` per camera:

```bash
  -e CAMERA_INDICES=0,1 \
  -e STREAM_INGEST_URL_0='rtsps://edgemediaingest.pods.icicleai.tapis.io:443/cam-dev_test-0' \
  -e STREAM_INGEST_URL_1='rtsps://edgemediaingest.pods.icicleai.tapis.io:443/cam-dev_test-1' \
```

**Notes**

- `--network host` is required for GigE camera discovery and low-latency RTSP.
- `--privileged` matches the other MVS containers for SDK device access.
- Replace `dev_test` with the device UID when testing portal paths.

On start-up the container prints the engine's resolved input and output shapes.
For this model at 1920 they should read `(1, 3, 1920, 1920)` and
`(1, 11, 75600)` — 11 being `4 + 7` classes. Anything else, in particular a
2-dimensional output, means the ONNX was exported with NMS fused in and the
decoder will not understand it.

### Verify HLS

```text
https://edgemediahls.pods.icicleai.tapis.io/cam-dev_test-0/index.m3u8
https://edgemediahls.pods.icicleai.tapis.io/cam-dev_test-1/index.m3u8
```

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MODEL_PATH` | `/workspace/models/faux_weed_1x3x1920x1920_agx_xavier_jp4.engine` | TensorRT engine built on this device |
| `CLASS_NAMES` | — | Comma-separated labels in class-id order; ids are shown if unset |
| `CONFIDENCE` | `0.25` | Minimum detection score |
| `IOU` | `0.45` | NMS IoU threshold |
| `MAX_DETECTIONS` | `300` | Cap on boxes per frame |
| `CAMERA_INDICES` | `0` | MVS device indices (`all` = every enumerated camera) |
| `STREAM_INGEST_URL_0`, `_1`, … | — | Per-camera RTSPS ingest; IEMS injects these on deploy |
| `STREAM_INGEST_URLS` | — | Comma-separated fallback |
| `STREAM_INGEST_URL` | — | Single-camera fallback |
| `STREAM_FPS` | `5` | Output stream frame rate |
| `STREAM_BITRATE_KBPS` | `1500` | x264 bitrate |
| `STREAM_WIDTH` / `STREAM_HEIGHT` | `960` / `540` | Downscale for RTSP push |
| `DISPLAY_WIDTH` | `1920` | Resize after grab, before inference (aspect preserved) |
| `DETECT_EVERY_N_FRAMES` | `3` | Run the engine every N grabs (raise to save GPU) |
| `ENABLE_AUTO_ADJUSTMENT` | `true` | MVS auto exposure/gain/white balance |
| `AUTO_ADJUST_MODE` | `once` | `off` / `once` / `continuous` |
| `ENABLE_IMAGE_SAVE` | `false` | Save JPEGs under `/data/camera_images` |

## Troubleshooting

| Symptom | Check |
|---------|--------|
| `enum devices fail` | Cameras powered, same L2 network, firewall |
| `Find 0 devices` | Missing `--network host`, or MVS not mounted at `/opt/MVS` |
| `MODEL_PATH must be a TensorRT engine` | Pointed at the `.pt`; export it first (step 2) |
| Engine deserialize fails | Rebuild on this Xavier; free GPU memory (`docker ps && docker rm -f ...`) |
| `trtexec` fails to parse the ONNX | `opset=11 simplify=True`; if it still fails, TensorRT 7 cannot take this YOLO11 graph — see the fallbacks above |
| `Dynamic input shape not supported` | Re-export with a fixed `imgsz` and batch 1 |
| Boxes labelled `id 0` | Set `CLASS_NAMES` |
| `det 0` and frame looks dark | Auto-exposure locked at a bad value after 1.5 s; use `AUTO_ADJUST_MODE=continuous`, or set exposure in `/opt/MVS/bin/MVS.sh` and run with `AUTO_ADJUST_MODE=off` |
| Boxes misaligned or absent | Output is not `(1, 4 + nc, anchors)`; re-export without `nms=True` |
| Plants detected but wrong colour class | Camera white balance differs from training; pin exposure/WB and set `AUTO_ADJUST_MODE=off` |
| Small plants missed | Engine `imgsz` below 1920, or `DISPLAY_WIDTH` below the engine width; the model has no scale augmentation to compensate |
| RTSP writer failed | `gstreamer1.0-rtsp` present, TLS reachability to the ingest pod |
| Low FPS with 2+ cams | Raise `DETECT_EVERY_N_FRAMES`, or rebuild the engine at a lower `imgsz` |
