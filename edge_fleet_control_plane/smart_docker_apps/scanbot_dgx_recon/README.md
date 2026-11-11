# Scanbot DGX service

Long-running reconstruction service for one NVIDIA DGX Spark. IEMS deploys this container and leaves it running. The container is the job queue and the GPU worker. It does not start once per ear.

The Jetson Orin is not an IEMS device in this setup. Its existing `scanbot.agent` polls this server, turns the turntable, records with the GoPro, and leaves frames for the worker to rsync.

## What the container runs

- `scanbot.server` on port 8800. LAN only. Do not publish that port on the public internet.
- `scanbot.worker`, which pulls frames from the Orin and runs `scripts/recon_turntable.sh` (masks, one COLMAP pass, nerfacto, preview videos).

The Corral bridge is not in this image. Scans are submitted to the job API.

## Before the first deploy

On the Spark, as root:

```bash
mkdir -p /srv/scanbot /srv/scanbot-ssh
# /srv/scanbot-ssh needs a private key and an ssh config with a Host entry
# whose name matches SCANBOT_PULL_HOST (default: jetson).
```

The Orin SSH daemon must accept that key, and the path it serves must be the agent's work directory. Set `SCANBOT_TOKEN` on the model card to the same secret the Orin agent uses. The card ships with an empty token, and the container exits until that value is set.

Enroll the Spark as generation `dgx-spark-gb10-v1` and install an agent that reports `edge-agent-v2.7.0` or newer. Older agents try to download a weights file this service does not have.

## Build

Build on the Spark. The base tag must be an arm64 PyTorch image with CUDA 13 whose `torch.cuda.get_device_capability()` is `(12, 1)`. `25.10-py3` is the starting guess, not a verified tag.

```bash
docker buildx build --platform linux/arm64 \
  -t habg21/scanbot-dgx-recon:0.1.1 \
  -f Dockerfile .
```

Check the GPU inside the image before pushing:

```bash
docker run --rm --gpus all habg21/scanbot-dgx-recon:0.1.1 \
  python -c "import torch; print(torch.cuda.get_device_capability())"
```

That command will fail the entrypoint's token check. Run it by overriding the entrypoint:

```bash
docker run --rm --gpus all --entrypoint python habg21/scanbot-dgx-recon:0.1.1 \
  -c "import torch; print(torch.cuda.get_device_capability(), torch.__version__)"
```
