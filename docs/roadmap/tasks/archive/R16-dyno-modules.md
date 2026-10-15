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

- [x] **R16.1 Inventory physical and persistence contracts.**
  - [x] List imports of dyno types, functions, constants and private helpers,
    including IMU processing, rolling circumference, controls and tests.
    `rg` over `src scripts tools tests docs` (HEAD 9b84fdc5): importers of
    `src.analysis.dyno` were `dyno_inputs` (`GPS_SPEED_CHANNEL`, `DynoRefusal`,
    `FloatArray`, `MotionSamples`, `MotionSources`), `dyno_pulls`
    (`POWER_GEAR_TOLERANCE`, `Driveline`, `DynoRefusal`, `MotionSources`,
    `PowerPull`, `RefusalKind`), `dyno_workbench` (12 names), the lazy import in
    `rolling_circumference.dyno_report` (`DYNO_FILENAME`, `load_dyno_profile`),
    and seven test files (`test_dyno`, `test_dyno_power`, `test_dyno_inputs`,
    `test_dyno_pulls`, `test_dyno_workbench`, `test_imu_session`,
    `test_change_only_can_storage`). The IMU modules (`preprocessing/imu_frame`,
    `imu_session`) and controls/routes (`control/workbench_jobs`,
    `dashboard/routes/workbench`) import none of it: IMU names the dyno only in
    docstrings and one refusal string (`src.analysis.dyno.estimate_pitch_offset`),
    and controls/routes import `dyno_workbench` only. Private helpers used outside
    the module: `_grade_with_se` (`tests/test_dyno.py`) and nothing else.
    Monkeypatch targets: `dyno_pulls.read_motion_inputs/read_channels` and
    `dyno_workbench.find_power_pulls/read_channels`, all names those modules still
    own; none targeted `dyno.*`.
  - [x] Capture current schemas and representative stored dyno profiles;
    preserve refusal kinds, units, evidence fields and conditions.
    `tests/fixtures/dyno_golden.json` (captured at HEAD, before any move):
    `DynoProfile.model_json_schema()`, the committed
    `data/nissan-xterra-2008/dyno.yaml` as loaded, a fully populated profile
    (motion sources, measured engine inertia, squat, road load, noise floor) as
    saved byte for byte plus reloaded, the 39 `RefusalKind` literals, the
    `DynoRefusal.as_dict()` payload, 31 constants, and floor-lookup refusals.
    `find / -name dyno.yaml` found no other stored profile on this machine.
  - [x] Pin results from existing squat, inertia, road-load, comparison and
    power fixtures, including both successful and refused calculations.
    `tests/test_dyno_golden.py` (5 tests) runs 103 seeded calculations from the
    existing test generators over the real chain (per-sample
    `correct_specific_force`, pitch offset, altitude noise, squat fits, 2-gear
    inertia incl. record and measured driveline, road load pair/single/no-squat,
    floors, comparisons, power runs incl. J1349 and single-direction, and
    `dyno_curve` outputs) and 30 distinct refusal kinds; arrays compare dtype,
    shape, NaN mask and value (1e-9 relative), lists and mappings compare order.
    Mutation check: `POWER_GEAR_TOLERANCE` 0.04 -> 0.05 failed two of the five
    tests. Fixture regenerates identically twice.
  - [x] Record any pickled records or annotations referring to the old path.
    None persisted: the workbench child receives `DynoRequest` (still in
    `dyno_workbench`) as a dict and returns plain data; no dyno record is
    pickled, and `data/**` names `src/analysis/dyno.py` only in a YAML comment.
    In-process, `__module__` of 47 moved classes and functions now names the owner
    (see Behaviour below).
