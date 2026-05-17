#!/usr/bin/env bash
set -euo pipefail

# Idempotent SocketCAN bring-up for the Canable, for the active read-only
# OBD + CAN capture mode (docs/can-setup.md). Safe to re-run; safe to run
# after a mid-drive USB re-enumeration to recover the link by hand.
#
#   sudo scripts/canup.sh                 # can0 @ 500000
#   sudo CAN_IFACE=can1 CAN_BITRATE=250000 scripts/canup.sh
#   sudo CAN_LISTEN_ONLY=1 scripts/canup.sh   # passive CAN-only capture
#
# No `restart-ms`: the Canable runs gs_usb/candleLight firmware, whose driver
# has no bus-off restart, so the kernel rejects IFLA_CAN_RESTART_MS and the
# `ip link set type can` call would error. Bus-off / USB re-enumeration
# recovery is handled at the application layer by the recorder's
# ReconnectPolicy (src/collection/can_collector.py). See
# docs/canable-firmware.md.

IFACE="${CAN_IFACE:-can0}"
BITRATE="${CAN_BITRATE:-500000}"
LISTEN_ONLY="${CAN_LISTEN_ONLY:-0}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo scripts/canup.sh" >&2
  exit 1
fi

if ! ip link show "${IFACE}" >/dev/null 2>&1; then
  echo "error: ${IFACE} not present — is the Canable plugged in?" >&2
  echo "  check: ls /sys/class/net | grep can ; dmesg | tail" >&2
  exit 1
fi

type_args=(type can bitrate "${BITRATE}")
if [[ "${LISTEN_ONLY}" == "1" ]]; then
  type_args+=(listen-only on)
fi

ip link set "${IFACE}" down || true
ip link set "${IFACE}" "${type_args[@]}"
ip link set "${IFACE}" up

echo "${IFACE} up:"
ip -details -brief link show "${IFACE}"
