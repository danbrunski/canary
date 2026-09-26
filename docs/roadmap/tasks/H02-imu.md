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

- [x] **H02.1 `ImuConfig` for the LSM6DSOX.** **Do:** add `lsm6dsox` to
  `ImuConfig.source`; fields `i2c_address` (0x6B), `odr_hz` (from the chip's
  set: 26/52/104/208/416), `accel_range_g` (2/4/8/16), `gyro_range_dps`
  (250/500/1000/2000), optional `int1_gpio` (16). Validators refuse values the
  chip cannot do rather than rounding. Defaults stay `source: disabled`
  (OVERNIGHT-QUEUE pre-made decision); `canary.pi.example.yaml` and
  `canary.example.yaml` gain the HAT stanza, commented. **Accept:**
  `tests/test_config.py` covers each validator and the default.
  **Evidence (2026-09-23):** `ImuConfig` gains `lsm6dsox`, `i2c_address`,
  `accel_range_g`, `gyro_range_dps`, `fifo_poll_seconds`; a model validator
  checks rate and ranges against the driver's own tables
  (`lsm6dsox.ImuSettings`), so the pre-HAT default of 100 Hz is refused for
  `lsm6dsox` rather than rounded. Tests in `tests/test_imu_collector.py`
  (`test_config_refuses_a_rate_the_lsm6dsox_cannot_do`); both example configs
  carry the HAT stanza, commented.
- [x] **H02.2 LSM6DSOX driver + `imu_collector.py`.** **Do:**
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
  **Evidence (2026-09-23):** `src/collection/i2c_dev.py` (`I2C_RDWR` ioctl,
  no dependency), `src/collection/lsm6dsox.py` (FIFO burst reads — the
  `0x7E`→`0x78` address wrap measured on the chip — instants grouped by tag
  counter, dated from the chip's timestamp words, ticks per instant learned
  from consecutive stamps, a 100 ms power-up settling window), and
  `src/collection/imu_collector.py` wired through `_imu_collectors`. A pre-HAT
  chip is refused at start-up rather than silently recording nothing. 25
  tests (`tests/test_lsm6dsox.py`, `tests/test_imu_collector.py`), including a
  real three-batch FIFO dump from the HAT at rest
  (`tests/fixtures/lsm6dsox/fifo_at_rest.json`). **The settling window came
  from that dump:** the first accelerometer sample after power-up read 0.89 g
  and the gyroscope read up to 33 deg/s for ~50 ms on a board sitting still.
  **On the chip (Pi, 5 s, 100 ms polls):** 517 samples at 104.55 Hz, 0
  overruns, 0 undated, 11 settling instants dropped, gaps 9.59 ms median
  (9.37–9.72), 384.00 ticks per instant learned (the chip's "104 Hz" is
  104.17 Hz), newest sample 34 ms old at read, |a| = 1.0055 ± 0.0056 g, gyro
  peak 2.45 deg/s. The ±0.2 ms gap jitter is the per-batch host mapping; it
  can be smoothed if a consumer ever needs better.
- [ ] **H02.3 Choose the recorded rate from a measurement, not a guess.**
  **Do (Pi):** run the collector at 52, 104 and 208 Hz on the Pi with the
  normal CAN/OBD load, engine running or replaying a busy session, and record
  storage queue depth, dropped records and CPU per step. Keep the chosen rate
  in config (`project_no_gratuitous_latency`: slow defaults go in config).
  If raw rows are too many, filter `imu:` out of the broadcast and live sinks
  (`drain.accepts_source`) before lowering the stored rate — storage is what
  the dyno reads. **Accept:** a table of the three runs here, and the
  default justified by it.
