# Two-pod MediaMTX over Tapis (RTSP ingest + HLS playback)

This is the **production-quality** streaming path (H.264 / LL-HLS, many viewers),
as an alternative to the built-in MJPEG relay. It works around Tapis's
"one external endpoint per pod" rule by using **two** pods, each with a single
networking entry — exactly the pattern your mosquitto broker already proves
works.

```
Jetson ──rtsps://edgemediaingest…:443/cam-<id>──▶ [ingest pod]  RTSP only, tcp/8554
                                                      ▲ pull (rtsps, on-demand)
Browser ──https://edgemediahls…/cam-<id>/index.m3u8─▶ [hls pod]  HLS only, http/8888
```

## The one thing we must prove first

Your mosquitto pod shows Tapis **terminates TLS at its ingress and forwards plain
TCP to the container's single port** (your `mosquitto.conf` has no TLS on `1883`,
yet clients connect with TLS on `443`). RTSP is also plain TCP, so the *same*
trick should work: the Jetson speaks `rtsps://…:443` (TLS to Tapis) and the
ingest container receives plain RTSP on `8554` (`MTX_RTSPENCRYPTION=no`).

The **uncertain part** is whether RTSP's media negotiation survives a TLS-
terminating HTTP-aware proxy. We validate that with **one pod and zero portal
changes** before building anything else.

---

## Phase 0 — prove the RTSPS-through-Tapis ingest hop (≈15 min)

1. **Allowlist** `bluenviron/mediamtx:latest` (already done) and **deploy the
   ingest pod** from [`pod-mediamtx-ingest.json`](pod-mediamtx-ingest.json).
   It exposes a single `tcp/8554` entry → reachable at
   `edgemediaingest.pods.icicleai.tapis.io:443`.

2. **Push a test stream** from any machine with GStreamer + an H.264 encoder
   (your laptop is easiest — it avoids the Orin Nano encoder question for now):

   ```bash
   gst-launch-1.0 -e videotestsrc is-live=true ! \
     video/x-raw,width=1280,height=720,framerate=30/1 ! \
     x264enc tune=zerolatency speed-preset=ultrafast bitrate=2000 key-int-max=60 ! \
     h264parse ! \
     rtspclientsink location=rtsps://edgemediaingest.pods.icicleai.tapis.io:443/test \
       protocols=tcp tls-validation-flags=0
   ```

   > `protocols=tcp` is **required**: the ingest pod is TCP-only
   > (`MTX_RTSPTRANSPORTS=tcp`) and only the single TLS/TCP connection on 443
   > exists, so RTP must be interleaved over it. Without it, GStreamer defaults to
   > UDP and `SETUP` fails with "Could not setup transport."
   >
   > Also **no** `profiles=GST_RTSP_PROFILE_SAVP`. Because Tapis terminates TLS,
   > MediaMTX must see plain RTSP/AVP, not SRTP. `tls-validation-flags=0` is only
   > needed if cert validation complains (it shouldn't — Tapis certs are valid).

3. **Play it back** from the same machine (also through Tapis, over RTSPS):

   ```bash
   ffplay -rtsp_transport tcp rtsps://edgemediaingest.pods.icicleai.tapis.io:443/test
   # or: vlc rtsps://edgemediaingest.pods.icicleai.tapis.io:443/test
   ```

   Watch the ingest pod logs in the Tapis UI — you should see a successful
   `published` / `is publishing` line for path `test`.

### Decision point

- ✅ **Test pattern round-trips** → the mechanism works. Continue to Phase 1.
- ❌ **Ingest fails** (handshake errors, `invalid request`, immediate close) →
  RTSP doesn't survive this proxy. **Stop here and stay on the MJPEG relay** —
  the remaining phases won't help. (Optional: ask the admins whether the pod can
  get raw TCP *pass-through* instead of TLS-terminating proxy; that would change
  the answer.)

---

