#!/usr/bin/env bash
set -euo pipefail

# Idempotent Raspberry Pi install helper for an already-cloned CANary repo.
#
# Defaults are intentionally runtime-light: base Python dependencies only and
# no Torch. Set CANARY_TORCH=cpu if you want LSTM inference on the Pi.
#
# Re-running it is the normal update path (canary-update calls it), so it
# restarts gpsd, chrony and journald only when the file it writes for that
# service changed — a restart of gpsd drops the daemon's GPS for a reconnect
# cycle, on every update, for nothing (S02.4).
#
# Test hooks only (tests/test_install_pi_script.py): CANARY_INSTALL_ROOT puts
# every system path this script writes under a scratch directory, and
# CANARY_INSTALL_ALLOW_UNPRIVILEGED=1 lifts the root check. Neither is for use
# on a Pi.

if [[ "${EUID}" -ne 0 && "${CANARY_INSTALL_ALLOW_UNPRIVILEGED:-0}" != "1" ]]; then
  echo "Run with sudo: sudo scripts/install_pi.sh" >&2
  exit 1
fi

ROOT="${CANARY_INSTALL_ROOT:-}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

CANARY_USER="canary"
CANARY_GROUP="canary"
CANARY_HOME="${CANARY_HOME:-${REPO_ROOT}}"
CANARY_CONFIG_DIR="${CANARY_CONFIG_DIR:-${ROOT}/etc/canary}"
CANARY_CONFIG="${CANARY_CONFIG:-${CANARY_CONFIG_DIR}/canary.yaml}"
CANARY_ENV="${CANARY_ENV:-${CANARY_CONFIG_DIR}/canary.env}"
CANARY_CONTROL_TOKEN_FILE="${CANARY_CONTROL_TOKEN_FILE:-${CANARY_CONFIG_DIR}/control.token}"
CANARY_DATA_DIR="${CANARY_DATA_DIR:-${ROOT}/data/canary}"
CANARY_PYTHON="${CANARY_PYTHON:-python3}"
CANARY_EXTRAS="${CANARY_EXTRAS:-none}"
CANARY_TORCH="${CANARY_TORCH:-none}"
CANARY_SERVICE="${CANARY_SERVICE:-${ROOT}/etc/systemd/system/canary.service}"

# Services whose managed config this run wrote (new or different content).
declare -A CONFIG_CHANGED=()

# write_managed <service> <dest>: install stdin at <dest> (mode 0644) only when
# it differs from what is already there, and mark <service> for a restart. An
# absent <dest> — the first install — counts as a change.
write_managed() {
  local service="$1" dest="$2" staged
  staged="$(mktemp)"
  cat >"${staged}"
  if [[ -f "${dest}" ]] && cmp -s "${staged}" "${dest}"; then
    rm -f "${staged}"
    return 0
  fi
  install -m 0644 "${staged}" "${dest}"
  rm -f "${staged}"
  echo "  wrote ${dest}"
  CONFIG_CHANGED["${service}"]=1
}

# restart_if_changed <service> <unit...>: restart the units only when
# write_managed changed <service>'s config; otherwise say so, and `start` them
# (a no-op for a running unit) so a stopped service still comes up. Returns 1
# when nothing was restarted, so callers can skip follow-up work.
restart_if_changed() {
  local service="$1"
  shift
  if [[ -n "${CONFIG_CHANGED[${service}]:-}" ]]; then
    echo "changed: ${service} config; restarting ${service}"
    systemctl restart "$@" || true
    return 0
  fi
  echo "unchanged: ${service} not restarted"
  systemctl start "$@" || true
  return 1
}

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
install -d -m 0755 "${CANARY_DATA_DIR}" "${CANARY_DATA_DIR}/sessions" "${CANARY_DATA_DIR}/anomalies" \
  "${CANARY_DATA_DIR}/control" "${CANARY_DATA_DIR}/models"
# canary-mcp writes its audit log here as user ``canary``; pre-create so
# ControlAuditLogger doesn't try to ``mkdir`` under a path it can't reach.
install -d -m 0750 "${CANARY_DATA_DIR}/audit"

chown -R "${CANARY_USER}:${CANARY_GROUP}" "${CANARY_HOME}" "${CANARY_DATA_DIR}"

# A first install is one with no daemon config yet. It decides the hostname
# below; a re-run (canary-update) never renames a Pi.
FIRST_INSTALL=0
[[ -f "${CANARY_CONFIG}" ]] || FIRST_INSTALL=1

if [[ ! -f "${CANARY_CONFIG}" ]]; then
  install -m 0644 "${CANARY_HOME}/canary.pi.example.yaml" "${CANARY_CONFIG}"
  echo "Created ${CANARY_CONFIG}; edit it before starting the service."
else
  echo "Keeping existing ${CANARY_CONFIG}."
