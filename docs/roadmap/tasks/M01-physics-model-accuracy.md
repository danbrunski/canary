# M01 — Physics model accuracy

A review of the physics in the code, 2026-11-12: do the equations say what
the engine and the truck do, in the units and conditions they are used in?
Run beside the refactor review ([R22](R22-refactor-README.md)); this file is
about correctness, not structure. Nothing here has been implemented.

**Scope.** Derived channels, trip and economy figures (`src/signals/`), the
driving dyno (`src/analysis/dyno_*`), IMU frame resolution
(`src/preprocessing/imu_*`), the manifold, fuel and torque plants and the
fuel-pressure evidence (`src/analysis/engine_dynamics.py`, `fuel_plant*.py`,
`torque_plant.py`, `delivery_evidence.py`, `fuel_comp_droop.py`), the λ chain
(`signals/afr.py`, `lambda_delivery.py`), vehicle parameters and the Xterra
profile data.

**Method.** Each relation was re-derived and compared with the code, then
exercised on hand-computed cases at this truck's operating points (63–82 kPa,
idle 700 rpm / 4 g/s, cruise 2,500 rpm / 25 g/s, full throttle 5,500 rpm /
150 g/s). Two errors were reproduced by running the repo's own functions on
synthetic input (M01.1, M01.7). **No recorded session was read**, so every
size below is a derivation until someone measures it on a stored drive; each
item says what that measurement is.

**Labels.** *Error*: the code computes something other than the quantity it
names. *Out of range*: a correct formula used outside the conditions it holds
for, without saying so. *Question*: the physics cannot settle it; a
measurement or the operator can. *Vehicle fact*: a property of this truck in
shared code.

All of this is Deep-tier work (measurement and inference). Several fixes
change stored numbers (trip sidecars, derived channels, dyno results). Before
changing a channel, check whether a trained feature set contains it: if one
does, the fix is a spec change and a retrain is owed (the 2026-09 IF incident).

## What matters most

| Item | Today | True |
|---|---|---|
| M01.1 0–60 mph, ¼ mile | include the wait at the line (39.0 s for a 9.0 s run) | time from launch |
| M01.2 trip distance, average speed | 8.0 % low | ÷ 0.9203 |
| M01.2 + M01.3 reported mpg | ≈ 0.955 × true (two errors that partly cancel) | fix both together |
| M01.7 dyno pitch offset | 0.262° for a true 0.300° on a ±0.5 % crest; road-load `A` 9 % low | weight by distance |
| M01.9 "corrected" power | × 1.26–1.70 at 82–63 kPa, ±4–5 % from assumed inputs | label as extrapolated |
| M01.14 fuel-pressure droop | exact for the NA engine | under boost it reports 0 % for a 5.6 % flow loss |

## Part A — Trip, economy and acceleration figures

- [x] **M01.1 (Error) 0–60 mph and quarter-mile times include the time spent
  stationary.** `signals/derived.py` `_accel_metrics` sets `launch_t` at the
  *first* sample at or below launch speed (2 mph). While the truck sits at
  exactly 0 km/h, which is what the integer speed PID reads at a stop,
  `dist_m` stays 0, the "bogged down, abandon" test (`speed <= launch and
  dist_m > 0`) never fires, and the clock keeps the old start.
  - Reproduced through the function: 30 s at rest, then a constant 3 m/s².
    It returns 0–60 = 39.0 s and ¼ mile = 48.0 s; the run is 9.0 s and
    18.0 s at 1 Hz sampling.
  - The "best across launches" rule hides it only when some launch in the
    session had no stop before it.
  - [x] Reset `launch_t` and `dist_m` on every sample at or below launch
    speed, so the clock starts at the last one. Regression test: the case
    above.
    *Done 2026-11-12:* `_accel_metrics` rewritten as one pass; every sample
    ≤ launch speed restarts the clock and zeroes distance (which also covers
    the old "bogged down" abandon). The case above, run through the function
    on the old code, returned (39.0, **47.0**) — not 48.0 — and returns
    (8.9408, 16.3704, trap 176.8 kph) now; analytic is 8.941 s and
    sqrt(2·402.336/3) = **16.377 s** (the 18.0 s above was a slip).
    Tests in `tests/test_derived.py`:
    `test_accel_metrics_clock_starts_at_last_stopped_sample`,
    `test_accel_metrics_wait_is_excluded_through_summarize_trip`,
    `test_accel_metrics_creep_below_launch_speed_restarts_clock` (creep at
    ≤ 2 mph before the pull), `test_accel_metrics_bogged_run_does_not_count`.
  - [x] The thresholds are on raw `obd:SPEED` (M01.2), so "60 mph" is reached
    at about 65 true and the quarter mile ends at about 437 m. Use corrected
    speed.
    *Done 2026-11-12 with M01.2:* `summarize_trip` hands `_accel_metrics`
    road speed (its `SpeedCorrection`). `test_accel_metrics_run_on_road_speed`:
    true speed ramps at 3 m/s² after a 30 s wait and the ECM reports 0.9203
    of it. Corrected: 0–60 8.9408 s (60 mph reached at an indicated 55.2),
    ¼ mile 16.3704 s at 402.336 m of road, trap 176.8 km/h; analytic 8.941 s
    and 16.377 s. Uncorrected, the same trace gives 9.7151 s and 17.0701 s
    (60 mph at a true 65.2, the quarter mile at 437 m).
  - [x] At 1 Hz the crossing time is quantized to a second. Interpolate the
    crossing between samples, or report the resolution with the figure.
    *Done 2026-11-12:* linear interpolation between the straddling samples —
    speed for the 60 mph time, cumulative trapezoid distance for the
    quarter-mile ET, and the trap speed is the speed at that interpolated
    instant (docstring says so). The start is still the last stopped sample,
    so figures can read long by up to one sample interval of the real
    launch. `test_accel_metrics_crossing_is_interpolated_not_quantized`
    (4 m/s²: 6.7056 s, not 7 s); the 3 m/s² case lands within 0.007 s of the
    analytic quarter mile.

