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
# An armed passive mode is named in the output (it survives the restart on a
# daemon that persists it, D03.30), never refused.
#
# An ECU backup (W08.7) is refused like a capture: it holds the ECM session
# and pauses all polling for ~10 minutes, and a restart throws the read away.
# It shows as the `rom_backup` holder in /status passive.ecm_session_claims.
# With --force the daemon cancels it on the way down (the ECM is sent back to
# its default session and the end event is recorded).
#
#   scripts/restart_canary.sh            refuse if a capture or backup is active
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

status_json="$(curl -fsS --max-time 5 "${STATUS_URL}" 2>/dev/null || true)"

# One line per fact: `capture<TAB>name<TAB>session_dir` for an active capture,
# `backup<TAB>holder` for an ECU backup holding the ECM session,
# `passive<TAB>source<TAB>ttl_left<TAB>survives_restart` for an armed passive.
facts="$(printf '%s' "${status_json}" | python3 -c '
import json, sys
try:
    status = json.load(sys.stdin)
except Exception:
    sys.exit(0)
profile = status.get("capture_profile") or {}
if profile.get("active"):
    print("capture", profile.get("name") or "unknown", profile.get("session_dir") or "", sep="\t")
passive = status.get("passive") or {}
if "rom_backup" in (passive.get("ecm_session_claims") or {}):
    print("backup", "rom_backup", sep="\t")
if passive.get("enabled"):
    ttl = passive.get("ttl_remaining_seconds")
    print(
        "passive",
        passive.get("source") or "unknown",
        "none" if ttl is None else f"{float(ttl):.0f}",
        "yes" if passive.get("survives_restart") else "no",
        sep="\t",
    )
' || true)"

active="$(printf '%s\n' "${facts}" | sed -n 's/^capture\t//p')"
backup="$(printf '%s\n' "${facts}" | sed -n 's/^backup\t//p')"
passive="$(printf '%s\n' "${facts}" | sed -n 's/^passive\t//p')"

# Passive mode (D03.30) is named, never refused: an armed passive is the
# operator's scan tool or reflash tool on the bus, and a restart that brings
# CANary back transmitting can collide with it. A daemon that persists the arm
# restores it before its first frame; an older one does not.
if [[ -n "${passive}" ]]; then
  IFS=$'\t' read -r p_source p_ttl p_persisted <<<"${passive}"
  if [[ "${p_ttl}" == "none" ]]; then
    p_left="no TTL"
  else
    p_left="${p_ttl} s of TTL left"
  fi
  if [[ "${p_persisted}" == "yes" ]]; then
    echo "note: passive mode is armed (source ${p_source}, ${p_left}); it is persisted and the restarted daemon restores it before transmitting."
  else
    echo "WARNING: passive mode is armed (source ${p_source}, ${p_left}) but this daemon does not persist it:" >&2
    echo "the restarted daemon comes up TRANSMITTING. Re-arm it immediately (POST /control/passive)" >&2
    echo "or wait until the other tool is off the bus." >&2
  fi
fi

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

if [[ -n "${backup}" ]]; then
  if [[ "${FORCE}" -ne 1 ]]; then
    echo "refusing: an ECU backup (ROM read) is running; it holds the ECM session." >&2
    echo "A restart throws the read away. Wait for it to finish (GET /control/rom/backup)," >&2
    echo "cancel it (POST /control/rom/backup/cancel), or pass --force if ending it is intended." >&2
    exit 3
  fi
  echo "WARNING: --force given; this ends the running ECU backup (ROM read). The daemon cancels" >&2
  echo "it on the way down: the ECM is sent back to its default session and nothing is stored." >&2
fi

if [[ "${CHECK_ONLY}" -eq 1 ]]; then
  if [[ -n "${active}" || -n "${backup}" ]]; then
    echo "restart permitted by --force"
  else
    echo "restart safe: no capture or ECU backup is active"
  fi
  exit 0
fi

sudo systemctl restart canary.service
echo "canary restarted"