## Phase 1 — add the HLS playback pod

1. **Build & push the custom HLS image** (same workflow as your mosquitto image)
   from [`hls-pod/`](hls-pod/):

   ```bash
   cd deploy/mediamtx/hls-pod
   docker buildx build --platform linux/amd64 \
     -t docker.io/<youruser>/edge-mediamtx-hls:latest --push .
   ```

   The config it bakes in ([`hls-pod/mediamtx.yml`](hls-pod/mediamtx.yml)) pulls
   every requested path from the ingest pod over `rtsps://…:443` on demand.

2. **Allowlist** that image in Tapis, then set the `image` field in
   [`pod-mediamtx-hls.json`](pod-mediamtx-hls.json) (replace `REPLACE_ME`) and
   **deploy the pod**. It exposes a single `http/8888` entry → reachable at
   `https://edgemediahls.pods.icicleai.tapis.io`.

3. **Test HLS end to end:** with the Phase-0 push still running, open

   ```
   https://edgemediahls.pods.icicleai.tapis.io/test/index.m3u8
   ```

   in an HLS player (Safari plays it natively; or use `hls.js` / VLC). The HLS
   pod will pull `test` from the ingest pod and serve segments. A few seconds of
   start-up latency is normal.

---

## Phase 2 — wire the portal back to MediaMTX

Only after Phases 0–1 pass. The portal currently uses the MJPEG relay; switching
a device (or the whole fleet) to MediaMTX means:

- Re-introduce two settings: `MEDIA_INGEST_URL=rtsps://edgemediaingest.pods.icicleai.tapis.io:443`
  and `MEDIA_HLS_BASE_URL=https://edgemediahls.pods.icicleai.tapis.io`, and make
  `MediaConfig` pick **MediaMTX mode** when both are set (else the relay).
- `stream_service` builds `ingest_url = {MEDIA_INGEST_URL}/cam-<id>` (RTSPS) and
  `hls_url = {MEDIA_HLS_BASE_URL}/cam-<id>/index.m3u8`.
- The agent's `build_stream_pipeline` uses H.264 → `rtspclientsink
  location=rtsps://…:443/cam-<id> tls-validation-flags=0` (software `x264enc` on
  the Orin Nano, since it has no NVENC).
- The device-detail template swaps the MJPEG `<img>` back to the `hls.js` player.

Tell me when Phase 0 passes and I'll implement Phase 2 (it's a contained change —
I kept the relay code intact so we can support both modes side by side).

---

## Phase 3 — the Jetson Orin Nano push (production)

Once the portal drives it, the agent runs (software encode, no NVENC needed):

```bash
gst-launch-1.0 -e nvarguscamerasrc ! \
  'video/x-raw(memory:NVMM),width=1280,height=720,framerate=15/1' ! \
  nvvidconv ! video/x-raw,format=I420 ! \
  x264enc tune=zerolatency speed-preset=ultrafast bitrate=2000 key-int-max=30 ! \
  h264parse ! \
  rtspclientsink location=rtsps://edgemediaingest.pods.icicleai.tapis.io:443/cam-<device_uid> \
    protocols=tcp tls-validation-flags=0
```

Requires `gstreamer1.0-plugins-ugly` (x264enc) + `gstreamer1.0-rtsp`
(rtspclientsink) on the device.

## Notes

- **Why two pods, not one?** A single pod gets one 443 slot; MediaMTX needs two
  reachable endpoints (RTSP in, HTTP/HLS out). Splitting them gives each pod its
  own single-port pod (like mosquitto).
- **On-demand pull** means the ingest→HLS link only activates when a browser is
  watching, saving bandwidth.
- **Security:** both pods currently allow anonymous publish/read for testing.
  Before production add MediaMTX auth (`MTX_AUTHINTERNALUSERS_*`) or per-device
  credentials, and lock `hlsAllowOrigin` to the portal origin.
