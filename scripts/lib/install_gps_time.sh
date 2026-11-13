# shellcheck shell=bash
# GPS and time-sync stack (gpsd, chrony) for scripts/install_pi.sh (R30.11).
# Sourced, not run. Uses install_pi.sh's globals (ROOT, CANARY_HOME,
# CANARY_SERVICE, CONFIG_CHANGED, hat_product), its write_managed and
# restart_if_changed, and install_hat.sh's hat_fact. Sets GPS_STACK_INSTALLED
# for the closing summary. No `local`: the moved code was top-level and its
# variables stay visible.

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

gps_time_settings() {
  CANARY_INSTALL_GPS_STACK="${CANARY_INSTALL_GPS_STACK:-1}"
  CANARY_GPS_DEVICE="${CANARY_GPS_DEVICE:-}"
  GPS_STACK_INSTALLED=0

  # The HAT receiver's UART speed and measurement period, set on every attach
  # (below). 100 ms is the 10 Hz the roadmap asks of the HAT's GNSS (H03.4).
  CANARY_HAT_GPS_BAUD="${CANARY_HAT_GPS_BAUD:-115200}"
  CANARY_HAT_GPS_RATE_MS="${CANARY_HAT_GPS_RATE_MS:-100}"
  # Both land in a unit file and in arithmetic, so they must be plain numbers.
  # 25 ms is UBX CFG-RATE-MEAS's floor; 1000 ms is the receiver's power-up 1 Hz.
  if [[ ! "${CANARY_HAT_GPS_BAUD}" =~ ^[1-9][0-9]*$ ]]; then
    echo "REFUSING: CANARY_HAT_GPS_BAUD=${CANARY_HAT_GPS_BAUD} is not a baud rate." >&2
    exit 1
  fi
  if [[ ! "${CANARY_HAT_GPS_RATE_MS}" =~ ^[1-9][0-9]*$ ]] \
      || ((CANARY_HAT_GPS_RATE_MS < 25 || CANARY_HAT_GPS_RATE_MS > 1000)); then
    echo "REFUSING: CANARY_HAT_GPS_RATE_MS=${CANARY_HAT_GPS_RATE_MS} is not a 25-1000 ms measurement period." >&2
    exit 1
  fi
  # At 9600 baud a *1 Hz* epoch of the M10's NMEA already overflowed its transmit
  # buffer and its time ran ~14 s late (2026-10-04, H03.4). Faster than 1 Hz needs
  # 38400 or more, which is H03.4's floor for 10 Hz of the sentences gpsd reads.
  if ((CANARY_HAT_GPS_RATE_MS < 1000 && CANARY_HAT_GPS_BAUD < 38400)); then
    echo "REFUSING: CANARY_HAT_GPS_BAUD=${CANARY_HAT_GPS_BAUD} cannot carry a ${CANARY_HAT_GPS_RATE_MS} ms GNSS rate." >&2
    echo "  At 9600 baud even 1 Hz overflowed the receiver (\`txbuf alloc\`, time ~14 s late)." >&2
    echo "  Use 38400 or higher, or CANARY_HAT_GPS_RATE_MS=1000." >&2
    exit 1
  fi
  # The receiver both HAT revisions carry, a u-blox MAX-M10M, speaks UBX protocol
  # 34 (SPG 5.x). ubxtool needs it to send CFG-VALSET, the M10's only way to
  # configure: it has no legacy CFG-RATE/CFG-MSG.
  HAT_GNSS_UBX_PROTVER=34
}

gps_choose_device() {
  # A CANary-HAT carries its own GNSS receiver on a board UART, from the overlay
  # its ID EEPROM names: uart2 (/dev/ttyAMA2) on rev 2.0, uart4 (/dev/ttyAMA4)
  # on rev 2.1. When the firmware reports the HAT, that receiver is the GPS —
  # the antenna is on the board — whatever USB serial devices are also plugged
  # in. An explicit CANARY_GPS_DEVICE still wins.
  if [[ -z "${CANARY_GPS_DEVICE}" && "${hat_product}" == CANary-HAT* ]]; then
    if hat_tty="$(hat_fact gnss_tty)"; then
      CANARY_GPS_DEVICE="${hat_tty}"
      echo "  CANary-HAT detected (rev $(hat_fact name)); GPS device: ${CANARY_GPS_DEVICE}"
    else
      echo "WARNING: CANary-HAT of unknown revision; its GNSS UART is not chosen" >&2
    fi
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
}

gps_configure_gpsd() {
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
    # A board UART (the HAT's /dev/ttyAMA2 or ttyAMA4) has no by-id symlink: udev never
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
    #
    # Then the measurement rate (H03.4), lost at a power cut for the same
    # reason: UBX CFG-VALSET into RAM (layer 1) through gpsd with ubxtool. Only
    # once the speed is set: 10 Hz at the power-up 9600 baud is the overflow
    # above ten times over. GSV goes first, to one in every 1000/rate epochs
    # (1 Hz): its 14 lines are most of an epoch's NMEA, and ten of those a
    # second (~10k chars/s by the 9600-baud overflow's growth) would nearly
    # fill 115200 baud's ~11.5k, or overfill it with more satellites in view.
    # gpsd reads TPV from RMC/GGA, not GSV, so the fix still comes every epoch.
    if [[ -n "${hat_product}" && "${hat_product}" == CANary-HAT* \
          && "${CANARY_GPS_DEVICE}" == /dev/ttyAMA* ]]; then
      _gps_unit="canary-gpsd@${CANARY_GPS_DEVICE#/dev/}.service"
      _gsv_every=$((1000 / CANARY_HAT_GPS_RATE_MS))
      _ubxtool="/usr/bin/timeout -s KILL 20 /usr/bin/ubxtool -P ${HAT_GNSS_UBX_PROTVER}"
      _ubx_target="localhost:2947:${CANARY_GPS_DEVICE}"
      install -d -m 0755 "${UNIT_DIR}/${_gps_unit}.d"
      write_managed gpsd "${UNIT_DIR}/${_gps_unit}.d/canary-baud.conf" <<EOF
# Managed by canary scripts/install_pi.sh — HAT GNSS at ${CANARY_HAT_GPS_BAUD} baud and a ${CANARY_HAT_GPS_RATE_MS} ms measurement rate on every attach.
[Service]
ExecStartPost=-/bin/sh -c '( for i in 1 2 3 4 5 6 7 8 9 10; do /usr/bin/timeout -s KILL 20 /usr/bin/gpsctl -s ${CANARY_HAT_GPS_BAUD} ${CANARY_GPS_DEVICE} && exit 0; sleep 1; done; exit 1 ) \\
  || { echo "could not set ${CANARY_GPS_DEVICE} to ${CANARY_HAT_GPS_BAUD} baud; its rate is left alone" >&2; exit 1; }; \\
  for i in 1 2 3 4 5; do \\
  ${_ubxtool} -z CFG-MSGOUT-NMEA_ID_GSV_UART1,${_gsv_every},1 ${_ubx_target} \\
  && ${_ubxtool} -z CFG-RATE-MEAS,${CANARY_HAT_GPS_RATE_MS},1 ${_ubx_target} && exit 0; sleep 1; done; \\
  echo "could not set ${CANARY_GPS_DEVICE} to a ${CANARY_HAT_GPS_RATE_MS} ms measurement rate" >&2; exit 1'
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
}

gps_configure_chrony() {
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
}

install_gps_time() {
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

  gps_choose_device
  gps_configure_gpsd
  gps_configure_chrony
fi
}
