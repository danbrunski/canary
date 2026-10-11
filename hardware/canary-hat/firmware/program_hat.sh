#!/usr/bin/env bash
# Program a CANary-HAT rev 2.0 on this Pi 5: install the board overlay, retire
# the Waveshare MCP2515 config lines, and write the HAT+ ID EEPROM.
#
#   sudo bash program_hat.sh            # overlay + config + EEPROM
#   sudo bash program_hat.sh --no-eeprom  # overlay + config only (via config.txt)
#
# Re-runnable. Backs up config.txt and the current EEPROM contents first.
# JP1 must stay OPEN (bridged = write-protected). Reboot afterwards, then run
# checkout_hat.sh.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CONFIG=/boot/firmware/config.txt
OVERLAYS=/boot/firmware/overlays
STAMP="$(date +%Y%m%d-%H%M%S)"
WRITE_EEPROM=1
[[ "${1:-}" == "--no-eeprom" ]] && WRITE_EEPROM=0

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
for tool in dtc eepmake eepdump eepflash.sh; do
    command -v "$tool" >/dev/null || { echo "missing $tool (raspi-utils)" >&2; exit 1; }
done

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "== overlay"
dtc -@ -q -I dts -O dtb -o "$work/canary-hat.dtbo" "$HERE/canary-hat-overlay.dts"
install -m 0644 "$work/canary-hat.dtbo" "$OVERLAYS/canary-hat.dtbo"
echo "installed $OVERLAYS/canary-hat.dtbo"

echo "== config.txt"
cp "$CONFIG" "$CONFIG.bak-$STAMP"
# The Waveshare overlays claim spi0 CE0/CE1 and GPIO23/25, which rev 2.0 wires
# to the MCP2518FD and the IMU's INT2.
sed -i -E 's/^(dtoverlay=mcp2515-can[01].*)$/#waveshare# \1/' "$CONFIG"
if [[ $WRITE_EEPROM -eq 0 ]]; then
    grep -q '^dtoverlay=canary-hat' "$CONFIG" || printf '\n[all]\ndtoverlay=canary-hat\n' >> "$CONFIG"
else
    # The EEPROM names the overlay; a config.txt line as well would load it twice.
    sed -i -E 's/^(dtoverlay=canary-hat.*)$/#eeprom# \1/' "$CONFIG"
fi
# The IMU, barometer and 12 V sense are read over /dev/i2c-1, which a fresh
# image leaves off (install_pi.sh adds the same line; 2026-10-11).
grep -qE '^[[:space:]]*dtparam=([^#]*,)?i2c(_arm)?=on' "$CONFIG" \
    || printf '\n[all]\ndtparam=i2c_arm=on\n' >> "$CONFIG"
grep -n -E 'mcp2515|canary-hat|i2c_arm' "$CONFIG" || true
echo "backup: $CONFIG.bak-$STAMP"

if [[ $WRITE_EEPROM -eq 1 ]]; then
    echo "== EEPROM"
    eepmake "$HERE/eeprom_settings.txt" "$work/canary-hat.eep"
    eepdump "$work/canary-hat.eep" "$work/canary-hat.txt" >/dev/null && sed -n '1,40p' "$work/canary-hat.txt"
    backup="$HERE/eeprom-backup-$STAMP.eep"
    echo "reading current contents -> $backup"
    eepflash.sh -r -y -t=24c256 -a=50 -f="$backup"
    echo "writing canary-hat.eep"
    eepflash.sh -w -y -t=24c256 -a=50 -f="$work/canary-hat.eep"
    cp "$work/canary-hat.eep" "$HERE/canary-hat-$STAMP.eep"
fi

# On 2026-10-11 the Pi lost power ~30 s after a run and the overlay and
# config.txt edits never reached the card. Flush before saying "done".
sync
echo
echo "done -- reboot, then: bash $HERE/checkout_hat.sh"
