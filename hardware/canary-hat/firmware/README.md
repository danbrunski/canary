# CANary-HAT firmware and bring-up tools

Rev 2.0 has no MCU. Its "firmware" is the device-tree overlay the Pi firmware
loads at boot, named by the HAT+ ID EEPROM (U1). Everything here runs on the Pi
except `kline_decode.py`, which runs anywhere. Results from the first board are
in `docs/CANary-HAT-Revisions.md` §13.1.1.

| File | What it is |
|---|---|
| `canary-hat-overlay.dts` | The board overlay: `mcp251xfd` on SPI0 CE0 (40 MHz, nINT GPIO25) → `can0`; TCAN1042 STB (GPIO22) held low by a `gpio-hog`; GNSS on `uart2` → `/dev/ttyAMA2`; TIMEPULSE → `pps-gpio` on GPIO18; K-line `uart0` → `/dev/ttyAMA0`. No kernel drivers for the IMU, barometer or ADC — CANary reads those over `/dev/i2c-1`. |
| `eeprom_settings.txt` | HAT+ v2 ID record for `eepmake`: `CANary-HAT rev 2.0`, `dt_blob "canary-hat"`. |
| `program_hat.sh` | Programs a board: installs the `.dtbo`, comments out Waveshare `mcp2515` lines in `config.txt`, backs up then writes the EEPROM. |
| `checkout_hat.sh` | Pass/fail checkout of every subsystem, saying what the bench cannot test. |
| `kline_bringup.py` | K-line: `sniff`, `echo` (single-wire loopback) and `fastinit` (StartCommunication → known-answer read → StopCommunication). |
| `kline_edges.py` | Passive edge capture of K-line — no transmit, pull-up off — for watching another tester (UpRev) at any baud. |
| `kline_decode.py` | Decodes an edge capture: bit time, wake-up pulses, bytes, messages. |
| `baro_diag.py` | U5 (BMP384) register-level diagnostic: each channel alone and together, STATUS/ERR per conversion, pressure compensated at the IMU's die temperature. |

## Programming a new board

JP1 must be **open** (bridged = EEPROM write-protected). On the Pi, from a copy
of this directory:

```bash
sudo bash program_hat.sh        # overlay + config.txt + EEPROM (asks nothing)
sudo reboot
bash checkout_hat.sh
```

`scripts/install_pi.sh` also installs `canary-hat.dtbo` on every run (so a fresh
image finds the overlay the EEPROM names) and, when the firmware reports a
CANary-HAT, points gpsd at `/dev/ttyAMA2`. It asks for a reboot when the
overlay changed.

## K-line

uart0 (`/dev/ttyAMA0`) is on in the overlay. On the Pi 5 the console stays on
the debug UART (`serial0` → `ttyAMA10`, checked on the HAT), and
`install_pi.sh` refuses a `console=` that names uart0: a wake-up pulse would
reach it as a serial break (SysRq).

In the daemon, `hardware.kline.enabled` lets the K-line collector sniff
whenever no capture profile asks for a session, and read the ECU when one does
(`docs/roadmap/tasks/H05-kline-native.md`, `H06-kline-sniffer.md`). For
bring-up by hand, put the daemon in passive mode first — the ECM has one
diagnostic session shared between CAN and K-line — and stop its K-line
collector if it is enabled (the port and the EN/pull-up lines are exclusive):

```bash
curl -sX POST http://canary.local:8000/control/passive \
  -H "Authorization: Bearer $CANARY_CONTROL_TOKEN" \
  -H 'Content-Type: application/json' -d '{"enabled": true, "ttl_seconds": 600}'
python3 kline_bringup.py echo       # hardware only; no ECU involved
python3 kline_bringup.py fastinit   # KOEO; defaults to what the Xterra ECM answers
```

`fastinit` defaults to tester `0xFC`, ECM `0x10`, trying the standard 25 ms wake-up
and then the 518.9 ms one this ECM actually answers (vehicle skill,
`nissan-xterra-2008`). Everything it sends is volatile; nothing writes the ECM.

To watch another tester at an unknown baud or with a 5-baud init, capture raw
edges. Claiming GPIO15 re-muxes it away from uart0 and the kernel leaves it
that way, so the script restores it on exit (`pinctrl set 15 a4` by hand if it
was killed):

```bash
python3 kline_edges.py 300 capture.txt      # Ctrl-C stops early, cleanly
python3 kline_decode.py capture.txt --baud 10400
```

The TLIN1027 order from `docs/CANary-HAT-Spec.md` §11 is enforced by both
scripts and by `src/collection/kline/transport.py`: tty open before EN (GPIO26)
rises, RX discarded across the mode change, EN and the pull-up (GPIO24) dropped
on every exit path. With EN low after power-up the transceiver sits in standby
with RXD low; one EN cycle takes it to sleep with RXD high.
