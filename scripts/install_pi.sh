#!/usr/bin/env bash
set -euo pipefail

# Idempotent Raspberry Pi install helper for an already-cloned CANary repo.
#
# Defaults are intentionally runtime-light: base Python dependencies only and
# no Torch. Set CANARY_TORCH=cpu if you want LSTM inference on the Pi.

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo scripts/install_pi.sh" >&2
  exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

CANARY_USER="canary"
CANARY_GROUP="canary"
CANARY_HOME="${CANARY_HOME:-${REPO_ROOT}}"
CANARY_CONFIG_DIR="${CANARY_CONFIG_DIR:-/etc/canary}"
CANARY_CONFIG="${CANARY_CONFIG:-${CANARY_CONFIG_DIR}/canary.yaml}"
CANARY_ENV="${CANARY_ENV:-${CANARY_CONFIG_DIR}/canary.env}"
CANARY_CONTROL_TOKEN_FILE="${CANARY_CONTROL_TOKEN_FILE:-${CANARY_CONFIG_DIR}/control.token}"
CANARY_DATA_DIR="${CANARY_DATA_DIR:-/data/canary}"
CANARY_PYTHON="${CANARY_PYTHON:-python3}"
CANARY_EXTRAS="${CANARY_EXTRAS:-none}"
CANARY_TORCH="${CANARY_TORCH:-none}"
CANARY_SERVICE="${CANARY_SERVICE:-/etc/systemd/system/canary.service}"

if [[ "${CANARY_HOME}" != "${REPO_ROOT}" ]]; then
  echo "CANARY_HOME must point at this clone for the in-place install." >&2
  echo "  CANARY_HOME=${CANARY_HOME}" >&2
  echo "  clone=${REPO_ROOT}" >&2
  exit 1
fi

if ! command -v "${CANARY_PYTHON}" >/dev/null 2>&1; then
  echo "Python executable not found: ${CANARY_PYTHON}" >&2
  exit 1
fi

if ! getent group "${CANARY_GROUP}" >/dev/null 2>&1; then
  groupadd --system "${CANARY_GROUP}"
fi

if ! id -u "${CANARY_USER}" >/dev/null 2>&1; then
  useradd --system --gid "${CANARY_GROUP}" --home-dir "${CANARY_HOME}" --create-home \
    --shell /usr/sbin/nologin "${CANARY_USER}"
fi

install -d -m 0755 "${CANARY_CONFIG_DIR}"
install -d -m 0755 "${CANARY_DATA_DIR}" "${CANARY_DATA_DIR}/sessions" "${CANARY_DATA_DIR}/anomalies"
# canary-mcp writes its audit log here as user ``canary``; pre-create so
# ControlAuditLogger doesn't try to ``mkdir`` under a path it can't reach.
install -d -m 0750 "${CANARY_DATA_DIR}/audit"

chown -R "${CANARY_USER}:${CANARY_GROUP}" "${CANARY_HOME}" "${CANARY_DATA_DIR}"

if [[ ! -f "${CANARY_CONFIG}" ]]; then
  install -m 0644 "${CANARY_HOME}/canary.pi.example.yaml" "${CANARY_CONFIG}"
  echo "Created ${CANARY_CONFIG}; edit it before starting the service."
else
  echo "Keeping existing ${CANARY_CONFIG}."
fi

if [[ ! -f "${CANARY_CONTROL_TOKEN_FILE}" ]]; then
  "${CANARY_PYTHON}" -c 'import secrets; print(secrets.token_urlsafe(32))' \
    >"${CANARY_CONTROL_TOKEN_FILE}"
  chown root:"${CANARY_GROUP}" "${CANARY_CONTROL_TOKEN_FILE}"
  chmod 0640 "${CANARY_CONTROL_TOKEN_FILE}"
  echo "Created ${CANARY_CONTROL_TOKEN_FILE}; export it as CANARY_CONTROL_TOKEN from your laptop."
else
  echo "Keeping existing ${CANARY_CONTROL_TOKEN_FILE}."
