# H07 — a driving dyno

Part of [H00](H00-hat-integration-README.md). Goal: wheel power and torque
against rpm from ordinary on-road pulls, repeatable enough to judge a ROM edit
or the supercharger (`supercharger-build.md` NA5: "the result the build is
for"). Implements engine-analysis-and-mapping 4.8 (grade-corrected power runs).

## The physics, and why the IMU makes it tractable

At the wheels, during a pull in a fixed gear:

```
F_wheel = m_eff·a + m·g·sin(θ) + F_rl(v)        P_wheel = F_wheel · v
```

`a` is acceleration along the road, `θ` the grade, `F_rl(v) = A + B·v + C·v²`
the road load (rolling resistance, driveline drag, aero), and `m_eff` the
mass plus the rotating inertia reflected through the gear. A longitudinal
accelerometer measures **specific force** `f = a + g·sin(θ)`, so

```
F_wheel = m·f + (m_eff − m)·a + F_rl(v)
```

The IMU carries the grade term by itself, with no barometer and no GPS
altitude. What it adds instead is **body pitch**: the truck squats under
acceleration, tilting the sensor by δ and adding `g·sin(δ)` to `f`. Half a
degree is 0.009 g, about 3 % of a 0.3 g pull, and it does not cancel between
directions. H07.4 measures it.

## Where it stands

Nothing computes road-load power. Nearby pieces: `derived:ESTIMATED_POWER_HP`
is a fuel-energy guess (the Xterra has no torque PIDs); `tools/pull_report.py`
detects pulls from dRPM/dt; `derived:GEAR_ESTIMATE` matches rpm/speed to a
table; `WotInventory` and the per-vehicle `wot_criterion` say whether a pull
was at wide-open throttle. Measured facts on file: final drive 4.08, LT285/75R16
with a back-solved loaded circumference of 2.609 m, gearbox ratios 1.000, 1.309
and 1.740 from cruise clusters (three of six gears), and every ECU speed ~8 %
under GPS. Defects a dyno would inherit:

- `VehicleParameters.gear_ratios` is documented as gearbox × final drive but
  compared as rpm per km/h; `canary.example.yaml` lists five ratios in the
  wrong units for a six-speed.
- The W03 Logging page tags `pull_start`/`pull_end`; `tuning_digest.py` only
  counts `tuning_pull_start`/`wot_pull`/`tuning_pull_end`, so a pull tagged
  from the page never counts as tagged.
- No mass measurement, no road-load coefficients, no rotating inertia, no
  correction factor.

## Rules

- Vehicle facts are profile data with their evidence and date: mass (with
  what was in the truck), tyre, gears, road load, inertia.
- Every result carries required **conditions**: gear, direction, tune
  revision, NA/boosted, baro, intake and ambient temperature, road
  identifier. A result without them cannot be constructed (the
  `noise_floor.yaml` precedent).
- **Both directions.** A reported pull is a pair on the same road, which
  cancels grade error and wind; a single direction is reported as such.
- Absolute speed comes from GPS. ECU and broadcast speeds are used for
  resolution (100 Hz) only after scaling by the measured ratio.
- Correction factors are named. SAE J1349 is defined for NA engines; on a
  boosted session it is refused, not applied.
- An empty result says which kind: no pull found, pull refused (which gate),
  or inputs not captured.
- No analysis on the Pi while it collects: this runs on the laptop, or in the
  single-flight child when idle.

---

## Desk

- [x] **H07.1 Driveline facts that cannot be misread.** **Do:** replace
  `gear_ratios` with typed fields: `final_drive`, `gearbox_ratios` (unitless,
  one per gear), `tyre_loaded_circumference_m` with its evidence; compute
  expected rpm per km/h from them, so the unit mismatch cannot recur. Fix
  `derived:GEAR_ESTIMATE` and its tests to match; fix `canary.example.yaml`.
  Put the measured Xterra values in its profile with their provenance (the
  0x6F7 notes in `data/bindings.yaml`). **Accept:** gear estimates on the
  ground-truth slice match the three measured clusters; a config with the old
  field is refused with a message naming the new ones.
  **Evidence (2026-09-23):** `VehicleParameters` gains `final_drive`,
  `gearbox_ratios` (unitless, all gears or none), `tyre_loaded_circumference_m`
  and a required `driveline_evidence`; `rpm_per_kph()` computes the table, and
  `derived:GEAR_ESTIMATE` uses it. A non-empty `gear_ratios` is refused naming
  the new fields; a stored empty one (the old default) still loads. The
  Xterra's registry entry (`profiles.yaml`) carries final drive 4.08 and
  2.609 m with their evidence; its gearbox table stays empty because nothing
  records which gears the three measured clusters were, so the gear estimate
  stays absent until H07.11. `canary.example.yaml`'s five-ratio,
  wrong-unit example is gone. Tests in `tests/test_derived.py` (rpm per km/h:
  26.06 in direct drive, ~2,606 rpm at 100 km/h).
- [x] **H07.2 One pull-tag vocabulary.** **Do:** decide the canonical names
  (the digest's `tuning_pull_start`/`tuning_pull_end`, or the page's), make
  both producers and all consumers use a shared constant, and accept the other
  spelling on read for old sessions. **Accept:** a test that a pull tagged
  from the Logging page counts as `tagged` in `WotInventory`.
  **Evidence (2026-09-23):** `src/analysis/pull_tags.py` names the canonical
  `tuning_pull_start`/`tuning_pull_end` (what the guided sessions and capture
  docs already used) and the readers accept every older spelling
  (`wot_pull`, `pull_start`, `pull_end`); `tuning_digest.py` uses it in all
  three places. The Logging page now writes the canonical labels, and
  `tests/test_pull_tags.py` reads the page's tag list and fails if its pull
  labels drift from the backend's. The digest's tagged-pull test runs over
  both spellings (`tests/test_tuning_digest.py`). `pnpm typecheck` clean; the
  tuning panel tests pass (38).
- [x] **H07.3 Speed and acceleration inputs.** **Do:** a function returning,
  for a pull window, GPS speed (absolute), `0x2D1` broadcast speed scaled by
  the profile's measured ECU/GPS ratio (100 Hz), `a` from a smoothed
  derivative of the scaled broadcast speed, and `imu:SF_LON` (H02.4), all on
  one time base from the acquisition clock. **Accept:** synthetic tests with
  known speed profiles; refusal named when GPS is below the H04.3 quality
  gates.
  **Evidence (2026-09-26):** `src/analysis/dyno_inputs.py`:
  `read_motion_inputs(session, start, end, sources)` (one bounded read;
  `decoded:` sources decoded from their raw frames whatever their binding
  confidence, since the loader decodes only `confirmed` ones on read) and the
  pure `build_motion_inputs(channels, ...)`, returning `DynoInputs` or a
  `DynoRefusal`.
  - **Speed** is the first source in `dyno.yaml` `motion_sources.speed` that
    has data and puts 5 samples in the smoothing window, divided by that
    channel's `ratio_to_gps`; GPS is the reference and last resort. Channel
    names and ratios are profile data (`MotionSources`, which refuses a listed
    `gps:speed` or a duplicate). **Measured for this item:** 0x2D1 reads
    **0.932** of GPS (per-session medians 0.9314 / 0.9320 / 0.9348 / 0.9324,
    committee method), not the 0x6F7/PID 0.9203: 0x6F7 on the same epochs
    reproduced the committee's 0.921–0.924. A test holds the dyno ratios equal
    to `committees.yaml` for every channel both state.
  - **One time base:** every channel at its own acquisition stamps, averaged
    into each 50 ms grid step when faster (a symmetric boxcar), interpolated
    when slower, never shifted. Acceleration is the smoothed central difference
    of speed, and **the same 0.5 s quadratic kernel smooths `imu:SF_LON`, speed
    and rpm** (`matched_kernels`). The fit's own slope, the obvious choice, is
    a differently weighted average: on a 0.1 s launch it put 0.25 m/s² into
    `f − a`; matched, 0.036.
  - GPS is received late, so the scale check fits the lag (−1…6 s) and pairs
    GPS epochs with the speed at the same instant. On
    `2026-09-20T175050` (21 windows of 20 s) GPS / scaled 0x2D1 read
    0.9916–1.0197, lag 0.3–2.6 s. Two steady windows hit the 6 s search
    bound, where the lag cannot be identified and does not matter.
  - **Specific force** comes through `imu_frame.json` in m/s², and its
    `reference_id` is copied onto `MotionSamples.imu_reference_id`.
    `PitchOffset` now requires the `reference_id` it was measured in;
    `estimate_pitch_offset` and `fit_road_load` refuse legs from mixed frames,
    and `fit_engine_inertia` (whose argument is now a `PitchOffset`, not a
    float) and `compute_power` refuse samples from another frame
    (`frame_mismatch`). The fits record the id. The public
    `correct_specific_force(samples, *, squat, pitch_offset: PitchOffset)`
    returns the corrected force or refuses `frame_mismatch`; the bare-float
    form is private (`_correct_specific_force`).
  - **Degrades by name:** without a sidecar (or with a refused one, or no raw
    accelerometer rows) `DynoInputs` is still built and records
    `ImuUnavailable`; `.motion()` refuses `imu_unavailable` with that cause.
  - **Refusals:** `gps_quality` (any epoch in the window failing
    `SPEED_COMMITTEE.gates`, sats ≥ 7 and HDOP ≤ 2, or a gate channel not
    recorded), `inputs_missing` (no GPS speed), `inputs_gap` (a hole in any
    selected channel; the GPS limit is an absolute 2.5 s because the pre-HAT
    receiver flushes each epoch twice), `speed_too_coarse` (names each source
    and its rate), `speed_scale_mismatch` (beyond 3 %, naming the ratio the
    window implies), `invalid_input`.
  - **Limits:** the scale check is a gross-error check. 0x6F7's ratio used for
    0x2D1 (1.3 % off) passes it (tested). A real session with a resolved
    IMU frame has not been run, because none is on the laptop. The real smoke
    above ran without IMU (`imu_frame_missing`, 21/23), and 2/23 windows were
    refused `inputs_gap` for a 0.19 s 0x2D1 hole. A 20 s window takes ~12 s
    to read from a whole stored session.
  - Tests: `tests/test_dyno_inputs.py` (13). Mutation-checked, all 8 killed:
    unsmoothed `f`, unsmoothed rpm, ratio not applied, GPS gate skipped,
    speed gap unchecked, frame id dropped, scale check off, fit-slope
    derivative. Also
    `tests/test_dyno.py::test_pitch_offset_is_bound_to_the_frame_it_was_measured_in`.
- [~] **H07.4 Body pitch under acceleration.** **Do:** on flat ground,
  `f − a` should equal `g·sin(θ)` with θ ≈ 0; fit the residual against `a` to
  get a squat coefficient (degrees of pitch per g). Store it as profile data
  with conditions (load in the truck, tyre pressure). **Accept:** synthetic
  recovery of a known coefficient; the fit refuses when the stretch is not
  flat by GPS altitude over the window.
  **Partial (2026-09-26): desk done; truck: real coefficients owed (H07.12),
  and with this receiver, most real windows cannot be judged flat (below).**
  `src/analysis/dyno.py` `fit_squat` fits `asin((f − a)/g)` against `a/g`.

  **Altitude noise is measured, not assumed.** This was revised twice on
  2026-09-26 after verification. The first flatness se treated GPS altitude
  samples as independent. Real GNSS altitude wanders: parked, on this
  truck, the 10 s-lag variogram grows as ~h^1.5 and varies ~20x within one
  afternoon. The white-noise se therefore refused 200/200 flat roads under
  AR(1) noise as `not_flat`.
  - `fit_squat` now requires `altitude_noise`: one or more `AltitudeNoise`
    models, measured by `measure_altitude_noise` on a parked stretch of the
    same session (≥ 180 s, speed ≤ 0.5 m/s). Its refusals are
    `not_stationary`, `too_few_samples`, `inputs_missing` and `no_spread`.
  - The model is white noise plus a powered-exponential wander,
    `γ(h) = white + σ²(1 − exp(−(h/τ)^α))` with 0 < α < 2 (a valid
    variogram). Every grade and leak se is the exact `−wᵀΓw` for slope
    weights `w`, which needs only the variogram because the weights sum to
    zero. On a uniform grid it uses the Toeplitz form, which matches the
    dense sum to 6e-16.
  - Given references before and after the runs, every se (the window's,
    each segment's, the leak's) is computed under each and the larger kept.
    This is the third revision: the second picked one reference by segment se
    alone.
  - On the real long parked run, the model's 200 m segment se matched the
    estimator's scatter along the reference: 0.298 / 0.291 %,
    0.200 / 0.178 %, 0.389 / 0.379 %, 0.168 / 0.179 %, 0.200 / 0.216 %.
    It errs high elsewhere (0.54 / 0.45 %).
  - Synthetic references: the model se has median model/true 0.96–1.03, and
    10th–90th percentile 0.84–1.10 at 120 s, 0.93–1.09 at 300 s.

  **Refusals, in this order.** The order was fixed in the third revision: a
  4 % grade read as 4.05 ± 0.30 % under AR(1) noise, and a ±5 % crest, used
  to come back `grade_unresolvable` ("the road may be flat").
  1. `not_flat` for any grade shown to exceed the gate, however noisy:
     - the window's mean beyond 0.5 % + 2 se;
     - or any 200 m segment (sliding by 50 m), resolved or not, beyond
       0.5 % + z·se, with z = Φ⁻¹(1 − 0.05/2m) family-wise over m
       independent segments (2.3 / 2.8 / 3.1 for 460 m / 90 s / 240 s
       windows).

     The first revision added the segment check for crests: a crest with ~0
     mean grade used to pass and read 6.9 deg/g against a true 1.2.
  2. `grade_unresolvable` only then: the window's or any segment's 2 se is
     above 0.5 %, or the altitude is too sparse to form a 200 m segment
     (fewer than 10 samples; this used to skip the crest check silently).
     Nothing was shown to exceed the gate, and nothing shows the road is
     inside it. The reason names what the parked noise does over the time a
     200 m segment takes, not its sill or τ, which are unidentifiable when τ
     reaches the fit grid's ceiling.
  3. `grade_correlated` when the local GPS grade, regressed on `a/g`, leaks
     more than 3 se into the slope. The real-noise false rates are in the
     table below.
  - `inputs_missing`, `too_few_samples` (< 50), `accel_span_too_small`
    (< 0.1 g).

  **Measured, synthetic (10 Hz):**

  | Case | Result |
  |---|---|
  | Flat, white 1 m | 199/200 scored (90 s), 98/100 (240 s); the rest `grade_correlated` |
  | Flat, AR(1) 0.5 m / τ 10 s | 100/100 (90 s) and 50/50 (240 s) `grade_unresolvable`, never `not_flat` |
  | Flat, AR(1) 1 m / τ 10 s | same |
  | Flat, AR(1) 1 m / τ 2 s | same |
  | 4 % grade, AR(1) 1 m / τ 10 s | `not_flat` 10/10 |
  | ±5 % crest, AR(1) 1 m / τ 10 s | `not_flat` 10/10 |
  | Constant 0.4 % (inside the gate), 240 s | 0/40 `not_flat` with the family-wise z; 15/40 at 2 se per segment |
  | Crest, 20/20 | `not_flat` |
  | Rolling ±1.5 %, 20/20 | `not_flat` |
  | In phase ±0.4 % | `grade_correlated` 50/50 (90 s), 30/30 (240 s) |
  | In phase ±0.3 % | `grade_correlated` 41/50 |
  | In phase ±0.2 % | `grade_correlated` 23/50 |
  | In phase ±0.1 % | passes 47/50 at +0.2 deg/g (the stated blind spot, ~3x the leak se of 0.12 deg/g at 90 s) |

  **Measured on real parked GNSS altitude** (all parked runs ≥ 270 s in the
  five 2026-09-20/21 sessions, injected into flat synthetic windows):

  | Reference | 90 s windows | 240 s windows |
  |---|---|---|
  | One, at the start of the parked run | 96/96 `grade_unresolvable` | 34/34 `grade_unresolvable` |
  | The preceding 180 s | 57/96 scored, 34 unresolvable, 4 `not_flat`, 1 `grade_correlated` | 15/34 scored, 15 unresolvable, 3 `not_flat`, 1 `grade_correlated` |
  | Bracketed before and after (the API's recommended use) | 42/90 scored, 47 unresolvable, **1 `not_flat`** | 13/33 scored, 19 unresolvable, **1 `not_flat`** |

  An independent bracketed sweep of `2026-09-20T152758` (the verifier's,
  with its own window alignment) found **2 `grade_correlated` and 1
  `not_flat` in 82 windows**. The false refusals come from noise worse than
  both references. This parked altitude is nonstationary and its z-tails
  are heavy, so a reference model cannot rule them out: expect a few per
  cent of flat windows to be refused with a wrong cause.

  The receiver was the pre-HAT rig's USB GPS (`docs/CANary-HAT-Spec.md` §10:
  "USB dongle, 1 Hz class"), since the rev 2.0 HAT's M10 was fitted
  2026-09-23. Whether the M10 does better is untested.

  **Protocol finding for H07.12:** about half of real flat windows could not
  be judged at 0.5 % per 200 m with this receiver's altitude, and none with
  a reference from the start of a parked run. Across 11 references sampled
  along the parked runs, 2 se of a 200 m segment was 0.28–0.40 % in 6 and
  0.60–2.3 % in 5: the four taken at the start of a parked run, plus one 30 minutes in (0.78 %).
  Unresolvable windows occur throughout a run, not only early. Options are
  in the report.

  Tests:
  - `test_flat_road_under_wandering_altitude_is_unresolvable_not_not_flat`
    (three AR(1) settings)
  - `test_real_parked_gnss_altitude_is_not_white_and_the_model_says_so` and
    `test_squat_on_real_parked_altitude_noise_never_reads_as_not_flat`, on
    the committed 720 s slice `tests/fixtures/gnss_altitude/parked-2026-09-20T152758.json`
    (17.8 KB, relative time and altitude only, pre-HAT USB receiver, chosen
    by rule as the first slice after 15 minutes parked; 3 of its 4 windows
    score)
  - `test_each_standard_error_takes_the_noisier_reference`: two references
    that rank oppositely for the window and the leak se
  - `test_a_steep_grade_is_not_flat_however_noisy_the_altitude` and
    `test_a_steep_crest_is_not_flat_however_noisy_the_altitude` (AR(1)
    1 m / τ 10 s)
  - Gate-branch pins, each checked by mutation (all six mutants killed:
    z → 2.0; window mean without + 2 se; window-unresolvable branch removed;
    only resolved segments tested; first reference only; sparse check
    removed):
    - `test_segment_threshold_is_family_wise_not_two_se_per_segment`
    - `test_window_mean_gate_allows_for_its_own_standard_error`
    - `test_an_unresolvable_window_says_so_about_the_window`
    - `test_altitude_too_sparse_for_segments_is_unresolvable`
  - `test_altitude_noise_reference_refusals`
  - the crest, rolling and in-phase tests

  The leak and its se, the largest segment grade and the noise model used
  are recorded on every fit. Stored as `SquatCoefficient` in the vehicle's
  `dyno.yaml`. Its required `SquatConditions` are payload, front and rear
  tyre pressure, road, date and sessions, and a record is only built through
  `SquatFit.to_record(conditions)`.

  Known answer 1.2 deg/g with a 0.3 deg mounting offset:
  - Noise-free: 1.2000 / 0.3000.
  - With noise (0.03 m/s² f, 0.02 m/s² a, 10 Hz, 240 s): 1.178 / 1.204 /
    1.183 (−1.8 / +0.4 / −1.4 %, se 0.024) on seeds 1–3; test tolerance 5 %.
  - A constant 0.3 % grade goes into the offset, not the slope.
  - The reported slope se and leak se track the seed scatter (tested
    0.7–1.4 and 0.7–1.5).

  Tests: `tests/test_dyno.py::test_squat_*`.
- [~] **H07.5 Road-load model from coast-downs.** **Do:** fit
  `F_rl(v) = A + B·v + C·v²` from clutch-in, neutral coast-downs (SAE J2263
  style), using `F_rl = −m·f − (m_eff,neutral − m)·a`, both directions, and
  store A, B, C with conditions and fit residuals. **Accept:** synthetic
  coast-down data with known coefficients recovered within 5 %.
  **Partial (2026-09-26): desk done; truck: real coefficients owed (H07.12).**
  `fit_road_load` fits each heading separately and averages the pair, so
  wind's ±2·C·w in B cancels and C·w² is left in A. Stored as
  `RoadLoadCoefficients` with per-heading fits, standard errors, residual sd,
  test mass, `m_eff,neutral`, the `inertia` flag and `squat_corrected`, plus
  required `RoadLoadConditions` (road, date, sessions, payload, tyre
  pressures, ambient temperature). `DynoProfile` refuses a single-direction
  road load. **Found by the known-answer test:** the IMU's residual pitch
  offset μ (mount, bias, the grade the gravity vector was estimated on) is the
  same force in both directions and indistinguishable from A; 0.3 deg is
  118.7 N at 2,311 kg. `estimate_pitch_offset` measures it from the pair's kinematics
  (grade flips sign, μ does not), and the fit removes it and records
  `imu_pitch_offset_deg`. The test drive: 0.5 % grade, 2 m/s wind, 1.2 deg/g
  squat and a 0.3 deg offset. Known answers A 180 N, B 5.0 N/(m/s), C 0.55
  N/(m/s)²; noise-free A 182.20 (= A + C·w²), B 5.0000, C 0.55000. With
  3 coasts per heading and noise 0.005 / 0.003 m/s² at 10 Hz, seeds 11–15
  give A +0.8..+1.7 %, B −2.2..+1.9 %, C −0.5..+0.6 %, and μ 0.2998–0.3007.
  **B is the weak coefficient:** at 0.03 m/s² noise one pair scatters B by
  ~19 % (A 4 %, C 5 %), and the reported se covers the truth (10 seeds within
  3.5 se), so H07.12 needs several coasts per heading. The standard errors
  are not inflated: over 20 seeds the se/scatter ratio is 0.84 / 0.83 / 0.85
  for A / B / C (tested 0.6–1.4), and a doubled se fails that test.
  Leaving C·w² in A costs 1.2 % of A at 2 m/s of wind and 2.75 % at 3 m/s.
  Refusals:
  `single_direction`, `too_many_directions`, `speed_span_too_small`
  (< 30 km/h), `too_few_samples`, `inputs_missing`, `non_physical` (A or
  C ≤ 0). Tests: `tests/test_dyno.py::test_road_load_*`,
  `::test_pitch_offset_*`.
- [~] **H07.6 Rotating inertia.** **Do:** estimate `m_eff` per gear from
  wheel, driveline and engine inertia; first from nominal values stated as
  estimates, then from pulls in two gears over the same rpm band, where
  engine torque is the same and the difference in wheel force isolates the
  inertia term. Until measured, results carry an `inertia: estimated` flag.
  **Accept:** a synthetic two-gear test recovers a known engine inertia.
  **Partial (2026-09-26): desk done; truck: real coefficients owed (H07.12
  pulls in two gears).** `data/nissan-xterra-2008/dyno.yaml` `inertia` holds
  the stated estimates, labelled as such: 4 road wheels, shape factor 0.7 of
  m·r² on the profile's 40.3 kg wheel and 2.609 m tyre, 1.0 kg·m² output
  driveline, 0.25 kg·m² engine side. That is ~119 kg wheel-side plus ~62 kg
  engine-side in 3rd (η × 0.25 through the ratio, with η = 0.85). `Driveline.from_vehicle` builds `m_eff` per gear and
  neutral from those and `VehicleParameters`, refusing `not_configured` and
  naming every missing fact. Its `inertia` flag reads `estimated` until
  `measured_engine` holds an `EngineInertiaRecord`, and `RoadLoadFit` carries
  the flag.

  **One inertia convention (revised 2026-09-26 after verification).** The
  fit and the first generator both put η in the same place, which is
  circular. The crank, flywheel and clutch sit upstream of the lossy
  gearbox, so the wheel sees `η·N/r·(T − I_e·α)`, and the fit measures
  `η·I_e`. `dyno.INERTIA_CONVENTION` names this.
  - `fit_engine_inertia` pairs the two gears per 100 rpm bin and fits
    `τ_b − τ_a = η·I_e·(α_a − α_b)`. It reports `reflected_kg_m2` (= η·I_e),
    the assumed `driveline_efficiency` (1 − the profile's
    `drivetrain_loss_fraction`) and `engine_kg_m2` (= reflected / η).
  - `Driveline.effective_mass` uses η·I_e·N²/r².
  - `EngineInertiaRecord` refuses an `engine_kg_m2` that is not
    reflected / η, and any other `assumption` string.
  - `Driveline.from_vehicle` refuses (`conditions_mismatch`) a measured
    record whose η differs from the profile's.
  - The `dyno.yaml` estimate is documented as the physical crank inertia.
  - The generator now applies η = 0.85 at the crank. A lossless driveline
    reads 0.153 for a true 0.18, and a test pins this.

  Known answer 0.18 kg·m² (the driveline estimates 0.25):
  - Noise-free: 0.1799 (reflected 0.1529).
  - Three pulls in each of 2nd and 3rd at 0.03 m/s² noise, 100 Hz, 1 % grade:
    0.1814 / 0.1829 / 0.1822 (+0.8 / +1.6 / +1.2 %, se 0.0027–0.0040) on
    seeds 21–23.
  - se/scatter over 12 seeds: 1.19 (tested 0.7–1.7).
  - `pitch_offset` (a `PitchOffset`, frame-checked since H07.3) is
    required: passing 0 instead of the true 0.3 deg gives 0.259 (+44 %).

  Wheel-side inertia stays an estimate, because two gears cannot separate
  it. The measured crank inertia scales as 1/η_assumed, while `m_eff` uses
  the measured product and is unaffected.
  Refusals: `same_gear`, `rpm_overlap_too_small`,
  `alpha_difference_too_small` (5th against 6th), `too_few_samples`,
  `inputs_missing`, `invalid_input`, `non_physical`. Tests:
  `tests/test_dyno.py::test_two_gear_*`, `::test_driveline_*`,
  `::test_xterra_profile_*`.
- [~] **H07.7 The power computation.** **Do:** `src/analysis/dyno.py`: detect
  pulls (reuse `pull_report.detect_pulls` and `WotInventory` for
  wide-open-throttle provenance), refuse on gear change, clutch slip (rpm/speed
  ratio leaving its gear band), throttle below the WOT criterion, grade
  varying more than a stated amount, or GPS quality; compute wheel force,
  power and torque against engine rpm through the gear; average direction
  pairs; optional SAE J1349 correction on NA sessions. Engine power is
  reported only as `wheel / (1 − stated driveline loss)` and labelled an
  estimate. **Accept:** a synthetic pull generated from a known torque curve,
  road load and grade is recovered within 2 % over the band; each refusal
  has a test and a named reason.
  **Partial (2026-09-26):** acceptance met at the desk. Still owed:
  - finding pulls in a session. `pull_report.detect_pulls` lives in `tools/`,
    which `src/` does not import, and `WotInventory` is the digest's. H07.9's
    job needs both.
  - the truck's curve (H07.12).

  *Finding pulls: done at the desk 2026-10-03.*
  - **`src/analysis/pulls.py`** holds `RpmSeries`, `Pull` and `detect_pulls`,
    moved verbatim from `tools/pull_report.py`. The tool imports them back,
    and its 14 tests pass unchanged.
  - **`src/analysis/dyno_pulls.find_power_pulls(session, sources, driveline,
    demand_channel)`** returns every rpm rise found, each as a `FoundPull`
    with its window, rpm span and GPS course, and either a `PowerPull` or the
    refusal:
    - `lift_or_shift`: the detector's dip flag, refused before anything is
      read;
    - any `read_motion_inputs` or `DynoInputs.motion()` refusal, handed on
      unchanged;
    - `gear_unresolved`: the window's median implied overall ratio
      (rpm·2πr/60v) is more than `POWER_GEAR_TOLERANCE` from every gear.
      `compute_power` still checks every sample.

    An empty list means no rpm rise, never candidates quietly dropped. The
    rpm source is the profile's first `motion_sources.rpm`, held between
    changes in a change-only session.
  - **`label_headings`** groups pulls whose courses are within 30° (circular,
    so 358° and 5° are one direction) into `course-<deg>` labels, which is
    what `compute_power` pairs. A pull with no course is its own direction,
    `course-unknown`.
  - Refusal kinds `gear_unresolved` and `lift_or_shift` were added to
    `RefusalKind`.
  - `tests/test_dyno_pulls.py` (6): two opposite pulls found in gear with
    their courses and labels, a part lift refused by name and never read, a
    clutch-slip window refused rather than rounded to a gear, a motion
    refusal handed on, heading grouping across north, and no rpm meaning no
    pulls (a vehicle with no rpm source is a `ValueError`).
  - **Not used:** `WotInventory`. WOT is `compute_power`'s own check on the
    demand channel (`not_wot`), so this needs no digest provenance.
  - **H07.9** can now go session → `find_power_pulls` → `label_headings` →
    `compute_power`. Its job kind and page remain, and real coefficients
    wait on H07.12.

  `src/analysis/dyno.py` `compute_power(pulls, *, driveline, test_mass_kg,
  road_load, squat, pitch_offset, wot, conditions, correction=None)` returns
  `DynoPower` or a `DynoRefusal`.
  - Per sample, `F = m·f + (m_eff − m)·a + F_rl(v)`, with `f` corrected for
    squat and the pitch offset, and `m_eff` from `Driveline.effective_mass`
    (`η·I_e·N²/r²`, one `INERTIA_CONVENTION`), so `F = η·N/r·T`.
  - Each pull is binned per 50 rpm, the headings are averaged at the same
    rpm, and the pair is their mean.
  - Wheel torque is `P/ω_engine` (chassis-dyno convention, stated). Engine
    power and torque are only `wheel / η` and carry `ENGINE_ESTIMATE_LABEL`.
  - `pitch_offset` (a `PitchOffset`, frame-checked), `squat`, a *pair* road
    load, the WOT criterion (`threshold_value`, `rpm_floor`) and
    `RunConditions` are all required.
  - SAE J1349 is optional (`J1349Correction`, with the vapour pressure stated
    and its source) and refused on a boosted run. `DynoPower.dyno_curve()`,
    what floors and comparisons read, is always uncorrected; the corrected
    curve is separate.

  **Known answer.** The generator (`tests/test_dyno_power.py`) writes every
  effect out independently of the module: a torque curve peaking at 320 N m;
  grade 0.4 ± 0.3 % along the road; 2 m/s wind (headwind northbound); squat
  1.5 deg/g; a 0.3 deg IMU pitch offset (the IMU reads `a·cos φ + g·sin(θ+φ)`);
  η = 0.85 at the crank after `I_e` = 0.18; the wheel side; noise on f, a, v
  and rpm. Pulls are in 3rd, 2,450–5,500 rpm.
  - Noise-free: worst engine-torque error 0.007 % over 2,550–5,450 rpm.
  - Each heading alone carries the wind: northbound −0.8 to −1.7 %,
    southbound +0.8 to +1.7 %. The pair cancels it.
  - End to end, road load from `fit_road_load` on 3 coasts each way, the
    offset from `estimate_pitch_offset` on a cruise pair, 2 noisy pulls each
    way: worst error 0.40 / 0.39 / 0.47 % on seeds 31–33, and 0.27–0.47 %
    over seeds 31–40 (test tolerance 2 %).
  - **Through the producer at its own rate** (revised after verification: the
    generator above samples at 100 Hz, which H07.3 never outputs, and a fixed
    3 samples per 50 rpm bin crashed `compute_power` on 2nd-gear pulls at
    20 Hz, ~1.8 samples per bin). Instruments at their own rates (speed and
    rpm at 100 Hz in broadcast resolution, the IMU at 208 Hz, GPS at 1 Hz)
    go through `build_motion_inputs` at 20 Hz, then `compute_power`. Worst
    error 0.32–0.59 % in 2nd (2,700–5,250 rpm) and 0.38–0.54 % in 3rd over
    seeds 41–48. 2nd gear sampled at 20 Hz, noise-free: 0.010 %. The per-bin
    ask is now sized from the pull's own rate (at most 3, at least 1), and a
    pull filling under half its bins is refused `too_few_bins`, naming the
    rate.
  - Each term's size, noise-free: squat omitted 2.2 %, offset omitted 2.7 %,
    road load omitted 22 %, `I_e` 0.25 for 0.18 0.64 %, `I_e` without η
    0.29 %.
  - Mutation-checked, all 12 killed: the inertia term dropped, neutral
    `m_eff`, squat ignored, the offset's sign flipped, engine = wheel × η, the
    pair = one heading, road load dropped, no frame check, WOT unchecked, a
    fixed per-bin ask of 3, no bin-fill gate, the public correction without
    its frame check.

  **Refusals, each tested with its reason**, in this order:
  - arguments: `inputs_missing`, `invalid_input` (mixed gears),
    `conditions_mismatch` (gear), `single_direction` (road load),
    `correction_refused`;
  - `frame_mismatch`;
  - per pull: `inputs_missing` (rpm; operator demand), `too_few_samples`,
    `gear_change` (rpm on another gear's line, named), `clutch_slip` (above
    the line), `gear_ratio_mismatch` (below it, which slip cannot do: the
    speed scale, tyre or gear is wrong), `not_wot`, `grade_varies` (2nd–98th
    percentile of the IMU's own grade > 2 %, H07.7's stated amount),
    `rpm_span_too_small` (< 1,000), `too_few_bins`;
  - `too_many_directions`, `single_direction` (unless allowed, then labelled
    `single`), `direction_mismatch`, `no_overlap`.

  The gear band is ±4 %, because H07.11 found 4th 2.0 % off its published
  ratio. GPS quality is refused where the samples are made (H07.3), not
  here. Tests: `tests/test_dyno_power.py` (14).
- [~] **H07.8 Repeatability before claims.** **Do:** a same-day, same-ROM
  noise floor: at least three pull pairs, the spread per rpm band recorded as
  data with conditions (as N01 does for STFT). The analysis refuses to call
  two runs different when they differ by less than the floor. **Accept:** the
  floor loader and the refusal have tests.
  **Partial (2026-09-26): desk done, acceptance met; truck: the Xterra's floor
  itself owed (H07.12's three pull pairs).** `record_dyno_floor` resamples
  each pull pair onto one 50 rpm grid and pools the per-point sd per rpm band.
  It refuses `too_few_pull_pairs` (< 3), `direction_mismatch` (a single
  direction), `not_same_day_same_rom` (gear, road, induction, tune revision or
  date differ, naming the field), `no_spread` (identical curves) and
  `no_overlap`. `DynoNoiseFloor` requires `FloorConditions`: gear, road, NA or
  boosted, tune revision, date, and the baro, IAT and ambient ranges.
  `n_pull_pairs ≥ 3` is enforced by the schema. `RunConditions` is the
  required per-result conditions block from H07's rules. `compare_runs`
  pairs the two curves at the same rpm and marks a band `differs` only at
  ≥ 2 × √2·sd (`noise_floor.SIGMA_THRESHOLD`); anything below is
  `within_floor`, and an uncovered band is `not_covered`. It refuses
  `no_floor`, `conditions_mismatch` (gear, road, induction or quantity, both
  named), `direction_mismatch` and `no_overlap`. The loader is
  `load_dyno_profile`. A missing file gives `None`, never an invented floor.
  An empty or non-mapping file raises `ValueError` naming the file, and a
  misspelt key, a wrong `schema_version`, a negative band sd or broken YAML
  also raises (`test_dyno_loader_refuses_malformed_files`).
  `DynoProfile.noise_floor_for` names the recorded regimes on a mismatch and
  refuses duplicate regimes. Tests: `tests/test_dyno.py::test_floor_*`,
  `::test_compare_*`, `::test_profile_floor_*`,
  `::test_committed_xterra_dyno_file_*`.
- [ ] **H07.9 A workbench page.** **Do:** a `dyno` workbench job kind, an
  `/analysis/dyno` route through the single-flight child, and a tab on the
  Analysis page: power and torque against rpm per pull pair, overlay of runs,
  the conditions block, the refusals, and export. **Accept:** route and
  component tests; the page's empty state names its cause.

## Truck

- [~] **H07.10 Weigh the truck.** A public scale, with the driver, fuel level
  and cargo noted. **Accept:** mass and conditions in the profile.
  **Partial (2026-09-24):** an itemised estimate in the profile until a scale
  ticket: **2,185 kg (4,816 lb), range 4,728–4,893 lb**, from the operator's
  4,387 lb curb (6-speed manual; 4,400 lb is the automatic) plus 489 lb of
  additions less ~60 lb of stock step rails and hitch they replaced (wheels and KO3 tyres, Hefty
  bumpers, winch, skids and sliders), each item's source in
  `mass_evidence`. About ±1.3 %, which scales absolute power by the same;
  before/after comparisons on the same truck cancel it. Also recorded:
  40.3 kg per road wheel and tyre (stock 28.6 kg), the rotating mass H07.6
  needs. Occupants, cargo and fuel level are per-run conditions; the usual
  load is `usual_payload_kg` = 127 kg (driver 180 lb + cargo 100 lb), so a
  typical run moves ~2,311 kg (5,096 lb) on a full tank.
- [~] **H07.11 Measure the missing gear ratios.** Steady cruise in 2nd, 4th,
  5th and 6th (1st if practical), with GPS speed. **Accept:** all six in the
  profile, and the three already measured confirmed.
  **Partial (2026-09-24):** the FS6R31A's published ratios are in the profile
  (4.368/2.518/1.743/1.283/1.000/0.769; the operator regeared the Off-Road's
  stock 3.69 axles to 4.08), so `derived:GEAR_ESTIMATE` now runs for the
  Xterra — it was already on the non-trainable categorical list, so no model
  changes. Against the three cruise clusters already measured: 5th exact (the
  tyre circumference was solved from it), 3rd within 0.2 %, 4th 2.0 % high.
  The estimate reads ECU speed, ~8 % under true, so its rpm per km/h runs
  ~8.7 % high; adjacent gears are 25–30 % apart, so the nearest match holds,
  with less margin (H07.3's scaled speed removes it). Still owed on the truck:
  hand-selected steady cruise in each gear, to confirm 1st, 2nd, 4th and 6th
  and explain 4th's 2 %.
  *2026-10-03 (desk, every September drive on disk):*
  [`2026-10-03-gear-ratios-steady-cruise.md`](../../../data/analyses/2026-10-03-gear-ratios-steady-cruise.md).
  - **Method.** Same-instant `decoded:ECM_RPM` / `gps:speed` pairs
    (`align_pairs`, 60 ms) on steady cruise.
  - **2nd–6th: confirmed.** Relative to 5th, the gears are 2.528 / 1.745 /
    1.282 / 0.769 against published 2.518 / 1.743 / 1.283 / 0.769 (2nd
    +0.4 % on 24 pairs; 3rd, 4th and 6th within 0.1 % on 412–5,952 pairs).
  - **4th's 2 % is explained.** Every gear reads +1.85–2.34 % against the
    profile, by one common factor in final drive × tyre, not the gearbox. With
    the 4.08 regear, these drives imply a loaded circumference of about
    **2.660 m**, not the profile's 2.609 m (5th: 9,611 pairs, 22 sessions,
    p10–p90 ±1.1 %).
  - **Operator decision owed:** whether to change
    `tyre_loaded_circumference_m` (it moves the dyno's wheel force by ~2 %).
  - **Still owed (truck):** 1st, which has 2 steady pairs on disk.
- [ ] **H07.12 NA baseline.** On one flat, straight road: coast-downs both
  directions for H07.5, flat-ground accelerations for H07.4, then three pull
  pairs in 3rd from ≤ 2,500 rpm to the limiter (supercharger NA1/NA5).
  **Accept:** road load, squat coefficient, noise floor and the NA power
  curve recorded, with conditions, before the supercharger goes on.
