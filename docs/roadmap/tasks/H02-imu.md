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
- [x] **H02.4 Frame resolution with a production caller.** **Do:** give
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
  - **Offline readers see the frame (2026-09-26; closes the verifier's
    remainder, which found the `ACCEL_LON/LAT` family had no offline
    consumer).** `src/preprocessing/imu_stream.py` rebuilds
    `imu:ACCEL_LON/LAT` from `imu_frame.json` inside the `value_number` chain,
    part by part. `imu_stream.iter_framed_numeric_parts` is the only way
    `derived.reduce_numeric_session` reads a session. So
    `loader.resample_and_segment` (the digest, classification, fingerprints,
    `scripts/autotag.py`) gets `imu:ACCEL_LON_MAX/_RMS`, `ACCEL_LAT_MAX` and
    `JERK_MAX`, with or without the derived store. The Pi needs no store.
    - The sidecar now records the low-pass's initial state
      (`linear_accel.first_quiescent_sample`, `initial_gravity_body_g`).
    - The rest is recomputed from the rows. Speed and course are aligned
      with each input's last reading carried across parts. The quiescent mask
      keeps `window − 1` rows of history and holds the last `lookahead` rows
      back for the next part. The EWM carries its last estimate. Yaw and sign
      come from the sidecar.
    - Ticks stay whole. Only the ticks still holding unresolved IMU rows are
      held back.
  - **Equality with the whole-session frame.** Bit-exact
    (`assert_array_equal`) against `resolve_imu_frame`'s `ACCEL_LON/LAT`
    (`ImuSessionFrame.rows()`, now the reference) in four cases:
    - 150-row parts (more than 10 part edges, several inside manoeuvres),
      1,000-row parts, and one part;
    - through a 40 s IMU dropout, where the held rows stay bounded by the
      look-ahead's ticks;
    - with 0x2D1 as the speed source.

    Every other row passes through exactly once, and no tick is split. One
    step is not bit-exact: pandas' rolling std drifts up to about 6.4e-10
    (verifier's measurement) depending on where the series starts. So the mask can differ only where a window's
    std is within that of 1.5 km/h or 3°. The stream checks its rows against
    the sidecar: the first quiescent sample and its value, the sample count
    and the span. On a mismatch it raises `ImuFrameStaleError`.
  - **Every kind of absent frame is named.** The status goes to
    `resample_and_segment(...).attrs["imu_frame"][session]` and the log, and
    `autotag.py` warns:
    - `applied`;
    - `no_imu_rows`;
    - `unresolved`: raw IMU rows but no sidecar. The accel channels stay
      absent, and the reason names `scripts/resolve_imu_frame.py`. The frame
      is **not** resolved on the fly. The whole-session resolver peaked at
      1.53 GB (30 min) and 3.63 GB (90 min) of 208 Hz data, which the Pi's
      capped digest child cannot hold;
    - `refused`, with its kind;
    - `stale`: a sidecar written before 2026-09-26. Re-run the CLI without
      `--replace`: a re-resolve keeps the frame of record's speed source, so
      the reference is unchanged. Without that pin, a HEAD-era sidecar that
      steered by `obd:SPEED` on a session with 0x2D1 frames re-resolved onto
      0x2D1, got another quiescent set and reference, and was refused
      (verifier repro, 2026-09-26). `--replace`, or no sidecar, takes the best
      source (`test_a_stale_sidecar_backfills_with_the_same_reference`).
  - **0x2D1 speed, honestly.** `VEHICLE_SPEED_2D1` is a `high` binding,
    outside the loader's on-read set. So `read_imu_inputs` never saw it, and
    the resolver silently steered by Mode 01 or GPS. The "resolves from
    0x2D1" test above passed a channel dict, not a stored session. A stored
    session whose only speed was 0x2D1 refused `no_speed_source`.
    - `imu_session.speed_decode_catalog` and `read_imu_inputs` now decode it
      from the raw frames.
    - The stream reads the same frames, by widening the catalog for itself
      and dropping those rows after it reads them.
    - `speed_source` records `decoded:VEHICLE_SPEED_2D1`
      (`test_0x2d1_speed_is_decoded_from_the_stored_frames`).
  - **Memory and time** (synthetic 208 Hz session, laptop, peak RSS):

    | chain | 30 min | 90 min |
    |---|---|---|
    | no sidecar | 249 MB, 5.4 s | 260 MB, 14.2 s |
    | frame applied | 320 MB, 14.5 s | 329 MB, 34.6 s |

    Applying the frame adds about 70 MB, flat in session length, and about
    0.23 s of CPU per minute of drive.
  - **Store.** `NUMERIC_CODE_VERSION` goes from 1 to 2. The numeric frame is
    versioned by `ImuFramePlan.key()`: the sidecar values the stream reads,
    plus the speed binding's spec. Resolving or removing a sidecar rebuilds
    the frame and says why ("the session's IMU frame (imu_frame.json)
    changed"). Store and no store are `check_exact` equal.
  - **H01.1: the training chain never reads through the stream.**
    - `build_feature_frame` on two real non-IMU sessions
      (`2026-05-24T193045`, `2026-05-24T190535`), before and after this
      change: `check_exact` equal (3,420 × 672). The `value_number` frame
      (3,422 × 135) is equal too.
    - A synthetic IMU session with and without its sidecar gives the same
      training frame, and no `imu:` column is trainable.
  - **Live vs offline**, one synthetic drive
    (`test_live_and_offline_agree_except_where_live_cannot_see_accel`):
    - `imu:ACCEL_LON_MAX` agrees within 1.1 mg on all 56 ticks that both
      paths have. Live has none for its first 17 ticks, because its yaw is
      decided about 2 s into the first pull. Offline has it from the first
      sample.
    - `operating_mode`, with the accel modes placed first: 54 of 73 ticks
      agree. All 19 disagreements are ticks offline puts in an accel mode:
      cornering→city 9, hard_brake→city 5, hard_brake→highway 2,
      launch→city 2, launch→stopped 1.
    - With `canary.example.yaml` as shipped: 73 of 73 agree.
  - **Found: the live segmenter never uses accel.**
    - `feature_pipeline.segment_or_constant` segments with
      `SEGMENTER_SIGNAL_COLUMNS`, which has no `accel_lon`/`accel_lat`. So
      live, and the trainers, never enter `launch`, `hard_brake` or
      `cornering`, even with `imu:ACCEL_*_MAX` on the operating point.
    - The offline chain uses `segmenter.DEFAULT_SIGNAL_COLUMNS`, which does
      have them.
    - The verifier's premise, that live feeds the live segmenter, was wrong.
      Before this change both paths were blind to accel by accident; now
      offline sees it and live does not.
    - Making live see accel changes training segmentation for IMU sessions,
      which is an operator decision. `feature_pipeline.py` is also held by
      a concurrent agent. Not in this item's acceptance; it is a follow-on.
  - The `value_number` chain now passes `idle_transient_lookback_seconds` to
    the segmenter, as the wide chain and live already did. It had used the
    180 s default whatever the config said.
  - `RESIDUAL_PITCH_OFFSET` and its composition test now use the frame-checked
    `dyno.correct_specific_force(samples, squat=, pitch_offset=PitchOffset)`,
    and a sample from another `reference_id` is refused (`frame_mismatch`).
  - **Tests.** `tests/test_imu_stream.py` (16 tests). Suites run by path,
    slow and integration included:
    - imu_stream, imu_session, imu_frame, imu_live, imu_decimation,
      imu_collector, autotag (4 files), segmenter, derived, derived_store,
      trip_end_digest, digest, preprocessing_live, trainability,
      preprocessing_loader, fingerprint, session_classifier,
      sequence_dataset, dyno, dyno_inputs and the CLI snapshots: 530 passed,
      1 skipped. The one failure is the `evaluate_detectors.py` CLI
      snapshot; that script carries another agent's uncommitted change.
    - bounded_session_resample, preprocessing_resampler, fingerprint_script
      and tuning_digest: 54 passed.
    - `-m ml_training` (`scripts/ml_paths.txt` covers `src/preprocessing/`):
      122 passed.
    - `ruff check .`, `ruff format --check .` and `mypy src` are clean.
    - After the stale-sidecar fix (speed source pinned on re-resolve): imu_*
      (6 files) and autotag (4 files), 109 passed; ruff, format and `mypy src`
      clean.
- [x] **H02.5 Health on `/status`.** `imu` freshness, achieved sample rate,
  FIFO overruns and the drop counter, in `/status` and the MCP `get_status`
  payload, alongside the GPS fields. **Accept:** route test; the dashboard
  shows it where it shows GPS health.
  *Done 2026-10-03.*
  - **Counters.** `ImuHealth` (`src/collection/imu_collector.py`) now also
    carries `queue_dropped` (the collector's `QueueDropCounter`) and
    `configured_hz` (the ODR it set). It measures the **achieved rate on the
    samples' own timestamps** over a trailing `RATE_WINDOW_SECONDS` (5 s):
    `(n − 1) / span`. A FIFO drained late still dates its samples, where a
    count over wall clock would measure the drain schedule (AGENTS.md,
    `achieved_hz_capture`). With fewer than two samples the rate is `None`,
    not 0.
  - **Wiring.** `DaemonComponents.imu_health` is built when an IMU is
    configured. It is passed through `default_collectors(imu_health=)` to the
    collector, and through `create_app` / the public routes to
    `gather_status_sources`.
  - **Payload.** `/status.imu` is `{fresh, age_seconds, configured_hz,
    achieved_hz, rate_window_seconds, samples, overruns, undated,
    queue_dropped}`. `fresh` uses `FRESHNESS_THRESHOLD_SECONDS`, the bound
    behind `vehicle_state`'s bus freshness, and the block is `null` without
    an IMU. It is full view only, like GPS: the compact view leaves GPS out
    for the same reason. MCP `get_status` proxies `/status`, so it carries
    the block unchanged.
  - **Dashboard.** System panel: an `imu` row above the GPS rows, showing
    "achieved / configured Hz", "stale · N s" or "no samples", plus overrun
    and dropped counts. It warns on any overrun or drop (`imuHealth`,
    `ImuStatus` in `types/live.ts`).
  - **Tests.**
    - `test_dashboard_api.py::test_status_route_carries_imu_health` (through
      `TestClient`, two late drains at 104 Hz), `::test_status_imu_is_stale_past_the_bus_freshness_bound`
      and `::test_status_imu_is_absent_without_an_imu`.
    - `test_imu_collector.py::test_achieved_rate_is_measured_on_sample_time_over_a_trailing_window`.
    - `SystemPanel.test.ts` (4).
    - The route-table, MCP tool and status suites pass (335).

  Not checked on the Pi: the first HAT drive's `/status.imu` reading, which
  H02.3's rate choice wants anyway.

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
