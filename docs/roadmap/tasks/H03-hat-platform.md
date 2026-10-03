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
- [x] **H03.2 Feed the non-volatile write gate.** **Do:** supply
  `NonvolatileWriteSignals.battery_voltage` from `sensor:SUPPLY_VOLTAGE` when
  it is fresh, else from `obd:CONTROL_MODULE_VOLTAGE`, and make an unknown
  voltage a refusal with its reason, not a pass. **Accept:** tests for fresh,
  stale and absent voltage. (No write is ever started by this; it only makes
  the existing gate able to say no.)
  **Evidence (2026-09-26):** `src/control/nonvolatile_write.py`:
  `supply_voltage_from_latest_rows(rows, now=, max_age_seconds=)` (pure; rows
  are `read_latest_per_channel`'s) tries `SUPPLY_VOLTAGE_SOURCES` in order and
  returns the first finite, dated, non-future value no older than the limit.
  `battery_voltage` is now a `SupplyVoltageReading` (volts, source, age,
  reason), not a `float | None`, so an unknown voltage cannot be built without
  a reason; the guard refuses `battery_unknown` quoting it ("no fresh supply
  voltage: sensor:SUPPLY_VOLTAGE is stale (60.0 s old, limit 5 s);
  obd:CONTROL_MODULE_VOLTAGE has no reading in the latest rows"), and
  re-checks the age itself (`battery_stale`) rather than trusting the reader;
  the audit entry carries the reading. `SupplyVoltageReading.__post_init__`
  makes a half-formed reading unconstructible (unknown without a reason; a
  non-finite voltage; no source; an age that is missing, NaN, infinite or
  negative), and the guard still re-checks: its age test is
  `not (0 <= age <= max)` with an explicit finiteness check, because the
  first version's `age < 0 or age > max` passed a NaN age (caught by the
  2026-09-26 verification). **Row source fixed too:**
  `dashboard.readers.read_latest_per_channel` used `groupby(...).last()`,
  which takes each column's last *non-null* value separately, so a newest
  `NO DATA`/NaN row came back with its fresh timestamp and an older row's
  number — a stale 12.6 V read as current. It now takes the literal newest
  row per channel (`groupby().tail(1)`, still sorted by source, channel);
  `tests/test_latest_per_channel.py` (3 tests, 2 of which fail on the old
  reader) includes the end-to-end repro reading as unknown. Its other callers
  (`/telemetry/latest?per_channel`, the companion seed, the Parquet
  `vehicle_state`) now show a newest row's own value: `None`/its text instead
  of an older number, and an rpm-less newest OBD row classifies as rpm
  missing rather than as the older rpm. Tests (`tests/test_nonvolatile_write.py`,
  70 pass): the fresh/stale/absent 3x3 table for both sources, newest row per
  source, NaN / text / bool / future-stamped / undated values unknown, numpy
  scalars and pandas Timestamps read, the guard refusing unknown and low and
  passing a fresh sensor or fresh-OBD fallback, forged readings (stale,
  negative, NaN, ±inf ages; NaN/inf volts) refused by the guard, and ten
  malformed readings refused at construction. **Not wired:** nothing
  constructs `NonvolatileWriteGuard` in production (no flash transport
  exists, and a test forbids any route that mints an authorization), so there
  is no production `SignalReader` to hand it to yet; the future transport
  composes `read_latest_per_channel(storage,
  session_dir=active_session_dir(writer))` (now row-intact) with this
  function. Until H03.1's collector is enabled on the Pi, the OBD fallback is
  the source that answers.
- [~] **H03.3 PPS as a chrony refclock.** **Do:** feed the HAT's PPS to
  chrony through gpsd and a `refclock SOCK` (chrony creates the socket; gpsd
  writes pulses to it when it has them), **not** `refclock PPS`: chronyd exits
  fatally when a configured PPS device is missing, so a direct refclock ties
  all time sync to the overlay loading and to a udev race at boot. Lock it to
  the NMEA `GPS` source. **Accept:** `test_install_pi_script.py` covers HAT and
  no HAT; **(Pi)** `chronyc sources` selects PPS with sub-millisecond offset;
  chrony still starts with the overlay removed; a cold boot away from the
  network still steps once, early.
  **What 2026-09-23 established:** (1) the kernel device's name reads
  `canary-pps.-1`, not `canary-pps` — the installer's udev rule now matches
  `canary-pps*` and gives `/dev/pps-canary` (the Ethernet PHY's PTP clock and
  gpsd's tty line discipline are also `/dev/pps` devices, so the number moves);
  (2) the first deploy's `refclock PPS /dev/pps-canary` line, with the exact
  match that never fired, **stopped chronyd at start** ("Could not open
  /dev/pps-canary"), leaving the Pi without time sync until the rule was fixed
  by hand — the installer no longer writes that line, and a test forbids it;
  (3) pulses do arrive (the assert counter advances at 1 Hz with a fix), but
  the direct refclock, locked to GPS, took no samples in over a minute — to
  understand with the SOCK path; (4) the HAT's NMEA runs +154..161 ms late
  (std dev < 1 ms) at 9600 baud, inside the ±0.5 s a lock needs.
  *Desk part done 2026-10-03; the Pi checks are owed.* On a HAT install:
  - `canary-gps.conf` gains `refclock SOCK /run/chrony.pps-canary.sock refid
    PPS lock GPS`. chronyd creates that socket itself, so a missing overlay
    leaves an unusable source and never stops chronyd.
  - `70-canary-pps.rules` now also pulls in the new
    `systemd/canary-gpsd-pps.service`, which runs `gpsdctl add
    /dev/pps-canary`. gpsd names the chrony socket after the path it is
    handed, so the unit hands it the stable symlink. The template
    `canary-gpsd@` cannot do this: its instance is either the moving `ppsN`
    or, unescaped, `/dev/pps/canary`. `BindsTo=dev-pps\x2dcanary.device`
    ties the attachment to the device.
  - Without a HAT there is no SOCK line, no rule and no unit, and the
    no-HAT chrony file is byte-identical to before.

  Tests in `test_install_pi_script.py`:
  `test_a_canary_hat_feeds_pps_to_chrony_through_a_gpsd_socket` and
  `test_without_a_hat_chrony_has_no_pps_source` (now also asserts no SOCK
  line and no unit). The existing no-`refclock PPS` guard still passes; the
  file is 27 of 27.

  **Pi, unverified:** gpsd must still hold the privileges to open the chrony
  socket after it starts (Debian runs gpsd as root, then drops privileges).
  The socket name must match what this gpsd version writes for a bare
  `/dev/pps*` device. `chronyc sources` should then show `#* PPS` with a
  sub-ms offset, and chrony must still start with the overlay removed. If
  gpsd writes nothing, the H03.3 note above about the direct refclock taking
  no samples applies here too.

- [ ] **H03.4 GNSS at 10 Hz.** **Do:** configure the MAX-M10M on every gpsd
  attach (it has no backup cell fitted, so settings in RAM do not survive a
  power cut): UBX `CFG-VALSET` for `CFG-RATE-MEAS` 100 ms and a UART1 baud that
  carries 10 Hz of the sentences gpsd needs (38400 or higher), sent by
  `canary-gpsd@.service` before `gpsdctl add`, or by gpsd's own `-s`/`ubxtool`.
  **Accept:** **(Pi)** `gpspipe -w` shows TPV at 10 Hz; the daemon's
  `gps:speed` arrives at ~10 Hz.
- [x] **H03.5 uart0 on by default in the overlay** once H05 has a collector
  that uses it. The SysRq hazard needs a console on uart0, and on the Pi 5 the
  console is `ttyAMA10`; make `install_pi.sh` refuse a `console=` on
  `ttyAMA0`/`serial0`-mapped-to-uart0 rather than trusting it. **Accept:**
  installer test for the refusal; the dormant `kline` parameter removed from
  the overlay and the README.
  **Evidence (2026-09-23):** the overlay's uart0 fragment is a normal one and
  the `kline` parameter is gone. **Checked by reboot on the HAT** with it
  enabled: `serial0` → `ttyAMA10`, consoles `tty1` and `ttyAMA10` only, the
  only serial getty on `ttyAMA10`, `ttyAMA0` muxed onto GPIO14/15 with
  nothing on it. `install_pi.sh` (HAT detected) refuses any `console=` that
  names or resolves (through `serial*`) to `ttyAMA0`; four installer tests.
  **Found while checking:** claiming GPIO15 as an input (the edge-capture
  tool) re-muxes it off uart0 and the kernel leaves it there after release;
  `kline_edges.py` now restores `a4` on exit.

## Hardware

- [ ] **H03.6 Barometer** **(hardware)**. Replace U5 (a BMP390 is footprint
  compatible, `CANary-HAT-Circuits.md` §3.5), rerun `checkout_hat.sh`, then a
  collector on the H02.2 bus abstraction. Decide the namespace then:
  `committees.py` names `baro:`, which is not a `TelemetrySource`; putting it
  under `sensor:` avoids a new source. Unblocks H04.4 and HAT measurement 9.
