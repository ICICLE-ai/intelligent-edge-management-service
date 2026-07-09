"""In-process Motion-JPEG relay buffer.

The device pushes a stream of JPEG frames into the control plane over HTTPS;
this module holds the *latest* frame per device in memory and lets any number of
browser viewers read it back. The video never touches the database or MQTT — it
is a transient fan-out that lives only inside the running web process.

    Jetson  --(HTTP PUT, MJPEG)-->  ingest route  --> publish() --> [_Channel]
    Browser <--(multipart/x-mixed-replace)-- mjpeg view <-- latest()

Because the buffer is per-process, ingest and playback must be handled by the
same worker. The control plane runs a single Uvicorn worker (see the Docker
entrypoint), so this holds. If it is ever scaled to multiple workers, this needs
to move to a shared store (e.g. Redis pub/sub).
"""

from __future__ import annotations

import time
from typing import Dict, Optional


class _Channel:
    __slots__ = ("frame", "seq", "updated", "publishers")

    def __init__(self) -> None:
        self.frame: Optional[bytes] = None
        self.seq: int = 0
        self.updated: float = 0.0
        self.publishers: int = 0


_channels: Dict[str, _Channel] = {}


def _channel(device_uid: str) -> _Channel:
    ch = _channels.get(device_uid)
    if ch is None:
        ch = _Channel()
        _channels[device_uid] = ch
    return ch


def publish(device_uid: str, frame: bytes) -> int:
    """Store the newest JPEG frame for a device. Returns the new sequence no."""
    ch = _channel(device_uid)
    ch.frame = frame
    ch.seq += 1
    ch.updated = time.time()
    return ch.seq


def latest(device_uid: str) -> tuple[int, Optional[bytes], float]:
    """Return ``(seq, frame, updated_ts)`` for a device (seq 0 = nothing yet)."""
    ch = _channels.get(device_uid)
    if ch is None:
        return 0, None, 0.0
    return ch.seq, ch.frame, ch.updated


def is_live(device_uid: str, max_age_seconds: float) -> bool:
    """True if a frame arrived recently enough to consider the device live."""
    ch = _channels.get(device_uid)
    if ch is None or ch.frame is None:
        return False
    return (time.time() - ch.updated) <= max_age_seconds


def publisher_start(device_uid: str) -> None:
    _channel(device_uid).publishers += 1


def publisher_stop(device_uid: str) -> None:
    ch = _channels.get(device_uid)
    if ch is not None:
        ch.publishers = max(0, ch.publishers - 1)
