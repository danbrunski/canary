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

  **Pi, 2026-10-04 (on the road, `a1da058`): chrony takes nothing from either
  source.** `chronyc sources` shows `#? GPS` and `#? PPS` at reach 0 with a 3D
  fix; time came from NTP the whole time. What `ntpshmmon` (as root) showed:
  - **SHM unit numbering depends on attach order.** gpsd hands out units as
    devices activate. At this boot `/dev/pps-canary` attached first
    (19:43:20Z), and the receiver's NMEA time went to **NTP1**, while
    `refclock SHM 0` reads unit 0, which carried no samples. A fixed unit
    number in `canary-gps.conf` cannot be right in both orders.
  - **The standalone PPS device writes no samples**, to SHM or to the SOCK.
    gpsd runs as user `gpsd` after start, and `/run/chrony.pps-canary.sock` is
    `root 0755`; whether that, or gpsd not pairing a bare `/dev/pps*` with the
    receiver, is the cause is not settled.
  - **Before the baud fix the NMEA time was ~14 s late** (H03.4), so a working
    GPS refclock would have stepped the clock 14 s on a boot without network
    (`makestep 1 -1`). The NMEA lag is now 62–68 ms.

  **Fixed the same afternoon (on the road): `#* PPS` at −205 ns ± 621 ns.**
  Three faults stacked:
  - **`precision 5e-1` on the GPS line made the PPS unusable.** chrony cannot
    number a pulse against a reference that may be half a second out, so it
    drops every one. A throwaway `chronyd -x` showed it: `strace` had it
    fetching each pulse (`PPS_FETCH`), and `refclocks.log` had 0 PPS samples
    with or without `lock`. With `precision 1e-1 offset 0.065` (the measured
    62–68 ms NMEA lag at 115200), it logged 62 in 60 s and selected PPS.
  - **gpsd never fed the SOCK.** A standalone `/dev/pps*` given to gpsd
    produced no samples on SHM or the socket.
  - **That attachment took SHM unit 0** whenever it beat the receiver. A
    `gpsdctl remove` did not free the unit; only a gpsd restart with the
    receiver alone attached moved its time back to NTP0.

  The new path (`install_pi.sh`):
  - chrony reads `/dev/pps-canary` directly. The `refclock PPS … lock GPS`
    line is written at each chrony start by
    `chrony.service.d/canary-pps.conf` (`ExecStartPre=+`, since the unit runs
    as `_chrony` under `ProtectSystem=strict`), as
    `/run/canary-chrony-pps.conf`, only when the device exists.
  - `canary-gps.conf` pulls it in with `include /run/canary-chrony-*.conf`.
    chrony 4.6.1, checked with `chronyd -p`: a glob matching nothing is
    ignored, a missing plain include is fatal, and so is a glob whose
    directory is missing (the first attempt used `/run/canary-chrony/*.conf`
    and failed that check before anything was restarted).
  - `70-canary-pps.rules` now pulls in `canary-chrony-pps.service`, which
    restarts chrony once if its generated file lacks the line. That covers a
    chrony that won the udev race at boot.
  - The HAT's GPS line is `refclock SHM 0 refid GPS precision 1e-1 offset
    0.065`. The USB receiver path is byte-identical to before.
  - `canary-gpsd-pps.service` and `gpsd.service.d/canary-reattach-pps.conf`
    are removed on upgrade.

  Tests: `test_a_canary_hat_offers_chrony_its_pps_only_when_the_device_exists`,
  `test_a_canary_hat_states_its_measured_nmea_lag`,
  `test_upgrading_removes_the_gpsd_pps_attachment`, and the no-HAT and
  no-static-`refclock PPS` guards, 32/32.

  **On the Pi, 2026-10-04 20:54–20:59Z** (files byte-identical to the
  installer's output):
  - `chronyd -p` parses the real config with the generated file absent (the
    missing-device case) and present.
  - After a gpsd restart the receiver published on NTP0. After a chrony
    restart PPS was selected within 60 s: −205 ns ± 621 ns, `Last offset`
    −2.2 µs.
  - `canary-chrony-pps.service` left chrony alone when the line was there.
    With the line deleted, it restarted chrony, the generator rewrote the
    line, and PPS was back at −300 ns 75 s later.

  **Still owed:**
  - A real start with the overlay removed (shown so far only by `chronyd -p`
    on the generated-file-absent config).
  - A cold boot away from the network that steps the clock once, early.

  Also found that day, and fixed (`install_pi.sh`): `PartOf=gpsd.service`
  carries a stop and a restart of gpsd to `canary-gpsd@` and
  `canary-gpsd-pps`, but not a fresh start. The 2026-10-03 `apt upgrade`
  upgraded gpsd (stop at 19:48:43, start at 19:49:50), which left gpsd with
  only the PPS device attached and **no receiver** until the 2026-10-04 reboot.
  gpsd drop-ins `canary-reattach.conf` / `canary-reattach-pps.conf` now
  `udevadm trigger --action=add` the receiver and the PPS after every gpsd
  start (checked on the Pi: re-firing the add event restarted a stopped
  `canary-gpsd-pps`). Tests:
  `test_every_gpsd_start_replays_the_receivers_add_event`,
  `test_a_canary_hat_reattaches_its_pps_and_sets_the_gnss_baud`.
  Installed by hand on the Pi 2026-10-04 (`daemon-reload`, nothing restarted;
  byte-identical to the installer's output). **Exercised on the parked truck
  the same day**, replaying the apt sequence with the receiver first forced to
  9600: `systemctl stop gpsd.service gpsd.socket`, then `start`. The PPS and
  `/dev/ttyAMA2` came back on their own, the daemon logged `gps collection
  started` within 6 s, and the receiver was at 115200 30 s after the start,
  with no `txbuf`.

- [~] **H03.4 GNSS at 10 Hz.** **Do:** configure the MAX-M10M on every gpsd
  attach (it has no backup cell fitted, so settings in RAM do not survive a
  power cut): UBX `CFG-VALSET` for `CFG-RATE-MEAS` 100 ms and a UART1 baud that
  carries 10 Hz of the sentences gpsd needs (38400 or higher), sent by
  `canary-gpsd@.service` before `gpsdctl add`, or by gpsd's own `-s`/`ubxtool`.
  **Accept:** **(Pi)** `gpspipe -w` shows TPV at 10 Hz; the daemon's
  `gps:speed` arrives at ~10 Hz.
  **Baud half done 2026-10-04, found on the road.** At the power-up 9600 baud,
  one second of the M10's NMEA (14 GSV lines for 28 satellites across four
  constellations, plus GGA/RMC/GSA/GBS/ZDA) does not fit in ~960 chars/s. The
  receiver reported `$GNTXT,01,01,00,txbuf alloc`, and `ntpshmmon` put its
  time **13.6–14.1 s behind** the NTP-disciplined clock, growing ~45 ms a
  second, then snapping back. TPV and the daemon's `gps:` rows were that late
  as well. The 2026-09-23 figure of +154..161 ms was with fewer satellites
  tracked. `gpsctl -s 115200 /dev/ttyAMA2` (live, through gpsd) brought it to
  62–68 ms steady, with no further `txbuf`. `install_pi.sh` now writes
  `canary-gpsd@ttyAMA2.service.d/canary-baud.conf` on a HAT install, which
  repeats that `gpsctl -s` after every attach (retried, since gpsd must
  identify the receiver first; `CANARY_HAT_GPS_BAUD` overrides the speed).
  Installed on the Pi by hand. **Verified 2026-10-04:**
  - **A real power cut.** The Pi shut down on UPS at key-off, so the HAT's
    receiver lost power and came back at 9600. At the 20:27Z boot, two
    `gpsctl` tries timed out waiting for gpsd to identify the receiver, the
    third set 115200, and gpsd reported 115200 with no `txbuf`.
  - **A gpsd stop/start** (H03.3's test), from a forced 9600: back to 115200
    in 30 s.
  - **A defect, found and fixed.** On the first stop/start, one `gpsctl` try
    hung for minutes and ignored SIGTERM. A oneshot unit has
    `TimeoutStartSec=infinity`, so the loop never retried, and the port
    stayed at 9600 until the try was SIGKILLed. Each try now runs under
    `timeout -s KILL 20`. On the rerun the first try hung again, was killed at
    20 s, and the second succeeded.
  **Rate half, desk side done 2026-10-13 (not yet on a Pi).** The same
  `canary-baud.conf` drop-in now sends, in one `ExecStartPost` and only after
  `gpsctl -s` succeeded (10 Hz at the power-up 9600 baud is the overflow above
  ten times over), two RAM-layer CFG-VALSETs through gpsd:
  `ubxtool -P 34 -z CFG-MSGOUT-NMEA_ID_GSV_UART1,10,1 localhost:2947:<tty>`, then
  `ubxtool -P 34 -z CFG-RATE-MEAS,100,1 …`, each under `timeout -s KILL 20`,
  five tries. GSV goes to 1 Hz first: the 9600-baud overflow's growth (~45 ms/s)
  puts a 1 Hz epoch near 1000 chars, so ten a second with GSV would nearly fill
  115200 baud's ~11.5k chars/s, or overfill it with more satellites in view.
  `CANARY_HAT_GPS_RATE_MS` (default 100) overrides the period, GSV following at
  1000/rate. The installer refuses a non-numeric baud or period, a period
  outside 25–1000 ms, and a sub-1 Hz period below 38400 baud. The tty still
  comes from `hat_revisions.json`: rev 2.0 gets `/dev/ttyAMA2`, rev 2.1
  `/dev/ttyAMA4`. Tests in `tests/test_install_pi_script.py`:
  `test_each_hat_revision_sets_its_receiver_to_10_hz_on_every_attach[2.0/2.1]`,
  `test_the_rate_is_set_only_once_the_baud_is` (runs the drop-in's own script
  against shims: gpsctl → GSV → rate in that order, and ten gpsctl tries and no
  ubxtool when the baud never sets), `test_the_gnss_rate_override_keeps_gsv_at_1_hz`,
  `test_a_gnss_setting_the_link_cannot_carry_is_refused` (4 cases),
  `test_1_hz_at_9600_baud_is_still_allowed`; the file is 64 passed.
  `systemd-analyze verify` parses the rendered drop-in, continuation lines
  included.
  **Still owed (Pi):** install, then after a power cut and after a gpsd
  stop/start: `gpspipe -w` shows TPV at 10 Hz, the daemon's `gps:speed` at
  ~10 Hz, no `txbuf alloc`, and the journal shows the ubxtool ACK (its exit
  status is not known to reflect a NAK). Recheck the NMEA lag with `ntpshmmon`:
  chrony's `offset 0.065` was measured at 1 Hz.
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