- [~] **M01.2 (Error) Trip distance integrates the ECM's speed, which reads
  0.9203 × true on this truck.** `compute_derived_channels`
  (`derived:TRIP_DISTANCE_KM`), `summarize_trip` and the live accumulator in
  `features/live.py` all integrate raw `obd:SPEED`. The bias is recorded in
  `data/nissan-xterra-2008/dyno.yaml` (`ratio_to_gps: 0.9203`), in
  `committees.yaml`, in the `gps_tracker` docstring ("wrong for distance") and
  in `docs/can-pids-dids.md`. The trip path does not apply it. Distance and
  average speed are 8.0 % low, so L/100 km reads 8.7 % high from this term
  alone. One hour at an indicated 100 km/h is 108.7 km.
  - [x] A per-profile speed correction for the Mode 01 speed channel, read by
    one function that the batch and live paths both call (they are pinned
    equal by
    `test_live_trip_integrals_match_batch_compute_derived_channels`). A
    vehicle with no measured ratio integrates uncorrected and **says so**:
    the trip summary carries a `speed_basis` field.
    *Done 2026-11-12:* `src/signals/speed_correction.py` `SpeedCorrection`
    (`road_speed`, `basis`; `TRIP_SPEED_CHANNEL = "obd:SPEED"`) is a
    **required** keyword of `compute_instantaneous_channels`,
    `compute_derived_channels`, `summarize_trip` and
    `TripSample.from_point`, so no caller can forget it. The ratio is read
    from the profile's own `dyno.yaml` `motion_sources.speed` entry for
    `obd:SPEED` by `src/vehicle/speed_correction.py`
    `trip_speed_correction(config)` (mtime-cached; no file / no entry is
    `SpeedCorrection.unmeasured`, naming which; malformed raises), not
    through a platform: another truck of the generation on other tires must
    not borrow it. Callers: `trip_summary_sidecar`, `feature_transform`
    (instant economy, gear) and `LiveFeatureSource` (trip integrals).
    `TripSummary.speed_basis` and `fuel_basis` are required dataclass fields
    (and in the TS type; `dashboard/schemas.py` `TripSummaryFields` also
    listed them but nothing validated against it, and R29.15 deleted it).
    Tests: `test_live_and_batch_apply_the_same_speed_ratio_and_fuel_cut`
    (live = batch = hand-computed, at ratio 0.9203 with a fuel cut),
    `test_distance_and_average_speed_integrate_road_speed` (1 h at an
    indicated 100 km/h = 108.66 km), `test_the_summary_states_its_speed_basis`,
    `test_the_basis_fields_are_required`, and `tests/test_speed_correction.py`
    (bounds, unmeasured kinds, malformed file, and the loader agreeing with
    the dyno's `MotionSources` schema for every tracked `dyno.yaml`).
    Maximum speed and instant economy use road speed too.
  - [x] The ratio drifts with tire wear (full tread is about 2 % of radius)
    and was measured in September on the pre-HAT receiver. Decide whether it
    is a profile constant or follows the rolling-circumference estimate
    (`/control/tires`).
    *Decided (operator, 2026-11-12): a profile constant, 0.9203 for
    `obd:SPEED`.* Provenance and the tire-wear caveat are in its `evidence`
    in `data/nissan-xterra-2008/dyno.yaml`: measured 2026-08-06T162657
    (2185 paired seconds), re-measured 0.915–0.924 on six drives
    2026-07-28..09-21 by the committee evaluator on 2026-09-25, all on the
    pre-HAT USB receiver; re-measure after a tire change.
  - [ ] *Future item (not part of this fix):* follow the per-drive
    rolling-circumference estimate (`/control/tires`) instead of the
    constant, so tire wear and a tire change track themselves.
  - [x] Bump the trip-summary sidecar's schema version so old sidecars
    recompute, and note the date in the docs: mpg trends do not carry across
    it (the same warning as the 2026-07-26 `dt` fix).
    *Done 2026-11-12:* `trip_summary_sidecar.SCHEMA_VERSION` 3 → 4 (the
    daemon route and `get_or_compute_trip_summary` recompute any other
    version); the dated warning is in its comment and in
    `docs/mcp-server.md` (`get_trip_summary`). `docs/headless-api.md` was
    dirty with another session's edit, so its row is not changed.
  - [x] `_gear_estimate` matches rpm ÷ speed against the ratio table using the
    same biased speed. It still picks correctly on this gearbox (8.6 % bias
    against 30 % spacing between 5th and 6th); apply the correction there too
    so a close-ratio box is not marginal.
    *Done 2026-11-12:* `compute_instantaneous_channels` hands
    `_gear_estimate` road speed.
    `test_gear_estimate_matches_on_road_speed`: gears 6 % apart, indicated
    8 % low; uncorrected names gear 1, corrected gear 2 (true).

- [x] **M01.3 (Error, two parts) One constant is doing two jobs: the fuel's
  stoichiometric ratio and the ECM's AFR display scale.**
  - **(a) Latent.** `analysis/tuning_context.py` fills
    `TuningContext.afr_stoich` from
    `fuel_properties(config.vehicle.parameters).afr_stoich`, and
    `lambda_delivery.to_lambda` divides the voltage route's "AFR" by it. That
    AFR comes from the ECM's own table (`afr_calibration.yaml`,
    `table_afr_x500`): it is λ times a constant the ECM's author chose, 14.7,
    whatever is in the tank. A wideband measures oxygen balance and cannot
    know the fuel. `dashboard/routes/rom_maps.py` already has it right
    (`"afr_stoich": editor.definition.afr_numerator / 128`). Today the two
    agree because `fuel_type` is `gasoline` = 14.7, so there is no numeric
    error. The day the profile says E10 (14.1), every voltage-route λ reads
    4.3 % lean at every load, which is above the 2.5 % noise floor and would
    be read as a fueling error in every cell; E85 gives 50 %. The
    `TuningContext` docstring ("it comes from the configured fuel … a
    gasoline literal is wrong for every other profile") steers the next
    editor toward that mistake.
    - [x] Carry the AFR scale on the thing that produced the number (the AFR
      calibration, or the measured-source declaration for a third-party
      gauge) and remove `afr_stoich` from `TuningContext`, so an AFR-quantity
      source cannot be converted with a fuel property. Sweep the callers
      (`delivery_evidence`, `sim_design`, `plant_validation`).
      *Done 2026-11-12:* `AfrCalibration.afr_scale` is a required field
      (Xterra `afr_scale: 14.7`, the 0xE644 table's λ × 7350 = AFR × 500);
      `MeasuredSource.afr_scale` is required for an `identity` AFR source and
      refused on `voltage_afr` (whose scale is the calibration's) and on λ
      sources; `CommandedSource.afr_scale` is required for `quantity: afr`.
      `to_lambda(..., afr_scale=)` refuses an AFR without one, and every
      `afr_stoich` parameter on the delivery, tuning-frame, channel and
      digest paths is gone: the scale is resolved from the source
      (`MeasuredSource.scale_for(calibration)`), so no caller can hand in a
      fuel property. The same defect in reverse was swept too: φ→AFR
      (`obd:COMMANDED_AFR`, the tuning frame's `equiv_ratio_afr`), the
      delivery report's AFR columns, `derived:AFR_B*` and the A/F-voltage λ
      bank balance now use `calibration.afr_scale`. `sim_design`,
      `plant_validation.fill_samples` and `tools/rom_sim.py` lost their
      `fuel_type` / `--fuel-type` (it only fed that conversion), as did
      `scripts/lambda_delivery_evidence.py` and three `tools/fuel_comp_*`
      CLIs. The ROM-maps API emits `afr_scale` (and the old `afr_stoich`
      alias). Values unchanged: the delivery samples of every
      commanded × measured route (voltage routes included) and the tuning
      frame hash identically at HEAD and after, on
      `_capture-ram-study-2026-10-04T204557`,
      `_capture-tuning-2026-11-12T010305`/`T034249` and the committed
      fixtures. Refusals: `test_an_afr_source_declared_without_a_scale_is_refused`,
      `test_an_afr_calibration_without_its_scale_is_refused`,
      `test_an_afr_reading_needs_its_sources_scale`
      (`tests/test_lambda_delivery.py`).
    - [x] Test: set the profile's fuel to something other than gasoline and
      assert the voltage-route λ does not move.
      *Done 2026-11-12:*
      `test_a_non_gasoline_fuel_does_not_move_the_voltage_route_lambda`
      (gasoline vs E85 profile through `TuningContext.from_config`; λ at the
      stoich knot = 7387/500/14.7),
      `test_the_context_carries_no_fuel_stoichiometry`,
      `test_a_non_gasoline_profiles_workbench_afr_is_the_ecms_afr`
      (`tests/test_tuning_context.py`), and
      `test_lambda_balance_is_taken_in_lambda_after_each_banks_own_transfer[e85]`
      (`tests/test_bank_balance_persistence.py`, fails on the old code).
  - **(b) Present.** `signals/derived.py`
    `FUEL_PROPERTIES["gasoline"] = (14.7, 0.745, 2.31, 43.0)` (stoich, kg/L,
    kg CO2/L, MJ/kg) are E0 values. `fuel_plant.yaml` works at 14.1 with the
    note "E10 assumed" (status `requires_measurement`), and the
    `engine-management` skill says pump fuel is E10 at about 14.1. For E10 by
    volume: stoich 14.08, density 0.749 kg/L, about 2.28 kg CO2/L, LHV about
    41.7 MJ/kg. Fuel volume = MAF ÷ (stoich · λ · ρ), so the derived figure is
    3.65 % low and mpg 3.8 % high. At 25 g/s and λ = 1 the fuel plant says
    1.773 g/s and the derived channel 1.701 g/s, for the same fuel.
    - **Answered (operator, 2026-11-12):** the truck runs whatever standard
      pump gasoline is available, usually the 10 % ethanol blend; with the
      supercharger it moves to 91 octane. So E10 is the working fuel and the
      blend is not constant: stoich lies between 14.1 (E10) and 14.7 (E0),
      the bracket `fuel_plant.yaml` already states, and any fuel-mass, economy
      or CO2 figure carries up to 4 % from the blend alone. λ does not.
    - [x] Add an `e10` fuel type (stoich 14.08, 0.749 kg/L, 2.28 kg CO2/L,
      41.7 MJ/kg), or put stoich and density in the profile, and select it for
      this truck. **Do (a) first**, or selecting it triggers the latent error.
      *Done 2026-11-12:* `FUEL_PROPERTIES["e10"]` in `signals/derived.py`,
      `"e10"` in `VehicleParameters.fuel_type` (dashboard choices follow:
      `tests/test_profile_edit.py`), and `profiles.yaml` sets
      `fuel_type: e10` for `nissan-xterra-2008` (the 2013 stays `gasoline`:
      no operator answer for it). Selected last, after (a) in this tree.
      Per sample on 2026-10-10T153117 and 2026-09-20T152758, every
      non-fuel-cut row's `derived:FUEL_RATE_LPH` moved by exactly
      ×1.038458 = (14.7·0.745)/(14.08·0.749) (8,219 rows, min = max).
      `test_e10_constants`,
      `test_e10_fuel_volume_moves_by_the_analytic_factor_per_unit_air`.
      LHV is **41.3**, not the 41.7 above: the blend arithmetic used for e85
      (0.0789 kg ethanol at 26.8 MJ/kg + 0.6705 kg E0 at 43.0, over
      0.7494 kg) gives 41.29, and 41.7 cannot be reproduced from those
      components; CO2 2.28 is that arithmetic's. LHV feeds only
      `_estimated_power_kw`'s fuel-energy route: E0 → E10 moves
      `derived:ESTIMATED_POWER_HP` / `ESTIMATED_TORQUE_NM` ×1.0028 (×1.0125
      at 41.7). Pinned by
      `test_blend_co2_and_heating_value_follow_the_blend_arithmetic[e10]`.
    - [x] The trip summary states the fuel basis it used, next to
      `speed_basis` (M01.2).
      *Done 2026-11-12:* `TripSummary.fuel_basis` (required), e.g. `e10:
      stoich 14.08, 0.749 kg/L, 2.28 kg CO2/L, LHV 41.3 MJ/kg; zero in decel
      fuel cut (obd:COMMANDED_EQUIV_RATIO = 0 on 60 rows)`, or "decel fuel
      cut not detectable (no obd:COMMANDED_EQUIV_RATIO), so fuel-cut rows
      count fuel". `test_fuel_basis_says_whether_fuel_cut_was_detectable`.
    - [x] Octane does not change stoich: 91 is still an ethanol blend, so
      these constants stay when the grade changes. What the grade changes is
      knock margin, so record the change as a vehicle event when it happens
      and do not compare spark or knock data across it without saying so.
      Z1's published power figures are on 93 octane
      (`docs/roadmap/supercharger-build.md`); the build will run on 91.
      *Done 2026-11-12 (text only, as briefed):* the `FUEL_PROPERTIES`
      comment and the comment on the Xterra's `fuel_type` in `profiles.yaml`
      say both. No machinery built.
    - [x] The `e85` row's CO2 (1.56 kg/L against about 1.64) and LHV are also
      a little low; check them when the table is touched.
      *Done 2026-11-12:* 1.64 kg CO2/L and 29.1 MJ/kg by blend arithmetic
      (85 % ethanol by volume at 0.789 kg/L, 52.14 % C, 26.8 MJ/kg; 15 % E0 at
      0.745 kg/L, 86.6 % C, 43.0 MJ/kg); the CO2 also matches the volume blend
      of EPA's 5.75 / 8.78 kg CO2/gal factors. Component figures are handbook
      values from memory, not a fetched source; the arithmetic is pinned in
      `test_blend_co2_and_heating_value_follow_the_blend_arithmetic[e85]`. Stoich
      and density unchanged.
  - With M01.2: reported mpg ≈ 0.955 × true. The two errors have opposite
    signs, so fixing one alone makes the number worse.

- [x] **M01.4 (Approximation, undocumented) Fuel is counted during decel fuel
  cut.** `_instant_fuel_g_s` computes MAF ÷ (stoich · λ) for every sample with
  λ > 0. In fuel cut the injectors are off, MAF still reads a few g/s and the
  wideband sits at its lean limit, so each second counts a little fuel that
  was not injected: 5 g/s at λ 2.0 is 0.82 L/h. `signals/afr.fuel_cut_rows`
  (PID 0x44 = 0) exists and this path does not use it.
  - [x] Measure it first on a stored mountain drive: the share of
    `derived:FUEL_USED_L` accrued in rows `fuel_cut_rows` marks. The estimate
    is about 1 % on an ordinary drive and 3 % on a long descent.
    *Measured 2026-11-12* (HEAD code, `resample_and_segment` 1 Hz grid,
    `compute_derived_channels` without calibration as the sidecar runs it;
    wideband λ present on every drive; descent = largest drop of 30 s-smoothed
    GPS altitude):

    | Drive | Whole drive: fuel, share in cut rows | Descent: drop, distance, share |
    |---|---|---|
    | 2026-09-20T152758 (mountain) | 2.706 L, **2.10 %** | 467 m over 8.9 min, 8.6 km indicated, **12.4 %** |
    | 2026-10-10T153117 (ordinary) | 3.031 L, **0.92 %** | 148 m, 11.3 km, 0.82 % |
    | 2026-08-06T162657 | 9.63 L, 1.46 % | 303 m, 12.5 km, 3.6 % |
    | 2026-09-10T142711 | 3.53 L, 2.30 % | 306 m, 7.0 km, 4.4 % |
    | 2026-10-10T134622 | 3.64 L, 1.11 % | 293 m, 7.5 km, 3.2 % |

    These are lower bounds: PID 0x44 is fresh on only 29–60 % of grid rows,
    and a row without it is not known to be in cut. On φ-observed rows alone
    the share is 3.5 % (09-20T152758) and 1.4 % (10-10T153117), and 24 % of
    the 09-20 descent. The 160 φ = 0 rows of those two drives sit at a median
    6 % throttle, 60 km/h and λ 2.0 (the wideband's lean limit), so φ 0 is
    fuel cut, not "λ unavailable" as an older test assumed.
  - [x] Zero the fuel rate in fuel-cut rows. Keep the existing rule that a
    missing λ during open-loop enrichment yields NaN, not stoich; that part is
    right and its comment says why.
    *Done 2026-11-12:* `_instant_fuel_g_s` returns 0 g/s on
    `fuel_cut_rows` for every method. `fuel_cut_rows` and
    `COMMANDED_EQUIV_RATIO_COLUMN` moved to `signals/fuel_cut.py` (`derived`
    cannot import `afr`, which imports `derived`); `afr` re-exports them, and
    `test_fuel_cut_is_defined_once` holds it to one definition under `src/`.
    The feature-frame cache moved to version 11 and hashes the profile's
    `dyno.yaml` (`test_the_feature_cache_key_hashes_the_speed_ratio_the_trip_channels_read`),
    so retraining cannot reuse pre-fix frames. The live integrals read
    the same zeroed `derived:FUEL_RATE_LPH`, so there is no live copy. The
    commanded-ratio route used to give NaN there, and the integrator bridged
    the cut with the fuelled samples either side. Removing it took 1.005 %
    of 10-10T153117's fuel and 2.379 % of 09-20T152758's (more than the
    rows' own increments, because a zeroed row also halves its neighbors'
    trapezoids). Tests: `test_fuel_cut_rows_count_no_fuel_on_the_wideband_route`,
    `test_fuel_cut_is_not_bridged_on_the_commanded_ratio_route`,
    `test_missing_lambda_outside_fuel_cut_stays_nan_not_stoich`,
    `test_a_commanded_ratio_of_zero_is_fuel_cut_not_infinite` (was
    `..._lambda_of_zero_is_absent_not_infinite`). Cross-check against the
    ECM's own fuel counter (0x551 byte 1, F01.14): in φ-0 rows it advanced on
    13 of 98 (09-20) and 19 of 60 (10-10) rows, at 0.32 and 0.68 L/h on the
    fuelled-row scale against the old 1.45 and 1.82 L/h, so zero is much the
    nearer; the residual (≈0.3–0.4 % of each drive's fuel) is the 1 Hz grid's
    φ timing at the edges of a cut.
    *Known answer, M01.2 + M01.3(b) + M01.4 together* (stored sidecar path
    `compute_trip_summary`, HEAD worktree against this tree, per sample):
    every moving row's distance increment ×1.086602 = 1/0.9203 (1,667 rows,
    min = max); every non-cut fuel-rate row ×1.038458; every cut row 0.
    Re-integrating the old per-row rates with those two changes reproduces
    the new totals to 5 decimals. 10-10T153117: 19.846 → 21.565 km, 3.0313 →
    3.1162 L, 15.40 → 16.28 mpg (×1.0570 = (1/0.9203) / 1.03846 /
    (1 − 0.01005)); 09-20T152758: 11.304 → 12.283 km, 2.7064 → 2.7436 L,
    9.82 → 10.53 mpg (×1.0719, cut share 2.379 %).

- [ ] **M01.5 (Question) MAF-based economy has never been checked against a
  fill-up.** Fuel = MAF ÷ (stoich · λ) inherits the MAF's calibration error
  1:1. The fuel plant's fitted scale (`stoich_air_per_open_ms` 0.0510) sits
  8.9 % below its stated prior (0.0560: 296 cc/min at 300 kPa → 319.7 at
  350 kPa, × 0.745 g/cc, × 14.1), and the fit does not say which of its four
  components moved. Candidates, cheapest first:
  - the injector rating's test fluid. cc/min ratings are commonly on
    n-heptane (0.684 g/cc); on that basis the gasoline mass flow is about
    4.2 % below what `× 0.745` gives. From memory of the rating convention,
    not from this part's data sheet;
  - an injector dead-time offset absorbed by a single scale;
  - the rail-pressure zero (M01.15: 337 kPa true instead of 350 is −1.9 %);
  - the MAF reading low.
  - [ ] One tank-to-tank comparison: liters at the pump against
    `derived:FUEL_USED_L` summed over the tank, distance from GPS. It assigns
    the MAF share and validates M01.2–M01.4 together. The injector headroom
    arithmetic for the supercharger rests on this scale (the implied
    full-throttle pulse is 11.2 ms at 5,500 rpm, 51 % duty).

- [ ] **M01.6 Small items in `derived.py`.**
  - `_estimated_power_kw`'s fuel-energy route scales with fuel flow. Rich of
    stoich the extra fuel does not burn, so at λ 0.85 it reads about 18 %
    high. Cap the energy at the stoichiometric fuel for the air, or do not
    use it to compare pulls at different commanded AFR.
  - Idle fraction divides idle time by session duration, which includes
    key-on engine-off time.
  - `gps_tracker`'s odometer uses a rectangle rule, and the collector's
    5 km/h snap-to-zero drops crawl distance.

## Part B — Driving dyno

The force balance itself is right (see "Checked and correct"). These are
about its inputs and its corrections. Same-day A/B comparisons cancel most of
them; absolute and "corrected" figures do not.

- [x] **M01.7 (Error) The two-heading pitch offset is averaged over time, not
  distance.** `dyno_motion.estimate_pitch_offset` takes `pitch.mean()` over
  each heading's samples and averages the two headings. Its docstring says
  the result is "exact when both legs cover the same stretch". Grade cancels
  between headings only if each meter of road carries the same weight in
  both; in a coast-down or a pull, speed changes monotonically, so each
  heading over-weights the end it crosses slowly, and those are opposite ends
  of the road.
  - Reproduced with the repo's functions (script in the review scratchpad;
    true offset 0.300°, 2,311 kg, a 1,400 m road with a symmetric crest,
    coast-downs both ways):

    | Crest grade | Estimated offset | Fitted `A` (true 180 N) |
    |---|---|---|
    | 0 | 0.3000° | 180.1 N |
    | ±0.3 % | 0.2770° | 170.1 N |
    | ±0.5 % | 0.2621° | 163.7 N |
    | ±1.0 % | 0.2261° | 147.8 N |

    Weighting each sample by `v·dt` returns 0.3000° in all three.
  - 0.1° is 39.6 N at this mass: under 1 % of a full-throttle pull, but the
    H07 notes record that 0.3° moves the fitted engine inertia by about 44 %,
    and road-load `A` comes straight from it.
  - [x] Distance-weight the per-heading mean. Commit the simulation as the
    regression test (a known-answer case, per `AGENTS.md`). Correct the
    docstring. Re-run the stored offset and road-load fits and record how far
    they move.

    *Done 2026-11-13:* `dyno_motion.distance_weights` gives each sample
    `v·dt`, `dt` half the interval to each neighbour (the trapezoid rule for
    `∫v dt`, so the weights sum to `distance_m(...)[-1]`), and
    `estimate_pitch_offset` takes each heading's weighted mean over all its
    runs. A stopped sample weighs nothing and does not count towards
    `min_samples` ("N moving samples (of M)"); a heading that covered no
    distance is refused `no_distance` (new `RefusalKind`; a moving heading
    whose runs are single samples is `too_few_samples`), a negative speed
    `invalid_input`. The motion reader (`dyno_inputs.build_motion_inputs`)
    now clamps its smoothed speed at 0: the quadratic smoother undershot to
    about −0.02 m/s at a standstill corner, so a first-gear launch from rest
    would have refused its gear's offset (found by the verifier). Acceleration
    is the derivative kernel on the raw speed, so the clamp does not touch
    it; the refusal stays for a caller's own negative input. Gaps: the motion reader already refuses holes
    (`inputs_gap`), and a hole that reaches the estimator is bridged by its
    two bounding samples. `PitchOffset.heading_distance_m` records each
    heading's distance, since unequal stretches are the leak that remains
    (the docstring now says so, and drops "exact when both legs cover the
    same stretch" for a time mean).
    - Regression test `tests/test_dyno.py::test_pitch_offset_is_distance_weighted_over_a_crest`,
      built from the repo's functions (`Driveline.neutral_effective_mass`,
      `_pitch_sf`, `estimate_pitch_offset`, `fit_road_load`): true offset
      0.300°, 2,311 kg, 1.2°/g squat, A/B/C 180 / 5.0 / 0.55, no wind, no
      noise, 10 Hz, a 1,400 m parabolic crest (grade `c·(1 − 2s/L)` in the
      direction of travel), coasts entering at 26 m/s both ways. The review
      script was not in the repo; this geometry reproduces its table to
      within 0.001°, not exactly (its `A` column, 170.1 / 163.7 / 147.8 N,
      is not reproduced):

      | Crest grade | Time-weighted (HEAD) | `A` at HEAD | `v·dt` weighted | `A` now |
      |---|---|---|---|---|
      | 0 | 0.3000° | 180.0 N | 0.3000° | 180.0 N |
      | ±0.3 % | 0.2773° | 171.0 N | 0.30014° | 180.06 N |
      | ±0.5 % | 0.2625° | 165.2 N | 0.30005° | 180.02 N |
      | ±1.0 % | 0.2271° | 151.1 N | 0.30009° | 180.03 N |

      The test asserts the new offset within 5e-4° (the residual is the last
      10 Hz sample stopping short of the road's end: 1e-5° at 100 Hz),
      `A` within 0.2 N, `B`/`C` within 1e-4 relative, and the old
      time-weighted formula on the same samples at the HEAD values within
      1e-4°. Run against HEAD's estimator in a worktree, the three graded
      cases fail at 0.2773 / 0.2625 / 0.2271.
      `test_pitch_offset_gives_a_stop_no_weight` (a minute parked on a 3 %
      slope moves the new offset < 1e-4°, the old one > 0.3°) and
      `test_pitch_offset_refuses_a_heading_that_covered_no_distance`
      (`no_distance`, `invalid_input`, stopped samples not counted, a
      one-sample run is `too_few_samples`).
      `test_pitch_offset_pools_a_headings_runs_by_distance`: a full-road and a
      summit-stop (700 m) run northbound pool to 0.39563° (= distance-weighted
      mean of the runs' 0.30009° and 0.58706°; a third of the climb's grade
      above the full run, 0.09554° vs 0.09549° analytic); run-by-run
      averaging would give 0.44357°.
      `tests/test_dyno_inputs.py::test_a_launch_from_rest_reads_no_negative_speed_and_reaches_the_pitch_offset`:
      stopped until 8 s then 2.5 m/s² through `build_motion_inputs` →
      `estimate_pitch_offset`. Without the clamp it fails (min speed
      −0.0165 m/s on seed 1).
    - Stored fits: **none to re-run.** `data/nissan-xterra-2008/dyno.yaml`
      has `road_load: null` and `squat: null` (owed by H07.12), nothing else
      in `data/` or `models/` stores an offset or `A`/`B`/`C`, and no session
      in `~/canary-data/sessions/nissan-xterra-2008` (265 entries) has an
      `imu_frame.json`, so no real window reaches the estimator. Nothing
      stored moves and no update is owed. What moved are the synthetic pins
      in `tests/fixtures/dyno_golden.json` (regenerated; constant-grade
      noisy coasts, where both weightings are unbiased): offset
      0.29910 → 0.29926°, no-squat offset 0.27835 → 0.27466°, road-load
      pair `A` 182.812 → 182.781 N (`B`, `C` < 1e-9 relative), no-squat
      `A` 178.25 → 176.69 N, engine inertia 0.184241 → 0.184276 kg·m²,
      plus the new `heading_distance_m` field, the reworded
      `too_few_samples` reason and `no_distance` in the stored refusal
      kinds.
- [ ] **M01.8 (Missing condition) Road load stores no air density.**
  `RoadLoadConditions` holds road, date, sessions, payload, tire pressures and
  ambient temperature; its docstring names "air". `C` in `A + B·v + C·v²` is
  ½·ρ·Cd·A, and `compute_power` applies the stored `C` whatever the run's
  barometer reads. A fit made at 80 kPa and used at 70 kPa overstates aero
  drag by 62 N at 100 km/h (1.7 kW) and 121 N at 140 km/h (4.7 kW, about 4 %
  of fourth-gear wheel force); at 63 kPa, 81 N and 160 N. This truck's roads
  span exactly that range.
  - [ ] `baro_kpa` becomes a required field of `RoadLoadConditions` (a
    constraint, so a schema field, not a note); `compute_power` scales
    `C` by `ρ_run / ρ_fit`. Existing records need the session's measured baro
    backfilled; refuse a record without it rather than assume one.
  - [ ] Also unenforced: that a run's mass, payload and tire pressures match
    the road-load record it borrows. Name the mismatch in the result.

- [ ] **M01.9 (Out of range) SAE J1349 is applied at 63–82 kPa.**
  `dyno_power._j1349_factor` implements the right formula
  (`1.18·(99/p_dry)·√((T+273)/298) − 0.18`). The standard limits the test
  conditions to roughly 90–105 kPa dry and 15–35 °C (from memory of the
  standard; check the text before quoting it). At 25 °C and 1 kPa vapor the
  factor is 1.262 at 82 kPa, 1.513 at 70 and 1.704 at 63. At that size the
  assumed inputs dominate: mechanical efficiency 0.80 against 0.90 moves it
  ±2 %; vapor pressure 0 against 2 kPa, 3.3 %, and humidity is stated, not
  measured; and the code feeds it `intake_temp_c`, which on this truck reads
  engine-bay air at low flow, where the standard means ambient inlet air
  (+10 °C is +1.9 %). Together about ±4–5 %, more than the A/B effects the
  workbench resolves. 150 kW at the wheels at 70 kPa becomes 227 "corrected".
  It is also applied to wheel power, though the 0.85 is engine friction.
  - Already right: the workbench compares uncorrected curves; the correction
    refuses forced induction and a vapor pressure that leaves no dry air.
  - [ ] Every corrected curve carries a range flag ("extrapolated: N kPa is
    outside J1349's range") and the temperature source it used. Decide
    whether a correction this far out is shown at all, or only the density
    ratio beside it.

- [ ] **M01.10 (Approximation) A speed-scale error enters power squared.**
  `P = F·v`, and both `a` and `v` scale with the speed ratio. The
  accelerometer term would not, except that `dyno_squat.fit_squat` regresses
  `asin((f − a)/g)` on `a/g`, so its slope also absorbs any speed-scale error
  and IMU scale error (1.3 % is equivalent to 0.74°/g). `dyno_inputs`
  `SPEED_SCALE_TOLERANCE = 0.03` is described as a gross-error check and its
  comment notes that using 0x6F7's 0.9203 for 0x2D1 (0.932) "is a 1.3 % error
  and passes it". That is a 2.6 % power error; at the tolerance, 6.1 %.
  - [ ] State the squared consequence beside the tolerance and in the result
    (`speed_scale_residual`, and the power band it implies).
  - [ ] Report the squat slope as *apparent*: it is not purely squat.

- [ ] **M01.11 (Question) Driven-wheel slip under full throttle is not
  bounded.** `decoded:VEHICLE_SPEED_2D1` is described in `data/bindings.yaml`
  as a vehicle speed of unknown wheel origin. It reads 0.932 × GPS while
  0x6F7 reads 0.9203 ×, consistent with different wheels, and both ratios
  were measured at steady cruise. Two to five percent longitudinal slip in
  second and third gear is ordinary on a rear-drive truck; if the channel
  follows the driven wheels, power and the two-gear inertia fit read high by
  about the slip, more in lower gears, so it does not cancel between gears.
  The rpm-against-speed gear check cannot see it.
  - [ ] From data on disk: the per-sample ratio of 0x2D1 to lagged GPS speed
    during pulls against the same ratio at cruise (`compare_channels`, then
    the ratio itself). And against an undriven-wheel ABS speed if one is
    bound.
  - [ ] The same effect is in `rolling_circumference.py` (it measures the
    driven tires including slip): about 0.3–0.5 % at flat cruise and more
    with grade and headwind, the same size as the 0.15–0.43 % drive-to-drive
    scatter in the tread-depth estimate. Gate on grade and tractive effort.

- [ ] **M01.12 Data notes in `data/nissan-xterra-2008/dyno.yaml`.**
  Wheel-side inertia is given as "about 19.4 kg m²" beside "~119 kg"; at
  r = 0.4073 m those are 117 kg and 19.7 kg m² (a 1.5 % mismatch in a stated
  estimate). `wheel_shape_factor: 0.7` looks high for this wheel and tire
  (about 0.57 by estimate, worth 0.9 % of effective mass); H07.6 is meant to
  replace it with a measurement. A tank of fuel is 2.6 % of test mass and
  enters force 1:1; payload is operator-stated only.

## Part C — IMU

- [ ] **M01.13 (Error) The gravity low-pass constant is per sample, so its
  time constant follows the IMU rate.**
  `imu_frame.GRAVITY_LOW_PASS_ALPHA = 0.02`, applied as
  `ewm(alpha=…, adjust=False)` over quiescent samples in `estimate_gravity`,
  `imu_stream` and `imu_live`. The neighboring quiescence window was
  converted to seconds (`imu_session`:
  `round(quiescent_seconds * rate_hz)`); this was not. τ = −Δt / ln(1 − α):
  3.96 s at 12.5 Hz, 0.48 s at 104 Hz, 0.24 s at 208 Hz. The default
  `quiescent_window=20` suggests it was chosen at about 10 Hz.
  - Why it matters (derived, with a standalone simulation; not measured on a
    drive): the quiescence gate passes a steady 0.04 g acceleration and about
    0.15 g of gentle cornering. At τ ≈ 0.5 s the "gravity" estimate swallows
    that within a second, so sustained gentle acceleration reads near zero,
    and the absorbed offset is then held into the next maneuver with the
    wrong sign. Changing the ODR setting changes what the channel means.
  - Not affected: the dyno's `SF_*` channels, which use one constant rotation
    from the mean of the quiescent rows.
  - [ ] Measure first on a stored drive: `imu:ACCEL_LON` against d(speed)/dt
    through a long gentle acceleration, per sample.
  - [ ] Fix the time constant, `α = 1 − exp(−Δt/τ)`, with τ a named constant
    in seconds. Offline and live are pinned bit for bit
    (`test_imu_stream`, `test_live_and_offline_agree_except_where_live_cannot_see_accel`);
    keep that. This changes `imu:` channels the segmenter, tags and the
    lon-accel committee read: treat it as a spec change.

## Part D — Fuel plant, pressure and λ

- [ ] **M01.14 (Approximation now; an error under boost) Fuel-pressure droop
  uses the gauge-pressure ratio, not the pressure across the injector.**
  `delivery_evidence.mint_fuel_pressure_droop` computes
  `loss = 1 − √(med_target / med_low)` from rail gauge pressure. Injector
  flow goes as √ΔP with ΔP = rail gauge + baro − manifold; `fuel_plant`
  has that (`injector_delta_p_kpa`). The gauge ratio is exact only when
  manifold pressure equals baro in both groups. For the NA engine it answers
  "did the pump sag" to within 0.02–0.16 points, which is the question. On
  the planned returnless boosted system the dominant loss is manifold
  pressure rising against a gauge-referenced rail: at 38 kPa of boost with
  the rail flat at 350 kPa, flow is down 5.6 % and this evidence reports
  0.00 %; with the rail drooping to 330, 8.7 % against a reported 2.9 %. The
  ECM's own pressure factor has the same blind spot, so nothing in the chain
  would flag it.
  - [ ] A required `limitations` field on the minted evidence, as
    `fuel_comp_droop.py` already has, stating that manifold pressure is not
    in it.
  - [ ] Refuse (by name) when the profile declares forced induction and no
    manifold-pressure channel is recorded. Do this before the supercharger
    goes on, not after.
  - [ ] `_median_se` assumes independent samples. Its inputs are
    `_window_means` over ±0.25 s around each MAF read; at 4–15 Hz reads the
    windows overlap 60–90 %, so the standard error is about 2× too tight
    (a 3.3 σ droop is nearer 1.5 σ). One value per non-overlapping window or
    per pull, or a block bootstrap.

- [ ] **M01.15 (Question) The AEM gauge's analog zero is unresolved and
  applied as 0.** `data/usb_adc_sensors.yaml` (and `hat_adc_rev2_1.yaml`)
  record that on 2026-10-03 the analog output read a constant +1.8 ± 0.3 psi
  above the gauge face from 0 to 51 psi, that the face read 0 at zero
  pressure, and leave `offset: 0.0` until the sensor is vented. The transfer
  (25·V − 12.5 psi) is AEM's and is applied correctly. If the face is right,
  the rail reads 362.8 kPa for a true 350: modeled flow +1.8 %, predicted λ
  −1.8 % on every sample that uses measured rail, against a 2.0 % acceptance
  bound.
  - [ ] Operator: with the rail at atmosphere the true gauge pressure is 0 by
    definition. If the "0 on the face, 0.574 V on the output" reading was
    taken with the rail fully bled, it already is the vented measurement and
    the offset is −1.85 psi. If residual pressure was possible, do the vented
    read the comment plans.
  - [ ] Record whether the sender is sealed-gauge or vented. A sealed sender
    would read 21 kPa low at 80 kPa ambient; the +1.85 psi zero argues
    against it, but the check is not written down.

- [ ] **M01.16 (Approximation) The VE envelope is a sample maximum.**
  `fuel_plant_eval.identify_ve` takes `values.max()` per rpm band. Without
  noise that is sound (charge fraction ≤ VE). With noise the maximum of n
  samples is biased up by about σ·√(2 ln n), and the bands have 805, 286, 24
  and 5 samples, so the bias differs by band and puts a small false shape
  into the curve. The effect on λ is small (VE +4 % is about +0.4 % flow).
  The practical effect is coverage: 150 g/s at 5,500 rpm, 80 kPa, 25 °C is a
  charge fraction of 0.885; the top band's envelope is 0.873 from five
  samples, so the plant answers `manifold_above_ambient` and predicts
  nothing. A slightly denser day refuses every full-throttle sample.
  - [ ] A high quantile with a minimum sample count per band, and the count
    in the refusal message.

- [ ] **M01.17 (Question) The voltage route's stoichiometric knot is 14.774,
  not 14.700.** `afr_calibration.yaml`: the table value at 880 counts
  (2.200 V) is 7387, ÷ 500 = 14.774, which is λ 1.0050 when divided by 14.7.
  The same file records the voltage route reading +0.52 % against PID 0x24
  near stoich, attributed to unmodeled learned offsets. 14.774 / 14.7 − 1 is
  +0.50 %: it reproduces that bias to 0.02 points. Inside the noise floor,
  but systematic and sitting where closed-loop cells are judged.
  - [ ] One check: does the ECM form PID 0x24 from this table with a
    different constant, or does its setpoint sit at a neighboring knot?
    Related to M01.3(a): the divisor belongs to the ECM.

- [ ] **M01.18 (Question) Does the Fuel Comp fit keep purge-active samples?**
  `fuel_comp_samples` reconstructs the purge term for the load byte
  (`purge_moves_cell`). No gate was found that removes or corrects
  purge-active samples in the trim-derived error; the fit internals were not
  read end to end, so one may exist under another name. The ECM cuts the
  pulse 5–10 % while purging and any error in its estimate lands in STFT. If
  that estimate is off by a fifth, 1.5–3 % of trim in light-cruise cells is
  purge, and a fit would write it into a table that applies with purge off.
  - [ ] Find out. If purge-active samples are kept, make purge state a stated
    stratifier of the fit or gate them out.

- [ ] **M01.19 Documented approximations; state their limits where they are
  used.**
  - One λ transport delay, 0.10 s (`lambda_delivery.yaml`), fitted on
    enrichment. Physically the delay is about 0.12 s at full throttle, 0.16 s
    at cruise and 0.35–0.45 s at idle. Fine for steady 2 s windows; it would
    bias a low-rpm tip-in comparison. Refuse or flag transient windows below
    a stated rpm.
  - `manifold_plant.yaml` says manifold volume is identifiable from a MAP
    step through τ = 120 V / (VE·Vd·rpm). That τ is the isothermal
    mass-filling constant; `engine_dynamics.integrate` implements the
    adiabatic law (correctly), where pressure responds at τ/γ. Measured in
    the model: τ 0.0694 s, 63 % rise at 0.0499 s. Read with the yaml's
    formula, a step would give a volume 1.4× too small. No MAP sensor exists
    yet; correct the text before the boosted build adds one.
  - `hold_on_grid` zero-order-holds MAF sampled at 4–15 Hz, a lag of
    33–125 ms against a manifold τ of 27 ms at 5,500 rpm. Cylinder-flow
    transients from this model are dominated by sampling.
  - The fuel plant uses IAT as charge temperature and gives idle manifold
    pressure of 22–26 kPa where 28–35 is typical at this altitude: 0.6–1.6 %
    of flow. Idle cells carry about ±1.5 % model uncertainty from this term.
  - `torque_plant`'s 0–15 kPa exhaust back-pressure bracket is probably low
    at full throttle (about 2 % of torque).

## Part E — Vehicle facts in shared code

- [ ] **M01.20 (Vehicle fact) `VehicleParameters` defaults describe a
  truck.** `src/config/vehicle.py`: `mass_kg = 1900.0`,
  `displacement_l = 4.0`, `volumetric_efficiency = 0.85`,
  `drivetrain_loss_fraction = 0.15`, `thermal_efficiency = 0.30`. A vehicle
  whose profile omits them computes power, VE and speed-density fuel as a
  4.0 L, 1,900 kg truck and says nothing.
  - [ ] `None` defaults for mass and displacement and a named refusal in the
    channels that need them ("displacement not set for this vehicle"), as the
    dyno already does for mass without `mass_evidence`. Sweep the readers.
  - [ ] `profiles.yaml` has `displacement_l: 4.0` for both Xterras. The
    VQ40DE is 3,954 cc (95.5 × 92.0 mm × 6), which `fuel_plant.yaml` and the
    vehicle skill use. VE channels read 1.15 % low. Set 3.954.

## What is not modeled

Say so wherever these outputs are quoted:

- No wall-wetting or fuel-film dynamics and no transient fuel compensation.
  The paired simulation solves steady fixed points; "the sim predicts λ after
  a ROM edit" is a steady-state statement.
- No manifold pressure sensor: every manifold pressure is inferred from MAF,
  rpm and an identified VE.
- No exhaust back-pressure dynamics and no cam-phasing term in VE.
- `sim_design`'s claim that λ deltas are independent of battery voltage holds
  only while the plant subtracts the same injector dead time the ECM adds.
  After an injector change it will not: 0.05 ms on a 2.1 ms idle pulse is
  3 %.

## Checked and correct

- **Manifold** (`engine_dynamics`): mass and adiabatic energy balance,
  `p = mRT/V`; steady 37.106 kPa at 25 g/s, 2,500 rpm, VE 0.7, 4 L matches
  the hand value; RK4 order 4.03–4.08; `charge_fraction`; R = 287.05.
- **Fuel plant**: events per second and air per event; injector ΔP and
  √ flow factor; `predict` (λ independent of `fuel_stoich_afr`).
- **ECU mirror** (`injector_pulse`): additive trims; dead time 0.47 ms at
  14.0 V with 0.090 ms/V; the linearized pressure factor 239/256 = 0.934
  against √(350/400) = 0.9354.
- **λ chain**: φ ↔ λ handling for PID 0x44; residual sign (positive = lean);
  the barometric word's scale and offset; the gain about stoich; the refusal
  without fresh baro.
- **Fuel Comp**: the required-factor arithmetic, consistent with additive
  trims; `fuel_comp_droop`'s √(P/P_ref) with its stated limitation.
- **Torque plant**: indicated torque, Vd/4π, pumping work, the friction form.
- **Rust twin core against Python**: `lookup_2d`, `lookup_shift12_q16`,
  `interpolate_q16`, `x_from_bfs`, `LoadCurves.reconstruct`: zero differences
  in about 160,000 fuzz cases (on the existing build, not a rebuild).
- **Dyno**: `F = m·f + (m_eff − m)·a + F_rl(v)` and the coast-down form;
  wind cancellation in the two-direction pair; the inertia convention and the
  two-gear fit; rolling radius = circumference / 2π for both kinematics and
  force; matched smoothing kernels; per-bin mean of F·v, not a ratio of
  averages.
- **Derived**: ideal-gas fill and speed-density; air mass per cycle;
  boost and vacuum channels refuse without measured baro (no sea-level
  default anywhere reviewed); the trapezoid integrator and its gap cap; unit
  constants.
- **Tires**: Δdepth = ΔC / 2π and its error propagation; 1 mm of tread =
  0.2455 % of 2.559 m.
- **IMU**: the rotation (up = mean quiescent specific force, principal-axis
  yaw, sign from the speed derivative); LSM6DSOX rate and scale codes.
- **`sim_design.build_grid_design`** refuses unstated baro, IAT, battery,
  rail pressure and load basis.

The skills' arithmetic was checked in [L01](L01-skill-updates.md); the three
physics statements wrong there are L01 items (trim composition, the injector
rescale, the WOT VE range).

## Not reviewed

Spark and knock physics (not at all); `ecu_state_models.py` (ignition chain);
`plant_validation.py`, `twin_runner.py`, `rom_levers.py`, `tools/rom_sim.py`,
`sim_cells` / `sim_montecarlo` / `sim_report` internals; `air_charge_twin.py`;
`fuel_comp_fit.py` internals; `dyno_inputs.py` past its speed check,
`dyno_workbench`, `dyno_pulls`; the committee evaluator; segmenter
thresholds; `plausibility/limits.py` logic; dashboard unit conversions.
Firmware constants were taken as stated in tracked documents; no disassembly
was consulted.

## Order

1. M01.1 (a three-line fix with a known answer).
2. M01.3(a), then M01.2, M01.3(b) and M01.4 as one change to the trip path
   with one sidecar version bump, then M01.5 to check the result against a
   tank.
3. M01.7 and M01.8 before any more road-load or inertia fitting.
4. M01.14's refusal and M01.19's manifold text before the supercharger build.
5. M01.13 after its measurement. The rest as the code is next touched.
