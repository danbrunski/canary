# H04 — committees that flag

Part of [H00](H00-hat-integration-README.md). Operator decision 2: external
sensors take part in detection **only** through committees, and the wanted
flags are OBD speed that greatly disagrees with GPS speed and (once a working
part is fitted) the HAT barometer disagreeing with OBD barometric pressure.

## Where it stands

`src/signals/committees.py` declares seven committees; only
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

- [x] **H04.1 Committees become detectors, not features.** **Do:** remove
  `trainable` from `SensorCommittee` (H01.1's source gate excludes every
  member anyway) and stop emitting committee residuals into the feature
  frame; keep computing them for the fingerprint and for flags. Add the
  relation each committee checks as data: `expected_ratio` (and/or offset)
  per member pair, with the evidence, loaded from the vehicle profile
  (`data/<profile>/committees.yaml`), and a `tolerance` with its conditions.
  **Accept:** feature lists unchanged for the current corpus (committees were
  all non-trainable); a test that a profile with no committee data runs every
  committee as "not configured", named, rather than with defaults.
  - [x] 2026-09-25: `trainable` removed from `SensorCommittee`; every committee
    channel (the `derived:<NAME>` spreads and the H04.4 member transforms) is
    refused by name in `src/trainability.py` via
    `committees.committee_channel_reason` — even ECU-only
    `derived:OBD_SPEED_ACCEL_G`. Relations are data in
    `data/nissan-xterra-2008/committees.yaml` (strict schema
    `CommitteeProfile`: `reference`, per-member `ratio`/`offset` + required
    `evidence`, `tolerance` with required `conditions`, validated against the
    declared committees). No deployed manifest names a committee column
    (`models/nissan-xterra-2008/{isolation_forest,lstm_autoencoder}/model.manifest.json`
    checked), so nothing served changes. Accept:
    `test_trainability.py::test_committee_channels_leave_the_trained_feature_list_unchanged`
    (candidate list identical with and without the committee columns);
    `test_committee_checks.py::test_no_committee_data_runs_every_committee_as_not_configured_named`
    and `::test_a_partial_profile_leaves_the_rest_not_configured`.
    Not rolled into the feature frame: `featurize_wide_frame` computes the
    committee channels into `operating_frame` (evaluator, fingerprint) and
    hands `compute_features` the frame without them
    (`committee_channel_reason(base_channel(c)) is not None`).
    `tests/test_replay_script.py::test_replay_sessions_builds_preprocessing_report`
    passes unmodified (rolling count 12, as before). Fingerprint of
    2026-09-21T143331, HEAD vs this tree: identical except the documented
    `committee_residual_mean:speed_source_divergence` 8.024 → 8.423 (6F7
    member, engineering units) and new
    `committee_residual_mean:lon_accel_source_divergence` 0.038.
- [x] **H04.2 A committee event path.** **Do:** evaluate committees on the
  live tick and in replay, and emit an event when the departure exceeds the
  tolerance for a sustained window (debounced, like behavior events), into the
  anomaly log with `detector: committee:<name>`, and a per-committee state in
  `/status` (`agree` / `disagree` / `no_verdict: <reason>`). **Accept:**
  synthetic tests for agree, disagree, and each no-verdict reason; the MCP
  `get_recent_anomalies` payload carries committee events.
  - [x] 2026-09-25: `CommitteeEvaluator` (`src/analysis/committees.py`) runs on
    every live tick over the operating point (`src/daemon/committees_live.py`,
    wired in `LiveInferencePipeline._snapshot`, timed as stage `committees`);
    replay is the same path and the event lands in the replay's log, never
    pushed. Debounce: departed verdicts spanning `sustain_seconds` over
    `min_verdicts`, gaps ≤ `max_verdict_gap_seconds`, one flag per episode,
    re-armed by `agree`. Events: `source: committee`,
    `model_version: committee/<NAME>`, `detail.detector: committee:<NAME>`,
    both members + values + relation (with evidence) + departure in
    `detail.cause`; score = departure / tolerance, threshold 1.0; dedupe per
    committee+member; roles/tuning/ntfy via `AnomalyEventEmitter._emit`.
    **Role `evidence` by default** (`models.roles.committee`, stored, never
    pushed) until a committee has a confirmed real catch, the bar the
    2026-09-23 IF/sequence decision set; `alert` promotes it with no
    artifact verdict (deterministic: applied beside, not through, the
    artifact role gate — `DetectorRolesConfig.deterministic_by_source`).
    `committees.enabled` (default true) removes the evaluator; `/status`
    then reads `{"enabled": false}`. Tests:
    `test_committees_live.py::test_committee_events_default_to_the_evidence_role`,
    `::test_committees_can_be_switched_off`,
    `test_role_gate.py::test_the_daemon_wires_the_gate_into_components`.
    `/status` `detectors.inference.committees.committees.<NAME>`: `state`,
    `reason`, `detail`, `alerting`, `events`, `counts`, relations.
    **R03.12 decision by analogy (operator may overrule):** not
    capture-profile gated — a committee compares two instruments against a
    relation measured once, learns no normal, so like plausibility it runs
    under `tuning` (reasoning in `docs/sensor-committees.md` and the module
    docstring). Tests: `tests/test_committee_checks.py` (agree, sustained
    disagree fires once and re-arms, isolated spikes silent, verdict-gap reset,
    session reset, each no-verdict reason parametrized: reference_missing,
    member_missing, stale, gate, gate-channel absent, condition slow,
    condition accel; event and emitter shape);
    `tests/test_committees_live.py` (real `build_components` +
    `LiveFeatureSource` under a tuning capture: one event, one push, `/status`
    block, dashboard `/anomalies` → MCP `get_recent_anomalies(source="committee")`
    carries it, replay stores without pushing; malformed file withheld and
    named).
