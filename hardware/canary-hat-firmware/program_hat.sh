#!/usr/bin/env bash
# Program a CANary-HAT on this Pi 5: install the board overlays, retire the
# Waveshare MCP2515 config lines, and write the HAT+ ID EEPROM.
#
#   sudo bash program_hat.sh --rev 2.1              # overlay + config + EEPROM
#   sudo bash program_hat.sh --rev 2.0 --no-eeprom  # overlay + config.txt only
#
# The revision is printed on the board. Each revision wires the GNSS and the
# K-line pull-up differently (hat_revisions.json), and the EEPROM tells the
# firmware which overlay to load, so it must be the right one. --rev may be left
# out only to re-program a board whose EEPROM already names its revision.
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
REV=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-eeprom) WRITE_EEPROM=0 ;;
        --rev) REV="${2:?--rev needs 2.0 or 2.1}"; shift ;;
        --rev=*) REV="${1#--rev=}" ;;
        *) echo "unknown argument: $1" >&2; exit 1 ;;
    esac
    shift
done

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
for tool in dtc eepmake eepdump eepflash.sh; do
    command -v "$tool" >/dev/null || { echo "missing $tool (raspi-utils)" >&2; exit 1; }
done

# Which board this is: --rev, or what its EEPROM already says. A blank EEPROM
# would read as "unidentified" (rev 2.0), which is a guess, not an answer.
if [[ -n "$REV" ]]; then
    facts="$(python3 "$HERE/hat_rev.py" --rev "$REV" --shell)" || exit 1
else
    facts="$(python3 "$HERE/hat_rev.py" --shell)" || exit 1
    if ! grep -q '^HAT_HOW=eeprom$' <<<"$facts"; then
        echo "which board is this? run with --rev 2.0 or --rev 2.1 (printed on the board)" >&2
        exit 1
    fi
fi
eval "$facts"
echo "CANary-HAT rev $HAT_REV: overlay $HAT_OVERLAY, EEPROM settings $HAT_EEPROM_SETTINGS"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Every revision's overlay, so the card works with whichever board is stacked.
echo "== overlays"
for src in "$HERE"/*-overlay.dts; do
    name="$(basename "$src" -overlay.dts)"
    dtc -@ -q -I dts -O dtb -o "$work/$name.dtbo" "$src"
    install -m 0644 "$work/$name.dtbo" "$OVERLAYS/$name.dtbo"
    echo "installed $OVERLAYS/$name.dtbo"
done

echo "== config.txt"
cp "$CONFIG" "$CONFIG.bak-$STAMP"
# The Waveshare overlays claim spi0 CE0/CE1 and GPIO23/25, which rev 2.0 wires
# to the MCP2518FD and the IMU's INT2.
sed -i -E 's/^(dtoverlay=mcp2515-can[01].*)$/#waveshare# \1/' "$CONFIG"
# Only one board overlay may load: the other revision's would claim the same
# SPI and GPIOs for a different wiring.
sed -i -E "/^dtoverlay=${HAT_OVERLAY}([,[:space:]]|$)/! s/^(dtoverlay=canary-hat.*)$/#other-rev# \1/" "$CONFIG"
if [[ $WRITE_EEPROM -eq 0 ]]; then
    grep -qE "^dtoverlay=${HAT_OVERLAY}([,[:space:]]|$)" "$CONFIG" \
        || printf '\n[all]\ndtoverlay=%s\n' "$HAT_OVERLAY" >> "$CONFIG"
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
    eepmake "$HERE/$HAT_EEPROM_SETTINGS" "$work/canary-hat.eep"
    eepdump "$work/canary-hat.eep" "$work/canary-hat.txt" >/dev/null && sed -n '1,40p' "$work/canary-hat.txt"
    backup="$HERE/eeprom-backup-$STAMP.eep"
    echo "reading current contents -> $backup"
    eepflash.sh -r -y -t=24c256 -a=50 -f="$backup"
    echo "writing canary-hat.eep"
    eepflash.sh -w -y -t=24c256 -a=50 -f="$work/canary-hat.eep"
    cp "$work/canary-hat.eep" "$HERE/$HAT_OVERLAY-$STAMP.eep"
fi

# On 2026-10-11 the Pi lost power ~30 s after a run and the overlay and
# config.txt edits never reached the card. Flush before saying "done".
sync
echo
echo "done -- reboot, then: bash $HERE/checkout_hat.sh"
