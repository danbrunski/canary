#!/usr/bin/env bash
# CANary-HAT checkout (rev 2.0 or 2.1), run on the Pi after program_hat.sh + reboot.
# The revision comes from the ID EEPROM (hat_rev.py); CANARY_HAT_REV=2.1 names it.
# Read-only except for bringing can0 up listen-only (sudo) when it is down.
# Works with no vehicle, no antenna and no UPS attached; says what each
# missing thing makes unmeasurable instead of calling it a failure.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

ok()   { printf '  PASS  %s\n' "$*"; }
bad()  { printf '  FAIL  %s\n' "$*"; }
note() { printf '  ----  %s\n' "$*"; }

echo "== HAT+ EEPROM"
HAT_DIR="${CANARY_HAT_DIR:-/proc/device-tree/hat}"  # hat_rev.py reads the same
if [[ -d "$HAT_DIR" ]]; then
    for f in vendor product product_id product_ver uuid; do
        [[ -f "$HAT_DIR/$f" ]] && note "$f: $(tr -d '\0' < "$HAT_DIR/$f")"
    done
    ok "firmware read the ID EEPROM"
else
    bad "no $HAT_DIR -- EEPROM blank, unreadable, or not programmed yet"
fi
if ! facts="$(python3 "$HERE/hat_rev.py" --shell)"; then
    bad "cannot tell which revision this is (above); stopping"
    exit 1
fi
eval "$facts"
case "$HAT_HOW" in
    eeprom) ok "rev $HAT_REV (from the EEPROM)" ;;
    named) note "rev $HAT_REV (CANARY_HAT_REV), whatever the EEPROM says" ;;
    *) note "checking as rev $HAT_REV, the default for a board with no CANary-HAT EEPROM" ;;
esac

echo "== CAN (MCP2518FD + TCAN1042)"
(dmesg 2>/dev/null || sudo dmesg) | grep -i mcp251xfd | tail -3 | sed 's/^/        /'
if ip link show can0 >/dev/null 2>&1; then
    ok "can0 exists"
    if ! ip link show can0 | grep -q 'state UP\|LOWER_UP'; then
        sudo ip link set can0 up type can bitrate 500000 listen-only on && note "brought can0 up listen-only @500k"
    fi
    ip -details link show can0 | grep -E -o 'clock [0-9]+|state [A-Z-]+|bitrate [0-9]+' | sed 's/^/        /'
else
    bad "no can0 -- overlay not applied or MCP2518FD probe failed (check dmesg for osc/SPI errors)"
fi
stb="$(pinctrl get 22)"
[[ "$stb" == *"| lo"* ]] && ok "STB (GPIO22) low -> transceiver in normal mode" || bad "STB not low: $stb"
rx0="$(cat /sys/class/net/can0/statistics/rx_packets 2>/dev/null || echo 0)"; sleep 1
rx1="$(cat /sys/class/net/can0/statistics/rx_packets 2>/dev/null || echo 0)"
if (( rx1 > rx0 )); then
    ok "receiving $((rx1 - rx0)) frames/s (includes echoes of our own TX); D6 should look solid"
else
    note "no frames in 1 s -- expected with no vehicle or key off; D6 blinks only with our own TX"
fi

echo "== I2C1 sensors + 12V sense"
HAT_BAROMETER="$HAT_BAROMETER" HAT_SUPPLY_ADC="$HAT_SUPPLY_ADC" python3 - <<'EOF'
import os, sys, time
from smbus2 import SMBus
b = SMBus(1)
def id(a, r):
    try: return b.read_byte_data(a, r)
    except OSError: return None
v = id(0x6B, 0x0F); print("  %s  LSM6DSOX WHO_AM_I=%s" % ("PASS" if v == 0x6C else "FAIL", v and hex(v)))
v = None
if os.environ["HAT_BAROMETER"] == "1":
    v = id(0x77, 0x00); print("  %s  BMP384 CHIP_ID=%s" % ("PASS" if v == 0x50 else "FAIL", v and hex(v)))
