# shellcheck shell=bash
# CANary-HAT detection, its revision facts, and the board overlay for
# scripts/install_pi.sh (R30.11). Sourced, not run. Uses install_pi.sh's
# globals (ROOT, CANARY_HOME, CANARY_PYTHON, CONFIG_CHANGED) and its
# write_managed; sets hat_product, hat_product_ver, HAT_REBOOT_NEEDED,
# HAT_CHANGED_DTBOS and I2C_REBOOT_NEEDED for the later steps and the summary.
# No `local`: the moved code was top-level and its variables stay visible.

# Is a CANary-HAT fitted? The firmware reads its ID EEPROM at boot and exposes
# the product string here; several install steps differ for it.
hat_detect() {
  HAT_PRODUCT_FILE="${CANARY_HAT_PRODUCT_FILE:-/proc/device-tree/hat/product}"
  HAT_PRODUCT_VER_FILE="${CANARY_HAT_PRODUCT_VER_FILE:-/proc/device-tree/hat/product_ver}"
  hat_product=""
  hat_product_ver=""
  [[ -r "${HAT_PRODUCT_FILE}" ]] && hat_product="$(tr -d '\0' <"${HAT_PRODUCT_FILE}")"
  [[ -r "${HAT_PRODUCT_VER_FILE}" ]] && hat_product_ver="$(tr -d '\0' <"${HAT_PRODUCT_VER_FILE}")"
  return 0
}

# hat_fact <field>: one fact about the fitted CANary-HAT's revision, from
# hardware/canary-hat-firmware/hat_revisions.json. Rev 2.0 and rev 2.1 wire the
# GNSS UART differently, and each loads its own overlay. The EEPROM's
# product_ver picks the row; an empty one is the table's "unidentified" (2.0).
# Fails, saying why, for a product_ver the checkout does not know.
hat_fact() {
  "${CANARY_PYTHON}" "${CANARY_HOME}/hardware/canary-hat-firmware/hat_rev.py" \
    --product-ver "${hat_product_ver}" "$1"
}

# The HAT's K-line transceiver is on uart0 (GPIO14/15). A kernel console there
# would turn a held-low K-line (every wake-up pulse) into a serial break, which
# with magic SysRq can reboot the Pi (docs/CANary-HAT-Spec.md §11 step 1). On
# the Pi 5 the console is the debug UART: `serial0` stays ttyAMA10 even with
# uart0 enabled (checked on the HAT 2026-09-23). Refuse a console that names
# uart0 rather than trust that nobody set one.
hat_refuse_uart0_console() {
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
}

# CANary-HAT board overlay
# ---------------------------------------------------------------------------
# The HAT's ID EEPROM names its revision's overlay ("canary-hat" on rev 2.0,
# "canary-hat-r21" on rev 2.1) and the firmware loads that .dtbo from
# /boot/firmware/overlays at boot — but only if the file is there. On a fresh
# image it is not, and the board comes up with no can0, no GNSS UART and the
# CAN transceiver in standby. Every revision's overlay is installed on every
# Pi, HAT or not: the firmware ignores an overlay nothing names, and a card then
# works with whichever board is stacked on it.
install_hat_overlay() {
  OVERLAY_DIR="${CANARY_OVERLAY_DIR:-${ROOT}/boot/firmware/overlays}"
  HAT_FIRMWARE_DIR="${CANARY_HOME}/hardware/canary-hat-firmware"
  HAT_REBOOT_NEEDED=0
  HAT_CHANGED_DTBOS=()
  I2C_REBOOT_NEEDED=0
  # Only the overlay the fitted board loads needs a reboot when it changes. A
  # CANary-HAT's EEPROM says which; without one, config.txt may name either.
  hat_loaded_overlay=""
  if [[ "${hat_product}" == CANary-HAT* ]]; then
    hat_loaded_overlay="$(hat_fact overlay 2>/dev/null)" || hat_loaded_overlay=""
  fi
  if [[ -d "${OVERLAY_DIR}" ]] && compgen -G "${HAT_FIRMWARE_DIR}/*-overlay.dts" >/dev/null; then
    if command -v dtc >/dev/null 2>&1; then
      for hat_src in "${HAT_FIRMWARE_DIR}"/*-overlay.dts; do
        hat_name="$(basename "${hat_src}" -overlay.dts)"
        hat_dtbo="$(mktemp)"
        if dtc -@ -q -I dts -O dtb -o "${hat_dtbo}" "${hat_src}"; then
          write_managed "hat-overlay-${hat_name}" "${OVERLAY_DIR}/${hat_name}.dtbo" <"${hat_dtbo}"
          if [[ -n "${CONFIG_CHANGED[hat-overlay-${hat_name}]:-}" ]] \
              && [[ -z "${hat_loaded_overlay}" || "${hat_loaded_overlay}" == "${hat_name}" ]]; then
            HAT_REBOOT_NEEDED=1
            HAT_CHANGED_DTBOS+=("${hat_name}.dtbo")
          fi
        else
          echo "WARNING: could not compile ${hat_src}; ${hat_name}.dtbo not updated" >&2
        fi
        rm -f "${hat_dtbo}"
      done
    else
      echo "WARNING: dtc not found (raspi-utils-dt); CANary-HAT overlays not installed" >&2
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
}
