"""Deploy-time metadata-policy check between a model card and a device.

Admission compares each declared constraint with the environment that
governs it. It does not inspect the engine binary and does not establish
that an engine loads.

1. The card lists the device's generation as compatible (an empty list
   allows nothing).
2. The device has the camera bus the app needs (``device_capabilities``).
3. If the device has reported its platform, it meets the resource minimums
   declared for that generation: memory, free disk, and an NVIDIA container
   runtime when CUDA is required.
4. Engine / image build target, when declared:
   - GPU compute capability: exact match against the device (the physical GPU).
   - L4T: the image's declared L4T (falling back to a legacy engine L4T) must
     match the device's L4T by dotted prefix.
   - TensorRT: the engine's declared TensorRT must match the runtime that
     loads it. That runtime is the image's declared TensorRT, or the device's
     host TensorRT when the image declares ``host``. An engine and an image
     that declare different TensorRT versions are refused as inconsistent,
     independent of the device.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.services import device_capabilities


def device_platform(device: dict) -> Optional[Dict[str, Any]]:
    raw = device.get("platform_json")
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def version_matches(declared: Optional[str], actual: Optional[str]) -> bool:
    """``declared`` is a dotted prefix: "8.5" matches "8.5.2" but not "8.50"."""
    want = [p for p in str(declared or "").split(".") if p]
    have = [p for p in str(actual or "").split(".") if p]
    return len(have) >= len(want) and have[: len(want)] == want


def _image_runtime(card: dict) -> Dict[str, Optional[str]]:
    """TensorRT / L4T declared for the container that loads the engine."""
    spec = card.get("spec") or card.get("container") or {}
    artifact = card.get("artifact") or {}
    image_trt = (spec.get("image_tensorrt_version") or "").strip() or None
    image_l4t = (spec.get("image_l4t_version") or "").strip() or None
    if not image_l4t:
        image_l4t = (artifact.get("engine_l4t_version") or "").strip() or None
    return {"tensorrt": image_trt, "l4t": image_l4t}


def check(card: dict, device: dict) -> List[str]:
    """Return every reason ``card`` cannot run on ``device`` (empty if compatible)."""
    reasons: List[str] = []
    compat = {c["generation_uid"]: c for c in card.get("compatibility") or []}
    if not compat:
        reasons.append("app declares no compatible device generations")
        return reasons
    rule = compat.get(device.get("generation_uid"))
    if rule is None:
        reasons.append(
            "generation %s is not in the app's compatible generations (%s)"
            % (device.get("generation_uid"), ", ".join(sorted(compat)))
        )
        return reasons

    bus_error = device_capabilities.device_compatible_with_card(device, card)
    if bus_error:
        reasons.append(bus_error)

    platform = device_platform(device)
    if platform:
        min_mem = rule.get("min_memory_mb")
        mem = platform.get("memory_mb")
        if min_mem and mem is not None and mem < min_mem:
            reasons.append("needs %s MB memory, device has %s MB" % (min_mem, mem))
        min_disk = rule.get("min_storage_gb")
        disk = platform.get("disk_free_gb")
        if min_disk and disk is not None and disk < min_disk:
            reasons.append("needs %s GB free disk, device has %.1f GB" % (min_disk, disk))
        if rule.get("requires_cuda") and platform.get("nvidia_runtime") is False:
            reasons.append("needs CUDA, but the NVIDIA container runtime is not available")

    artifact = card.get("artifact") or {}
    want_cc = artifact.get("engine_compute_capability")
    want_trt = artifact.get("engine_tensorrt_version")
    runtime = _image_runtime(card)
    want_l4t = runtime["l4t"]
    image_trt = runtime["tensorrt"]

    # Engine TensorRT must agree with the image's own TensorRT when both are declared.
    if want_trt and image_trt and image_trt.lower() != "host":
        if not version_matches(want_trt, image_trt) and not version_matches(image_trt, want_trt):
            reasons.append(
                "engine built with TensorRT %s, but the image declares TensorRT %s"
                % (want_trt, image_trt)
            )

    needs_device = bool(want_cc or want_l4t or (want_trt and (not image_trt or image_trt.lower() == "host")))
    if not (want_cc or want_trt or want_l4t or image_trt):
        return reasons
    if needs_device and not platform:
        reasons.append(
            "engine is built for a specific GPU/TensorRT, but the device has not reported "
            "its platform (agent v2.2.0 or newer required)"
        )
        return reasons

    if platform:
        have_cc = platform.get("gpu_compute_capability")
        if want_cc and str(want_cc) != str(have_cc or ""):
            reasons.append(
                "engine built for GPU sm %s, device GPU is sm %s" % (want_cc, have_cc or "unknown")
            )
        have_l4t = platform.get("l4t_version")
        if want_l4t and not version_matches(want_l4t, have_l4t):
            reasons.append(
                "image built for L4T %s, device runs L4T %s" % (want_l4t, have_l4t or "unknown")
            )
        if want_trt:
            if image_trt and image_trt.lower() != "host":
                # Compared with the image above; the host version is irrelevant.
                pass
            else:
                have_trt = platform.get("tensorrt_version")
                if not version_matches(want_trt, have_trt):
                    reasons.append(
                        "engine built with TensorRT %s, device has TensorRT %s"
                        % (want_trt, have_trt or "unknown")
                    )
    return reasons
