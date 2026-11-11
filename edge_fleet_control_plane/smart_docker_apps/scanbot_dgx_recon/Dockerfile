# Scanbot on the DGX Spark: job API plus reconstruction worker.
# Build on the Spark or another arm64 machine with CUDA 13. The GB10 GPU is
# compute capability 12.1. Confirm the base image reports that before trusting it:
#   docker buildx build --platform linux/arm64 -t habg21/scanbot-dgx-recon:0.1.1 .
#
# The Orin capture agent is a separate process. It is not built here.
ARG BASE=nvcr.io/nvidia/pytorch:25.10-py3
FROM ${BASE}

ENV DEBIAN_FRONTEND=noninteractive \
    TORCH_CUDA_ARCH_LIST="12.1" \
    TCNN_CUDA_ARCHITECTURES=121 \
    MAX_JOBS=6 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        git rsync openssh-client curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# COLMAP and ffmpeg in their own prefix so they cannot replace the image's PyTorch.
RUN curl -Ls https://micro.mamba.pm/api/micromamba/linux-aarch64/latest | tar -xvj -C /usr/local bin/micromamba \
    && micromamba create -y -p /opt/tools -c conda-forge colmap=3.11.1 ffmpeg=7.1 \
    && micromamba clean -a -y \
    && ln -s /opt/tools/bin/colmap /usr/local/bin/colmap \
    && ln -s /opt/tools/bin/ffmpeg /usr/local/bin/ffmpeg

# Pins match the live Spark: nerfstudio 1.1.5 at 50e0e3c, tiny-cuda-nn built for arch 121.
# --no-build-isolation keeps the base image's torch. scikit-build-core and cmake
# must already be installed: nerfstudio's xatlas package builds in that environment.
RUN python -m pip install scikit-build-core cmake \
    && python -m pip install --no-build-isolation \
        "git+https://github.com/NVlabs/tiny-cuda-nn@3daa6e5b8b8e0fa17971a1a20acfbb3754494b38#subdirectory=bindings/torch" \
    && python -m pip install --no-build-isolation gsplat==1.4.0 \
    && python -m pip install --no-build-isolation \
        "git+https://github.com/nerfstudio-project/nerfstudio.git@50e0e3c70c775e89333256213363badbf074f29d" \
    && python -m pip install requests opencv-python-headless matplotlib scipy imageio-ffmpeg \
    && python -c "import torch; print('torch', torch.__version__, 'cuda', torch.version.cuda)"

RUN useradd -m -s /bin/bash scanbot
ENV HOME=/home/scanbot
WORKDIR /home/scanbot

COPY --chown=scanbot scanbot/ /home/scanbot/ai-testbed/scanbot_dgx/scanbot/
COPY --chown=scanbot scripts/ /home/scanbot/ai-testbed/scripts/
COPY --chown=scanbot entrypoint.sh /entrypoint.sh
RUN printf '%s\n' '# Tools are already on PATH in this image.' > /home/scanbot/ai-testbed/activate.sh \
    && chmod +x /entrypoint.sh /home/scanbot/ai-testbed/scripts/*.sh \
    && chown scanbot:scanbot /home/scanbot/ai-testbed/activate.sh

# The entrypoint starts as root to set up the mounted folders, then drops to scanbot.
VOLUME ["/home/scanbot/scanbot"]
EXPOSE 8800

ENV SCANBOT_PORT=8800 \
    SCANBOT_PULL_HOST=jetson \
    SCANBOT_TRANSFER_MODE=pull \
    RECON_TIMEOUT_H=6 \
    MAX_ITERS=30000

ENTRYPOINT ["/entrypoint.sh"]
