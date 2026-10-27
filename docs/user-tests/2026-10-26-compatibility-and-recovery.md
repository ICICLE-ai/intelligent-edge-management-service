# User test: deploy-time compatibility and a supported Xavier deploy

| Field | Value |
|---|---|
| Date | 2026-10-26 |
| Tester | Harikesh Byrandurga Gopinath |
| Pull request or issue | not yet in a pull request |
| Commit SHA tested | `e485f49` |
| Overall result | Pass |

## Environment

| Item | Value |
|---|---|
| Where it ran | Tapis pod `edgecontrolplane`, plus a Jetson AGX Xavier on the bench |
| Python | Agent on the device (Python 3.6). Portal image Python is the image default. |
| Browser and viewport | Portal in a desktop browser |
| Image tag (if any) | `habg21/edge-control-plane:v13` (rebuilt and pushed on this tag) |

Device under test: Xavier-FABE-dev-3, generation Jetson AGX Xavier (JetPack 4). After reinstall the portal showed agent `edge-agent-v2.5.1`, L4T 32.5.1, TensorRT 7.1.3.0, GPU sm 7.2.

Automated checks on the same commit: `tests/smoke_test.py`, `tests/provenance_test.py`, and `tests/compatibility_test.py`.

## Scope

Confirm that a device reports its platform, that an app declared for a different GPU is refused from that report, and that an app built for this Xavier still reaches RUNNING.

## Preconditions

- Portal `https://edgecontrolplane.pods.icicleai.tapis.io` running the image above.
- Xavier enrolled, online, and reinstalled from that portal.
- Faux Weed YOLO11 catalog app, and the UNet++ Corn Residue (Live Stream) app with the Xavier generation added for the rejection case only.

## Test cases

| Step | Expected | Actual | Pass/Fail |
|---|---|---|---|
| 1. Open the Xavier device page after reinstall. | Agent `edge-agent-v2.5.1`, device online, GPU no longer unknown. | Online, `edge-agent-v2.5.1`. A later rejection listed sm 7.2, TensorRT 7.1.3.0, L4T 32.5.1. | Pass |
| 2. Deploy Faux Weed YOLO11 to this Xavier. | Deployment reaches RUNNING and a camera tile updates. | Reached RUNNING. Camera 0 showed a live frame. | Pass |

## Edge cases

| Step | Expected | Actual | Pass/Fail |
|---|---|---|---|
| 1. Add the Xavier generation to the Orin UNet++ Corn Residue (Live Stream) app, then deploy that app to the Xavier. | Refused before dispatch, naming the GPU, TensorRT, and L4T mismatches. | Refused: engine built for GPU sm 8.7, device GPU is sm 7.2; TensorRT 10.3 vs 7.1.3.0; L4T 36.4 vs 32.5.1. | Pass |

The Xavier generation was removed from that Orin app after the check.

## Failure cases

| Step | Expected | Actual | Pass/Fail |
|---|---|---|---|
| 1. Deploy Faux Weed with the previous agent, which did not report a compute capability. | Refused, because the engine target cannot be compared. | Refused: needs 8 GB free disk, device has 4 GB; engine built for GPU sm 7.2, device GPU is sm unknown. | Pass |

The disk figure is the minimum stored on the app. The unknown GPU was the old agent. Reinstall cleared the GPU line.

## Regression check

The device stayed online and accepted a later deploy of its own app after the rejection. Stop and delete of earlier failed deploys still completed.

## Evidence

- Device page: Xavier-FABE-dev-3 online, agent `edge-agent-v2.5.1`.
- Rejection toast for UNet++ Corn Residue (Live Stream) with the three platform reasons above.
- Faux Weed camera tile in the RUNNING state.

## Issues found

None.

## Not tested

- A MATCHED and a DRIFT deployment on hardware. Provenance classifications were checked in `tests/provenance_test.py` and the smoke test.
- Reboot recovery and the stale-deploy guard on this Xavier. Those paths are covered by the smoke test.
- Cold and warm container timing. The manual timer was not run in this session.
- On-device agent CPU and inference-impact sampling. That run is still in progress and is not part of this record.
- Orin Nano was not part of this session. A separate check on that board found host TensorRT 10.3 and TensorRT 10.9 inside `habg21/unet-trt-infer-live`. Noted as a product limitation in the 2026-10 release notes.
