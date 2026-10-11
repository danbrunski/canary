#!/usr/bin/env bash
set -euo pipefail

# One-command in-vehicle deployment. Composes the pieces that need root into
# a single idempotent run so the Pi boots ready for a truck test:
#
#   1. scripts/install_pi.sh — system user, /etc/canary, /data/canary,
#      control token, canary.service (installed + enabled).
#   2. /data/canary/control — audit/jobs dir the control plane writes to.
#   3. can0 auto-recovery — systemd/can0.service + the udev rule, so the
#      SocketCAN link comes up on boot and after a USB re-enumeration
#      without a human (docs/can-setup.md).
#   4. Enable + start the daemon; print how to reach it from a phone/laptop.
#
# Idempotent — safe to re-run after a `git pull`. Run as root:
#
#   sudo bash scripts/deploy_truck.sh

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo bash scripts/deploy_truck.sh" >&2
  exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
CANARY_USER="${CANARY_USER:-canary}"
CANARY_GROUP="${CANARY_GROUP:-canary}"
DATA_DIR="${CANARY_DATA_DIR:-/data/canary}"
TOKEN_FILE="${CANARY_CONTROL_TOKEN_FILE:-/etc/canary/control.token}"

step() { printf '\n=== %s ===\n' "$1"; }

step "1/4 Pi systemd install"
bash "${SCRIPT_DIR}/install_pi.sh"

step "2/4 control-plane data dir"
install -d -o "${CANARY_USER}" -g "${CANARY_GROUP}" -m 0750 "${DATA_DIR}/control"
echo "ensured ${DATA_DIR}/control"

step "3/4 can0 auto-recovery"
# install_pi.sh (step 1) installs and enables can0.service and its udev rule
# since 2026-10-11; this step only reports it.
systemctl is-enabled can0.service >/dev/null 2>&1 \
  && echo "can0.service enabled (starts when can0 appears)" \
  || echo "WARNING: can0.service is not enabled" >&2

step "4/4 start the daemon"
systemctl enable canary.service
systemctl restart canary.service

ip_addr="$(hostname -I 2>/dev/null | awk '{print $1}')"
host_name="$(hostname)"
cat <<EOF

=== CANary truck deployment complete ===

Service:   sudo systemctl status canary --no-pager
Logs:      sudo journalctl -u canary -f
Reach it:  http://${ip_addr:-<pi-ip>}:8000/   (or http://${host_name}.local:8000/)

From your phone/laptop on the same network:
  export CANARY_HOST=http://${ip_addr:-<pi-ip>}:8000
  export CANARY_CONTROL_TOKEN="\$(ssh ${CANARY_USER}@${host_name}.local sudo cat ${TOKEN_FILE})"
  scripts/canaryctl.py preflight

When the Canable is plugged into the OBD-II port, can0 auto-comes-up.
Verify: ip -details -brief link show can0   (expect: UP, can <bitrate>)
EOF
