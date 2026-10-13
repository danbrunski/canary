# CANary-HAT firmware and bring-up tools

The board has no MCU. Its "firmware" is the device-tree overlay the Pi firmware
loads at boot, named by the HAT+ ID EEPROM (U1). Everything here runs on the Pi
except `kline_decode.py`, which runs anywhere. Results from the first board are
in `docs/CANary-HAT-Revisions.md` §13.1.1.

## Two revisions, two pin maps

Rev 2.0 and rev 2.1 are both in service, and rev 2.1 moved pins
(`docs/CANary-HAT-Rev2.1.md` §13.5.13). Nothing here assumes one revision.

| | Rev 2.0 | Rev 2.1 |
|---|---|---|
| Overlay / EEPROM file | `canary-hat` / `eeprom_settings.txt` | `canary-hat-r21` / `eeprom_settings-r21.txt` |
| EEPROM product_ver | `0x0200` | `0x0201` |
| GNSS | `uart2`, GPIO4/5 → `/dev/ttyAMA2` | `uart4`, GPIO12/13 → `/dev/ttyAMA4` |
| GNSS PPS | GPIO18 | GPIO20 |
| K-line EN / pull-up | GPIO26 / GPIO24 | GPIO26 / GPIO21 |
| Barometer (U5) | yes | no (baro from the ECM, PID 0x33) |
| 12 V sense | ADS1015 (I²C 0x48) | ADS8688A CH0 (SPI0 CE1; driver still owed) |

`hat_revisions.json` is the table. `hat_rev.py` reads it for the scripts here
and for `install_pi.sh`; the daemon reads the same file
(`src/collection/hat_revision.py`). The fitted revision is the EEPROM's
product_ver. A Pi with no CANary-HAT EEPROM is treated as rev 2.0. To name one
instead, set `CANARY_HAT_REV=2.1` for the scripts and
`hardware.hat_revision: "2.1"` for the daemon. `python3 hat_rev.py --shell`
prints everything it knows about the fitted board.

| File | What it is |
|---|---|
| `canary-hat-overlay.dts` | Rev 2.0's overlay: `mcp251xfd` on SPI0 CE0 (40 MHz, nINT GPIO25) → `can0`; TCAN1042 STB (GPIO22) held low by a `gpio-hog`; GNSS on `uart2` → `/dev/ttyAMA2`; TIMEPULSE → `pps-gpio` on GPIO18; K-line `uart0` → `/dev/ttyAMA0`. No kernel drivers for the IMU, barometer or ADC — CANary reads those over `/dev/i2c-1`. |
| `canary-hat-r21-overlay.dts` | Rev 2.1's overlay: the same, except GNSS on `uart4` → `/dev/ttyAMA4` and TIMEPULSE on GPIO20. The ADS8688A on CE1 is left to userspace (`/dev/spidev0.1`). |
| `eeprom_settings.txt`, `eeprom_settings-r21.txt` | HAT+ v2 ID records for `eepmake`: `CANary-HAT rev 2.0` / `dt_blob "canary-hat"`, and `CANary-HAT rev 2.1` / `dt_blob "canary-hat-r21"`. |
| `hat_revisions.json`, `hat_rev.py` | The per-revision pin map, and its reader (standard library only). |
| `program_hat.sh` | Programs a board: installs every revision's `.dtbo`, comments out Waveshare `mcp2515` lines in `config.txt`, backs up then writes the EEPROM for `--rev 2.0` or `--rev 2.1`. |
| `checkout_hat.sh` | Pass/fail checkout of every subsystem of the fitted revision, saying what the bench cannot test. |
| `kline_bringup.py` | K-line: `sniff`, `echo` (single-wire loopback) and `fastinit` (StartCommunication → known-answer read → StopCommunication). |
| `kline_edges.py` | Passive edge capture of K-line — no transmit, pull-up off — for watching another tester (UpRev) at any baud. |
| `kline_decode.py` | Decodes an edge capture: bit time, wake-up pulses, bytes, messages. |
| `baro_diag.py` | Rev 2.0 only. U5 (BMP384) register-level diagnostic: each channel alone and together, STATUS/ERR per conversion, pressure compensated at the IMU's die temperature. |

## Programming a new board

JP1 must be **open** (bridged = EEPROM write-protected). On the Pi, from a copy
of this directory, naming the revision printed on the board:

```bash
sudo bash program_hat.sh --rev 2.1   # overlays + config.txt (incl. I2C) + EEPROM
sudo reboot
bash checkout_hat.sh
```

`--rev` is required on a blank EEPROM. Re-programming a board whose EEPROM
already names its revision may leave it out. The wrong revision would load the
other board's overlay, which leaves the GNSS dark and the K-line pull-up on a
dead pin.

`scripts/install_pi.sh` also installs both revisions' overlays on every run, so
a fresh image finds whichever one the EEPROM names. When the firmware reports a
CANary-HAT, it points gpsd at that revision's UART (`/dev/ttyAMA2` or
`/dev/ttyAMA4`). It asks for a reboot only when the fitted board's overlay
changed.

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
collector if it is enabled (the port and the EN/pull-up lines are exclusive).
Both scripts take their pins from the fitted revision; `kline_bringup.py --rev
2.1 ...` or `CANARY_HAT_REV=2.1` names it:

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
rises, RX discarded across the mode change, EN and the pull-up (GPIO24 on rev
2.0, GPIO21 on rev 2.1) dropped on every exit path. With EN low after power-up the transceiver sits in standby
with RXD low; one EN cycle takes it to sleep with RXD high.
