#!/usr/bin/env bash
set -euo pipefail

# canary-token: the Pi's control token, for someone SSH'd into the Pi.
#
#   canary-token          print the token, and say where it goes
#   canary-token pair     approve a browser waiting on "Pair this device"
#
# The dashboard locks most panels until a browser holds a control token, and
# the only place it was written down was /etc/canary/control.token, which a
# new owner had no way to know (2026-11-06). install_pi.sh installs this as
# /usr/local/bin/canary-token. Pairing is the better route where it works: the
# browser gets a token of its own, which `canaryctl devices --remove` revokes
# without touching any other device.
#
# The token file is root:canary 0640, so the canary user reads it without sudo.
# CANARY_CONTROL_TOKEN_FILE and CANARY_ENV point elsewhere (tests, other
# layouts), as in install_pi.sh and update_canary.sh.

TOKEN_FILE="${CANARY_CONTROL_TOKEN_FILE:-/etc/canary/control.token}"
ENV_FILE="${CANARY_ENV:-/etc/canary/canary.env}"

usage() {
  cat <<'EOF'
Usage: canary-token [pair [--yes]]

  canary-token          Print this Pi's control token. Paste it into the
                        dashboard: click the "control token" pill in the top
                        bar (on a phone: the menu, under "Control token").
  canary-token pair     Approve a browser waiting to pair. Press "Pair this
                        device" in the dashboard first; the browser then gets
                        its own token and nothing needs copying.
EOF
}

read_token() {
  if [[ ! -e "${TOKEN_FILE}" ]]; then
    echo "canary-token: ${TOKEN_FILE} does not exist; run: sudo scripts/install_pi.sh" >&2
    exit 1
  fi
  if [[ ! -r "${TOKEN_FILE}" ]]; then
    echo "canary-token: cannot read ${TOKEN_FILE} as $(id -un); it is readable by the" >&2
    echo "canary group. Log in as canary, or run: sudo canary-token" >&2
    exit 1
  fi
  local token
  token="$(tr -d '[:space:]' <"${TOKEN_FILE}")"
  if [[ -z "${token}" ]]; then
    echo "canary-token: ${TOKEN_FILE} is empty" >&2
    exit 1
  fi
  printf '%s' "${token}"
}

case "${1:-}" in
  "")
    token="$(read_token)"
    # The token alone on stdout, so $(canary-token) and pipes get just it; the
    # explanation goes to the terminal only.
    printf '%s\n' "${token}"
    if [[ -t 1 ]]; then
      cat >&2 <<'EOF'

Paste it into the dashboard: click the "control token" pill in the top bar
(on a phone: the menu, under "Control token"). The browser remembers it.
From a laptop shell: export CANARY_CONTROL_TOKEN=<the token above>
Or skip the copying: press "Pair this device" in the dashboard, then run
  canary-token pair
EOF
    fi
    ;;
  pair)
    shift
    token="$(read_token)"
    home="${CANARY_HOME:-}"
    if [[ -z "${home}" && -r "${ENV_FILE}" ]]; then
      home="$(sed -n 's/^CANARY_HOME=//p' "${ENV_FILE}" | tail -n 1)"
    fi
    home="${home:-/opt/canary}"
    python="${home}/.venv/bin/python"
    if [[ ! -x "${python}" ]]; then
      echo "canary-token: no CANary venv at ${python}" >&2
      exit 1
    fi
    CANARY_CONTROL_TOKEN="${token}" exec "${python}" "${home}/scripts/canaryctl.py" \
      --host "${CANARY_HOST:-http://localhost:8000}" pair "$@"
    ;;
  -h | --help | help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