- [x] **R16.2 Define independent owners.**
  - [x] Assign `MotionSamples`, common refusal/types and array validation to
    leaf contracts; put specific force and pitch operations in motion.
    Names recorded before moving code (dependency map in
    `tests/test_dyno_boundaries.py::DEPENDS_ON`): `dyno_contract` (leaf),
    `dyno_motion`, `dyno_altitude_noise`, `dyno_squat`, `dyno_driveline`,
    `dyno_inertia`, `dyno_road_load`, `dyno_comparison`, `dyno_power`,
    `dyno_profile`.
  - [x] Group altitude noise/squat fitting, inertia/driveline fitting and
    road-load fitting into focused calibration modules where dependencies allow.
    Altitude noise and the squat fit are separate (the squat fit needs the
    noise model, nothing needs the fit). The stored inertia facts and `Driveline`
    are split from the two-gear fit (`dyno_driveline`, `dyno_inertia`) because
    road load and power need `Driveline`, not the inertia fit; both fits take the
    squat and road load as protocols, so neither imports the other.
  - [x] Assign `PowerPull`, correction, pull-point gates and `compute_power`
    to power; assign noise-floor/run comparison to comparison.
    `dyno_power`: `PowerPull`, `J1349Correction`, `_pull_points`, `PowerCurve`,
    `DynoPower`, `compute_power`, the `POWER_*` constants. `dyno_comparison`:
    `RunConditions`, `DynoCurve`, the floor schema, `record_dyno_floor`,
    `compare_runs`, `rpm_grid`.
  - [x] Put `DynoProfile` load/save/path policy in persistence, importing
    stored record schemas rather than the complete workbench.
    `dyno_profile` imports `SquatCoefficient`, `InertiaData`,
    `RoadLoadCoefficients`, `DynoNoiseFloor`, `MotionSources` from their owners;
    `test_every_owner_imports_only_the_modules_the_map_allows`.
  - [x] Keep schema/record types with their smallest coherent owner. Do not
    create a types module that imports the estimators it is meant to support.
    Records live beside the fit that produces them (`SquatCoefficient` with
    `fit_squat`, `RoadLoadCoefficients` with `fit_road_load`,
    `EngineInertiaRecord` with the driveline). `dyno_contract` imports no dyno
    module (`test_loading_an_owner_loads_only_its_dependencies[dyno_contract]`).
- [x] **R16.3 Move in dependency order.**
  - [x] Move contracts and motion first, then calibration, power, comparison
    and persistence, retaining direct exports from `dyno.py`.
    Modules were generated from line ranges of the HEAD file by script (scratchpad
    `build.py`), so no body was retyped; `dyno.py` is now a 260-line facade of
    direct imports plus `__all__` (2,722 -> 260 lines).
  - [x] Preserve exact formula order, array operations, rounding, grids,
    weighting, least-squares inputs, numerical tolerances and refusal order.
    AST comparison (`verbatim.py`, scratchpad) of all 113 top-level definitions
    of HEAD's `dyno.py` against the new modules, after the nine renames below:
    0 missing, 0 different, 0 new. Golden: 103 results identical.
  - [x] Preserve measured conditions, reference IDs and uncertainty propagation;
    a moved condition cannot become optional or prose-only.
    Same AST check covers every `Field(...)` requirement and validator;
    golden pins `conditions`, `imu_reference_id` and every `*_se` field, and the
    schema fixture would show an optional field.
  - [x] Preserve raw versus corrected output, inertia conventions, gear gates,
    same-day/same-ROM comparison conditions and unit conversions.
    Golden: `power_corrected` (J1349, `corrected` curve) beside the uncorrected
    `dyno_curve`s, `INERTIA_CONVENTION` text, `power_gear_change`/`clutch_slip`/
    `gear_ratio_mismatch`, `floor_days` (`not_same_day_same_rom`).
  - [x] Keep vehicle facts in scoped config/profile data; introduce no fixed
    Xterra parameters while splitting driveline logic.
    `Driveline.from_vehicle` unchanged (AST-identical); no vehicle constant in
    any new module; `rg -i "xterra|nissan" src/analysis/dyno_*.py` finds none.
  - [x] Preserve atomic profile writes and operational paths outside the checkout.
    `save_dyno_profile` (`atomic_write_text(..., mkdir=True)`) and
    `dyno_profile_path` (lazy `profile_dir`) moved unchanged;
    `test_the_saved_profile_is_byte_for_byte_the_pinned_text`, `test_state_dir.py`.
