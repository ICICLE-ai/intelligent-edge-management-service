#!/usr/bin/env bash
# Long-running Scanbot service for the DGX Spark.
# Writes config.toml from the environment, then runs the job server and the
# reconstruction worker. The Orin capture agent is not in this image. It polls
# this server. Logs go to stdout. If either process exits, the container exits
# so the agent can restart it.
set -euo pipefail

APP="/home/scanbot/ai-testbed/scanbot_dgx"
DATA="/home/scanbot/scanbot"

# Starts as root only to set up the mounted folders, then runs as scanbot.
# The host folders may be owned by anyone (a fresh /srv/scanbot is root's, and
# the SSH folder belongs to the station user), so fix ownership on our copies.
if [ "$(id -u)" = 0 ]; then
  mkdir -p "$DATA/jobs" "$DATA/data" "$DATA/logs"
  chown scanbot:scanbot "$DATA" "$DATA/jobs" "$DATA/data" "$DATA/logs"
  if [ -d /home/scanbot/.ssh-mount ]; then
    rm -rf /home/scanbot/.ssh
    mkdir -p /home/scanbot/.ssh
    cp -R /home/scanbot/.ssh-mount/. /home/scanbot/.ssh/
    # Keys and config from the station user must belong to scanbot or ssh refuses them.
    rm -f /home/scanbot/.ssh/authorized_keys
    # An ssh config may name keys by the station user's absolute path.
    if [ -n "${SCANBOT_SSH_DIR:-}" ] && [ -f /home/scanbot/.ssh/config ]; then
      sed -i "s#${SCANBOT_SSH_DIR%/}#/home/scanbot/.ssh#g" /home/scanbot/.ssh/config
    fi
    chown -R scanbot:scanbot /home/scanbot/.ssh
    chmod 700 /home/scanbot/.ssh
    find /home/scanbot/.ssh -type f -exec chmod 600 {} +
  fi
  exec setpriv --reuid=scanbot --regid=scanbot --init-groups env HOME=/home/scanbot "$0" "$@"
fi

: "${SCANBOT_TOKEN:?Set the scan token in the station settings on the portal. It is the shared secret for the job API.}"

SCANBOT_PORT="${SCANBOT_PORT:-8800}"
SCANBOT_PULL_HOST="${SCANBOT_PULL_HOST:-jetson}"
SCANBOT_TRANSFER_MODE="${SCANBOT_TRANSFER_MODE:-pull}"
RECON_TIMEOUT_H="${RECON_TIMEOUT_H:-6}"
MAX_ITERS="${MAX_ITERS:-30000}"

mkdir -p "$DATA/jobs" "$DATA/data" "$DATA/logs"

umask 077
cat > "$APP/config.toml" <<EOF
[server]
url = "http://127.0.0.1:${SCANBOT_PORT}"
host = "0.0.0.0"
port = ${SCANBOT_PORT}
token = "${SCANBOT_TOKEN}"
jobs_dir = "${DATA}/jobs"

[transfer]
mode = "${SCANBOT_TRANSFER_MODE}"

[worker]
data_dir = "${DATA}/data"
pull_host = "${SCANBOT_PULL_HOST}"
recon_cmd = "$HOME/ai-testbed/scripts/recon_turntable.sh"
recon_timeout_h = ${RECON_TIMEOUT_H}
poll_s = 10
progress_s = 15
bundle_exclude = ["*.db"]
EOF
umask 022
export MAX_ITERS

python - <<'PY'
import torch
assert torch.cuda.is_available(), "No GPU visible. Deploy with the NVIDIA runtime and --gpus all."
print("GPU:", torch.cuda.get_device_name(0), "compute", tuple(torch.cuda.get_device_capability(0)), "torch", torch.__version__)
PY

# Pull mode: the worker rsyncs frames from the Orin with the key and ssh config
# copied from the station's SSH folder above (Host must match SCANBOT_PULL_HOST).
if [ "$SCANBOT_TRANSFER_MODE" = pull ] && ! ssh -o BatchMode=yes -o ConnectTimeout=5 "$SCANBOT_PULL_HOST" true 2>/tmp/ssh_check.err; then
  echo "WARNING: cannot ssh to ${SCANBOT_PULL_HOST} without a password; frame copies will fail:" >&2
  cat /tmp/ssh_check.err >&2
fi

cd "$APP"
python -u -m scanbot.server --config "$APP/config.toml" &
server_pid=$!
sleep 2
python -u -m scanbot.worker --config "$APP/config.toml" &
worker_pid=$!
echo "scanbot DGX service up: job API on :${SCANBOT_PORT}, pull host ${SCANBOT_PULL_HOST}"

trap 'kill "$server_pid" "$worker_pid" 2>/dev/null || true; wait' TERM INT
wait -n "$server_pid" "$worker_pid"
echo "a scanbot process exited; stopping the container" >&2
kill "$server_pid" "$worker_pid" 2>/dev/null || true
exit 1
