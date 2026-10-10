#!/usr/bin/env bash
# CANary's data drive: put /data on a separate drive (a USB NVMe on the dev Pi
# since 2026-10-10) when one is fitted, and leave it on the SD card when not.
#
#   data_drive.sh status              where /data lives, and any drive that could hold it
#   data_drive.sh setup DEVICE        format a BLANK drive as canary-data, then adopt it
#   data_drive.sh adopt [--if-present]
#                                     mount the drive labelled canary-data at /data,
#                                     copying what /data already holds onto it
#   data_drive.sh guard               canary/canary-mcp require the mount that holds
#                                     the data directory (from fstab)
#
# A drive is recognised by its filesystem label, canary-data, and nothing else:
# `setup` is the only thing that formats, it refuses a drive with anything on
# it, and it is never run automatically (operator, 2026-10-10). install_pi.sh
# (and so canary-update) runs `adopt --if-present` and `guard` every time, so a
# labelled drive is picked up and a Pi without one keeps /data on the SD card.
#
# Why the guard: with /data on a drive, a service started without it would
# write into the empty mount point on the SD card, and that data would vanish
# behind the drive once it came back. Measured on the dev Pi (2026-10-10):
# reading all of /data took 47 s from the NVMe against ~225 s from the SD card.
set -euo pipefail

ROOT="${CANARY_INSTALL_ROOT:-}"
LABEL="${CANARY_DATA_LABEL:-canary-data}"
MOUNT="${CANARY_DATA_MOUNT:-/data}"
CANARY_DATA_DIR="${CANARY_DATA_DIR:-${ROOT}/data/canary}"
UNIT_DIR="${CANARY_UNIT_DIR:-${ROOT}/etc/systemd/system}"
FSTAB="${ROOT}/etc/fstab"
SERVICES=(canary canary-mcp)
#: Below this a drive is not worth moving the data onto.
MIN_BYTES=$((32 * 1000 * 1000 * 1000))

say() { echo "data drive: $*"; }
die() { echo "data drive: $*" >&2; exit 1; }