fi

cat >"${CANARY_ENV}" <<EOF
CANARY_HOME=${CANARY_HOME}
CANARY_CONFIG=${CANARY_CONFIG}
CANARY_DATA_DIR=${CANARY_DATA_DIR}
CANARY_CONTROL_TOKEN=$(cat "${CANARY_CONTROL_TOKEN_FILE}")
CANARY_MCP_AUDIT_PATH=${CANARY_DATA_DIR}/audit/mcp.jsonl
EOF
chown root:"${CANARY_GROUP}" "${CANARY_ENV}"
chmod 0640 "${CANARY_ENV}"

install -m 0644 "${CANARY_HOME}/systemd/canary.service" "${CANARY_SERVICE}"
install -m 0644 "${CANARY_HOME}/systemd/canary-mcp.service" \
  "$(dirname "${CANARY_SERVICE}")/canary-mcp.service"
# UPS power-loss auto-shutdown watchdog (Geekworm X1202). Independent of the
# canary daemon; self-disarms when no UPS is present. Runs on system python3
# (uses lgpio), not the venv.
install -m 0644 "${CANARY_HOME}/systemd/ups-shutdown.service" \
  "$(dirname "${CANARY_SERVICE}")/ups-shutdown.service"
# Load i2c-dev at boot so /dev/i2c-1 (X1202 fuel gauge at 0x36) exists.
install -m 0644 "${CANARY_HOME}/systemd/canary-modules.conf" \
  /etc/modules-load.d/canary.conf
modprobe i2c-dev 2>/dev/null || true

# /run/canary — shared runtime dir between the canary daemon and the root
# ups-shutdown watchdog (UPS keep-alive request + status heartbeat). tmpfs
# (/run), so systemd-tmpfiles must recreate it every boot.
install -m 0644 "${CANARY_HOME}/systemd/tmpfiles-canary.conf" \
  /etc/tmpfiles.d/canary.conf
systemd-tmpfiles --create /etc/tmpfiles.d/canary.conf

# Let the canary user restart the services without a password, so model/code
# deploys (and the MCP control plane) can roll the daemon without an interactive
# sudo. Scoped to exactly the restart/try-restart verbs for these two units —
# nothing else. Validated with `visudo -c`; a malformed sudoers file would lock
# out sudo, so we discard it on failure.
SYSTEMCTL="$(command -v systemctl)"
CANARY_SUDOERS="/etc/sudoers.d/canary-restart"
cat >"${CANARY_SUDOERS}" <<EOF
${CANARY_USER} ALL=(root) NOPASSWD: ${SYSTEMCTL} restart canary.service, ${SYSTEMCTL} restart canary, ${SYSTEMCTL} restart canary-mcp.service, ${SYSTEMCTL} restart canary-mcp, ${SYSTEMCTL} try-restart canary.service, ${SYSTEMCTL} try-restart canary-mcp.service
EOF
chmod 0440 "${CANARY_SUDOERS}"
if ! visudo -cf "${CANARY_SUDOERS}" >/dev/null; then
  echo "WARNING: generated ${CANARY_SUDOERS} failed validation; removing it" >&2
  rm -f "${CANARY_SUDOERS}"
fi

# ---------------------------------------------------------------------------
# GPS + time sync stack (gpsd feeds both canary and chrony)
# ---------------------------------------------------------------------------
# The Pi has no battery-backed RTC, so a cold boot away from the network comes
# up with a stale clock and telemetry gets stamped wrong — then jumps mid-
# session when NTP finally lands. gpsd fixes that without any network: it owns
# the receiver's serial port, publishes GPS time into a SHM segment that chrony
# consumes as a refclock, and serves fixes to canary over its socket.
#
# A serial device has one owner, so this is all-or-nothing: with gpsd running,
# canary must be configured `gps.source: gpsd` (see docs/time-sync.md).
# Set CANARY_INSTALL_GPS_STACK=0 to skip (e.g. a Pi with no GPS attached).
CANARY_INSTALL_GPS_STACK="${CANARY_INSTALL_GPS_STACK:-1}"
CANARY_GPS_DEVICE="${CANARY_GPS_DEVICE:-}"

