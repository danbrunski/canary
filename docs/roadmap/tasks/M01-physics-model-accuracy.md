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

- [~] **M01.1 (Error) 0–60 mph and quarter-mile times include the time spent
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
  - [~] The thresholds are on raw `obd:SPEED` (M01.2), so "60 mph" is reached
    at about 65 true and the quarter mile ends at about 437 m. Use corrected
    speed.
    *Remaining:* needs M01.2's per-profile speed correction function, which
    does not exist yet; `summarize_trip` still feeds raw `obd:SPEED`. Wire
    the corrected series into the `_accel_metrics` call when M01.2 lands.
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

- [ ] **M01.2 (Error) Trip distance integrates the ECM's speed, which reads
  0.9203 × true on this truck.** `compute_derived_channels`
  (`derived:TRIP_DISTANCE_KM`), `summarize_trip` and the live accumulator in
  `features/live.py` all integrate raw `obd:SPEED`. The bias is recorded in
  `data/nissan-xterra-2008/dyno.yaml` (`ratio_to_gps: 0.9203`), in
  `committees.yaml`, in the `gps_tracker` docstring ("wrong for distance") and
  in `docs/can-pids-dids.md`. The trip path does not apply it. Distance and
  average speed are 8.0 % low, so L/100 km reads 8.7 % high from this term
  alone. One hour at an indicated 100 km/h is 108.7 km.
  - [ ] A per-profile speed correction for the Mode 01 speed channel, read by
    one function that the batch and live paths both call (they are pinned
    equal by
    `test_live_trip_integrals_match_batch_compute_derived_channels`). A
    vehicle with no measured ratio integrates uncorrected and **says so**:
    the trip summary carries a `speed_basis` field.
  - [ ] The ratio drifts with tire wear (full tread is about 2 % of radius)
    and was measured in September on the pre-HAT receiver. Decide whether it
    is a profile constant or follows the rolling-circumference estimate
    (`/control/tires`).
  - [ ] Bump the trip-summary sidecar's schema version so old sidecars
    recompute, and note the date in the docs: mpg trends do not carry across
    it (the same warning as the 2026-07-26 `dt` fix).
  - [ ] `_gear_estimate` matches rpm ÷ speed against the ratio table using the
    same biased speed. It still picks correctly on this gearbox (8.6 % bias
    against 30 % spacing between 5th and 6th); apply the correction there too
    so a close-ratio box is not marginal.

- [ ] **M01.3 (Error, two parts) One constant is doing two jobs: the fuel's
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
    - [ ] Carry the AFR scale on the thing that produced the number (the AFR
      calibration, or the measured-source declaration for a third-party
      gauge) and remove `afr_stoich` from `TuningContext`, so an AFR-quantity
      source cannot be converted with a fuel property. Sweep the callers
      (`delivery_evidence`, `sim_design`, `plant_validation`).
    - [ ] Test: set the profile's fuel to something other than gasoline and
      assert the voltage-route λ does not move.
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
    - [ ] Add an `e10` fuel type (stoich 14.08, 0.749 kg/L, 2.28 kg CO2/L,
      41.7 MJ/kg), or put stoich and density in the profile, and select it for
      this truck. **Do (a) first**, or selecting it triggers the latent error.
    - [ ] The trip summary states the fuel basis it used, next to
      `speed_basis` (M01.2).
    - [ ] Octane does not change stoich: 91 is still an ethanol blend, so
      these constants stay when the grade changes. What the grade changes is
      knock margin, so record the change as a vehicle event when it happens
      and do not compare spark or knock data across it without saying so.
      Z1's published power figures are on 93 octane
      (`docs/roadmap/supercharger-build.md`); the build will run on 91.
    - [ ] The `e85` row's CO2 (1.56 kg/L against about 1.64) and LHV are also
      a little low; check them when the table is touched.
  - With M01.2: reported mpg ≈ 0.955 × true. The two errors have opposite
    signs, so fixing one alone makes the number worse.

- [ ] **M01.4 (Approximation, undocumented) Fuel is counted during decel fuel
  cut.** `_instant_fuel_g_s` computes MAF ÷ (stoich · λ) for every sample with
  λ > 0. In fuel cut the injectors are off, MAF still reads a few g/s and the
  wideband sits at its lean limit, so each second counts a little fuel that
  was not injected: 5 g/s at λ 2.0 is 0.82 L/h. `signals/afr.fuel_cut_rows`
  (PID 0x44 = 0) exists and this path does not use it.
  - [ ] Measure it first on a stored mountain drive: the share of
    `derived:FUEL_USED_L` accrued in rows `fuel_cut_rows` marks. The estimate
    is about 1 % on an ordinary drive and 3 % on a long descent.
  - [ ] Zero the fuel rate in fuel-cut rows. Keep the existing rule that a
    missing λ during open-loop enrichment yields NaN, not stoich; that part is
    right and its comment says why.

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

- [ ] **M01.7 (Error) The two-heading pitch offset is averaged over time, not
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
  - [ ] Distance-weight the per-heading mean. Commit the simulation as the
    regression test (a known-answer case, per `AGENTS.md`). Correct the
    docstring. Re-run the stored offset and road-load fits and record how far
    they move.

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
