#!/usr/bin/env bash
set -euo pipefail
sudo systemctl stop icicle-edge-agent || true
sudo systemctl disable icicle-edge-agent || true
sudo rm -f /etc/systemd/system/icicle-edge-agent.service
sudo systemctl daemon-reload
sudo rm -rf /opt/icicle-edge
