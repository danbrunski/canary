# H03 — HAT platform: 12 V sense, PPS, GNSS rate, uart0, barometer

Part of [H00](H00-hat-integration-README.md). The board-level services every
other checklist leans on.

## Where it stands

- **12 V sense works** (ADS1015 at I²C1 0x48, AIN0 behind a 100 k/22 k
  divider: 12.68 V key-off, 12.43 V KOEO) and nothing records it. The only
  vehicle-voltage channel is `obd:CONTROL_MODULE_VOLTAGE`, which needs the ECM
  awake. `NonvolatileWriteConfig.min_battery_voltage` exists and **no
  production code supplies the voltage it gates on.**
- **GNSS feeds the daemon** through gpsd on `/dev/ttyAMA2` (since `260dbcc`),
  at the M10's default 1 Hz and 9600 baud. The dyno and the speed committee
  want 10 Hz.
- **PPS pulses** (`pps-gpio` on GPIO18, a `/dev/ppsN` named `canary-pps`) but
  chrony uses only NMEA time through gpsd SHM 0, at `precision 5e-1` because
  NMEA arrives ~350 ms late.
- **uart0 (K-line) is dormant** in the overlay (`dtoverlay=canary-hat,kline`
  or a runtime `dtoverlay uart0-pi5`). H05 needs it on.
- **The barometer is dead** on this board (operator decision 3: parked).

## Rules

- The divider ratio, the ADC's range and any per-board calibration are data
  (config / HAT profile), never constants in a collector.
- Changing the GNSS receiver's configuration is fine; it is not the ECU.
- A clock change mid-session fractures session files (`docs/time-sync.md`):
  anything that makes chrony trust a new source is tested for step behaviour.

---

## Desk

- [x] **H03.1 12 V sense collector.** **Do:** an ADS1015 driver on the same
  bus abstraction as H02.2 (single-shot, AIN0 single-ended, ±4.096 V FSR,
  11.1 mV per LSB at the 12 V side), a collector emitting
  `sensor:SUPPLY_VOLTAGE` in volts at a configured rate (default 10 Hz), with
  the divider ratio in config (`hardware.hat.supply_divider`, default
  122/22). `sensor:` is the existing source for external sensors
  (`telemetry.py` already anticipates "an ADC-read sender"), so no new source.
  **Accept:** fake-bus tests for the conversion and the reconnect path;
  H01.1 keeps it out of the detectors.
  **Evidence (2026-09-23):** `src/collection/ads1015.py` (single-shot,
  single-ended, 16-bit registers through the new `RegisterBus.write_block`),
  `src/collection/supply_voltage_collector.py`, and
  `hardware.supply_voltage` (`SupplyVoltageConfig`: bus, address, input,
  `divider_ratio` default 122/22, `rate_hz` default 10, disabled by default),
  wired through `_supply_voltage_collectors`. 11 tests
  (`tests/test_supply_voltage.py`) pin the two bring-up answers: config word
  `0xC383` and code 1143 = 12.68 V. **On the HAT:** 50 conversions in 128 ms,
  12.311 V, steady (the truck resting since the KOEO test). The config key is
  `hardware.supply_voltage`, not the `hardware.hat.supply_divider` sketched
  above: the HAT has no other block to share. Not enabled on the Pi until
  H01.1 keeps `sensor:` out of the detectors.
- [ ] **H03.2 Feed the non-volatile write gate.** **Do:** supply
  `NonvolatileWriteSignals.battery_voltage` from `sensor:SUPPLY_VOLTAGE` when
  it is fresh, else from `obd:CONTROL_MODULE_VOLTAGE`, and make an unknown
  voltage a refusal with its reason, not a pass. **Accept:** tests for fresh,
  stale and absent voltage. (No write is ever started by this; it only makes
  the existing gate able to say no.)
- [~] **H03.3 PPS as a chrony refclock.** **Do:** a udev rule giving the
  `canary-pps` device a stable name (`/dev/pps-canary`), and in
  `install_pi.sh`'s chrony drop-in `refclock PPS /dev/pps-canary lock GPS
  refid PPS`, only when the HAT is detected. **Accept:**
  `test_install_pi_script.py` covers HAT and no-HAT; **(Pi)** `chronyc
  sources` shows PPS selected with sub-millisecond offset, and a cold boot
  away from the network still steps once, early.
  **Partial (2026-09-23):** `install_pi.sh`, when the HAT is detected, writes
  `70-canary-pps.rules` (`/dev/pps-canary` by device name, since the Ethernet
  PHY's PTP clock is also a `/dev/pps`) and adds
  `refclock PPS /dev/pps-canary lock GPS refid PPS precision 1e-7 prefer` to
  the chrony drop-in, whose NMEA `GPS` source it locks to. Locking needs NMEA
  within ±0.5 s of true time: measured on the HAT's receiver at 9600 baud,
  +154 ms with a standard deviation under 1 ms (chrony against NTP). Three
  installer tests (HAT, no HAT, HAT fitted later restarts chrony once).
  **Owed (Pi):** deploy, and check `chronyc sources` selects PPS with a fix.
- [ ] **H03.4 GNSS at 10 Hz.** **Do:** configure the MAX-M10M on every gpsd
  attach (it has no backup cell fitted, so settings in RAM do not survive a
  power cut): UBX `CFG-VALSET` for `CFG-RATE-MEAS` 100 ms and a UART1 baud that
  carries 10 Hz of the sentences gpsd needs (38400 or higher), sent by
  `canary-gpsd@.service` before `gpsdctl add`, or by gpsd's own `-s`/`ubxtool`.
  **Accept:** **(Pi)** `gpspipe -w` shows TPV at 10 Hz; the daemon's
  `gps:speed` arrives at ~10 Hz.
- [ ] **H03.5 uart0 on by default in the overlay** once H05 has a collector
  that uses it. The SysRq hazard needs a console on uart0, and on the Pi 5 the
  console is `ttyAMA10`; make `install_pi.sh` refuse a `console=` on
  `ttyAMA0`/`serial0`-mapped-to-uart0 rather than trusting it. **Accept:**
  installer test for the refusal; the dormant `kline` parameter removed from
  the overlay and the README.

## Hardware

- [ ] **H03.6 Barometer** **(hardware)**. Replace U5 (a BMP390 is footprint
  compatible, `CANary-HAT-Circuits.md` §3.5), rerun `checkout_hat.sh`, then a
  collector on the H02.2 bus abstraction. Decide the namespace then:
  `committees.py` names `baro:`, which is not a `TelemetrySource`; putting it
  under `sensor:` avoids a new source. Unblocks H04.4 and HAT measurement 9.
