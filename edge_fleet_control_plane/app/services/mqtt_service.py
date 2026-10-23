"""Thin MQTT publish wrapper.

Implementation details:
* TLS is on by default, plain TCP is allowed for self-hosted brokers.
* ``publish`` opens a connection for a single message.
* ``publisher()`` opens one connection for a batch. Group deploys send one
  message per device; reconnecting (TLS handshake + login) for each cost
  ~1.4 s per device against the Tapis broker, so batches share a connection
  and wait for all PUBACKs together.
"""

from __future__ import annotations

import json
import socket
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator

from app.config import get_settings
from app.core.logging import get_logger

try:
    import paho.mqtt.client as mqtt
except Exception:  # pragma: no cover
    mqtt = None

log = get_logger("mqtt")


def topic_for(target_type: str, target_uid: str) -> str:
    settings = get_settings()
    base = settings.mqtt.base_topic.rstrip("/")
    if target_type == "GROUP":
        return f"{base}/commands/device-group/{target_uid}"
    if target_type == "DEVICE":
        return f"{base}/commands/device/{target_uid}"
    if target_type == "GENERATION":
        return f"{base}/commands/generation/{target_uid}"
    raise ValueError(f"Unknown target_type {target_type}")


class Publisher:
    """One open broker connection. ``send`` queues, ``confirm`` waits for the PUBACK."""

    def __init__(self, client: "mqtt.Client") -> None:
        self._client = client

    def send(self, topic: str, payload: Dict[str, Any], qos: int = 1):
        return self._client.publish(topic, json.dumps(payload), qos=qos)

    @staticmethod
    def confirm(info, timeout: float = 10) -> None:
        info.wait_for_publish(timeout=timeout)
        if not info.is_published():
            raise RuntimeError(f"MQTT publish not acknowledged within {timeout:g}s")


@contextmanager
def publisher() -> Iterator[Publisher]:
    if mqtt is None:
        raise RuntimeError("paho-mqtt is not installed")
    settings = get_settings()
    mc = settings.mqtt
    if not mc.host:
        raise RuntimeError("MQTT host not configured")
    client_id = f"{mc.client_id_prefix}-{uuid.uuid4().hex[:8]}"
    client = mqtt.Client(client_id=client_id, callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    if mc.tls:
        client.tls_set()
    if mc.username:
        client.username_pw_set(mc.username, mc.password)
    try:
        client.connect(mc.host, mc.port, keepalive=30)
    except (OSError, socket.error) as e:
        raise RuntimeError(f"MQTT connect failed: {e}") from e
    client.loop_start()
    try:
        yield Publisher(client)
    finally:
        # disconnect() first wakes the network thread; loop_stop() alone waits out its 1 s select.
        client.disconnect()
        client.loop_stop()


def publish(topic: str, payload: Dict[str, Any], qos: int = 1) -> None:
    with publisher() as pub:
        pub.confirm(pub.send(topic, payload, qos))