- [x] **H04.3 Speed committee: OBD ↔ GPS.** **Do:** members `obd:SPEED` and
  broadcast `decoded:WHEEL_SPEED_6F7_A` (whichever is present, since the
  `tuning` profile does not poll SPEED) against `gps:speed`; gates: satellites
  ≥ 7, HDOP ≤ 2, speed ≥ 20 km/h, and not accelerating hard (GPS speed lags);
  expected ratio 0.9203 from the profile. Tolerance from data: measure the
  ratio's spread on known-good drives first and set the flag well outside it.
  **Accept:** replaying the ground-truth drive slice and two other known-good
  drives gives zero events; a synthetic 25 % VSS drop fires within the
  debounce window.
  - [x] 2026-09-25: members `obd:SPEED`, `decoded:WHEEL_SPEED_6F7_A` (scaled
    through its binding) vs `gps:speed`, ratio 0.9203 each; gates sats ≥ 7,
    HDOP ≤ 2 (code), conditions GPS ≥ 20 km/h, |`derived:GPS_SPEED_ACCEL_G`|
    ≤ 0.05 g and |`derived:OBD_SPEED_ACCEL_G`| ≤ 0.05 g (profile). The last
    was added after verification: on held-out
    `_capture-tuning-2026-09-22T235224` a launch at 30-40 km/h
    (00:09:12-00:09:22Z) fired a false event — GPS lagged the wheels 3-5 s,
    so the GPS derivative stayed under 0.05 g while the wheels read 0.13 g;
    with the ECU condition 1 → 0 events, 318 agree
    (`test_committee_checks.py::test_a_launch_with_gps_lagging_the_wheels_is_not_an_event`,
    which also shows the event returns without that condition). Re-measured
    with all three conditions, |departure| p95/p99: default-profile drives
    0.044-0.104 / 0.058-0.183; tuning captures (6F7 member only)
    21T223734 0.094/0.197, 21T232153 0.085/0.176, 22T170459 0.066/0.171,
    22T235224 0.105/0.220; 0 events on all 17 drives listed in the YAML
    evidence (23T022831, 23T142926, 23T164007, 08-04T162651: no verdicts). Measured with the evaluator on gated same-tick pairs
    (~/canary-data/sessions/nissan-xterra-2008): median obd/gps
    0.9152-0.9248 across 9 drives (known answer 0.9203 reproduced);
    |departure| p95 0.075-0.118, p99 0.139-0.232, single-tick tails to 0.84.
    Tolerance 0.15 / 10 s / ≥ 5 verdicts / gap ≤ 3 s. Calibration drives
    (07-28T164657, 07-30T210101, 07-31T145209, 08-04T162651, 08-06T162657):
    0 events, and already 0 at 0.10. Held out, 0 events at 0.10 and 0.15:
    08-03T163440 (the ground-truth drive's source), 09-20T152758,
    09-20T175050, 09-21T143331. At 0.10 g the accel condition let
    09-23T150658 fire twice (HAT GNSS lagging ~5 s into a stop); at 0.05 g it
    does not. Synthetic 25 % drop injected mid-drive into the real sessions:
    fires 10-13 s after onset where the truck is cruising (07-28, 07-30,
    07-31, 09-20T175050, tuning 22T235224; the step itself reads as a hard
    deceleration for ~2 ticks), at the next qualifying cruise otherwise. Ground-truth slice: offline (`evaluate_committee_frame`, the
    `scripts/replay.py` resample path) 0 events — as committed every tick is
    `no_verdict:gate`, the slice was cut without sats/HDOP; with a good fix
    asserted, 104 `agree`, 0 `disagree`, 0 events. The same slice pushed
    through the *live* path (records → `LiveFeatureSource`, CAN decoded live,
    1 Hz `tick` → `LiveCommittees`, fix-health injected; scratch script, not a
    test — 419 s): 0 events, 199 `agree`, 1 `disagree` (the live decode adds
    `WHEEL_SPEED_6F7_A` verdicts the offline slice lacks). Tests:
    `test_committee_checks.py::test_a_25_percent_vss_drop_fires_within_the_debounce_window`,
    `::test_the_ground_truth_slice_gives_no_event_and_says_why`,
    `::test_the_ground_truth_slice_agrees_when_its_fix_is_trusted`,
    `::test_either_ecu_member_gives_a_verdict`.
- [~] **H04.4 Longitudinal-acceleration committee members.** **Do:** compute
  the missing `derived:` members as upstream transforms: `OBD_SPEED_ACCEL_G`
  (prefer the 100 Hz `0x2D1` speed, scaled by the profile ratio, over the
  1 km/h-quantised PID), `GPS_SPEED_ACCEL_G`, against `imu:ACCEL_LON` from
  H02.4. **Accept:** synthetic known-acceleration tests; with H02.6 data,
  zero events on the known-answer drive. Catches a loose IMU mount or a
  wandering frame estimate, which H07 depends on.
  - [~] 2026-09-25: `derived:OBD_SPEED_ACCEL_G` (first present of
    `decoded:VEHICLE_SPEED_2D1` → `decoded:WHEEL_SPEED_6F7_A` → `obd:SPEED`,
    per frame, never mixed per row) and `derived:GPS_SPEED_ACCEL_G`, 2 s
    trailing difference over real elapsed time, NaN across holes > 5 s,
    computed in `append_committee_member_transforms` (called at the existing
    committee seam in `featurize_wide_frame`, so live and offline agree). The
    ECU derivative stays ECU-relative; the relation's `ratio` carries the
    ECU/ground factor. The GPS one already serves as the speed committee's
    accel condition. Tests: `test_committee_checks.py::test_speed_accel_g_recovers_a_known_constant_acceleration`,
    `::test_speed_accel_g_uses_real_elapsed_time_and_refuses_holes`,
    `::test_member_transforms_prefer_the_100hz_broadcast_speed`.
    **Remaining:** the `imu:ACCEL_LON` member (H02.4) and the relation +
    tolerance in `committees.yaml`, measured on the H02.6 known-answer drive;
    until then `LON_ACCEL_SOURCE_DIVERGENCE` reports `not_configured`.
  - [ ] **Found 2026-10-03 (desk): the IMU member can never reach the
    committee, on either path.** The committee names `imu:ACCEL_LON`, but
    nothing delivers that column to the committee seam in
    `featurize_wide_frame`:
    - **Offline:** `imu_stream` resolves a signed `imu:ACCEL_LON`. Then
      `_decimate_imu_long` (`src/preprocessing/loader.py`; `decimate_imu_wide`
      on the wide path) replaces it with `ACCEL_LON_MAX`/`_RMS`/`JERK_MAX`
      and drops the raw column, before resampling.
    - **Live:** `imu_live` emits only those aggregates, and its docstring
      says it resolves no sign ("every live output is a magnitude").

    Adding the relation to `committees.yaml` would therefore not compare the
    IMU at all. It would run OBD against the GPS derivative, which the speed
    committee already does. The member needs a **signed** per-tick channel
    (for example `imu:ACCEL_LON_MEAN`), produced offline by the decimation
    and live by an `imu_live` that resolves sign, with a live/offline parity
    test. The member name should follow it. Also add a refusal so a committee
    member that no path produces cannot be declared silently. Desk work, but
    live sign resolution is a design change to H02.4's frame. Do it before
    the H02.6 drive, so that drive can set the tolerance.

## Hardware

- [ ] **H04.5 Barometer committee** **(hardware — H03.6)**. HAT barometer vs
  `obd:BAROMETRIC_PRESSURE` (the ECM's own sensor). Offset measured per
  session at key-on (both read ambient then), not assumed: the truck drives
  between ~63 and ~80 kPa (`project_operating_altitude_80kpa`). A departure
  from the key-on offset during a drive flags either sensor.
