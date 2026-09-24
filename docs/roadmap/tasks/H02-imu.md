# H02 — the IMU (LSM6DSOX on the HAT)

Part of [H00](H00-hat-integration-README.md). Supersedes Phase 2b items 2b.1,
2b.2 and 2b.4 of `docs/roadmap/imu-gps-integration.md`, which planned a
BNO055. What is already built there stays: the `imu` source, `ImuConfig`,
`imu_decimation.py`, `imu_frame.py`, the auto-tagger and the committee
declarations (Phase 2a, all ticked).

## Where it stands

The part works (2026-09-23): `WHO_AM_I` 0x6C at I²C1 0x6B, |a| = 1.00 g
at rest with ±0.01 g noise, and 4,174 samples in 20 s at 208 Hz with ±8 g /
±2000 dps ranges while the board was shaken (0.27–2.16 g, 244 dps peak).
Nothing in `src/` reads it. Three gaps beyond the collector:

- **Nothing calls `imu_frame.resolve_frame`.** Raw `ACCEL_X/Y/Z` never becomes
  `ACCEL_LON/LAT`, so decimation, the segmenter's accel modes and the IMU
  auto-tags stay dormant even with data. On the live path
  `imu:ACCEL_LON_MAX` is never produced.
- **The frame code removes gravity with a low-pass**, which also removes the
  slowly varying grade term. Tagging wants that; the dyno (H07) needs the
  opposite: longitudinal *specific force*, `dv/dt + g·sin(grade)`.
- **Record load.** 208 Hz × 6 axes is ~1,250 rows/s into a daemon whose
  default load is ~2,355 rows/s and whose storage writer has overloaded
  before (`project_bus_budget_overload`).

## Rules

- H01.1 lands first: `imu:*` must never become a detector feature.
- Units are the ones the existing consumers assume: acceleration in **g**,
  angular rate in **deg/s**, body frame as the chip reports it.
- Mounting is never assumed. Tilt is re-estimated from quiet stretches all the
  time, yaw from GPS course, sign from the speed derivative
  (`imu-gps-integration.md` "Physics", a recorded decision).
- The transport is swappable: rev 3.0 moves the IMU onto an MCU bus
  (`CANary-HAT-Revisions.md` §13.3). The chip driver must not know it is on
  `/dev/i2c-1`.

---

## Desk

- [ ] **H02.1 `ImuConfig` for the LSM6DSOX.** **Do:** add `lsm6dsox` to
  `ImuConfig.source`; fields `i2c_address` (0x6B), `odr_hz` (from the chip's
  set: 26/52/104/208/416), `accel_range_g` (2/4/8/16), `gyro_range_dps`
  (250/500/1000/2000), optional `int1_gpio` (16). Validators refuse values the
  chip cannot do rather than rounding. Defaults stay `source: disabled`
  (OVERNIGHT-QUEUE pre-made decision); `canary.pi.example.yaml` and
  `canary.example.yaml` gain the HAT stanza, commented. **Accept:**
  `tests/test_config.py` covers each validator and the default.
- [ ] **H02.2 LSM6DSOX driver + `imu_collector.py`.** **Do:**
  `src/collection/lsm6dsox.py`: register-level driver over a small bus
  protocol (`read(reg, n)`, `write(reg, value)`), so a fake bus drives every
  test and rev 3.0 can supply an MCU-backed bus. Use the **FIFO** with a
  watermark, read in batches (not one I²C transaction per sample), and batch
  the chip's timestamp counter so each sample gets its own sample instant
  rather than the read instant; map chip time to host time once per batch.
  `src/collection/imu_collector.py`: a thread like the GPS collector's, a
  `ReconnectPolicy`, a `QueueDropCounter("imu")`, and records
  `imu:ACCEL_X/Y/Z` (g), `imu:GYRO_X/Y/Z` (deg/s), `imu:TEMP` (°C, slow).
  Wire it through `_imu_collectors()` in `src/collection/collectors.py`
  beside `_gps_collectors`. **Accept:** fake-bus tests for FIFO parsing (tag
  decoding, partial batches, overrun), timestamp spacing, the reconnect path
  when the bus errors, and a daemon test that the collector starts only when
  `source: lsm6dsox`.
- [ ] **H02.3 Choose the recorded rate from a measurement, not a guess.**
  **Do (Pi):** run the collector at 52, 104 and 208 Hz on the Pi with the
  normal CAN/OBD load, engine running or replaying a busy session, and record
  storage queue depth, dropped records and CPU per step. Keep the chosen rate
  in config (`project_no_gratuitous_latency`: slow defaults go in config).
  If raw rows are too many, filter `imu:` out of the broadcast and live sinks
  (`drain.accepts_source`) before lowering the stored rate — storage is what
  the dyno reads. **Accept:** a table of the three runs here, and the
  default justified by it.
- [ ] **H02.4 Frame resolution with a production caller.** **Do:** give
  `resolve_frame` a caller that runs per session offline (a sidecar, like
  `imu_yaw_offset.json`), producing two families:
  `imu:ACCEL_LON/LAT` (gravity removed — for tags and segmenting, as today)
  and `imu:SF_LON/SF_LAT/SF_VERT` (**specific force**, rotated into the
  vehicle frame, gravity kept — for H07). Then a live path that produces
  `imu:ACCEL_LON_MAX`-style decimated channels so `LiveAutoTagHook` can fire.
  Replace the quiescent mask's dependence on `obd:SPEED` (the `tuning`
  capture profile does not poll it) with the best available speed:
  broadcast `0x2D1` vehicle speed, then OBD, then GPS. **Accept:**
  synthetic tests for both families on a known tilt, known grade and known
  manoeuvre; a test that a session with no OBD speed still resolves.
- [ ] **H02.5 Health on `/status`.** `imu` freshness, achieved sample rate,
  FIFO overruns and the drop counter, in `/status` and the MCP `get_status`
  payload, alongside the GPS fields. **Accept:** route test; the dashboard
  shows it where it shows GPS health.

## Truck

- [ ] **H02.6 Known answer: IMU acceleration vs speed derivative.** **Do
  (truck):** a flat, straight stretch, a few firm accelerations and brakings
  with the collector at the H02.3 rate. Compare `imu:SF_LON` minus the
  grade term against `d/dt` of GPS speed (and of `0x2D1` speed scaled by the
  profile's measured ECU/GPS ratio) through `compare_channels`. Read the
  per-sample ratio, not only `scale_verdict` (AGENTS.md). **Accept:** slope
  within ±3 % of 1 with the recorded pairing policy; add the relation to
  `tests/fixtures/ground_truth/expectations.yaml` with a committed slice of
  the drive.
- [ ] **H02.7 Real-data thresholds for the auto-tagger and segmenter** (was
  2b.4). Re-run `scripts/autotag.py` over drives with IMU data and tune the
  θ values in config against what the operator did. **Accept:** tags agree
  with the operator's physical-action tags on at least one drive
  (`feedback_physical_action_protocols`).
