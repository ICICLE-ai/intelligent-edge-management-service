# Live Camera Streaming

This adds a **live camera feed** to the portal's device detail page. The portal
supports **two modes**, auto-selected from config:

| Mode | When | Pipeline | Infra |
|---|---|---|---|
| **relay** (default) | `MEDIA_ENABLED=true`, no MediaMTX URLs | MJPEG over HTTPS into the control plane | none — reuses port 443 |
| **mediamtx** | `MEDIA_INGEST_URL` + `MEDIA_HLS_BASE_URL` set | H.264 over RTSPS → MediaMTX → HLS | two MediaMTX pods (see [`mediamtx/MEDIAMTX_TWO_POD.md`](mediamtx/MEDIAMTX_TWO_POD.md)) |

Use **relay** for zero-infra, single-viewer simplicity; use **mediamtx** for
H.264 quality and many simultaneous viewers. Both are driven by the same
`stream_start` / `stream_stop` MQTT commands and the same device-page controls.
The rest of this doc covers the **relay**; the MediaMTX path is documented in
[`mediamtx/MEDIAMTX_TWO_POD.md`](mediamtx/MEDIAMTX_TWO_POD.md).

---

## Relay mode

Instead of the camera preview only appearing as an X11 popup on the Jetson, the
device JPEG-encodes its camera and pushes the frames straight into the control
plane over the **same HTTPS port it already uses for heartbeats**. The control
plane fans the frames out to browsers as `multipart/x-mixed-replace` (MJPEG).

```
 Portal  ──(MQTT: stream_start / stream_stop)──▶  Agent (Jetson)
                                                   │ GStreamer: jpegenc
                                                   ▼ HTTPS PUT (MJPEG)
                                          Control plane  /api/stream/{id}/ingest
                                                   │  in-memory relay
 Browser ◀── multipart/x-mixed-replace ──── /devices/{id}/stream.mjpg
```

**MQTT carries only the start/stop command. The video never goes through MQTT.**

## Relay vs. MediaMTX on Tapis

A *single* MediaMTX pod can't work here: it needs **two** externally reachable
ports (RTSP ingest + HLS playback), but Tapis exposes **one endpoint per pod**.
The relay sidesteps this entirely (HTTP in *and* out on one port). The
**mediamtx** mode solves it differently — **two** single-port pods (one RTSPS
ingest, one HLS playback) that talk to each other internally, mirroring the
mosquitto broker's single-TCP-port-on-443 pattern. That path is fully working;
see [`mediamtx/MEDIAMTX_TWO_POD.md`](mediamtx/MEDIAMTX_TWO_POD.md).

---

## 1. Configure the control plane

Add to the control-plane pod env (see `deploy/env.production.example`):

```
MEDIA_ENABLED=true
MEDIA_DEFAULT_CAMERA=csi      # or usb
MEDIA_DEFAULT_WIDTH=1280
MEDIA_DEFAULT_HEIGHT=720
MEDIA_DEFAULT_FPS=15
MEDIA_JPEG_QUALITY=80
```

That's it — no media-server URLs. The device ingest URL is derived from
`APP_BASE_URL`, so make sure `APP_BASE_URL` is the public pod URL
(`https://edgecontrolplane.pods.icicleai.tapis.io`). The portal builds:

- **Ingest (device → portal):** `{APP_BASE_URL}/api/stream/<device_uid>/ingest?token=<signed>`
- **Playback (browser):** `/devices/<device_uid>/stream.mjpg`

The ingest token is a short-lived HMAC of the device id signed with `APP_SECRET`
(TTL `MEDIA_TOKEN_TTL_SECONDS`). It is delivered to the device inside the
`stream_start` MQTT command, so no extra credential plumbing is needed.

> **Single worker required.** The relay buffers the latest frame in process
> memory, so ingest and playback must hit the same worker. The control plane
> runs one Uvicorn worker (see `deploy/docker-entrypoint.sh`), which satisfies
> this. If you ever scale to multiple workers, move the buffer to a shared store
> (e.g. Redis).

## 2. Device (Jetson) requirements

