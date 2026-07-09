"""Thin MQTT publish wrapper.

Implementation details:
* TLS is on by default, plain TCP is allowed for self-hosted brokers.
* We open a connection per publish for now. The control plane publishes
  infrequently (one message per fleet command) so this is a reasonable tradeoff
  for simplicity. A long-lived client can be introduced later behind the same
  function signature.
"""

from __future__ import annotations

import json
import socket
import uuid
from typing import Any, Dict

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


def publish(topic: str, payload: Dict[str, Any], qos: int = 1) -> None:
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
        info = client.publish(topic, json.dumps(payload), qos=qos)
        info.wait_for_publish(timeout=10)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"MQTT publish failed (rc={info.rc})")
    finally:
        client.loop_stop()
        client.disconnect()
