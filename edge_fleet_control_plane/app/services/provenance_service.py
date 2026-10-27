"""Compare the deploy command's declared artifact/image with what the agent observed.

The command payload (``device_commands.payload_json``) is the immutable record of
what IEMS sent. The RUNNING ack is what the device actually started. Two fields
are comparable: the engine file's sha256 and the container image digest.

Decision table, applied in this order:

=========================================================  ==========
Case                                                       Status
=========================================================  ==========
Device reported neither a file hash nor an image digest    UNKNOWN
Some declared/observed pair is well formed and differs     DRIFT
A digest on either side is malformed                       INVALID
Command declares neither field                             UNDECLARED
Declared fields exist but none was observed (no overlap)   UNVERIFIED
Some declared fields matched, at least one went unobserved PARTIAL
Every declared field was observed and equal                MATCHED
=========================================================  ==========

MATCHED therefore always means complete verification of what was declared,
backed by at least one comparison. A proven mismatch outranks a malformed
value elsewhere in the record.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple


MATCHED = "MATCHED"
PARTIAL = "PARTIAL"
DRIFT = "DRIFT"
INVALID = "INVALID"
UNVERIFIED = "UNVERIFIED"
UNDECLARED = "UNDECLARED"
UNKNOWN = "UNKNOWN"

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def normalize_digest(value: Optional[str]) -> Optional[str]:
    """Hex of a sha256 digest, ignoring ``sha256:`` and ``repo@sha256:`` prefixes."""
    text = (value or "").strip().lower()
    if "@sha256:" in text:
        text = text.split("@sha256:", 1)[1]
    elif text.startswith("sha256:"):
        text = text[7:]
    return text or None


def is_valid_digest(hex_value: Optional[str]) -> bool:
    return bool(hex_value) and bool(_SHA256_HEX.match(hex_value))


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
    fields = (
        ("artifact sha256", normalize_digest(declared_sha256), normalize_digest(observed_sha256)),
        ("image digest", normalize_digest(declared_digest), normalize_digest(observed_digest)),
    )

    if not any(have for _, _, have in fields):
        return UNKNOWN, ["device did not report artifact hash or image digest"]

    drift: List[str] = []
    malformed: List[str] = []
    unobserved: List[str] = []
    matched = 0
    for name, want, have in fields:
        for side, value in (("declared", want), ("observed", have)):
            if value and not is_valid_digest(value):
                malformed.append("%s %s is not a sha256 digest" % (side, name))
        if not want:
            continue
        if not have:
            unobserved.append("%s was declared but not observed" % name)
            continue
        if not (is_valid_digest(want) and is_valid_digest(have)):
            continue
        if want == have:
            matched += 1
        else:
            drift.append("%s does not match the command payload" % name)

    if drift:
        return DRIFT, drift + malformed
    if malformed:
        return INVALID, malformed
    if not any(want for _, want, _ in fields):
        return UNDECLARED, ["command payload has no sha256 or image digest to verify against"]
    if not matched:
        return UNVERIFIED, unobserved
    if unobserved:
        return PARTIAL, unobserved
    return MATCHED, []


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
