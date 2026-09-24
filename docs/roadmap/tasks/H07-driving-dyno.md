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

- [ ] **H07.1 Driveline facts that cannot be misread.** **Do:** replace
  `gear_ratios` with typed fields: `final_drive`, `gearbox_ratios` (unitless,
  one per gear), `tyre_loaded_circumference_m` with its evidence; compute
  expected rpm per km/h from them, so the unit mismatch cannot recur. Fix
  `derived:GEAR_ESTIMATE` and its tests to match; fix `canary.example.yaml`.
  Put the measured Xterra values in its profile with their provenance (the
  0x6F7 notes in `data/bindings.yaml`). **Accept:** gear estimates on the
  ground-truth slice match the three measured clusters; a config with the old
  field is refused with a message naming the new ones.
- [ ] **H07.2 One pull-tag vocabulary.** **Do:** decide the canonical names
  (the digest's `tuning_pull_start`/`tuning_pull_end`, or the page's), make
  both producers and all consumers use a shared constant, and accept the other
  spelling on read for old sessions. **Accept:** a test that a pull tagged
  from the Logging page counts as `tagged` in `WotInventory`.
- [ ] **H07.3 Speed and acceleration inputs.** **Do:** a function returning,
  for a pull window, GPS speed (absolute), `0x2D1` broadcast speed scaled by
  the profile's measured ECU/GPS ratio (100 Hz), `a` from a smoothed
  derivative of the scaled broadcast speed, and `imu:SF_LON` (H02.4), all on
  one time base from the acquisition clock. **Accept:** synthetic tests with
  known speed profiles; refusal named when GPS is below the H04.3 quality
  gates.
- [ ] **H07.4 Body pitch under acceleration.** **Do:** on flat ground,
  `f − a` should equal `g·sin(θ)` with θ ≈ 0; fit the residual against `a` to
  get a squat coefficient (degrees of pitch per g). Store it as profile data
  with conditions (load in the truck, tyre pressure). **Accept:** synthetic
  recovery of a known coefficient; the fit refuses when the stretch is not
  flat by GPS altitude over the window.
- [ ] **H07.5 Road-load model from coast-downs.** **Do:** fit
  `F_rl(v) = A + B·v + C·v²` from clutch-in, neutral coast-downs (SAE J2263
  style), using `F_rl = −m·f − (m_eff,neutral − m)·a`, both directions, and
  store A, B, C with conditions and fit residuals. **Accept:** synthetic
  coast-down data with known coefficients recovered within 5 %.
- [ ] **H07.6 Rotating inertia.** **Do:** estimate `m_eff` per gear from
  wheel, driveline and engine inertia; first from nominal values stated as
  estimates, then from pulls in two gears over the same rpm band, where
  engine torque is the same and the difference in wheel force isolates the
  inertia term. Until measured, results carry an `inertia: estimated` flag.
  **Accept:** a synthetic two-gear test recovers a known engine inertia.
- [ ] **H07.7 The power computation.** **Do:** `src/analysis/dyno.py`: detect
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
- [ ] **H07.8 Repeatability before claims.** **Do:** a same-day, same-ROM
  noise floor: at least three pull pairs, the spread per rpm band recorded as
  data with conditions (as N01 does for STFT). The analysis refuses to call
  two runs different when they differ by less than the floor. **Accept:** the
  floor loader and the refusal have tests.
- [ ] **H07.9 A workbench page.** **Do:** a `dyno` workbench job kind, an
  `/analysis/dyno` route through the single-flight child, and a tab on the
  Analysis page: power and torque against rpm per pull pair, overlay of runs,
  the conditions block, the refusals, and export. **Accept:** route and
  component tests; the page's empty state names its cause.

## Truck

- [ ] **H07.10 Weigh the truck.** A public scale, with the driver, fuel level
  and cargo noted. **Accept:** mass and conditions in the profile.
- [ ] **H07.11 Measure the missing gear ratios.** Steady cruise in 2nd, 4th,
  5th and 6th (1st if practical), with GPS speed. **Accept:** all six in the
  profile, and the three already measured confirmed.
- [ ] **H07.12 NA baseline.** On one flat, straight road: coast-downs both
  directions for H07.5, flat-ground accelerations for H07.4, then three pull
  pairs in 3rd from ≤ 2,500 rpm to the limiter (supercharger NA1/NA5).
  **Accept:** road load, squat coefficient, noise floor and the NA power
  curve recorded, with conditions, before the supercharger goes on.
