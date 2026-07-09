#!/usr/bin/env bash
# Build and push the control plane image for Tapis pods (linux/amd64).
#
# "exec format error" on the pod means the image was built for the wrong CPU
# (e.g. arm64 on Apple Silicon). Always build with --platform linux/amd64.
#
# Usage:
#   export IMAGE=habg21/edge-control-plane:latest
#   PUSH=true ./deploy/build-and-push.sh
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
IMAGE=${IMAGE:-edge-control-plane:latest}
PLATFORM=${PLATFORM:-linux/amd64}

if [[ "${PUSH:-false}" == "true" ]]; then
  echo ">> Building and pushing ${IMAGE} for ${PLATFORM}"
  docker buildx build \
    --platform "${PLATFORM}" \
    --provenance=false \
    --sbom=false \
    -t "${IMAGE}" \
    --push \
    "${ROOT}"
else
  echo ">> Building ${IMAGE} for ${PLATFORM} (local load only)"
  docker buildx build \
    --platform "${PLATFORM}" \
    --provenance=false \
    --sbom=false \
    -t "${IMAGE}" \
    --load \
    "${ROOT}"
fi

echo ""
echo "Done. Restart or recreate the edgecontrolplane pod to pick up the new image."
