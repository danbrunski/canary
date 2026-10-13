#!/usr/bin/env bash
set -euo pipefail

# First boot of an imaged CANary: give this Pi its own identity
# (scripts/prepare_image.sh removed the reference Pi's). Run once, as root,
# by canary-firstboot.service, before the daemon and the BLE setup service.
#
# - a new control token, in /etc/canary/control.token and canary.env;
# - the hostname canary-XXXX from this Pi's MAC;
# - SSH host keys.
#
# The marker is removed last, so an interrupted run repeats on the next boot.
# CANARY_INSTALL_ROOT and CANARY_NET_DIR are test hooks
# (tests/test_prepare_image.py).

ROOT="${CANARY_INSTALL_ROOT:-}"
CONFIG_DIR="${ROOT}/etc/canary"
ENV_FILE="${CONFIG_DIR}/canary.env"
TOKEN_FILE="${CONFIG_DIR}/control.token"
MARKER="${CONFIG_DIR}/firstboot"
NET_DIR="${CANARY_NET_DIR:-/sys/class/net}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/canary_hostname.sh
source "${SCRIPT_DIR}/lib/canary_hostname.sh"

[[ -f "${MARKER}" ]] || exit 0

if [[ ! -s "${TOKEN_FILE}" ]]; then
  python3 -c 'import secrets; print(secrets.token_urlsafe(32))' >"${TOKEN_FILE}"
  chgrp canary "${TOKEN_FILE}" 2>/dev/null || true
  chmod 0640 "${TOKEN_FILE}"
  echo "generated this Pi's control token"
fi
token="$(cat "${TOKEN_FILE}")"
if grep -q '^CANARY_CONTROL_TOKEN=' "${ENV_FILE}"; then
  sed -i "s|^CANARY_CONTROL_TOKEN=.*|CANARY_CONTROL_TOKEN=${token}|" "${ENV_FILE}"
else
  printf 'CANARY_CONTROL_TOKEN=%s\n' "${token}" >>"${ENV_FILE}"
fi

if name="$(canary_hostname_from_mac "${NET_DIR}")"; then
  if [[ -z "${ROOT}" ]] && command -v hostnamectl >/dev/null 2>&1; then
    hostnamectl set-hostname "${name}"
  else
    printf '%s\n' "${name}" >"${ROOT}/etc/hostname"
  fi
  if [[ -f "${ROOT}/etc/hosts" ]]; then
    sed -i "s/^127\.0\.1\.1[[:space:]].*/127.0.1.1\t${name}/" "${ROOT}/etc/hosts"
  fi
  echo "hostname ${name}"
else
  echo "WARNING: no network MAC found; keeping hostname 'canary'" >&2
fi

if [[ -z "${ROOT}" ]] && ! compgen -G "/etc/ssh/ssh_host_*_key" >/dev/null; then
  ssh-keygen -A
fi

rm -f "${MARKER}"