fi
# The daemon config is the canary user's, like everything else the
# application touches: editing it, or migrating channel names in it
# (tools/migrate_channel_names.py writes via temp file + rename, which needs
# the directory), must not need root. Re-applied on every run, so an install
# that predates this is fixed too. The secrets below stay root-owned and
# group-readable: the daemon reads them, nothing in userspace rewrites them.
chown "${CANARY_USER}:${CANARY_GROUP}" "${CANARY_CONFIG_DIR}" "${CANARY_CONFIG}"

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
# can0 at the configured bitrate on every boot and every re-appearance of the
# device (HAT driver probe, Canable re-enumeration). It lived only in
# deploy_truck.sh, so a Pi installed by docs/pi-setup.md had can0 up until its
# first reboot and then a daemon with no bus (2026-10-11). Enabled, not
# started: the unit BindsTo the device and fails without one.
install -m 0644 "${CANARY_HOME}/systemd/can0.service" "$(dirname "${CANARY_SERVICE}")/can0.service"
install -m 0644 "${CANARY_HOME}/systemd/70-canable-can0.rules" \
  "${ROOT}/etc/udev/rules.d/70-canable-can0.rules"
# UPS power-loss auto-shutdown watchdog (Geekworm X1202). Independent of the
# canary daemon; self-disarms when no UPS is present. Runs on system python3
# (uses lgpio), not the venv.
install -m 0644 "${CANARY_HOME}/systemd/ups-shutdown.service" \
  "$(dirname "${CANARY_SERVICE}")/ups-shutdown.service"
# Load i2c-dev at boot so /dev/i2c-1 (X1202 fuel gauge at 0x36) exists.
install -m 0644 "${CANARY_HOME}/systemd/canary-modules.conf" \
  "${ROOT}/etc/modules-load.d/canary.conf"
modprobe i2c-dev 2>/dev/null || true

# /run/canary — shared runtime dir between the canary daemon and the root
# ups-shutdown watchdog (UPS keep-alive request + status heartbeat). tmpfs
# (/run), so systemd-tmpfiles must recreate it every boot.
install -m 0644 "${CANARY_HOME}/systemd/tmpfiles-canary.conf" \
  "${ROOT}/etc/tmpfiles.d/canary.conf"
systemd-tmpfiles --create "${ROOT}/etc/tmpfiles.d/canary.conf"

# ---------------------------------------------------------------------------
# Hostname: canary-<last four hex digits of the board's MAC>
# ---------------------------------------------------------------------------
# Every CANary answering as `canary.local` made two of them on one network
# race for the name: mDNS renamed the loser `canary-2.local`, so the name
# meant whichever booted first (2026-10-11, a second Pi on the dev LAN). A
# suffix from the hardware is unique without coordination and stable across
# reinstalls, the usual appliance convention. Only on a first install, and
# only over a generic name (the image's `raspberrypi`, or a bare `canary`): a
# name someone chose is theirs. CANARY_HOSTNAME=<name> sets one explicitly on
# any run. Runs before cloud-init is disabled below, which matters: on a
# later boot cloud-init would put the Imager's hostname back.
HOSTNAME_FILE="${ROOT}/etc/hostname"
HOSTS_FILE="${ROOT}/etc/hosts"
NET_DIR="${CANARY_NET_DIR:-/sys/class/net}"
current_hostname="$(tr -d '[:space:]' <"${HOSTNAME_FILE}" 2>/dev/null || true)"
canary_hostname=""
if [[ -n "${CANARY_HOSTNAME:-}" ]]; then
  canary_hostname="${CANARY_HOSTNAME}"
elif [[ "${FIRST_INSTALL}" -eq 1 && "${current_hostname}" =~ ^(raspberrypi|canary|localhost)?$ ]]; then
  # The wired MAC: printed nowhere, but fixed in the SoC's OTP on a Pi, and
  # the Wi-Fi MAC is derived from it.
  mac=""
  if [[ -r "${NET_DIR}/eth0/address" ]]; then
    mac="$(cat "${NET_DIR}/eth0/address")"
  else
    for addr in "${NET_DIR}"/*/address; do
      [[ "${addr}" == */lo/address || ! -r "${addr}" ]] && continue
      mac="$(cat "${addr}")"
      break
    done
  fi
  mac="${mac//:/}"
  if [[ "${mac}" =~ ^[0-9a-fA-F]{12}$ ]]; then
    canary_hostname="canary-$(tr '[:upper:]' '[:lower:]' <<<"${mac: -4}")"
  else
    echo "WARNING: no network MAC found; keeping hostname '${current_hostname}'" >&2
  fi
