#!/usr/bin/env bash
# ICICLE Edge Agent installer — v2.
#
# Drops the v2 agent into /opt/icicle-edge, removes any v1 leftovers
# (current_release.json, current/, releases/, stale device_config.json),
# installs the systemd unit, and forces a fresh enrollment using the
# enrollment.json that shipped in this bundle.
set -euo pipefail

BASE=/opt/icicle-edge
SRC_DIR=$(cd "$(dirname "$0")" && pwd)

echo ">> stopping any running agent…"
sudo systemctl stop icicle-edge-agent 2>/dev/null || true

echo ">> scrubbing v1 leftovers (if present)…"
sudo rm -rf  "$BASE/current"            \
             "$BASE/releases"           \
             "$BASE/current_release.json"

# A stale device_config.json from a previous install would short-circuit
# enrollment, so blow it away. The fresh enrollment.json that ships with
# this bundle will rebuild it.
sudo rm -f   "$BASE/config/device_config.json"

echo ">> creating directories…"
sudo mkdir -p "$BASE/agent"        \
              "$BASE/config"       \
              "$BASE/state"        \
              "$BASE/deployments"  \
              "$BASE/logs"
sudo chmod 775 "$BASE/deployments" "$BASE/state"

echo ">> copying agent code…"
sudo rm -f   "$BASE/agent/"*.py
sudo cp      "$SRC_DIR/agent/"*.py "$BASE/agent/"

echo ">> writing enrollment token…"
sudo cp      "$SRC_DIR/config/enrollment.json" "$BASE/enrollment.json"

echo ">> installing systemd unit…"
sudo cp      "$SRC_DIR/systemd/icicle-edge-agent.service" \
             /etc/systemd/system/icicle-edge-agent.service

echo ">> installing X11 hook (xhost at GUI login for Docker camera access)…"
sudo cp      "$SRC_DIR/x11/99-icicle-docker-xhost.sh" \
             /etc/X11/Xsession.d/99-icicle-docker-xhost
sudo chmod 755 /etc/X11/Xsession.d/99-icicle-docker-xhost

if command -v python3 >/dev/null 2>&1; then
  PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
  echo ">> python3 version: $PYVER"
  echo ">> installing python dependencies (JP4/JP6 compatible pins)…"
  if ! sudo python3 -m pip install -r "$SRC_DIR/requirements.txt"; then
    echo "ERROR: pip install failed — agent will not start until dependencies are installed."
    echo "       Try: sudo python3 -m pip install -r $SRC_DIR/requirements.txt"
    exit 1
  fi
fi

# Best-effort: ensure the GStreamer elements used by live-camera streaming.
#   relay mode    -> jpegenc + souphttpclientsink (plugins-good)
#   mediamtx mode -> x264enc (plugins-ugly) + rtspclientsink (gstreamer1.0-rtsp)
# Non-fatal if it fails.
if command -v apt-get >/dev/null 2>&1; then
  echo ">> ensuring GStreamer streaming plugins (jpegenc/souphttpclientsink + x264enc/rtspclientsink)…"
  sudo apt-get install -y --no-install-recommends \
    gstreamer1.0-plugins-good gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-ugly gstreamer1.0-rtsp >/dev/null 2>&1 || \
    echo "   (skipped — install gstreamer1.0-plugins-good/ugly + gstreamer1.0-rtsp manually for live streaming)"
fi

sudo systemctl daemon-reload
sudo systemctl enable  icicle-edge-agent
sudo systemctl restart icicle-edge-agent
sudo systemctl status  icicle-edge-agent --no-pager || true

echo ""
echo "Installed ICICLE Edge Agent v2 at $BASE"
echo "Tail logs with:  sudo journalctl -u icicle-edge-agent -f"