- [~] **H02.4 Frame resolution with a production caller.** **Do:** give
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
  **Evidence (2026-09-26, synthetic; H02.6 on the truck is the real known
  answer):**
  - **Offline caller.** `src/preprocessing/imu_session.py`
    `resolve_session_imu_frame` (CLI `scripts/resolve_imu_frame.py`) reads
    the session and writes `imu_frame.json`. `ACCEL_LON/LAT` come from
    `resolve_frame` as before. `SF_*` come from the new
    `imu_frame.resolve_specific_force`: **one constant rotation per
    session**, whose "up" is the mean specific force over every quiescent
    sample. Axes are ISO 8855 (LAT left positive), in g. `resolve_frame` now
    flips LAT with LON, so LAT stays `up × LON`.
  - **Pitch reference: recorded, not corrected.** The sidecar holds the
    rotation, its `reference_id`, the reference gravity vector with |g| and
    tilt, every quiescent window (times, samples, speed, gravity, angle from
    the reference), the stationary fraction and `pitch_offset_removed:
    false`. The grade under the reference windows stays in `SF_LON` as one
    constant `μ ≈ −θr`, which `dyno.estimate_pitch_offset` measures.
    `specific_force_from_sidecar` recomputes `SF_*` from the recorded
    rotation. A re-resolution that would change a resolved reference refuses
    unless `replace=True` (`ImuFrameOfRecordError`). `dyno.py` is unchanged.
  - **Speed.** `imu_frame.SPEED_SOURCES` =
    `decoded:VEHICLE_SPEED_2D1` → `obd:SPEED` → `gps:speed`, the first with
    any value. The 2D1 raw counts are scaled through
    `engineering.physical_series`, the same in `resolve_frame` and in the
    live path.
  - **Look-ahead (found while testing).** The speed at an IMU instant is
    the newest *earlier* reading. With 1 Hz GPS, a trailing window counted
    the first second of every pull from rest as quiescent, and put a 0.27°
    pitch into the reference. That is ~106 N of phantom force by H07.5's
    figure. The masks now judge `[t − window, t + 1.5 s]`
    (`QUIESCENT_LOOKAHEAD_SECONDS`), and the live path decides its updates
    1.5 s late. Mutation check: with the look-ahead at 0 the reference tilt
    is 0.2747°; the test requires < 0.01°.
  - **Live.** `src/preprocessing/imu_live.py` `LiveImuFrame`, wired into
    `LiveFeatureSource`, publishes `imu:ACCEL_LON_MAX/ACCEL_LON_RMS/
    ACCEL_LAT_MAX/JERK_MAX` on each tick. Its `status` names the step it is
    waiting on. Cost: 3.5 ms per second of data on the laptop, with 208 Hz
    IMU and 100 Hz 2D1.
  - **Refusals, each with a named cause:** `no_telemetry`,
    `imu_rows_missing`, `no_speed_source`, `no_heading`,
    `no_quiescent_period`, `gravity_implausible` (m/s² rows read 9.8 g),
    `yaw_unresolved`, `sign_unresolved`. A refusal is written to the sidecar
    too.
  - **Tests.** `tests/test_imu_session.py` (24):
    - known tilt and manoeuvres at mount yaw 25° and 205° (the sign flips),
      for both families;
    - `SF_LON` = a + sin θ on a 4 % grade, within 2 mg;
    - `ACCEL_LON` excludes a 5 % grade after a steady climb, within 0.01 g;
    - a direction pair where `estimate_pitch_offset` recovers μ = −θr within
      0.01°, and `correct_specific_force` then leaves a + g·sin θ;
    - no OBD speed: resolves from 0x2D1, and from GPS;
    - every refusal, the sidecar round trip, the frame-of-record guard and
      the CLI;
    - H01.1: `imu:SF_*`, `ACCEL_*` and `*_MAX` are `source_not_trainable`.

    `tests/test_imu_live.py` (5): `LiveAutoTagHook` writes `hard_accel` and
    `cornering` from raw IMU records through `LiveFeatureSource`.
    `tests/test_imu_frame.py` (+4).

    Suites run: imu_frame, imu_session, imu_live, imu_decimation,
    imu_collector, autotag (3 files), segmenter, preprocessing_live and the
    other live suites, trainability and dyno. 334 passed.
  - **Remaining (verifier, 2026-09-26; why this is `[~]`):** the offline
    `ACCEL_LON/LAT` family has no consumer yet.
    - `ImuSessionFrame.rows()` has no caller, and `imu_frame.json` stores
      only the SF rotation and the linear yaw/sign. `ACCEL_LON/LAT` itself
      cannot be rebuilt from the sidecar, because its low-pass depends on the
      inputs.
    - `scripts/autotag.py` and the offline segmenter read through
      `loader.resample_and_segment`, whose IMU decimation sees only raw
      `imu:ACCEL_LON` rows, which no session contains.
    - So offline decimation, the segmenter's accel modes and the offline IMU
      auto-tags stay dormant, while live `imu:ACCEL_*_MAX` does feed the
      live segmenter.
    - That split matters wherever accel-gated modes are configured
      (`launch`, `hard_brake`, `cornering` in `canary.example.yaml`). No mode
      divergence reproduced with the example config, but it is a real
      live/offline difference for `operating_mode`.
    - Owed: the loader appends the resolved frame's rows, or reads a persisted
      per-session frame (the implementer recommends a parquet under the
      derived-store cache), inside `read_sessions` / `resample_and_segment`.
      P02.1 has landed (`b37e713`), so the loader is free. H02.7 depends on
      this.
  - **Follow-on, outside this item's acceptance:** the offline segmenter
    and `scripts/autotag.py` read through `loader.resample_and_segment`, so
    they see `ACCEL_LON` only once the loader appends
    `ImuSessionFrame.rows()`. `loader.py` was held by the concurrent P02.1
    work.
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
