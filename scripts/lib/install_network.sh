# shellcheck shell=bash
# Hostname and wired link for scripts/install_pi.sh (R30.11). Sourced, not run:
# the functions use install_pi.sh's globals (ROOT, CANARY_HOME, FIRST_INSTALL)
# and set the ones its closing summary reads (current_hostname, WIRED_SUMMARY,
# WIRED_NOTICE). They keep the script's `set -euo pipefail`; `exit` leaves the
# installer. No `local`: the moved code was top-level and its variables stay
# visible, as before.

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
# any run. Runs before install_pi.sh disables cloud-init, which matters: on a
# later boot cloud-init would put the Imager's hostname back.

install_hostname() {
  HOSTNAME_FILE="${ROOT}/etc/hostname"
  HOSTS_FILE="${ROOT}/etc/hosts"
  NET_DIR="${CANARY_NET_DIR:-/sys/class/net}"
  current_hostname="$(tr -d '[:space:]' <"${HOSTNAME_FILE}" 2>/dev/null || true)"
  canary_hostname=""
  if [[ -n "${CANARY_HOSTNAME:-}" ]]; then
    canary_hostname="${CANARY_HOSTNAME}"
  elif [[ "${FIRST_INSTALL}" -eq 1 && "${current_hostname}" =~ ^(raspberrypi|canary|localhost)?$ ]]; then
    # The same rule an imaged Pi applies on its first boot.
    # shellcheck source=canary_hostname.sh
    source "${CANARY_HOME}/scripts/lib/canary_hostname.sh"
    if ! canary_hostname="$(canary_hostname_from_mac "${NET_DIR}")"; then
      canary_hostname=""
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
}

# ---------------------------------------------------------------------------
# Wired link: 192.168.137.2 on eth0, the address every CANary answers on a cable
# ---------------------------------------------------------------------------
# The docs, sync-train.sh and companion discovery all reach a Pi on a direct
# cable at 192.168.137.2, but nothing set it: the dev Pi and canary-8974 had it
# by hand, and a Pi installed by this script alone sat on Raspberry Pi OS's DHCP
# default and could not be found on the cable (2026-11-06, another user's Pi).
# Static, so it needs no DHCP server on the laptop; never-default, so a cable
# never steals the Wi-Fi's route; IPv6 link-local, so the Pi is still findable
# with `ping ff02::1%<if>` if the laptop side is misconfigured. The profile
# outranks Pi OS's and netplan's own eth0 profiles (autoconnect-priority).
#
# It is rewritten only while it still carries the marker below. `nmcli
# connection modify canary-wired ...` rewrites the keyfile without comments, so
# an edit made that way is the owner's and is kept. CANARY_WIRED_ADDRESS=<cidr>
# picks another address, `none` leaves eth0 alone.

install_wired_link() {
  WIRED_MARKER="# Managed by scripts/install_pi.sh; edit with nmcli to keep your changes."
  WIRED_ADDRESS="${CANARY_WIRED_ADDRESS:-192.168.137.2/24}"
  WIRED_DIR="${ROOT}/etc/NetworkManager/system-connections"
  WIRED_PROFILE="${WIRED_DIR}/canary-wired.nmconnection"
  WIRED_NOTICE=""
  WIRED_SUMMARY="not configured by this installer (eth0 left as it was)"
  if [[ "${WIRED_ADDRESS}" != "none" && -d "${WIRED_DIR}" ]]; then
    WIRED_SUMMARY="${WIRED_ADDRESS%/*} (the laptop takes another address on ${WIRED_ADDRESS}, e.g. 192.168.137.1)"
    if [[ ! "${WIRED_ADDRESS}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$ ]]; then
      echo "REFUSING CANARY_WIRED_ADDRESS '${WIRED_ADDRESS}': an IPv4 address with a prefix, e.g. 192.168.137.2/24, or 'none'." >&2
      exit 1
    fi
    if [[ ! -e "${WIRED_PROFILE}" ]] || grep -qxF "${WIRED_MARKER}" "${WIRED_PROFILE}"; then
      staged="$(mktemp)"
      cat >"${staged}" <<EOF
${WIRED_MARKER}
[connection]
id=canary-wired
uuid=6c1f2a4e-9b3d-4c57-8e21-0a7f5d3b9c64
type=ethernet
interface-name=eth0
autoconnect=true
autoconnect-priority=100

[ethernet]

[ipv4]
method=manual
address1=${WIRED_ADDRESS}
never-default=true

[ipv6]
method=link-local

[proxy]
EOF
      if [[ -f "${WIRED_PROFILE}" ]] && cmp -s "${staged}" "${WIRED_PROFILE}"; then
        rm -f "${staged}"
        echo "unchanged: eth0 wired profile (${WIRED_ADDRESS})"
      else
        # NetworkManager ignores a keyfile that is not root-owned and 0600.
        install -m 0600 "${staged}" "${WIRED_PROFILE}"
        rm -f "${staged}"
        chown root:root "${WIRED_PROFILE}"
        echo "  wrote ${WIRED_PROFILE} (eth0 ${WIRED_ADDRESS})"
        if [[ -z "${ROOT}" ]] && command -v nmcli >/dev/null 2>&1; then
          nmcli connection reload || true
          # Not activated here: the installer may be running over SSH on eth0's
          # current address, and switching it would cut the session mid-install.
          # With no cable, NetworkManager picks this profile when one is plugged.
          active="$(nmcli -g GENERAL.CONNECTION device show eth0 2>/dev/null || true)"
          if [[ -n "${active}" && "${active}" != "canary-wired" ]]; then
            WIRED_NOTICE="eth0 is still on '${active}'; it moves to ${WIRED_ADDRESS%/*} at the next boot, or now with: sudo nmcli connection up canary-wired"
          fi
        fi
      fi
    else
      echo "unchanged: ${WIRED_PROFILE} was edited with nmcli; keeping it"
      WIRED_SUMMARY="as set in the canary-wired profile (edited with nmcli)"
    fi
  fi
}