if [[ "${CANARY_INSTALL_GPS_STACK}" == "1" ]] && command -v apt-get >/dev/null 2>&1; then
  echo "Installing GPS + time-sync stack (gpsd, chrony)..."
  DEBIAN_FRONTEND=noninteractive apt-get install -y gpsd gpsd-clients chrony

  # Autodetect the receiver when not told. The by-id symlink is stable across
  # re-enumeration; /dev/ttyUSB0 is not, and gpsd holds this path for the life
  # of the service, so the unstable name is a real failure mode.
  if [[ -z "${CANARY_GPS_DEVICE}" ]]; then
    mapfile -t _gps_candidates < <(ls /dev/serial/by-id/* 2>/dev/null || true)
    if [[ "${#_gps_candidates[@]}" -eq 1 ]]; then
      CANARY_GPS_DEVICE="${_gps_candidates[0]}"
      echo "  Autodetected GPS device: ${CANARY_GPS_DEVICE}"
    elif [[ "${#_gps_candidates[@]}" -gt 1 ]]; then
      echo "  Multiple serial devices found; not guessing which is the GPS." >&2
      printf '    %s\n' "${_gps_candidates[@]}" >&2
      echo "  Re-run with CANARY_GPS_DEVICE=/dev/serial/by-id/... to configure gpsd." >&2
    else
      echo "  No serial device found; leaving gpsd unconfigured." >&2
    fi
  fi

  if [[ -n "${CANARY_GPS_DEVICE}" ]]; then
    # -n is load-bearing: without it gpsd won't open the receiver until a
    # client connects, so chrony would have no time source during early boot —
    # exactly when the clock is wrong and canary is deciding whether to record.
    #
    # USBAUTO stays false: it's a global "grab every matching USB serial
    # device" switch, and /dev/ttyUSB* is shared with the OBD adapter here.
    # Hotplug is handled precisely instead, by the udev rule below.
    #
    # DEVICES is deliberately EMPTY, and the udev rule is the only thing that
    # ever hands the receiver to gpsd. Listing it here as well raced: gpsd
    # tracks devices by the *string* it was given, so the static by-id entry
    # and the hotplug entry (/dev/ttyUSBn) are two identities for one port,
    # and whichever open()s second dies with EBUSY —
    #   SER: device open of /dev/ttyUSB0 failed: Device or resource busy(16)
    #   /dev/ttyUSB0: device activation failed, freeing device.
    # Which side lost varied per plug event, so GPS came up intermittently.
    # One device, one owner.
    cat >/etc/default/gpsd <<EOF
# Managed by canary scripts/install_pi.sh
# DEVICES intentionally empty — 70-canary-gps.rules owns device attachment.
START_DAEMON="true"
USBAUTO="false"
DEVICES=""
GPSD_OPTIONS="-n"
EOF

    # Hotplug: gpsd opens its device list once at startup and, if a node isn't
    # there yet, logs "device activation failed, freeing device" and never
    # retries. So a receiver plugged in after boot stayed dark until someone
    # restarted gpsd by hand. This rule matches that one receiver and pulls in
    # canary-gpsd@.service, which attaches it via gpsdctl — so plug order stops
    # mattering. At boot, systemd-udev-trigger replays add events for
    # already-present devices, so the plugged-in-at-boot case runs through the
    # same single path.
    #
    # Matching on SYMLINK (rather than ID_SERIAL) keeps CANARY_GPS_DEVICE the
    # single source of truth and needs no parsing, and — the reason it matters
    # here — works even when the receiver is absent at install time, which is
    # precisely the case this rule exists to cover. 70- runs after 60-serial's
    # by-id symlink is in place.
    install -m 0644 "${CANARY_HOME}/systemd/canary-gpsd@.service" \
      "$(dirname "${CANARY_SERVICE}")/canary-gpsd@.service"
    cat >/etc/udev/rules.d/70-canary-gps.rules <<EOF
# Managed by canary scripts/install_pi.sh — see systemd/canary-gpsd@.service
ACTION=="add", SUBSYSTEM=="tty", SYMLINK=="${CANARY_GPS_DEVICE#/dev/}", TAG+="systemd", ENV{SYSTEMD_WANTS}+="canary-gpsd@%k.service"
EOF
    systemctl daemon-reload
    udevadm control --reload || true

    systemctl enable gpsd.socket gpsd.service >/dev/null 2>&1 || true
    systemctl restart gpsd.socket gpsd.service || true
    # Catch the already-plugged-in case: replay the add event so the rule runs
    # for a receiver that was attached before the rule existed.
    udevadm trigger --subsystem-match=tty --action=add || true
  fi

  # chrony config goes in a drop-in, never by rewriting chrony.conf — that file
  # is the admin's and may carry local NTP policy we shouldn't clobber.
  install -d -m 0755 /etc/chrony/conf.d
  cat >/etc/chrony/conf.d/canary-gps.conf <<'EOF'
# Managed by canary scripts/install_pi.sh — GPS-disciplined offline time.
#
# gpsd publishes GPS time to SHM unit 0. NMEA time has no PPS pulse to align
# to, so it arrives a few hundred ms late — receiver- and baud-dependent (this
# one measured ~350 ms). precision must be honest about that: claiming 1e-1
# makes chrony flag GPS as a falseticker against network NTP and refuse to use
# it. 5e-1 covers the real lag and is still three orders of magnitude tighter
# than the hours of drift being corrected, and far tighter than telemetry needs.
refclock SHM 0 refid GPS precision 5e-1 offset 0.0

# Step the clock by ANY offset, at ANY point in the session — not just the
# first few updates. The failure being fixed is a large correction landing
# mid-drive and fracturing a session file; slewing a 65-minute error would
# take days, so a step is the correct and safer behavior here. Nothing on
# this box is safety-critical or ordering-sensitive w.r.t. wall clock.
makestep 1 -1
EOF

  # Debian's chrony.conf pulls in conf.d by default; older configs may not.
  if [[ -f /etc/chrony/chrony.conf ]] && ! grep -qE '^\s*confdir\s+.*conf\.d' /etc/chrony/chrony.conf; then
    echo "confdir /etc/chrony/conf.d" >>/etc/chrony/chrony.conf
    echo "  Added confdir include to /etc/chrony/chrony.conf"
  fi

  systemctl enable chrony >/dev/null 2>&1 || true
  systemctl restart chrony || true
  echo "  Set 'hardware.gps.source: gpsd' in ${CANARY_CONFIG} to use it."
fi

cd "${CANARY_HOME}"
runuser -u "${CANARY_USER}" -- "${CANARY_PYTHON}" scripts/install_deps.py \
  --venv "${CANARY_HOME}/.venv" \
  --extras "${CANARY_EXTRAS}" \
  --torch "${CANARY_TORCH}"

systemctl daemon-reload
systemctl enable canary.service
systemctl enable canary-mcp.service
systemctl enable ups-shutdown.service

cat <<EOF
CANary Pi install complete.

Next:
  1. Edit ${CANARY_CONFIG}
  2. Bring up can0 for your chosen OBD/CAN mode; see docs/can-setup.md
  3. Start with: sudo systemctl start canary canary-mcp
  4. Watch logs: sudo journalctl -u canary -f  (or -u canary-mcp -f)
  5. Export the laptop token: export CANARY_CONTROL_TOKEN="$(cat "${CANARY_CONTROL_TOKEN_FILE}")"
  6. Open: http://<pi-hostname-or-ip>:8000/status
  7. MCP server: http://<pi-hostname-or-ip>:8001/mcp (see docs/mcp-server.md)
  8. GPS/time: verify with 'gpspipe -w -n 5' and 'chronyc sources' — the GPS
     refclock should appear. See docs/time-sync.md
EOF
