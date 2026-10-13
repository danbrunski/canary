# shellcheck shell=bash
# canary_hostname_from_mac NET_DIR -> prints canary-XXXX from the wired MAC.
#
# The wired MAC is printed nowhere, but it is fixed in the SoC's OTP on a Pi,
# and the WiFi MAC is derived from it, so the suffix is unique without
# coordination and stable across reinstalls. Falls back to the first non-lo
# interface. Prints nothing (status 1) when no MAC can be read.
#
# Sourced by scripts/canary_firstboot.sh. scripts/install_pi.sh carries the
# same rule inline and should source this once its pending edits land.
canary_hostname_from_mac() {
  local net_dir="$1" mac="" addr
  if [[ -r "${net_dir}/eth0/address" ]]; then
    mac="$(cat "${net_dir}/eth0/address")"
  else
    for addr in "${net_dir}"/*/address; do
      [[ "${addr}" == */lo/address || ! -r "${addr}" ]] && continue
      mac="$(cat "${addr}")"
      break
    done
  fi
  mac="${mac//:/}"
  [[ "${mac}" =~ ^[0-9a-fA-F]{12}$ ]] || return 1
  printf 'canary-%s\n' "$(tr '[:upper:]' '[:lower:]' <<<"${mac: -4}")"
}