fi
if [[ -n "${canary_hostname}" && "${canary_hostname}" != "${current_hostname}" ]]; then
  if [[ ! "${canary_hostname}" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]]; then
    echo "REFUSING hostname '${canary_hostname}': lowercase letters, digits and inner hyphens only." >&2
    exit 1
  fi
  # /etc/hosts first: between the two, sudo cannot resolve its own host.
  if [[ -f "${HOSTS_FILE}" ]] && grep -q '^127\.0\.1\.1[[:space:]]' "${HOSTS_FILE}"; then
    sed -i "s/^127\.0\.1\.1[[:space:]].*/127.0.1.1\t${canary_hostname}/" "${HOSTS_FILE}"
  else
    printf '127.0.1.1\t%s\n' "${canary_hostname}" >>"${HOSTS_FILE}"
  fi
  if [[ -z "${ROOT}" ]] && command -v hostnamectl >/dev/null 2>&1; then
    hostnamectl set-hostname "${canary_hostname}"
  else
    printf '%s\n' "${canary_hostname}" >"${HOSTNAME_FILE}"
  fi
  echo "Hostname: ${current_hostname:-<none>} -> ${canary_hostname} (reach it at ${canary_hostname}.local)"
  current_hostname="${canary_hostname}"
fi

# Stable operator command. It discovers the active checkout through
# /etc/canary/canary.env, so callers do not need to cd into the repository.
install -m 0755 "${CANARY_HOME}/scripts/update_canary.sh" "${ROOT}/usr/local/sbin/canary-update"

# Let the canary user restart the services without a password, so model/code
# deploys (and the MCP control plane) can roll the daemon without an interactive
# sudo. Scoped to exactly the restart/try-restart verbs for these two units —
# nothing else. Validated with `visudo -c`; a malformed sudoers file would lock
# out sudo, so we discard it on failure.
SYSTEMCTL="$(command -v systemctl)"
CANARY_SUDOERS="${ROOT}/etc/sudoers.d/canary-restart"
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
# Is a CANary-HAT fitted? The firmware reads its ID EEPROM at boot and exposes
# the product string here; several steps below differ for it.
HAT_PRODUCT_FILE="${CANARY_HAT_PRODUCT_FILE:-/proc/device-tree/hat/product}"
hat_product=""
[[ -r "${HAT_PRODUCT_FILE}" ]] && hat_product="$(tr -d '\0' <"${HAT_PRODUCT_FILE}")"

# The HAT's K-line transceiver is on uart0 (GPIO14/15). A kernel console there
# would turn a held-low K-line (every wake-up pulse) into a serial break, which
# with magic SysRq can reboot the Pi (docs/CANary-HAT-Spec.md §11 step 1). On
# the Pi 5 the console is the debug UART: `serial0` stays ttyAMA10 even with
# uart0 enabled (checked on the HAT 2026-09-23). Refuse a console that names
# uart0 rather than trust that nobody set one.
if [[ "${hat_product}" == CANary-HAT* ]]; then
  CMDLINE_FOR_CONSOLE="${CANARY_CMDLINE:-${ROOT}/boot/firmware/cmdline.txt}"
  SERIAL_DEV_DIR="${CANARY_SERIAL_DEV_DIR:-/dev}"
  if [[ -f "${CMDLINE_FOR_CONSOLE}" ]]; then
    for console in $(tr ' ' '\n' <"${CMDLINE_FOR_CONSOLE}" | sed -n 's/^console=\([^,]*\).*/\1/p'); do
      target="${console}"
      if [[ "${console}" == serial* && -L "${SERIAL_DEV_DIR}/${console}" ]]; then
        target="$(readlink "${SERIAL_DEV_DIR}/${console}")"
      fi
      if [[ "${target##*/}" == "ttyAMA0" ]]; then
        echo "REFUSING: console=${console} is uart0 (${target}), the CANary-HAT's K-line." >&2
        echo "  A wake-up pulse on K-line would reach that console as a break (SysRq)." >&2
        echo "  Remove it from ${CMDLINE_FOR_CONSOLE} (the debug UART is ttyAMA10)." >&2
        exit 1
      fi
    done
  fi
fi

CANARY_INSTALL_GPS_STACK="${CANARY_INSTALL_GPS_STACK:-1}"
CANARY_GPS_DEVICE="${CANARY_GPS_DEVICE:-}"
GPS_STACK_INSTALLED=0

