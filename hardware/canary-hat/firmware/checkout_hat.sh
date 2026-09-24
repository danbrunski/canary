#!/usr/bin/env bash
# CANary-HAT rev 2.0 checkout, run on the Pi after program_hat.sh + reboot.
# Read-only except for bringing can0 up listen-only (sudo) when it is down.
# Works with no vehicle, no antenna and no UPS attached; says what each
# missing thing makes unmeasurable instead of calling it a failure.
set -uo pipefail

ok()   { printf '  PASS  %s\n' "$*"; }
bad()  { printf '  FAIL  %s\n' "$*"; }
note() { printf '  ----  %s\n' "$*"; }

echo "== HAT+ EEPROM"
if [[ -d /proc/device-tree/hat ]]; then
    for f in vendor product product_id product_ver uuid; do
        [[ -f /proc/device-tree/hat/$f ]] && note "$f: $(tr -d '\0' < /proc/device-tree/hat/$f)"
    done
    ok "firmware read the ID EEPROM"
else
    bad "no /proc/device-tree/hat -- EEPROM blank, unreadable, or not programmed yet"
fi

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
python3 - <<'EOF'
import time
from smbus2 import SMBus
b = SMBus(1)
def id(a, r):
    try: return b.read_byte_data(a, r)
    except OSError: return None
v = id(0x6B, 0x0F); print("  %s  LSM6DSOX WHO_AM_I=%s" % ("PASS" if v == 0x6C else "FAIL", v and hex(v)))
v = id(0x77, 0x00); print("  %s  BMP384 CHIP_ID=%s" % ("PASS" if v == 0x50 else "FAIL", v and hex(v)))
if v == 0x50:
    b.write_byte_data(0x77, 0x7E, 0xB6); time.sleep(0.05)
    b.write_byte_data(0x77, 0x1B, 0x13); time.sleep(0.1)
    d = b.read_i2c_block_data(0x77, 0x04, 6)
    p = d[0] | d[1] << 8 | d[2] << 16; t = d[3] | d[4] << 8 | d[5] << 16
    print("  %s  BMP384 conversion raw P=%d T=%d" % ("FAIL" if t == 0 else "PASS", p, t))
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

echo "== GNSS (uart2 -> /dev/ttyAMA2) + PPS"
if [[ -e /dev/ttyAMA2 ]]; then
    ok "/dev/ttyAMA2 exists"
    if fuser /dev/ttyAMA2 >/dev/null 2>&1; then
        note "ttyAMA2 is held by $(fuser /dev/ttyAMA2 2>&1 | awk '{print $2}' | xargs -r ps -o comm= -p) -- skipping raw read"
    else
        stty -F /dev/ttyAMA2 9600 raw -echo
        nmea="$(timeout 3 cat /dev/ttyAMA2 | grep -a -E '^\$G.(GGA|RMC)' | tail -2)"
        if [[ -n "$nmea" ]]; then
            ok "NMEA on ttyAMA2"; sed 's/^/        /' <<<"$nmea"
        else
            bad "no NMEA on ttyAMA2 in 3 s -- check GPIO4/5 are muxed to uart2 (pinctrl get 4-5)"
        fi
    fi
else
    bad "no /dev/ttyAMA2 -- uart2 not enabled"
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
note "D6 CAN LED and J3 pass-through: visual / meter checks"
note "X1202 coexistence (fuel gauge 0x36, GPIO6): needs the UPS stacked"
