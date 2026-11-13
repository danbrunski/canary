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

# The sections that change with the board and the network live in lib/ (R30.11):
# canary-update re-runs this script when any of them changes.
# shellcheck source=lib/install_network.sh
source "${SCRIPT_DIR}/lib/install_network.sh"
# shellcheck source=lib/install_hat.sh
source "${SCRIPT_DIR}/lib/install_hat.sh"
# shellcheck source=lib/install_gps_time.sh
source "${SCRIPT_DIR}/lib/install_gps_time.sh"

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
# The daemon reaches the CANary-HAT through device nodes Raspberry Pi OS gives
# to groups: /dev/i2c-* (i2c: the IMU, rev 2.0's 12 V ADC), /dev/gpiochip*
# (gpio: the K-line lines, rev 2.1's sensor-supply enable) and /dev/spidev*
# (spi: rev 2.1's ADC). Join whichever exist; a re-run changes nothing. The
# service picks the groups up at its next start.
for device_group in i2c gpio spi; do
  if getent group "${device_group}" >/dev/null 2>&1; then
    usermod -aG "${device_group}" "${CANARY_USER}"
  fi
done

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
# Phone setup and updates (docs/roadmap/archive/phone-ui.md). canary-provision: WiFi
# setup over Bluetooth (Improv), root, standalone from the daemon like the UPS
# watchdog (docs/ble-provisioning.md). canary-update.path/.service: the root
# side of "update from the dashboard", started by the request file the
# daemon writes (src/control/updater.py). canary-firstboot: an imaged Pi's own
# token, hostname and SSH keys, a no-op without the marker
# scripts/prepare_image.sh leaves.
for unit in canary-provision.service canary-update.path canary-update.service \
    canary-firstboot.service; do
  install -m 0644 "${CANARY_HOME}/systemd/${unit}" "$(dirname "${CANARY_SERVICE}")/${unit}"
done
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

# Hostname (canary-<last four hex digits of the board's MAC>), then the wired
# link (192.168.137.2 on eth0): lib/install_network.sh.
install_hostname
install_wired_link

# Stable operator command. It discovers the active checkout through
# /etc/canary/canary.env, so callers do not need to cd into the repository.
install -m 0755 "${CANARY_HOME}/scripts/update_canary.sh" "${ROOT}/usr/local/sbin/canary-update"
# For whoever is SSH'd in: print the control token, or approve a browser
# waiting to pair (scripts/canary_token.sh). In bin, not sbin: the canary user
# runs it, no sudo needed.
install -D -m 0755 "${CANARY_HOME}/scripts/canary_token.sh" "${ROOT}/usr/local/bin/canary-token"

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

# GPS + time sync stack (gpsd feeds both canary and chrony): lib/install_hat.sh
# says which board is fitted, lib/install_gps_time.sh installs the stack.
hat_detect
hat_refuse_uart0_console
gps_time_settings
install_gps_time

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

# CANary-HAT board overlay and the ARM I2C bus: lib/install_hat.sh.
install_hat_overlay

# ---------------------------------------------------------------------------
# Dashboard build tools: Node.js + pnpm
# ---------------------------------------------------------------------------
# canary-update rebuilds the dashboard bundle (gitignored dist/) whenever the
# frontend changes or dist/ is missing, and refuses the whole update without
# pnpm. A fresh Pi had neither, so its first `canary-update` failed (exit 7,
# 2026-10-11). Node comes from apt, alone: Debian's npm is 362 packages on
# trixie, and trixie's Node ships without corepack. pnpm is its standalone
# binary at the version package.json pins (`packageManager`), so the lockfile
# stays valid. CANARY_DASHBOARD_TOOLS=0 skips both.
PNPM_BIN="${ROOT}/usr/local/bin/pnpm"
if [[ "${CANARY_DASHBOARD_TOOLS:-1}" == "1" ]]; then
  pnpm_version="$(sed -n 's/.*"packageManager": *"pnpm@\([0-9.]*\)".*/\1/p' \
    "${CANARY_HOME}/src/dashboard/static/package.json" 2>/dev/null || true)"
  if ! command -v node >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends nodejs
  fi
  if [[ -z "${pnpm_version}" ]]; then
    echo "WARNING: no pnpm version in package.json's packageManager; pnpm not installed" >&2
  elif [[ "$("${PNPM_BIN}" --version 2>/dev/null || true)" != "${pnpm_version}" ]]; then
    case "$(uname -m)" in
      aarch64 | arm64) pnpm_arch=arm64 ;;
      x86_64) pnpm_arch=x64 ;;
      *) pnpm_arch="" ;;
    esac
    pnpm_tmp="$(mktemp)"
    if [[ -n "${pnpm_arch}" ]] && curl -fsSL -o "${pnpm_tmp}" \
        "https://github.com/pnpm/pnpm/releases/download/v${pnpm_version}/pnpm-linux-${pnpm_arch}"; then
      chmod 0755 "${pnpm_tmp}"
      if [[ "$("${pnpm_tmp}" --version 2>/dev/null || true)" == "${pnpm_version}" ]]; then
        install -d -m 0755 "$(dirname "${PNPM_BIN}")"
        install -m 0755 "${pnpm_tmp}" "${PNPM_BIN}"
        echo "Installed pnpm ${pnpm_version} at ${PNPM_BIN}"
      else
        echo "WARNING: downloaded pnpm does not report ${pnpm_version}; not installed" >&2
      fi
    else
      echo "WARNING: could not download pnpm ${pnpm_version} for $(uname -m); canary-update cannot build the dashboard" >&2
    fi
    rm -f "${pnpm_tmp}"
  fi
