# R16 — Dyno domain modules

Opened 2026-10-14. Shared rules: [R10](R10-refactor-README.md).
Size M–L. No prerequisite. Coordinate H07 work and live changes to the same files.

## Problem and target

`src/analysis/dyno.py` was 2,722 lines. It contains motion contracts, specific
force/pitch correction, altitude-noise modeling, squat calibration, driveline
inertia, road-load fitting, run comparison, power computation, and profile IO.
These have separate physical assumptions and test groups.

Keep the `src.analysis.dyno` public import path. Extract sibling implementation
modules within `analysis` for contracts, motion, calibration, power, comparison
and profile persistence; record the chosen names before moving code. Existing
`dyno_inputs`, `dyno_pulls` and `dyno_workbench` retain their responsibilities.
Common contracts must not depend on a facade that imports all estimators.

## Implementation checklist

- [ ] **R16.1 Inventory physical and persistence contracts.**
  - [ ] List imports of dyno types, functions, constants and private helpers,
    including IMU processing, rolling circumference, controls and tests.
  - [ ] Capture current schemas and representative stored dyno profiles;
    preserve refusal kinds, units, evidence fields and conditions.
  - [ ] Pin results from existing squat, inertia, road-load, comparison and
    power fixtures, including both successful and refused calculations.
  - [ ] Record any pickled records or annotations referring to the old path.
- [ ] **R16.2 Define independent owners.**
  - [ ] Assign `MotionSamples`, common refusal/types and array validation to
    leaf contracts; put specific force and pitch operations in motion.
  - [ ] Group altitude noise/squat fitting, inertia/driveline fitting and
    road-load fitting into focused calibration modules where dependencies allow.
  - [ ] Assign `PowerPull`, correction, pull-point gates and `compute_power`
    to power; assign noise-floor/run comparison to comparison.
  - [ ] Put `DynoProfile` load/save/path policy in persistence, importing
    stored record schemas rather than the complete workbench.
  - [ ] Keep schema/record types with their smallest coherent owner. Do not
    create a types module that imports the estimators it is meant to support.
- [ ] **R16.3 Move in dependency order.**
  - [ ] Move contracts and motion first, then calibration, power, comparison
    and persistence, retaining direct exports from `dyno.py`.
  - [ ] Preserve exact formula order, array operations, rounding, grids,
    weighting, least-squares inputs, numerical tolerances and refusal order.
  - [ ] Preserve measured conditions, reference IDs and uncertainty propagation;
    a moved condition cannot become optional or prose-only.
  - [ ] Preserve raw versus corrected output, inertia conventions, gear gates,
    same-day/same-ROM comparison conditions and unit conversions.
  - [ ] Keep vehicle facts in scoped config/profile data; introduce no fixed
    Xterra parameters while splitting driveline logic.
  - [ ] Preserve atomic profile writes and operational paths outside the checkout.
- [ ] **R16.4 Migrate callers and enforce direction.**
  - [ ] Sweep sibling analyses, IMU modules, controls, routes and tools; point
    internal callers at owners and leave external-compatible dyno exports.
  - [ ] Retarget monkeypatches and ensure the old exported classes are aliases,
    not duplicate definitions with incompatible identity.
  - [ ] Add targeted checks preventing implementation modules from importing
    the facade or upper workbench/persistence orchestration.
  - [ ] Run top-level layering, including the existing IMU-to-analysis constraints;
    do not move analysis-dependent types down by adding an upward import.
- [ ] **R16.5 Verify and close out.**
  - [ ] Run `test_dyno.py`, `test_dyno_power.py`, `test_dyno_inputs.py`,
    `test_dyno_pulls.py`, `test_dyno_workbench.py`, affected IMU tests and state-dir tests.
  - [ ] Compare per-sample corrected motion, fitted coefficients, uncertainties,
    per-bin curves and comparison verdicts against the captured baseline.
  - [ ] Round-trip existing profiles; compare schema/data, conditions,
    reference IDs and refusal payloads. No profile migration for a pure split.
  - [ ] Run affected slow tests and shared gates; inspect ML path coverage
    if any code moves into a differently gated package.
  - [ ] Record final dependency map, fixture differences, preserved old imports,
    tests and commits. No new hardware calibration is required for the move.

## Acceptance

Each physical question has a focused implementation owner. Existing dyno
imports, stored profiles and numerical/refusal results remain compatible.
Input preparation, pull detection and workbench orchestration stay separate.
