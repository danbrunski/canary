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

- [ ] **H01.1 A source-level trainability gate.** **Do:** in `src/telemetry.py`,
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
- [~] **H01.3 Runtime dependencies.** The collectors need I²C (`smbus2`, pure
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
- [ ] **H01.4 Reconcile the documents that predate the HAT.**
  `docs/roadmap/imu-gps-integration.md` Phase 2b still plans a BNO055 over
  UART, a BMP390 and upper-case `gps:SPEED`/`LAT` names (the code uses
  lower-case); `OVERNIGHT-QUEUE.md` says "BNO055 is the assumed sensor";
  `docs/CANary-HAT-Spec.md` §9 says commanded AFR and timing are "not on the
  CAN bus" (true of broadcast, false of active CAN: DID `0x110A`,
  `mem:ECM_TARGET_EQUIV_RATIO`); `docs/uprev-usb-kline-research.md` still calls
  ignition timing unresolved and gives BFS as /2040..2048. **Do:** fix each,
  pointing Phase 2b at H02/H03. **Accept:** `grep -rn BNO055 docs/roadmap`
  returns only historical mentions marked as such.
