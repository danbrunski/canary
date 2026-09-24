# H04 — committees that flag

Part of [H00](H00-hat-integration-README.md). Operator decision 2: external
sensors take part in detection **only** through committees, and the wanted
flags are OBD speed that greatly disagrees with GPS speed and (once a working
part is fitted) the HAT barometer disagreeing with OBD barometric pressure.

## Where it stands

`src/analysis/committees.py` declares seven committees; only
`SPEED_COMMITTEE` has members that exist. All seven are `trainable=False`.
Each emits a `derived:<NAME>` **residual**, the spread (max − min) across
members on rows where the gates hold, designed as an IF/LSTM feature
("residual-as-feature"). Three things stand between that and a flag:

- **Nothing flags.** A residual column is computed inside the feature
  pipeline and read by the fingerprint; no event, anomaly-log entry or status
  field says "these two disagree".
- **Residual-as-feature is retired by decision 1.** Every member of every
  committee is an external sensor, so no committee residual may become a
  detector feature again; `trainable` stops being a per-committee choice.
- **Known offsets read as faults.** ECU speed reads ~8 % under GPS on this
  truck (`obd/gps` 0.9203, `data/bindings.yaml` 0x6F7 notes: tyre size plus an
  unexplained remainder, magnitude settled). A raw spread of 0.08 × speed would
  fire on every drive.

## Rules

- A committee flags a **departure from the known relationship**, never the
  relationship itself. Per-vehicle offsets and ratios are profile data with
  the evidence that measured them.
- Gates fail closed: a marginal GPS fix, a stationary vehicle or a stale member
  means "no verdict", and the flag says which.
- A flag names both members, their values, the expected relation and the
  departure, like a behavior-model event does.
- Replay a known-good session before trusting a new committee
  (`feedback_replay_to_test_models`): it must stay silent there.

---

## Desk

- [ ] **H04.1 Committees become detectors, not features.** **Do:** remove
  `trainable` from `SensorCommittee` (H01.1's source gate excludes every
  member anyway) and stop emitting committee residuals into the feature
  frame; keep computing them for the fingerprint and for flags. Add the
  relation each committee checks as data: `expected_ratio` (and/or offset)
  per member pair, with the evidence, loaded from the vehicle profile
  (`data/<profile>/committees.yaml`), and a `tolerance` with its conditions.
  **Accept:** feature lists unchanged for the current corpus (committees were
  all non-trainable); a test that a profile with no committee data runs every
  committee as "not configured", named, rather than with defaults.
- [ ] **H04.2 A committee event path.** **Do:** evaluate committees on the
  live tick and in replay, and emit an event when the departure exceeds the
  tolerance for a sustained window (debounced, like behavior events), into the
  anomaly log with `detector: committee:<name>`, and a per-committee state in
  `/status` (`agree` / `disagree` / `no_verdict: <reason>`). **Accept:**
  synthetic tests for agree, disagree, and each no-verdict reason; the MCP
  `get_recent_anomalies` payload carries committee events.
- [ ] **H04.3 Speed committee: OBD ↔ GPS.** **Do:** members `obd:SPEED` and
  broadcast `decoded:WHEEL_SPEED_6F7_A` (whichever is present, since the
  `tuning` profile does not poll SPEED) against `gps:speed`; gates: satellites
  ≥ 7, HDOP ≤ 2, speed ≥ 20 km/h, and not accelerating hard (GPS speed lags);
  expected ratio 0.9203 from the profile. Tolerance from data: measure the
  ratio's spread on known-good drives first and set the flag well outside it.
  **Accept:** replaying the ground-truth drive slice and two other known-good
  drives gives zero events; a synthetic 25 % VSS drop fires within the
  debounce window.
- [ ] **H04.4 Longitudinal-acceleration committee members.** **Do:** compute
  the missing `derived:` members as upstream transforms: `OBD_SPEED_ACCEL_G`
  (prefer the 100 Hz `0x2D1` speed, scaled by the profile ratio, over the
  1 km/h-quantised PID), `GPS_SPEED_ACCEL_G`, against `imu:ACCEL_LON` from
  H02.4. **Accept:** synthetic known-acceleration tests; with H02.6 data,
  zero events on the known-answer drive. Catches a loose IMU mount or a
  wandering frame estimate, which H07 depends on.

## Hardware

- [ ] **H04.5 Barometer committee** **(hardware — H03.6)**. HAT barometer vs
  `obd:BAROMETRIC_PRESSURE` (the ECM's own sensor). Offset measured per
  session at key-on (both read ambient then), not assumed: the truck drives
  between ~63 and ~80 kPa (`project_operating_altitude_80kpa`). A departure
  from the key-on offset during a drive flags either sensor.