# The longest non-root fstab mount point that contains the data directory.
fstab_data_mount() {
  local rel="${CANARY_DATA_DIR#"${ROOT}"}" best="" src target rest
  [[ -f "${FSTAB}" ]] || return 0
  while read -r src target rest; do
    [[ -z "${src}" || "${src}" == \#* || -z "${target}" || "${target}" == "/" ]] && continue
    case "${rel%/}/" in
      "${target%/}"/*) if (( ${#target} > ${#best} )); then best="${target}"; fi ;;
    esac
  done <"${FSTAB}"
  echo "${best}"
}

labelled_device() { blkid -t "LABEL=${LABEL}" -o device 2>/dev/null | head -1 || true; }

# Whole solid-state disks with nothing on them: candidates for `setup`, never
# formatted here.
blank_disks() {
  local name type rota size
  while read -r name type rota size; do
    [[ "${type}" == "disk" && "${rota}" == "0" && "${name}" != mmcblk* && "${name}" != zram* ]] || continue
    (( size >= MIN_BYTES )) || continue
    [[ -z "$(lsblk -nro NAME "/dev/${name}" | tail -n +2)" ]] || continue
    [[ -z "$(wipefs -n "/dev/${name}" 2>/dev/null)" ]] || continue
    echo "/dev/${name}"
  done < <(lsblk -dnbo NAME,TYPE,ROTA,SIZE 2>/dev/null)
}

guard() {
  local mount unit dropin
  mount="$(fstab_data_mount)"
  for unit in "${SERVICES[@]}"; do
    dropin="${UNIT_DIR}/${unit}.service.d/data-drive.conf"
    if [[ -n "${mount}" ]]; then
      install -d -m 0755 "$(dirname "${dropin}")"
      local want
      want="# Managed by canary scripts/data_drive.sh -- ${mount} holds the data directory.
# Without it the service does not start: writing into the empty mount point
# would hide that data once the drive came back.
[Unit]
RequiresMountsFor=${mount}"
      if [[ ! -f "${dropin}" ]] || [[ "$(cat "${dropin}")" != "${want}" ]]; then
        printf '%s\n' "${want}" >"${dropin}"
        say "wrote ${dropin}"
      fi
    elif [[ -f "${dropin}" ]]; then
      rm -f "${dropin}"
      say "removed ${dropin}"
    fi
  done
}

active_services() {
  local unit
  for unit in "${SERVICES[@]}"; do
    systemctl is-active --quiet "${unit}" && echo "${unit}"
  done
  return 0
}

adopt() {
  local if_present="${1:-}" device uuid staging stopped backup
  if [[ -n "$(fstab_data_mount)" ]]; then
    say "${MOUNT} is already a drive in fstab ($(fstab_data_mount))"
    return 0
  fi
  device="$(labelled_device)"
  if [[ -z "${device}" ]]; then
    for blank in $(blank_disks); do
      say "found a blank drive ${blank} ($(lsblk -dno SIZE,MODEL "${blank}" | xargs)); to keep CANary data on it run:"
      say "  sudo bash scripts/data_drive.sh setup ${blank}"
    done
    [[ "${if_present}" == "--if-present" ]] && return 0
    die "no filesystem labelled ${LABEL}"
  fi
  uuid="$(blkid -s UUID -o value "${device}")"
  say "adopting ${device} (UUID ${uuid}) as ${MOUNT}"

  staging="$(mktemp -d)"
  mount -o noatime "${device}" "${staging}"
  mapfile -t stopped < <(active_services)
  if (( ${#stopped[@]} )); then
    say "stopping ${stopped[*]} for the copy"
    systemctl stop "${stopped[@]}"
  fi
  if [[ -d "${MOUNT}" ]] && [[ -n "$(ls -A "${MOUNT}")" ]]; then
    # Merge, never delete: a drive adopted again keeps what it already holds.
    rsync -aHAX --numeric-ids "${MOUNT}/" "${staging}/"
    if [[ -n "$(rsync -aHAXn --numeric-ids --itemize-changes "${MOUNT}/" "${staging}/")" ]]; then
      umount "${staging}"
      die "the copy onto ${device} does not match ${MOUNT}; nothing switched"
    fi
    backup="${MOUNT}.sd-backup-$(date +%Y%m%d%H%M%S)"
    mv "${MOUNT}" "${backup}"
    say "the SD card's copy is kept at ${backup} (delete it once the drive has proven itself)"
  fi
  umount "${staging}"
  rmdir "${staging}"
  mkdir -p "${MOUNT}"
  chmod 0755 "${MOUNT}"
  # The empty mount point can take no writes, should anything bypass the guard.
  chattr +i "${MOUNT}" 2>/dev/null || true
  cp "${FSTAB}" "${FSTAB}.bak-data-drive-$(date +%Y%m%d%H%M%S)"
  echo "UUID=${uuid} ${MOUNT} ext4 defaults,noatime,nofail,x-systemd.device-timeout=15s 0 2" >>"${FSTAB}"
  systemctl daemon-reload
  mount "${MOUNT}"
  guard
  systemctl daemon-reload
  if (( ${#stopped[@]} )); then
    systemctl start "${stopped[@]}"
  fi
  say "${MOUNT} is on ${device}: $(df -h --output=avail "${MOUNT}" | tail -1 | xargs) free"
}

setup() {
  local device="${1:-}" part
  [[ -b "${device}" ]] || die "setup needs a block device, e.g. /dev/sda"
  [[ "$(lsblk -dno TYPE "${device}")" == "disk" ]] || die "${device} is not a whole disk"
  [[ "${device}" != /dev/mmcblk* ]] || die "${device} is the SD card"
  if [[ -n "$(lsblk -nro MOUNTPOINTS "${device}" | tr -d '[:space:]')" ]]; then
    die "${device} has mounted filesystems; refusing"
  fi
  if [[ -n "$(lsblk -nro NAME "${device}" | tail -n +2)" || -n "$(wipefs -n "${device}" 2>/dev/null)" ]]; then
    die "${device} is not blank (partitions or a signature on it); refusing to format it"
  fi
  say "formatting ${device} ($(lsblk -dno SIZE,MODEL "${device}" | xargs)) as ${LABEL}"
  printf 'label: gpt\n,,L,\n' | sfdisk --quiet "${device}"
  partprobe "${device}" 2>/dev/null || true
  udevadm settle
  part="$(lsblk -nro PATH "${device}" | tail -n +2 | head -1)"
  [[ -b "${part}" ]] || die "no partition appeared on ${device}"
  mkfs.ext4 -q -L "${LABEL}" -m 1 -E lazy_itable_init=0,lazy_journal_init=0 "${part}"
  udevadm settle
  adopt
}

status() {
  local mount device
  mount="$(fstab_data_mount)"
  if [[ -n "${mount}" ]]; then
    say "${MOUNT}: drive in fstab, mounted from $(findmnt -no SOURCE "${mount}" 2>/dev/null || echo 'NOT MOUNTED')"
  else
    say "${MOUNT}: on the root filesystem (no data drive)"
  fi
  device="$(labelled_device)"
  [[ -n "${device}" ]] && say "labelled ${LABEL}: ${device}"
  for blank in $(blank_disks); do say "blank drive: ${blank} (use: setup ${blank})"; done
  return 0
}

case "${1:-}" in
  status) status ;;
  setup) shift; setup "${1:-}" ;;
  adopt) shift; adopt "${1:-}" ;;
  guard) guard ;;
  *) sed -n '2,12p' "$0" >&2; exit 2 ;;
esac
