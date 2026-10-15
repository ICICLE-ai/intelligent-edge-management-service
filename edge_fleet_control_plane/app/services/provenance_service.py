"""Compare the deploy command's declared artifact/image with what the agent observed.

The command payload (``device_commands.payload_json``) is the immutable record of
what IEMS sent. The RUNNING ack is what the device actually started. Seeds often
leave sha256/digest empty; that is UNDECLARED, not a match.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple


MATCHED = "MATCHED"
DRIFT = "DRIFT"
UNDECLARED = "UNDECLARED"
UNKNOWN = "UNKNOWN"


def normalize_digest(value: Optional[str]) -> Optional[str]:
    """Hex of a sha256 digest, ignoring ``sha256:`` and ``repo@sha256:`` prefixes."""
    text = (value or "").strip().lower()
    if "@sha256:" in text:
        text = text.split("@sha256:", 1)[1]
    elif text.startswith("sha256:"):
        text = text[7:]
    return text or None


def declared_from_payload(payload: Optional[dict]) -> Dict[str, Optional[str]]:
    payload = payload or {}
    artifact = payload.get("artifact") or {}
    container = payload.get("container") or {}
    return {
        "artifact_sha256": artifact.get("sha256"),
        "image": container.get("image"),
        "image_digest": container.get("image_digest"),
    }


def declared_from_command(cmd: Optional[dict]) -> Dict[str, Optional[str]]:
    if not cmd:
        return declared_from_payload(None)
    raw = cmd.get("payload_json")
    if isinstance(raw, dict):
        return declared_from_payload(raw)
    try:
        return declared_from_payload(json.loads(raw or "{}"))
    except (TypeError, ValueError):
        return declared_from_payload(None)


def observed_from_ack(payload: Optional[dict]) -> Dict[str, Optional[str]]:
    payload = payload or {}
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        provenance = {}
    return {
        "artifact_sha256": provenance.get("artifact_sha256"),
        "artifact_bytes": provenance.get("artifact_bytes"),
        "image": provenance.get("image"),
        "image_id": provenance.get("image_id"),
        "image_digest": provenance.get("image_digest"),
        "container_id": provenance.get("container_id") or payload.get("container_id"),
    }


def compare(
    *,
    declared_sha256: Optional[str] = None,
    observed_sha256: Optional[str] = None,
    declared_digest: Optional[str] = None,
    observed_digest: Optional[str] = None,
) -> Tuple[str, List[str]]:
    """Return (status, reasons) for one device's observed vs declared hashes."""
    want_sha = normalize_digest(declared_sha256)
    have_sha = normalize_digest(observed_sha256)
    want_img = normalize_digest(declared_digest)
    have_img = normalize_digest(observed_digest)

    if not have_sha and not have_img:
        return UNKNOWN, ["device did not report artifact hash or image digest"]

    reasons: List[str] = []
    compared = 0
    if want_sha and have_sha:
        compared += 1
        if want_sha != have_sha:
            reasons.append("artifact sha256 does not match the command payload")
    if want_img and have_img:
        compared += 1
        if want_img != have_img:
            reasons.append("image digest does not match the command payload")

    if reasons:
        return DRIFT, reasons
    if compared:
        return MATCHED, []
    return UNDECLARED, ["command payload has no sha256 or image digest to verify against"]


def evaluate(cmd: Optional[dict], ack: dict) -> Dict[str, Any]:
    declared = declared_from_command(cmd)
    observed = observed_from_ack(ack)
    status, reasons = compare(
        declared_sha256=declared.get("artifact_sha256"),
        observed_sha256=observed.get("artifact_sha256"),
        declared_digest=declared.get("image_digest"),
        observed_digest=observed.get("image_digest"),
    )
    return {
        "status": status,
        "reasons": reasons,
        "declared": declared,
        "observed": observed,
    }
