#!/usr/bin/env bash
set -euo pipefail

# Restart the CANary daemon on the Pi -- unless a capture is running.
#
# Capture state is deliberately ephemeral: a restarted daemon comes back on
# the `default` profile, so restarting mid-capture silently ends the capture
# and the rest of the drive is recorded without it. On 2026-09-21 one agent
# deployed an update four minutes into another agent's tuning drive; the
# capture died, nothing reported it, and the drive could not be repeated that
# day. Every agent restarting the daemon goes through here so that cannot
# happen by accident.
#
#   scripts/restart_canary.sh            refuse if a capture is active
#   scripts/restart_canary.sh --check    check whether a restart is safe
#   scripts/restart_canary.sh --force    restart anyway (says what it killed)

STATUS_URL="${CANARY_STATUS_URL:-http://localhost:8000/status}"
FORCE=0
CHECK_ONLY=0
for arg in "$@"; do
  case "${arg}" in
    --force) FORCE=1 ;;
    --check) CHECK_ONLY=1 ;;
    *)
      echo "usage: $0 [--check] [--force]" >&2
      exit 2
      ;;
  esac
done

active="$(curl -fsS --max-time 5 "${STATUS_URL}" 2>/dev/null | python3 -c '
import json, sys
try:
    profile = (json.load(sys.stdin).get("capture_profile") or {})
except Exception:
    sys.exit(0)
if profile.get("active"):
    print(profile.get("name") or "unknown", profile.get("session_dir") or "", sep="\t")
' || true)"

if [[ -n "${active}" ]]; then
  name="${active%%$'\t'*}"
  session="${active#*$'\t'}"
  if [[ "${FORCE}" -ne 1 ]]; then
    echo "refusing: capture '${name}' is active (${session})." >&2
    echo "A restart ends it silently. Stop it first (DELETE /control/capture)," >&2
    echo "wait for it to finish, or pass --force if ending it is intended." >&2
    exit 3
  fi
  echo "WARNING: --force given; this ends capture '${name}' (${session})." >&2
fi

if [[ "${CHECK_ONLY}" -eq 1 ]]; then
  if [[ -n "${active}" ]]; then
    echo "restart permitted by --force"
  else
    echo "restart safe: no capture is active"
  fi
  exit 0
fi

sudo systemctl restart canary.service
echo "canary restarted"