The agent runs a GStreamer pipeline on `stream_start`. It needs GStreamer with
the standard plugins (all ship with JetPack):

- CSI camera → `nvarguscamerasrc` + `nvvidconv`
- USB camera → `v4l2src`
- `jpegenc` + `souphttpclientsink` (both in `gstreamer1.0-plugins-good`)

No H.264 / NVENC is used, so **the Jetson Orin Nano's missing hardware encoder
is no longer a problem.** If the plugins are missing:

```bash
sudo apt-get install -y gstreamer1.0-plugins-good gstreamer1.0-plugins-base
```

> Re-issue the device installer after deploying this branch so the Jetson gets
> the agent build with the MJPEG streaming handlers.

Quick manual sanity check on the Jetson (CSI) — grab the ingest URL the portal
generates (it's logged by the agent, with the token redacted; or read it from
the `stream_start` command payload) and run:

```bash
gst-launch-1.0 -e nvarguscamerasrc ! \
  'video/x-raw(memory:NVMM),width=1280,height=720,framerate=15/1' ! \
  nvvidconv ! video/x-raw,format=I420 ! jpegenc quality=80 ! \
  souphttpclientsink location='https://edgecontrolplane.pods.icicleai.tapis.io/api/stream/<device_uid>/ingest?token=<token>'
```

USB variant:

```bash
gst-launch-1.0 -e v4l2src device=/dev/video0 ! videoconvert ! \
  video/x-raw,width=1280,height=720,framerate=15/1 ! jpegenc quality=80 ! \
  souphttpclientsink location='https://.../api/stream/<device_uid>/ingest?token=<token>'
```

If that runs without exiting, open `/devices/<device_uid>` in the portal — the
Live camera card should show the feed.

## 3. Use it

On a device detail page (when `MEDIA_ENABLED=true`):

1. Pick the camera type (CSI / USB) and click **Start stream**.
2. The portal publishes `stream_start` over MQTT; the agent launches GStreamer
   and acks `RUNNING`.
3. The Live camera card connects to `/devices/<id>/stream.mjpg` and starts
   showing frames. A few seconds of start-up latency is normal while the camera
   opens and the first JPEG frames arrive.
4. **Stop stream** publishes `stream_stop`; the agent terminates the pipeline.

## How it maps to the code

| Piece | Location |
|---|---|
| Media config | `app/config.py` (`MediaConfig`) |
| Start/stop + token + command publish | `app/services/stream_service.py` |
| In-memory frame relay | `app/services/stream_relay.py` |
| Ingest endpoint (device → portal) | `app/routes/api/stream.py` (`PUT /api/stream/{id}/ingest`) |
| Playback + start/stop web routes | `app/routes/web/devices.py` (`/devices/{id}/stream.mjpg`, `/stream/start`, `/stream/stop`) |
| Player UI | `app/templates/devices/detail.html` (Live camera card, MJPEG `<img>`) |
| Agent GStreamer handlers | `app/agent_package/agent/main.py` (`build_stream_pipeline`, `stream_start` / `stream_stop`) |

The command flows through the exact same MQTT path and ack/command-audit
machinery as deployments, so stream commands show up in **Recent commands** and
the event log.

## Notes & caveats

- **Bandwidth / FPS.** MJPEG is simple and firewall-friendly but heavier on the
  wire than H.264. Keep resolution/FPS modest (720p @ 10–15 fps is a good start)
  and tune `MEDIA_JPEG_QUALITY`.
- **Camera contention (CSI).** A Jetson CSI camera is single-consumer. If a model
  container already holds the camera, the stream pipeline can't open it at the
  same time. Stream when no model is using the camera, or use a separate USB
  camera.
- **Raw vs. annotated feed.** This streams the *raw* camera. Streaming the
  model's *annotated* output is a future enhancement (have the model container
  push frames to the same ingest endpoint).
- **Security.** Ingest is gated by a signed, expiring per-device token; playback
  is gated by the normal portal login + device-ownership check. Rotate
  `APP_SECRET` to invalidate all outstanding ingest tokens.
