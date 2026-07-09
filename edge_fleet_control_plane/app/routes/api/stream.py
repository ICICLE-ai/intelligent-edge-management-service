"""MJPEG ingest endpoint used by the on-device agent.

The agent opens a single long-lived HTTP request and streams JPEG frames at us
(via GStreamer ``souphttpclientsink``). We scan the byte stream for JPEG
start/end markers and hand each complete frame to the in-process relay, which
browsers read back from ``/devices/{device}/stream.mjpg``.

Auth is a short-lived HMAC token in the query string (minted by
``stream_service`` and delivered to the device inside the ``stream_start`` MQTT
command), so this route is exempt from the human-login middleware.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.core.logging import get_logger
from app.services import stream_relay, stream_service

router = APIRouter(prefix="/api/stream", tags=["stream"])
log = get_logger("stream")

_SOI = b"\xff\xd8"  # JPEG start-of-image
_EOI = b"\xff\xd9"  # JPEG end-of-image
_MAX_BUFFER = 8 * 1024 * 1024  # guard against a runaway/garbage stream


@router.api_route("/{device_uid}/ingest", methods=["PUT", "POST"])
async def ingest(device_uid: str, request: Request):
    token = request.query_params.get("token", "")
    if not stream_service.verify_token(token, device_uid):
        raise HTTPException(status_code=401, detail="Invalid or expired stream token")

    stream_relay.publisher_start(device_uid)
    frames = 0
    buf = bytearray()
    log.info("stream ingest opened for %s", device_uid)
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            buf.extend(chunk)
            while True:
                i = buf.find(_SOI)
                if i < 0:
                    # No frame start yet — keep the tail in case a marker is split.
                    if len(buf) > 2:
                        del buf[:-1]
                    break
                j = buf.find(_EOI, i + 2)
                if j < 0:
                    # Incomplete frame; drop anything before the start marker.
                    if i > 0:
                        del buf[:i]
                    if len(buf) > _MAX_BUFFER:
                        buf.clear()
                    break
                frame = bytes(buf[i:j + 2])
                del buf[:j + 2]
                stream_relay.publish(device_uid, frame)
                frames += 1
    except Exception as e:  # client disconnect or transport error ends the stream
        log.info("stream ingest for %s ended: %s", device_uid, e)
    finally:
        stream_relay.publisher_stop(device_uid)
        log.info("stream ingest closed for %s (%d frames)", device_uid, frames)
    return {"ok": True, "frames": frames}
