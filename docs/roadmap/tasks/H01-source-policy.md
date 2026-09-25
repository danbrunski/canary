# H01 — source policy and plumbing for the HAT's new sources

Part of [H00](H00-hat-integration-README.md). Everything else in the H series
adds records under `imu:`, `sensor:` and (H05) `kline:`. This file makes sure
that is safe before it happens: which sources may feed a detector, which
allowlists must learn the new namespaces, and which dependencies the daemon
needs.

## Why this comes first

`feature_pipeline.candidate_feature_columns` has **no source gate**. It
excludes GPS position/quality channels by name, counters, state and policy
lists, and otherwise takes any numeric column that clears the
minimum-observed-fraction filter. The first session recorded with an IMU
collector running would put `imu:ACCEL_X` into the next Isolation Forest and
sequence-detector fit. That is the 2026-09 "binding retraction silently
blinded the IF" failure in reverse: a feature that arrives and leaves with a
piece of hardware.

## Rules

- **Operator decision 1 (H00):** external sensors (`gps`, `imu`, `sensor`, the
  HAT's 12 V and barometer channels) never train or score IF, the sequence
  detector or behavior models.
- **Allowlist, not blocklist.** A source is eligible because it is listed as
  ECU-origin; a source nobody has classified is not eligible. A new source
  must not become a feature by being forgotten.
- A `derived:` channel is eligible only when every input it is computed from
  is eligible. "Derived" is not an origin.

---

## Desk

- [~] **H01.1 A source-level trainability gate.** **Do:** in `src/telemetry.py`,
  a `TRAINABLE_SOURCES` frozenset beside `CAPTURED_SOURCES`
  (`obd`, `decoded`; `can` raw frames never become features anyway) with an
  import-time assert that it is a subset of `TelemetrySource`, and every
  source classified as trainable or not. `feature_pipeline.exclusion_reason`
  returns a named reason (`source_not_trainable`) for any column whose source
  is outside it. For `derived:` columns, trainability follows
  `derived.DERIVED_CHANNEL_INPUTS`: make that registry cover **every** derived
  channel the pipeline can emit (it holds four today), and treat an
  undeclared derived channel as not trainable, so declaring inputs is how a
  new derived channel becomes a feature. Apply the same gate wherever feature
  lists are chosen outside `candidate_feature_columns`
  (`sequence_feature_set.py`, behavior-model recipe validation: a recipe that
  names an `imu:`/`gps:`/`sensor:` input is refused at load, naming the rule).
  **This is a feature-set change and needs a retrain.** The deployed IF and
  LSTM manifests for `nissan-xterra-2008` (and the 2026-09-23 NA candidates)
  carry `gps:speed` and its `__mean`/`__std`/`__roc` expansions, so the gate
  applies to *training* first; a deployed model keeps being served the
  features its manifest names until the retrain replaces it. Excluding them
  at serve time before then would blind it (the D02.5 failure). Coordinate
  the retrain with the A-series work that owns it (A02/A03/A04/A06).
  **Accept:** a synthetic session carrying `imu:*`, `gps:*`, `sensor:*` and a
  derived channel built from `gps:` yields no feature from any of them; the
  feature list the current corpus produces differs only by the `gps:speed`
  family and channels already excluded by name (diff it, and record the diff
  here); the next retrained manifests carry no external-sensor feature; a
  test that a source added to the Literal without a trainability
  classification fails at import. **Sequencing:** `feature_pipeline.py` had
  another session's uncommitted edits on 2026-09-23. Land this after that
  work is committed.
  **Partial (2026-09-24).** Done, offline:
  - `src/telemetry.py`: `TRAINABLE_SOURCES` = {`obd`, `decoded`},
    `INPUT_GATED_SOURCES` = {`derived`}, `NON_TRAINABLE_SOURCES` (reason per
    source: `can`, `gps`, `imu`, `sensor`, `uprev`, `kline`), the subset
    `assert`, and `require_trainability_classified`, a raise (not an assert)
    run at import over the Literal.
  - `src/trainability.py` (new): `source_exclusion_reason(column)`, the one
    gate every chooser calls. Allowlist; wide-only namespaces that may train
    are listed with a reason (`did`, `dbc`, `deviation`); a bare or
    unclassified prefix (e.g. `baro:`) is refused; `derived:` follows every
    declared input, recursively, optional inputs included; an undeclared
    derived channel is refused. Reasons start `source_not_trainable: `.
  - `src/analysis/derived.py`: `DERIVED_CHANNEL_INPUTS` 4 → 38 entries (every
    channel `compute_anomaly_signals`, `append_afr_channels`,
    `compute_instantaneous_channels`, `compute_derived_channels` and every
    committee emit) plus `DERIVED_CHANNEL_OPTIONAL_INPUTS`; the seven inline
    dependency-gap declarations now read the registry.
  - `FeaturePolicy.exclusion_reason` checks the source gate first, so
    `candidate_feature_columns` (IF and sequence), `build_feature_spec`'s
    `feature_policy_exclusions` and the IF load audit all see it.
    `sequence_feature_set`: `gps:speed` role removed; a roles file naming a
    non-trainable channel is refused at load; `all_channels` refuses a
    non-trainable candidate, `engine_response` drops it with the reason.
    Behavior models: `refuse_non_trainable_sources` runs in the
    `BehaviorModel1D`/`2D` validators (artifact load) and in
    `_RecipeBase.__post_init__` (refit recipes), over axes, output, rate
    gates and every eligibility channel. All tracked `behavior_models.yaml`
    files (6) and the 24 default recipes still load.
  - The three library defaults that discover columns when none are passed
    apply the same gate: `isolation_forest._resolve_feature_columns`,
    `sequence_dataset.resolve_sequence_feature_columns` and
    `normalization._resolve_columns` (the scaler). Failing-first tests
    (each fails with the gate disabled, passes with it):
    `test_default_feature_discovery_never_picks_an_external_sensor` in
    `test_isolation_forest_a04.py` (`fit_isolation_forest` with no columns on
    a frame with `imu:ACCEL_X`, `gps:speed`, `sensor:FUEL_PRESSURE`) and
    `test_sequence_dataset.py`, and
    `test_default_column_discovery_never_picks_an_external_sensor` in
    `test_preprocessing_normalization.py`. Those two older files used bare
    names (`x`, `rpm`) that the allowlist now refuses; renamed to `obd:`.
  - Behaviour change from the registry growing 4 → 38:
    `capture_profiles._channel_satisfied` now resolves 34 more derived
    channels instead of treating them as unresolvable, so
    `model_input_warnings` can report a trim/AFR/fuel/committee behavior model
    as starved when its physical inputs are not polled
    (`test_model_input_warnings_resolve_derived_and_decoded_channels` loosened
    to allow the trim residuals its profile genuinely starves). Both tracked
    default profiles (`data/capture_profiles.yaml`,
    `data/nissan-xterra-2008/capture_profiles.yaml`) give `[]` before and
    after. `scripts/evaluate_behavior_models._input_resolution` likewise now
    reports input resolution for every derived output in the registry, not
    only the VE/air-mass four.
  - Serve is untouched: `reconcile_feature_frame_to_spec` still aligns to the
    manifest, so the deployed IF/LSTM keep receiving `gps:speed`; the IF load
    audit now lists its 4 `gps:speed*` features as `policy_violations`.
  - Tests: `tests/test_trainability.py` (26: synthetic session with `imu:*`,
    `gps:*`, `sensor:*`, `baro:*` and `derived:SPEED_SOURCE_DIVERGENCE` yields
    none of them as a feature; a source added to the Literal alone fails the
    import of a patched `telemetry.py` with `TypeError`; every emitted derived
    channel is declared; recipe and artifact refusals), plus updated
    `test_gps_feature_exclusion.py`, `test_sequence_feature_set.py` (deployed
    schema now 80 kept / 41 context / 281 dropped; was 82/43/279, the
    difference is `gps:speed` + `__roc`), `test_capture_profiles.py`.
  - **Corpus diff**, `build_feature_frame` over the 30 non-capture sessions
    2026-09-21T010459..2026-09-23T162918 in `~/canary-data` (29,649 rows),
    `candidate_feature_columns` with vs without the source gate: 348 → 344,
    removed exactly `gps:speed`, `gps:speed__mean`, `gps:speed__std`,
    `gps:speed__roc`, added none. The other source exclusions recorded
    (`gps:latitude/longitude/altitude/heading/fix_quality/num_satellites/hdop`,
    `derived:SPEED_SOURCE_DIVERGENCE`) were already excluded by name /
    committee flag. Applied to the tracked manifests, the gate removes only
    the `gps:speed` family: deployed IF 295 → 291, LSTM 361 → 357; the
    2026-09-23 NA candidates IF 204 → 201, LSTM 254 → 250 (r2, r3) and
    250 → 246 (first).

  **Remaining:** "the next retrained manifests carry no external-sensor
  feature" needs the retrain the A-series owns (A02/A03/A04/A06); not run
  here (retrain and deployment are not this checklist's).
- [x] **H01.2 Teach the analysis allowlists the new namespaces.** The survey
  found three that disagree with `TelemetrySource`:
  `src/analysis/channels.py` `SpecSource` and its hard-coded tuple (no `imu`,
  no `derived`), `src/models/spec_producibility.py` `CAPTURED_NAMESPACES`
  (no `imu`, no `sensor`). **Do:** derive them from `telemetry.py` where they
  mean "every captured source", or list the difference explicitly with the
  reason where they do not; add `imu` to `channels.py` so `compare_channels`
  can read IMU channels (H02.5 and H07 need it). **Accept:** a test that
  every `CAPTURED_SOURCES` member is readable by `channels.read_channels`, or
  is named in an explicit exclusion with its reason.
  **Evidence (2026-09-23):** `channels.SpecSource` gains `imu`, with a reader;
  the validation tuple is now `SPEC_SOURCES = get_args(SpecSource)`, so the
  Literal and the check cannot drift again (mypy then narrows the spec source
  by itself, and the old `type: ignore` went). `spec_producibility`
  deliberately does **not** gain `imu`/`sensor`: it checks what tracked model
  manifests can depend on, and decision 1 says none may depend on external
  sensors; the reason is in the code comment, which also records that `gps`
  stays only until the retrain H01.1 needs. Test:
  `test_every_captured_source_can_be_named_in_a_spec`
  (`tests/test_analysis_channels.py`).
- [x] **H01.3 Runtime dependencies.** The collectors need I²C (`smbus2`, pure
  Python) and GPIO line control (`gpiod`, libgpiod v2 bindings; the bring-up
  scripts use the system package). **Do:** add `smbus2` to `pyproject.toml`
  and the constraints lock per `scripts/lock_dev_constraints.sh`; decide
  whether `gpiod` becomes a Pi-only extra (it needs libgpiod) or whether GPIO
  lines are driven through a thin `ioctl` wrapper on `/dev/gpiochip0`, as
  `scripts/ups_shutdown_watch.py` already does for I²C. Either way the import
  is lazy and every test runs with a fake, so CI never needs the hardware.
  **Accept:** `scripts/check_dev_env.py` passes on the laptop venv; the Pi
  venv imports both; CI green.
  **Partial (2026-09-23):** I²C needs **no dependency** — `src/collection/i2c_dev.py`
  does the combined write/read transaction through the kernel's `I2C_RDWR`
  ioctl in a few lines of `ctypes` (what `smbus2` does inside), so nothing
  changes in `pyproject.toml` or the constraints lock. GPIO (K-line EN and
  pull-up) is still open and is decided with H05.4.
  **Done (2026-09-23):** GPIO needs no dependency either:
  `src/collection/gpio_lines.py` requests output lines through the kernel's
  GPIO v2 uAPI in `ctypes` (struct sizes asserted at import), checked on the
  HAT (claim, set, a second claimant refused with EBUSY). Nothing was added to
  `pyproject.toml`; pyserial was already a dependency.
- [x] **H01.4 Reconcile the documents that predate the HAT.**
  `docs/roadmap/imu-gps-integration.md` Phase 2b still plans a BNO055 over
  UART, a BMP390 and upper-case `gps:SPEED`/`LAT` names (the code uses
  lower-case); `OVERNIGHT-QUEUE.md` says "BNO055 is the assumed sensor";
  `docs/CANary-HAT-Spec.md` §9 says commanded AFR and timing are "not on the
  CAN bus" (true of broadcast, false of active CAN: DID `0x110A`,
  `mem:ECM_TARGET_EQUIV_RATIO`); `docs/uprev-usb-kline-research.md` still calls
  ignition timing unresolved and gives BFS as /2040..2048. **Do:** fix each,
  pointing Phase 2b at H02/H03. **Accept:** `grep -rn BNO055 docs/roadmap`
  returns only historical mentions marked as such.
  **Evidence (2026-09-24):** `imu-gps-integration.md` gains a status banner
  (HAT parts, remaining work in H02/H03/H04, BNO055/BMP390 historical,
  external sensors never train, H00 decision 1); its channel list, auto-tag
  table and dropout note use the code's lower-case names (`gps:speed`,
  `gps:latitude`, `gps:fix_quality`, `imu:ACCEL_LON/LAT`); the config sketch
  says `lsm6dsox`; every BNO055/BMP390 recommendation is labelled historical;
  Phase 2b's five items now point at H02.1–H02.3/H02.5/H02.7, H03.3/H03.4/H03.6
  and H07. `OVERNIGHT-QUEUE.md`'s two BNO055 lines are marked historical
  (the pre-made decisions themselves unchanged). `CANary-HAT-Spec.md` §9 now
  says commanded AFR and timing are absent from the *broadcast* bus only:
  timing is DID `0x110A` (`110 − raw` °BTDC) and the final commanded phi is
  `mem:ECM_TARGET_EQUIV_RATIO`, both over active CAN, with K-line's remaining
  value named (synchronous sets, multi-DID read, passive sniffing).
  `uprev-usb-kline-research.md`: ignition timing resolved (`0x110A`), BFS
  `raw × 0.00049` ms (≈ /2040; /2048 refuted on 360/405 vs 405/405 rows, per
  `data/dids.yaml` `ECM_AIR_CHARGE` evidence) in all four places that said
  /2040..2048. Also reconciled with decision 1: `docs/sensor-committees.md`
  (residual-as-feature narrowed) and the `GpsConfig` docstring. After the edit
  `grep -rn BNO055 docs/roadmap` returns 9 lines in
  `imu-gps-integration.md` and 2 in `OVERNIGHT-QUEUE.md`, each inside text
  marked historical, 1 in `H02-imu.md` ("which planned a BNO055") and the rest
  in this checklist's own text.