if [[ "${CANARY_INSTALL_GPS_STACK}" == "1" ]] && command -v apt-get >/dev/null 2>&1; then
  echo "Installing GPS + time-sync stack (gpsd, chrony)..."
  GPS_STACK_INSTALLED=1
  # gpsd itself carries gpsctl, ubxtool and ntpshmmon, all this stack calls.
  # Not gpsd-clients: on trixie it *depends* on GTK and matplotlib (xgps,
  # gpsplot), 138 packages and 384 MB on a fresh card (2026-10-11), for
  # gpspipe alone. gpsd-tools is the ncurses cgps/gpsmon, where it exists.
  _gps_pkgs=(gpsd chrony)
  apt-cache show gpsd-tools >/dev/null 2>&1 && _gps_pkgs+=(gpsd-tools)
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${_gps_pkgs[@]}"

  # A CANary-HAT carries its own GNSS receiver on uart2 (/dev/ttyAMA2, from the
  # board overlay its ID EEPROM names). When the firmware reports the HAT, that
  # receiver is the GPS — the antenna is on the board — whatever USB serial
  # devices are also plugged in. An explicit CANARY_GPS_DEVICE still wins.
  if [[ -z "${CANARY_GPS_DEVICE}" && "${hat_product}" == CANary-HAT* ]]; then
    CANARY_GPS_DEVICE="/dev/ttyAMA2"
    echo "  CANary-HAT detected; GPS device: ${CANARY_GPS_DEVICE}"
  fi

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
    write_managed gpsd "${ROOT}/etc/default/gpsd" <<EOF
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
    #
    # A board UART (the HAT's /dev/ttyAMA2) has no by-id symlink: udev never
    # lists it under SYMLINK, so a SYMLINK match would silently never fire.
    # Match those on their kernel name, which a device-tree UART keeps.
    if [[ "${CANARY_GPS_DEVICE}" == /dev/serial/* ]]; then
      GPS_MATCH="SYMLINK==\"${CANARY_GPS_DEVICE#/dev/}\""
    else
      GPS_MATCH="KERNEL==\"${CANARY_GPS_DEVICE#/dev/}\""
    fi
    write_managed gpsd "$(dirname "${CANARY_SERVICE}")/canary-gpsd@.service" \
      <"${CANARY_HOME}/systemd/canary-gpsd@.service"
    write_managed gpsd "${ROOT}/etc/udev/rules.d/70-canary-gps.rules" <<EOF
# Managed by canary scripts/install_pi.sh — see systemd/canary-gpsd@.service
ACTION=="add", SUBSYSTEM=="tty", ${GPS_MATCH}, TAG+="systemd", ENV{SYSTEMD_WANTS}+="canary-gpsd@%k.service"
EOF
    # PartOf=gpsd.service carries a gpsd *stop* and *restart* to the attach
    # unit, but not a fresh *start*. A package upgrade stops gpsd and starts it
    # again, so on 2026-10-03 an `apt upgrade` left gpsd running with no
    # receiver and GPS dark until a reboot. Replaying the receiver's add event
    # after every gpsd start pulls the attach unit back in through the same
    # udev path as boot and hotplug (a no-op while it is already active).
    UNIT_DIR="$(dirname "${CANARY_SERVICE}")"
    install -d -m 0755 "${UNIT_DIR}/gpsd.service.d"
    write_managed gpsd "${UNIT_DIR}/gpsd.service.d/canary-reattach.conf" <<EOF
# Managed by canary scripts/install_pi.sh — re-attach the receiver after any gpsd start.
[Service]
ExecStartPost=-/usr/bin/udevadm trigger --action=add ${CANARY_GPS_DEVICE}
EOF
    # The HAT's MAX-M10M powers up at 9600 baud, and at 9600 one second of its
    # NMEA (14 GSV lines for 28 satellites, plus GGA/RMC/GSA/GBS/ZDA) does not
    # fit: it reported `$GNTXT txbuf alloc` and its time arrived ~14 s late and
    # growing (2026-10-04), which would have stepped the clock 14 s on a boot
    # without network. 115200 brought the lag to ~65 ms. The receiver has no
    # backup cell, so the speed is lost at every power cut and is set on every
    # attach, through gpsd, which follows the port to the new speed. gpsctl
    # refuses until gpsd has identified the receiver, hence the retries. It can
    # also hang for good, ignoring SIGTERM: after a gpsd stop/start on
    # 2026-10-04 one try sat for minutes, and a oneshot unit has no start
    # timeout, so the loop never reached its next try. Each try is killed at
    # 20 s, which bounds the loop at ~3.5 min.
    if [[ -n "${hat_product}" && "${hat_product}" == CANary-HAT* \
          && "${CANARY_GPS_DEVICE}" == /dev/ttyAMA* ]]; then
      _gps_unit="canary-gpsd@${CANARY_GPS_DEVICE#/dev/}.service"
      install -d -m 0755 "${UNIT_DIR}/${_gps_unit}.d"
      write_managed gpsd "${UNIT_DIR}/${_gps_unit}.d/canary-baud.conf" <<EOF
# Managed by canary scripts/install_pi.sh — HAT GNSS at ${CANARY_HAT_GPS_BAUD:-115200} baud on every attach.
[Service]
ExecStartPost=-/bin/sh -c 'for i in 1 2 3 4 5 6 7 8 9 10; do /usr/bin/timeout -s KILL 20 /usr/bin/gpsctl -s ${CANARY_HAT_GPS_BAUD:-115200} ${CANARY_GPS_DEVICE} && exit 0; sleep 1; done; echo "could not set ${CANARY_GPS_DEVICE} to ${CANARY_HAT_GPS_BAUD:-115200} baud" >&2; exit 1'
EOF
    fi
    systemctl daemon-reload
    udevadm control --reload || true

    systemctl enable gpsd.socket gpsd.service >/dev/null 2>&1 || true
    # Only when one of the three files above changed (or this is the first
    # install): a gpsd restart costs the daemon a GPS reconnect cycle.
    if restart_if_changed gpsd gpsd.socket gpsd.service; then
      # Catch the already-plugged-in case: replay the add event so the rule
      # runs for a receiver that was attached before the rule existed.
      udevadm trigger --subsystem-match=tty --action=add || true
    fi
  fi

  # chrony config goes in a drop-in, never by rewriting chrony.conf — that file
  # is the admin's and may carry local NTP policy we shouldn't clobber.
  install -d -m 0755 "${ROOT}/etc/chrony/conf.d"
  # A CANary-HAT also brings the receiver's TIMEPULSE (pps-gpio on GPIO18, a
  # /dev/ppsN whose name reads `canary-pps.-1`). Its number depends on probe
  # order (the Ethernet PHY's PTP clock and gpsd's tty line discipline are
  # other /dev/pps devices), so a udev rule gives it a stable name.
  #
  # chrony reads it directly as `refclock PPS`, but never from a static line:
  # chronyd exits fatally when a configured PPS device does not exist, and on
  # 2026-09-23 a rule that never fired took all time sync down. So the line is
  # generated at every chrony start, only when /dev/pps-canary exists
  # (chrony.service.d/canary-pps.conf), as /run/canary-chrony-pps.conf, which
  # canary-gps.conf pulls in with a *glob* include. chrony 4.6.1, checked on
  # the Pi: a glob matching no file is ignored, but a missing plain include is
  # fatal, and so is a glob whose directory does not exist. Hence a glob
  # directly in /run, which always exists. A PPS that appears after chrony started (udev race at boot)
  # pulls in canary-chrony-pps.service, which restarts chrony once if the
  # generated file lacks the line.
  #
  # Replaced 2026-10-04: a SOCK refclock fed by gpsd (canary-gpsd-pps.service
  # attaching /dev/pps-canary to gpsd). gpsd never wrote a pulse for a
  # standalone PPS device, and attaching it took SHM unit 0 whenever it beat
  # the receiver, which moved the receiver's time to unit 1, where chrony does
  # not look. Both are removed below.
  #
  # The PPS can only be numbered (`lock GPS`) when the GPS refclock's own
  # error is well under half a second, so the HAT's GPS line states the lag
  # actually measured: 62–68 ms at 115200 baud (2026-10-04), steady to < 1 ms.
  # With `precision 5e-1` chrony dropped every pulse.
  CHRONY_GPS_LINE='refclock SHM 0 refid GPS precision 5e-1 offset 0.0'
  CHRONY_PPS_LINE=""
  UNIT_DIR="$(dirname "${CANARY_SERVICE}")"
  for _old in "${UNIT_DIR}/canary-gpsd-pps.service" "${UNIT_DIR}/gpsd.service.d/canary-reattach-pps.conf"; do
    if [[ -e "${_old}" ]]; then
      [[ "${_old}" == *canary-gpsd-pps.service ]] && { systemctl stop canary-gpsd-pps.service || true; }
      rm -f "${_old}"
      echo "  removed ${_old}"
      CONFIG_CHANGED[chrony]=1
    fi
  done
  if [[ "${hat_product:-}" == CANary-HAT* ]]; then
    CHRONY_GPS_LINE='refclock SHM 0 refid GPS precision 1e-1 offset 0.065'
    write_managed chrony "${UNIT_DIR}/canary-chrony-pps.service" \
      <"${CANARY_HOME}/systemd/canary-chrony-pps.service"
    install -d -m 0755 "${UNIT_DIR}/chrony.service.d"
    write_managed chrony "${UNIT_DIR}/chrony.service.d/canary-pps.conf" <<'EOF'
# Managed by canary scripts/install_pi.sh — offer chrony the HAT PPS only when it exists (H03.3).
[Service]
ExecStartPre=+/bin/sh -c 'if [ -e /dev/pps-canary ]; then echo "refclock PPS /dev/pps-canary refid PPS lock GPS" >/run/canary-chrony-pps.conf; else rm -f /run/canary-chrony-pps.conf; fi'
EOF
    write_managed chrony "${ROOT}/etc/udev/rules.d/70-canary-pps.rules" <<'EOF'
# Managed by canary scripts/install_pi.sh — stable name for the CANary-HAT's GNSS PPS;
# canary-chrony-pps.service restarts chrony if it started before the device existed (H03.3).
SUBSYSTEM=="pps", ATTR{name}=="canary-pps*", SYMLINK+="pps-canary", TAG+="systemd", ENV{SYSTEMD_WANTS}+="canary-chrony-pps.service"
EOF
    CHRONY_PPS_LINE='
# CANary-HAT TIMEPULSE: its PPS refclock line is generated at chrony start
# only when /dev/pps-canary exists (see chrony.service.d/canary-pps.conf). A
# glob, so no generated file is not an error.
include /run/canary-chrony-*.conf'
    if [[ -n "${CONFIG_CHANGED[chrony]:-}" ]]; then
      systemctl daemon-reload
      udevadm control --reload || true
    fi
  fi

  write_managed chrony "${ROOT}/etc/chrony/conf.d/canary-gps.conf" < <(
    cat <<'EOF'
# Managed by canary scripts/install_pi.sh — GPS-disciplined offline time.
#
# gpsd publishes GPS time to SHM unit 0. NMEA time has no PPS pulse to align
# to, so it arrives a few hundred ms late — receiver- and baud-dependent (this
# one measured ~350 ms). precision must be honest about that: claiming 1e-1
# makes chrony flag GPS as a falseticker against network NTP and refuse to use
# it. 5e-1 covers the real lag and is still three orders of magnitude tighter
# than the hours of drift being corrected, and far tighter than telemetry needs.
EOF
    printf '%s\n' "${CHRONY_GPS_LINE}"
    cat <<'EOF'

# Step the clock by ANY offset, at ANY point in the session — not just the
# first few updates. The failure being fixed is a large correction landing
# mid-drive and fracturing a session file; slewing a 65-minute error would
# take days, so a step is the correct and safer behavior here. Nothing on
# this box is safety-critical or ordering-sensitive w.r.t. wall clock.
makestep 1 -1
EOF
    [[ -n "${CHRONY_PPS_LINE}" ]] && printf '%s\n' "${CHRONY_PPS_LINE}"
  )

  # Debian's chrony.conf pulls in conf.d by default; older configs may not.
  CHRONY_CONF="${ROOT}/etc/chrony/chrony.conf"
  if [[ -f "${CHRONY_CONF}" ]] && ! grep -qE '^\s*confdir\s+.*conf\.d' "${CHRONY_CONF}"; then
    echo "confdir /etc/chrony/conf.d" >>"${CHRONY_CONF}"
    echo "  Added confdir include to ${CHRONY_CONF}"
    CONFIG_CHANGED[chrony]=1
  fi

  systemctl enable chrony >/dev/null 2>&1 || true
  restart_if_changed chrony chrony || true
fi

# ---------------------------------------------------------------------------
# Persistent journal
# ---------------------------------------------------------------------------
# Default journald storage is volatile on a stock Pi image: the journal lives
# in /run and is discarded at every boot. That is exactly backwards for this
# box — the failures worth reading are the ones that happen in the vehicle,
# and the trip home is a power cycle. Without this, any in-vehicle incident is
# already unreadable by the time the Pi reaches the bench.
#
# A drop-in, not an edit to journald.conf — same reasoning as the chrony
# config above: that file belongs to the admin.
#
# The `90-` prefix is load-bearing. Raspberry Pi OS ships
# /usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf to spare the
# SD card, and drop-ins are applied in filename order across all search
# directories — so beating the vendor default is a matter of sorting after
# `40-`, not of living in /etc. This deliberately trades a bounded amount of
# card wear for being able to read what happened during a drive; the size
# caps below are what keep that trade small.
install -d -m 0755 "${ROOT}/etc/systemd/journald.conf.d"
write_managed systemd-journald "${ROOT}/etc/systemd/journald.conf.d/90-canary-persistent.conf" <<'EOF'
# Managed by canary scripts/install_pi.sh — survive the key-off power cycle.
[Journal]
Storage=persistent
# Bounded so months of 1 Hz telemetry logging can't fill the card. 200 MB
# holds several weeks of normal daemon chatter at this log volume.
SystemMaxUse=200M
SystemMaxFileSize=20M
MaxRetentionSec=1month
EOF

# systemd creates /var/log/journal itself on restart, but doing it through
# tmpfiles gets the root:systemd-journal 2755 ownership right up front.
install -d -m 0755 "${ROOT}/var/log/journal"
systemd-tmpfiles --create --prefix "${ROOT}/var/log/journal" >/dev/null 2>&1 || true
restart_if_changed systemd-journald systemd-journald || true
# Restarting journald is not enough: it keeps writing to /run until told to
# migrate. That handoff is systemd-journal-flush's job and normally only
# happens at boot — i.e. before this config existed — so trigger it here or
# persistence silently waits for the next reboot. A no-op once the journal is
# already persistent, so it runs whether or not journald was restarted.
journalctl --flush >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# cloud-init: first boot only
# ---------------------------------------------------------------------------
# Raspberry Pi Imager provisions the card through cloud-init on first boot
# (user, hostname, first network). By the time this script runs that is done,
# and the network connections are NetworkManager's own keyfiles, so cloud-init
# only costs ~4.5 s on every later boot's critical path (2026-09-22).
if [[ -d "${ROOT}/etc/cloud" && ! -e "${ROOT}/etc/cloud/cloud-init.disabled" ]]; then
  touch "${ROOT}/etc/cloud/cloud-init.disabled"
  echo "Disabled cloud-init for later boots (${ROOT}/etc/cloud/cloud-init.disabled)."
fi

# ---------------------------------------------------------------------------
# Memory cgroup controller (enforces canary.service MemoryMax/MemorySwapMax)
# ---------------------------------------------------------------------------
# The Pi firmware prepends cgroup_disable=memory to the kernel command line, so
# without this the unit's MemoryMax=6G / MemorySwapMax=0 are accepted by
# systemd, echoed by `systemctl show`, and enforced by nothing — found
# 2026-09-18, after the limits had been "installed" for a day. The Pi kernel
# honours a later cgroup_enable=memory over the firmware's disable. Takes
# effect on the next boot; canary-update checks for it and says so.
CMDLINE="${CANARY_CMDLINE:-${ROOT}/boot/firmware/cmdline.txt}"
CGROUP_REBOOT_NEEDED=0
if [[ -f "${CMDLINE}" ]] && ! grep -qw 'cgroup_enable=memory' "${CMDLINE}"; then
  backup="${CMDLINE}.canary-$(date +%Y%m%dT%H%M%S)"
  cp -p "${CMDLINE}" "${backup}"
  # cmdline.txt must stay ONE line; a stray newline makes the rest of it
  # invisible to the kernel and can leave the Pi unbootable.
  sed -i '1 s/[[:space:]]*$/ cgroup_enable=memory/' "${CMDLINE}"
  if [[ "$(wc -l <"${CMDLINE}")" -gt 1 ]] || ! grep -q 'root=' "${CMDLINE}" \
      || ! grep -qw 'cgroup_enable=memory' "${CMDLINE}"; then
    cp -p "${backup}" "${CMDLINE}"
    echo "WARNING: could not edit ${CMDLINE} safely; restored ${backup}" >&2
  else
    echo "Added cgroup_enable=memory to ${CMDLINE} (backup ${backup}); reboot to enforce memory limits."
    CGROUP_REBOOT_NEEDED=1
  fi
fi
if [[ ! -f "${ROOT}/sys/fs/cgroup/cgroup.controllers" ]] \
    || ! grep -qw memory "${ROOT}/sys/fs/cgroup/cgroup.controllers"; then
  CGROUP_REBOOT_NEEDED=1
fi

# ---------------------------------------------------------------------------
# CANary-HAT board overlay
# ---------------------------------------------------------------------------
# The HAT's ID EEPROM names the overlay ("canary-hat") and the firmware loads
# /boot/firmware/overlays/canary-hat.dtbo at boot — but only if that file is
# there. On a fresh image it is not, and the board comes up with no can0, no
# GNSS UART and the CAN transceiver in standby. Installed on every Pi whether or
# not a HAT is fitted: the firmware ignores an overlay nothing names.
OVERLAY_DIR="${CANARY_OVERLAY_DIR:-${ROOT}/boot/firmware/overlays}"
HAT_OVERLAY_SRC="${CANARY_HOME}/hardware/canary-hat/firmware/canary-hat-overlay.dts"
HAT_REBOOT_NEEDED=0
I2C_REBOOT_NEEDED=0
if [[ -d "${OVERLAY_DIR}" && -f "${HAT_OVERLAY_SRC}" ]]; then
  if command -v dtc >/dev/null 2>&1; then
    hat_dtbo="$(mktemp)"
    if dtc -@ -q -I dts -O dtb -o "${hat_dtbo}" "${HAT_OVERLAY_SRC}"; then
      write_managed hat-overlay "${OVERLAY_DIR}/canary-hat.dtbo" <"${hat_dtbo}"
      [[ -n "${CONFIG_CHANGED[hat-overlay]:-}" ]] && HAT_REBOOT_NEEDED=1
    else
      echo "WARNING: could not compile ${HAT_OVERLAY_SRC}; canary-hat.dtbo not updated" >&2
    fi
    rm -f "${hat_dtbo}"
  else
    echo "WARNING: dtc not found (raspi-utils-dt); canary-hat.dtbo not installed" >&2
  fi
fi

# The HAT's IMU, barometer and 12 V sense are read from userspace over
# /dev/i2c-1, which exists only with the ARM I2C bus enabled. A fresh image
# leaves it off, and the overlay deliberately binds no driver there, so on
# 2026-10-11 a new board came up with can0 and GNSS but no I2C at all: no IMU,
# no supply voltage, so no `parked`/`bench` verdict. Appended under its own
# [all], since the file may end inside a conditional section.
BOOT_CONFIG="${CANARY_BOOT_CONFIG:-${ROOT}/boot/firmware/config.txt}"
if [[ "${hat_product}" == CANary-HAT* && -f "${BOOT_CONFIG}" ]] \
    && ! grep -qE '^[[:space:]]*dtparam=([^#]*,)?i2c(_arm)?=on' "${BOOT_CONFIG}"; then
  cp -p "${BOOT_CONFIG}" "${BOOT_CONFIG}.canary-$(date +%Y%m%dT%H%M%S)"
  printf '\n[all]\n# Added by canary scripts/install_pi.sh: the CANary-HAT sensors are on /dev/i2c-1.\ndtparam=i2c_arm=on\n' \
    >>"${BOOT_CONFIG}"
  echo "Enabled the ARM I2C bus in ${BOOT_CONFIG} for the CANary-HAT sensors."
  I2C_REBOOT_NEEDED=1
fi

cd "${CANARY_HOME}"
runuser -u "${CANARY_USER}" -- "${CANARY_PYTHON}" scripts/install_deps.py \
  --venv "${CANARY_HOME}/.venv" \
  --extras "${CANARY_EXTRAS}" \
  --torch "${CANARY_TORCH}"

# Data drive (docs/pi-setup.md "Data Drive"). Run from a terminal, this asks
# where /data should live, with a drive (an NVMe or other SSD) the default when
# one is fitted; picking a blank drive formats it. Run unattended, it only
# adopts a drive already labelled canary-data and never formats. Either way a
# Pi without a drive keeps /data on the SD card, and once /data is on a drive
# canary/canary-mcp require it (operator, 2026-10-10).
DATA_DRIVE_ENV=(
  CANARY_INSTALL_ROOT="${ROOT}"
  CANARY_DATA_DIR="${CANARY_DATA_DIR}"
  CANARY_UNIT_DIR="$(dirname "${CANARY_SERVICE}")"
)
if [[ "${CANARY_DATA_DRIVE_ADOPT:-1}" != "0" ]]; then
  if [[ -t 0 && -t 1 ]]; then
    env "${DATA_DRIVE_ENV[@]}" bash "${CANARY_HOME}/scripts/data_drive.sh" choose
  else
    env "${DATA_DRIVE_ENV[@]}" bash "${CANARY_HOME}/scripts/data_drive.sh" adopt --if-present
  fi
fi
env "${DATA_DRIVE_ENV[@]}" bash "${CANARY_HOME}/scripts/data_drive.sh" guard

systemctl daemon-reload
udevadm control --reload || true
systemctl enable can0.service
systemctl enable canary.service
systemctl enable canary-mcp.service
systemctl enable ups-shutdown.service

# The hint is only worth printing when it is true: read hardware.gps.source
# through the project's own config loader (not a grep, which a comment or a
# nested key would fool), as the daemon's user, with the venv just installed.
if [[ "${GPS_STACK_INSTALLED}" -eq 1 ]]; then
  if gps_source="$(runuser -u "${CANARY_USER}" -- env PYTHONDONTWRITEBYTECODE=1 \
      "${CANARY_HOME}/.venv/bin/python" -c '
import sys
from pathlib import Path

try:
    from src.config import CanaryConfig

    print(CanaryConfig.from_yaml(Path(sys.argv[1])).hardware.gps.source)
except Exception as exc:  # noqa: BLE001 - reported, not raised
    print(f"{type(exc).__name__}: {exc}".splitlines()[0])
    sys.exit(1)
' "${CANARY_CONFIG}")"; then
    if [[ "${gps_source}" != "gpsd" ]]; then
      echo "GPS: hardware.gps.source is '${gps_source}' in ${CANARY_CONFIG}; set 'hardware.gps.source: gpsd' to read GPS through gpsd."
    fi
  else
    echo "GPS: could not read hardware.gps.source from ${CANARY_CONFIG} (${gps_source:-see the error above}); it must be 'hardware.gps.source: gpsd' to read GPS through gpsd."
  fi
fi

cat <<EOF
CANary Pi install complete.

This Pi answers as ${current_hostname:-<unknown>}.local
  Dashboard: http://${current_hostname:-<pi-hostname>}.local:8000/

Next:
  1. Edit ${CANARY_CONFIG}
  2. Bring up can0 for your chosen OBD/CAN mode; see docs/can-setup.md
  3. Start with: sudo systemctl start canary canary-mcp
  4. Watch logs: sudo journalctl -u canary -f  (or -u canary-mcp -f)
  5. Export the laptop token: export CANARY_CONTROL_TOKEN="$(cat "${CANARY_CONTROL_TOKEN_FILE}")"
  6. Open: http://<pi-hostname-or-ip>:8000/status
  7. MCP server: http://<pi-hostname-or-ip>:8001/mcp (see docs/mcp-server.md)
  8. GPS/time: verify with 'cgps' (or gpsmon) and 'chronyc sources' — the GPS
     refclock should appear. See docs/time-sync.md
  9. Future updates: sudo canary-update
EOF
if [[ "${CGROUP_REBOOT_NEEDED}" -eq 1 ]]; then
  echo
  echo "REBOOT REQUIRED: the memory cgroup controller is not active, so"
  echo "canary.service's MemoryMax/MemorySwapMax are not enforced until the Pi reboots."
fi
if [[ "${HAT_REBOOT_NEEDED}" -eq 1 ]]; then
  echo
  echo "REBOOT REQUIRED: canary-hat.dtbo changed; the firmware loads it only at boot."
fi
if [[ "${I2C_REBOOT_NEEDED}" -eq 1 ]]; then
  echo
  echo "REBOOT REQUIRED: dtparam=i2c_arm=on was added; /dev/i2c-1 appears only after a boot."
fi