- [x] **R16.4 Migrate callers and enforce direction.**
  - [x] Sweep sibling analyses, IMU modules, controls, routes and tools; point
    internal callers at owners and leave external-compatible dyno exports.
    Migrated to owners: `dyno_inputs`, `dyno_pulls`, `dyno_workbench`,
    `rolling_circumference` (lazy import). IMU modules, controls, routes and
    tools import none of it (see R16.1); their docstring/refusal-text references
    to `src.analysis.dyno.estimate_pitch_offset` stay valid through the facade.
    Tests and `tools/` keep the facade deliberately.
  - [x] Retarget monkeypatches and ensure the old exported classes are aliases,
    not duplicate definitions with incompatible identity.
    No monkeypatch targeted a moved name. `test_the_facade_exports_the_owners_object`
    (83 names, `is` identity), `test_a_class_or_function_is_defined_once_in_its_owner`,
    `test_the_facade_defines_nothing` (AST: no def/class/assignment in `dyno.py`),
    `test_the_protocols_the_fits_share_are_defined_once`.
  - [x] Add targeted checks preventing implementation modules from importing
    the facade or upper workbench/persistence orchestration.
    `tests/test_dyno_boundaries.py` (196 tests): `DEPENDS_ON` map, AST scanner
    over top-level, function, `TYPE_CHECKING`, `try`, `from src.analysis import
    dyno` and relative forms (15 scanner cases incl. relative and look-alike
    negatives), no cycle, nothing under `src/analysis` imports the facade,
    consumers never import the facade, owners never import the workbench or
    `dyno_inputs/pulls/workbench`, and a subprocess check per owner that
    importing it loads exactly its dependency closure.
  - [x] Run top-level layering, including the existing IMU-to-analysis constraints;
    do not move analysis-dependent types down by adding an upward import.
    `tests/test_layering.py` 11 passed (`OWED`/`OWED_PAIRS` empty); the new
    modules import only `src.analysis`, `src.signals.committees`,
    `src.atomic_write` and (lazily / type-only) `src.vehicle.profiles` and
    `src.config`, as `dyno.py` did.
- [x] **R16.5 Verify and close out.**
  - [x] Run `test_dyno.py`, `test_dyno_power.py`, `test_dyno_inputs.py`,
    `test_dyno_pulls.py`, `test_dyno_workbench.py`, affected IMU tests and state-dir tests.
    `pytest -n 4` on those, all eight `test_imu_*.py`, `test_state_dir.py`,
    `test_layering.py`, `test_analysis_child_registry`, `test_workbench_analysis`,
    `test_tire_wear`, `test_trip_end_digest`, `test_rom_trace`,
    `test_tuning_context`, `test_control_classification`,
    `test_change_only_can_storage`, `test_recipe_boundaries` and the two new
    files: 641 passed.
  - [x] Compare per-sample corrected motion, fitted coefficients, uncertainties,
    per-bin curves and comparison verdicts against the captured baseline.
    `test_every_calculation_matches_its_pin`: `correct_specific_force` arrays
    (both with and without squat), squat/road-load/inertia coefficients and
    `*_se`, `PowerCurve` arrays on the 50 rpm grid for pair, single heading and
    J1349, `RunComparison.bands` sigma and verdicts. 0 differences.
  - [x] Round-trip existing profiles; compare schema/data, conditions,
    reference IDs and refusal payloads. No profile migration for a pure split.
    `test_the_stored_side_matches_its_pin` (committed Xterra `dyno.yaml` as
    loaded, full profile saved and reloaded, JSON schema, refusal kinds and
    payloads), `test_the_saved_profile_is_byte_for_byte_the_pinned_text`.
    `dyno.yaml` was not edited.
  - [x] Run affected slow tests and shared gates; inspect ML path coverage
    if any code moves into a differently gated package.
    `pytest -m slow` on the dyno, golden, boundary, layering and IMU-session
    files: 7 passed (`test_dyno.py` 2, `test_dyno_inputs.py` 1, `test_imu_session.py` 2 among them).
    `ruff check .`, `ruff format --check .`, `mypy src` (570 files): clean.
    `scripts/ml_paths.txt` lists neither `src/analysis/` nor any dyno module,
    no `ml_training` test imports the dyno, and nothing moved out of a gated
    package (`src.signals.committees` is still only imported, not moved), so
    no tier changed.
  - [x] Record final dependency map, fixture differences, preserved old imports,
    tests and commits. No new hardware calibration is required for the move.
    Implementation commit: the commit that archives this file. Close-out below.

## Acceptance

Each physical question has a focused implementation owner. Existing dyno
imports, stored profiles and numerical/refusal results remain compatible.
Input preparation, pull detection and workbench orchestration stay separate.

## Close-out record (2026-10-15)

Dependency map (a module imports only what is to its right; enforced by
`tests/test_dyno_boundaries.py::DEPENDS_ON`):