else:
    print("  ----  no barometer on this revision (baro comes from the ECM, PID 0x33)")
if v == 0x50:
    b.write_byte_data(0x77, 0x7E, 0xB6); time.sleep(0.05)
    b.write_byte_data(0x77, 0x1B, 0x13); time.sleep(0.1)
    d = b.read_i2c_block_data(0x77, 0x04, 6)
    p = d[0] | d[1] << 8 | d[2] << 16; t = d[3] | d[4] << 8 | d[5] << 16
    print("  %s  BMP384 conversion raw P=%d T=%d" % ("FAIL" if t == 0 else "PASS", p, t))
if os.environ["HAT_SUPPLY_ADC"] != "ads1015":
    print("  ----  12 V sense is on the %s (CH0); this script has no reader for it yet"
          % os.environ["HAT_SUPPLY_ADC"].upper())
    sys.exit(0)
cfg = b.read_i2c_block_data(0x48, 0x01, 2)
b.write_i2c_block_data(0x48, 0x01, [0xC3, 0x83]); time.sleep(0.01)
r = b.read_i2c_block_data(0x48, 0x00, 2); code = int.from_bytes(bytes(r), "big", signed=True) >> 4
b.write_i2c_block_data(0x48, 0x01, cfg)
v12 = code * 2e-3 * 122 / 22
if v12 > 9:
    print("  PASS  +12V input stage + sense: %.2f V" % v12)
elif v12 < 0.5:
    print("  ----  +12V absent (%.2f V) -- no vehicle; input stage untested" % v12)
else:
    print("  FAIL  +12V sense reads %.2f V -- neither absent nor a battery" % v12)
EOF

tty="$HAT_GNSS_TTY"; name="${tty#/dev/}"
echo "== GNSS ($HAT_GNSS_UART -> $tty) + PPS (GPIO$HAT_PPS_GPIO)"
if [[ -e "$tty" ]]; then
    ok "$tty exists"
    if fuser "$tty" >/dev/null 2>&1; then
        note "$name is held by $(fuser "$tty" 2>&1 | awk '{print $2}' | xargs -r ps -o comm= -p) -- skipping raw read"
    else
        stty -F "$tty" 9600 raw -echo
        nmea="$(timeout 3 cat "$tty" | grep -a -E '^\$G.(GGA|RMC)' | tail -2)"
        if [[ -n "$nmea" ]]; then
            ok "NMEA on $name"; sed 's/^/        /' <<<"$nmea"
        else
            bad "no NMEA on $name in 3 s -- check GPIO$HAT_GNSS_GPIOS are muxed to $HAT_GNSS_UART (pinctrl get $HAT_GNSS_GPIOS)"
        fi
    fi
else
    bad "no $tty -- $HAT_GNSS_UART not enabled (is the rev $HAT_REV overlay, $HAT_OVERLAY, the one loaded?)"
fi
pps="$(grep -l canary-pps /sys/class/pps/pps*/name 2>/dev/null | head -1)"
if [[ -n "$pps" ]]; then
    dir="$(dirname "$pps")"; a1="$(cat "$dir/assert")"; sleep 2; a2="$(cat "$dir/assert")"
    [[ "$a1" != "$a2" ]] && ok "PPS pulsing on $(basename "$dir"): $a2" || note "PPS registered ($(basename "$dir")) but no pulses -- normal until the receiver has a fix"
else
    bad "no pps-gpio device"
fi

echo "== Not covered by this script"
note "K-line (TLIN1027): kline_bringup.py echo / fastinit -- needs +12V on J2 and uart0 (README.md)"
note "D6 CAN LED and the X1202 12 V feed (rev 2.0 J3, rev 2.1 J2 pins 3-4): visual / meter checks"
if [[ "$HAT_REV" != "2.0" ]]; then
    note "ADS8688A (SPI0 CE1) and the 5 V sensor supply (GPIO19 SENS_PWR_EN): no checks yet"
fi
note "X1202 coexistence (fuel gauge 0x36, GPIO6): needs the UPS stacked"
