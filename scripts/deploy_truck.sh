#!/usr/bin/env bash
set -euo pipefail

# One-command in-vehicle deployment: installs everything that needs root, then
# restarts the daemon so the Pi boots ready for a truck test.
#
#   1. scripts/install_pi.sh — system user, /etc/canary, /data/canary (with
#      its control/ audit/jobs dir, owned by the canary user), control token,
#      can0.service + its udev rule (SocketCAN comes up on boot and after a USB
#      re-enumeration, docs/can-setup.md), and canary.service installed and
#      enabled. Everything this script used to do itself in separate steps
#      (control dir, can0 report, `systemctl enable canary`) is done there.
#   2. scripts/restart_canary.sh — the only way the daemon is restarted. It
#      refuses while a capture or ECU backup is running (a restart silently
#      ends a capture; on 2026-09-21 a deploy ended another session's tuning
#      drive that way). Arguments to this script pass through to it, so
#      `--force` is how ending a capture is made deliberate.
#
# Idempotent — safe to re-run after a `git pull`. Run as root:
#
#   sudo bash scripts/deploy_truck.sh [--force]

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo bash scripts/deploy_truck.sh" >&2
  exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CANARY_USER="${CANARY_USER:-canary}"
TOKEN_FILE="${CANARY_CONTROL_TOKEN_FILE:-/etc/canary/control.token}"

step() { printf '\n=== %s ===\n' "$1"; }

step "1/2 Pi systemd install"
bash "${SCRIPT_DIR}/install_pi.sh"

step "2/2 restart the daemon (refuses while a capture is active)"
restart_rc=0
bash "${SCRIPT_DIR}/restart_canary.sh" "$@" || restart_rc=$?
if [[ "${restart_rc}" -ne 0 ]]; then
  echo "Install finished, but the daemon was NOT restarted (exit ${restart_rc})." >&2
  echo "It keeps running the old code until restart_canary.sh succeeds." >&2
  exit "${restart_rc}"
fi

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