| Module | Lines | Owns | Imports |
|---|---|---|---|
| `dyno_contract` | 232 | `DynoRefusal`, `RefusalKind`, `MotionSamples`, `MotionSources`/`SpeedSource`, `MIN_SAMPLES`, `FloatArray`, `StrictModel`, `as_float_array`, `lstsq`, `SquatLike`, `RoadLoadLike` | none |
| `dyno_motion` | 220 | `pitch_deg`, `remove_sensor_tilt`, `Leg`, `PitchOffset`, `frame_mismatch`, `group_legs`, `correct_specific_force`, `estimate_pitch_offset`, `distance_m` | contract |
| `dyno_altitude_noise` | 151 | `AltitudeNoise`, `measure_altitude_noise` | contract |
| `dyno_squat` | 477 | squat constants, `SquatConditions/Coefficient/Fit`, `fit_squat`, grade-se helpers | contract, motion, altitude_noise |
| `dyno_driveline` | 214 | `InertiaConditions`, `EngineInertiaRecord`, `InertiaData`, `Driveline`, `INERTIA_CONVENTION` | contract |
| `dyno_inertia` | 240 | `GearPull`, `EngineInertiaFit`, `fit_engine_inertia`, `INERTIA_*` | contract, motion, driveline |
| `dyno_road_load` | 284 | `RoadLoadConditions`, `DirectionFit`, `RoadLoadCoefficients`, `RoadLoadFit`, `fit_road_load` | contract, motion, driveline |
| `dyno_comparison` | 366 | `RunConditions`, `DynoCurve`, floor schema, `record_dyno_floor`, `compare_runs`, `rpm_grid` | contract |
| `dyno_power` | 568 | `PowerPull`, `J1349Correction`, `PowerCurve`, `DynoPower`, `compute_power`, `POWER_*` | contract, motion, driveline, road_load, comparison |
| `dyno_profile` | 112 | `DYNO_FILENAME`, `DynoProfile`, load / save / `dyno_profile_path` | contract, comparison, driveline, road_load, squat |
| `dyno` | 260 | facade: direct re-exports and `__all__` | all of the above |

`dyno_inputs`, `dyno_pulls` and `dyno_workbench` keep their responsibilities
and now import owners (inputs: contract; pulls: contract, driveline, power;
workbench: comparison, contract, driveline, motion, power, profile).

Renames made because a module now calls a helper across a module boundary
(bodies unchanged, verified by AST): `_Strict` -> `StrictModel`,
`_as_float_array` -> `as_float_array`, `_lstsq` -> `lstsq`, `_SquatLike` ->
`SquatLike`, `_RoadLoadLike` -> `RoadLoadLike`, `_correct_specific_force` ->
`remove_sensor_tilt`, `_grouped` -> `group_legs`, `_distance_m` -> `distance_m`,
`_grid` -> `rpm_grid`.

Compatibility: every name in the old `__all__` (51) and every public
constant/alias (32 more) is importable from `src.analysis.dyno` as the owner's
object; `_grade_with_se` is kept for `tests/test_dyno.py`.

Fixture differences: none. `tests/fixtures/dyno_golden.json` was captured from
the single-file module before any code moved and is unmodified by the split.

Behaviour: unchanged numerically and for every refusal. Deliberate side
effects, listed so none is a surprise:
- `__module__` of the 47 moved classes and functions now names the owner
  (`src.analysis.dyno_contract.DynoRefusal`, ...); `repr`s of dataclasses are
  unchanged. Nothing is pickled by path.
- `dyno.__all__` grew from 51 to 83 names (constants, `RefusalKind`,
  `FloatArray`, `Quantity`, `InertiaFlag`, `MIN_SAMPLES`, ...: public names that
  were importable already, now declared).
- No longer attributes of `src.analysis.dyno`: the module's incidental imports
  (`np`, `yaml`, `math`, `dt`, `Path`, `Field`, `BaseModel`, `STANDARD_GRAVITY`,
  `SIGMA_THRESHOLD`, `atomic_write_text`, ...) and its private helpers other than
  `_grade_with_se`. Nothing in `src`, `scripts`, `tools` or `tests` imported them.
- `data/nissan-xterra-2008/dyno.yaml` still says "Loaded by src/analysis/dyno.py"
  in a comment; left untouched so the data file is unchanged by a code move.

Measured limitations: the golden fixture is synthetic (seeded generators from the
existing tests) plus the one committed profile; there is no real-drive dyno
profile on this machine to round-trip. Float comparison is 1e-9 relative, so a
platform BLAS difference does not fail it, while an operation reordering that
moves a result does.