fi

# ---------------------------------------------------------------------------
# Dashboard bundle
# ---------------------------------------------------------------------------
# A fresh clone has no dist/, and this script used to leave building it to
# canary-update, so a Pi set up by install_pi.sh served no dashboard until a
# `canary-update` built it (2026-11-06). It is built here when missing or older
# than its sources, by the rule canary-update uses (scripts/lib/
# dashboard_build.sh). canary-update builds before it runs this script, so an
# update does not build twice. A failed build does not stop the install, since
# the daemon, API and MCP server work without it; the end of the run repeats
# the failure. CANARY_DASHBOARD_BUILD=0 skips the build.
DASHBOARD_BUILD="skipped"
DASHBOARD_WAS_MISSING=0
if [[ "${CANARY_DASHBOARD_BUILD:-1}" == "1" ]]; then
  # shellcheck source=lib/dashboard_build.sh
  source "${CANARY_HOME}/scripts/lib/dashboard_build.sh"
  dashboard_stale="$(dashboard_stale_reason "${CANARY_HOME}")"
  if [[ -z "${dashboard_stale}" ]]; then
    DASHBOARD_BUILD="current"
    echo "unchanged: dashboard dist/ is current, not rebuilt"
  else
    if [[ "${dashboard_stale}" == "dist/ missing" ]]; then
      DASHBOARD_WAS_MISSING=1
    fi
    echo "Building the dashboard (${dashboard_stale})"
    if dashboard_build "${CANARY_HOME}" "${CANARY_USER}"; then
      DASHBOARD_BUILD="built"
    else
      DASHBOARD_BUILD="failed"
      echo "WARNING: the dashboard build failed; see above" >&2
    fi
  fi
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

# Advertise the dashboard over mDNS-SD (_canary._tcp) so a laptop or phone
# that can browse Bonjour finds this Pi by service, not only by guessing its
# hostname. A laptop that cannot (WSL) sweeps the LAN for GET /identity
# instead (src/companion/discovery.py). Avahi ships with Raspberry Pi OS; a
# system without it is left alone.
if [[ -d "${ROOT}/etc/avahi/services" ]]; then
  write_managed avahi-daemon "${ROOT}/etc/avahi/services/canary.service" <<EOF
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">CANary on %h</name>
  <service>
    <type>_canary._tcp</type>
    <port>8000</port>
    <txt-record>product=canary</txt-record>
    <txt-record>path=/identity</txt-record>
  </service>
</service-group>
EOF
  if [[ -z "${ROOT}" ]]; then
    restart_if_changed avahi-daemon avahi-daemon.service || true
  fi
fi

systemctl daemon-reload
udevadm control --reload || true
systemctl enable can0.service
systemctl enable canary.service
systemctl enable canary-mcp.service
systemctl enable ups-shutdown.service
systemctl enable canary-provision.service
systemctl enable canary-firstboot.service
systemctl enable --now canary-update.path

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
  On a direct Ethernet cable: ${WIRED_SUMMARY}

Next:
  1. Edit ${CANARY_CONFIG}
  2. Bring up can0 for your chosen OBD/CAN mode; see docs/can-setup.md
  3. Start with: sudo systemctl start canary canary-mcp
  4. Watch logs: sudo journalctl -u canary -f  (or -u canary-mcp -f)
  5. Unlock the dashboard: run 'canary-token' on the Pi and paste what it prints
     into the "control token" pill in the dashboard's top bar, or press "Pair
     this device" there and run 'canary-token pair'
  6. Open: http://<pi-hostname-or-ip>:8000/status
  7. MCP server: http://<pi-hostname-or-ip>:8001/mcp (see docs/mcp-server.md)
  8. GPS/time: verify with 'cgps' (or gpsmon) and 'chronyc sources' — the GPS
     refclock should appear. See docs/time-sync.md
  9. Future updates: sudo canary-update
EOF
if [[ -n "${WIRED_NOTICE}" ]]; then
  echo
  echo "WIRED ADDRESS PENDING: ${WIRED_NOTICE}"
fi
if [[ "${CGROUP_REBOOT_NEEDED}" -eq 1 ]]; then
  echo
  echo "REBOOT REQUIRED: the memory cgroup controller is not active, so"
  echo "canary.service's MemoryMax/MemorySwapMax are not enforced until the Pi reboots."
fi
if [[ "${HAT_REBOOT_NEEDED}" -eq 1 ]]; then
  echo
  echo "REBOOT REQUIRED: ${HAT_CHANGED_DTBOS[*]} changed; the firmware loads it only at boot."
fi
if [[ "${I2C_REBOOT_NEEDED}" -eq 1 ]]; then
  echo
  echo "REBOOT REQUIRED: dtparam=i2c_arm=on was added; /dev/i2c-1 appears only after a boot."
fi
if [[ "${DASHBOARD_BUILD}" == "failed" ]]; then
  echo
  echo "DASHBOARD NOT BUILT: the pnpm install/build above failed, so port 8000 serves"
  echo "the API but no browser dashboard. Fix the error and run this installer again"
  echo "(or: sudo canary-update --full)."
elif [[ "${DASHBOARD_BUILD}" == "built" && "${DASHBOARD_WAS_MISSING}" -eq 1 ]] \
    && systemctl is-active --quiet canary.service; then
  # The daemon mounts dist/ only if it exists when it starts.
  echo
  echo "RESTART NEEDED: canary started before the dashboard existed and will not serve"
  echo "it until it restarts: sudo ${CANARY_HOME}/scripts/restart_canary.sh"
fi
