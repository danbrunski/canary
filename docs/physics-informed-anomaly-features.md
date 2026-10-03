# Physics-informed anomaly features

CANary should teach the anomaly detectors physics, but not by trying to make the
LSTM a full engine simulator. The practical design is to compute physical
estimates and residuals first, then feed those residuals into the same
IsolationForest / LSTM feature pipeline as ordinary telemetry.

This gives the model a cleaner signal for "that relationship broke" without
forcing it to rediscover engine equations from sparse, noisy PIDs.

## Current state

`src/signals/derived.py` already computes several physics-derived channels for
dashboard/export/trip review:

- `derived:FUEL_RATE_LPH`
- `derived:INSTANT_ECONOMY_L_PER_100KM`
- `derived:INSTANT_ECONOMY_MPG`
- `derived:BOOST_VACUUM_KPA`
- `derived:ESTIMATED_POWER_HP`
- `derived:ESTIMATED_TORQUE_NM`
- cumulative trip channels such as distance, fuel used, CO2, and idle time

The shared anomaly preprocessing path now **does** append an
anomaly-oriented derived set before `compute_features`:
`compute_anomaly_signals` (`src/signals/derived.py`) contributes
`derived:AIR_MASS_PER_CYCLE`, `derived:STFT_BANK_BALANCE` /
`LTFT_BANK_BALANCE` / `AFR_BANK_BALANCE`, and
`derived:TPS_VOLTAGE_PER_PCT`, followed by committee residuals and
behavior-model deviation columns (`src/features/feature_pipeline.py`,
`featurize_wide_frame` tail). Still open from the plan below: the
explicit `physics_residual:` / `relational:` equation-backed columns
(MAF vs speed-density, fuel-rate vs MAF, power vs fuel, MAP vs baro) and
the 2nd-derivative/curvature features.

## Design principle

Use physics as feature engineering and guardrails:

1. Compute a physics expectation from available inputs.
2. Compare the observed sensor/PID value against that expectation.
3. Feed the residual, rolling residual stats, and residual rate-of-change into
   the anomaly detectors.
4. Keep the residual visible in the dashboard so alerts are explainable.

Do not make the first implementation a physics-informed neural network loss.
PINN-style training is useful when the governing equations are complete and
the system is directly observed. A vehicle ECU is not that clean: enrichment,
closed-loop fuel control, DFCO, knock retard, sensor filtering, learned trims,
and proprietary torque logic all intervene. Hard constraints would create
confident false positives when the ECU is doing something valid but unmodeled.

## Feature families

### Physical estimates

These are direct estimates from known equations. They can be useful features
when they are instantaneous and stationary:

- Fuel flow from `ENGINE_FUEL_RATE`, or from `MAF / AFR`, or from
  speed-density using RPM, MAP, IAT, displacement, and VE.
- Boost/vacuum from `MAP - BARO`.
- Estimated power from torque PIDs, or from fuel energy and thermal efficiency.
- Estimated torque from estimated power and RPM.

These should use the existing `derived:*` prefix.

### Physics residuals

Residuals are usually more valuable than the estimates themselves:

- `physics_residual:MAF_vs_speed_density`
  = observed MAF minus expected airflow from RPM/MAP/IAT/displacement/VE.
- `physics_residual:FUEL_RATE_pid_vs_maf`
  = fuel-rate PID minus fuel rate inferred from MAF and lambda.
- `physics_residual:POWER_torque_vs_fuel`
  = power from torque PIDs minus power inferred from fuel energy.
- `physics_residual:MAP_vs_baro`
  = manifold pressure minus barometric pressure, with operating-mode context.
- `deviation:fuel_trim_vs_rpm_load`
  = actual fuel trim minus the fitted RPM/load behavior surface.
- `deviation:timing_advance_vs_rpm_load`
  = actual spark advance minus the fitted RPM/load behavior surface.

Behavior-model residuals already use the `deviation:` prefix. New residuals
whose expectation comes from equations rather than fitted behavior should use a
separate `physics_residual:` prefix.

### Cross-check residuals

When two independent measurement paths exist, compare them:

- OBD speed vs GPS speed.
- OBD acceleration from speed derivative vs IMU longitudinal acceleration.
- Fuel-rate PID vs MAF-derived fuel rate.
- Torque PID power vs fuel-energy power.

These are strong anomaly features because they flag disagreement, not merely a
large value.

### Engineered relational features

A physics residual or a fitted behavior surface is the *right* answer for a
known relationship, but each one is hand-built. A much cheaper, general family
is the raw ratio or difference of two physically-coupled channels, fed as its
own column:

- `relational:MAF_per_RPM` = MAF / RPM (airflow per revolution — sags on a
  restriction or a slipping VE assumption).
- `relational:LOAD_minus_THROTTLE` = engine load − throttle position
  (decouples on a vacuum leak or a sticking plate).
- `relational:O2_B1_minus_B2` = bank-1 vs bank-2 narrowband (a bank-specific
  fault breaks the symmetry).
- `relational:RPM_per_SPEED` = RPM / vehicle speed in gear (clutch slip,
  torque-converter behaviour, wrong-gear inference).

These need no equation and no fitted surface — they exist precisely to turn a
*relational* violation into a *marginal* one the IsolationForest can split on
(see below). They are weaker and noisier than a real residual, so treat them
as breadth, not depth: cheap coverage for pairs nobody has modelled yet.

Expanding the fitted behavior-model catalog is the higher-quality version of
the same lever: every new surface is one more correlation encoded as an
IF-digestible `deviation:` residual. Relational ratios buy coverage today;
behavior surfaces buy accuracy later.

## What to exclude

Do not train anomaly models on cumulative trip counters:

- `derived:TRIP_DISTANCE_KM`
- `derived:FUEL_USED_L`
- `derived:CO2_KG`
- `derived:IDLE_TIME_S`
- `derived:AVG_ECONOMY_L_PER_100KM`

They are monotonic or history-dependent by construction, so the model would
mostly learn drive length and session shape. They belong in trip summaries and
dashboards, not the anomaly feature matrix.

Also treat instant economy carefully. `L/100km` and MPG explode near zero speed;
they should be masked to moving windows or excluded from the first model pass.

## How this helps correlations — and which detector needs it

The "two channels normally co-move; under a fault one lags or inverts while
each stays individually in range" case is exactly the discriminator between
the two detectors, so the answer is model-split.

**IsolationForest is structurally blind to this.** It isolates points with
axis-aligned random splits, and `ZScoreScaler` standardizes each feature
*marginally* — no whitening, no decorrelation. A point where every individual
feature is within its normal marginal range but the *relationship* is inverted
sits on a diagonal that axis-aligned splits cannot see, so it is usually not
isolated. Per-feature scaling sharpens the blind spot rather than helping.

This is the entire reason for the residual/deviation feature families above.
A residual collapses a relational (multi-channel) violation into a marginal
(single-channel) one the IF *can* split on. Example: under nominal behavior
RPM/load rises and MAF rises with it; if RPM/load rises but MAF stays flat or
drops, the raw channels each look only "a little unusual," but:

```text
physics_residual:MAF_vs_speed_density =
    observed_MAF - expected_MAF(rpm, map, iat, displacement, VE)
```

is a direct, marginal signal. The IF then sees the residual value, its rolling
mean/std, and its rate of change. Anticorrelation is identical — a learned
negative relationship is just a negative direction; the residual carries it
with the opposite sign, the IF is equally blind without it. So for the IF,
correlation-break coverage **equals** the residual/deviation/relational
catalog: unmodelled pairs remain blind spots, which is why catalog expansion
is the leverage, not detector tuning.

**The LSTM autoencoder is the general handler, and it localizes.** A
multivariate sequence autoencoder learns the joint manifold including
cross-channel correlation *and its temporal evolution*; a lag or inversion
leaves the manifold and raises reconstruction error, and the per-channel
reconstruction error (`channel_scores` / `anomalous_channels` in
`src/models/inference.py`) reports *which* channel broke the relationship even
when each is individually in range. Residuals still help it — they are a
strong inductive bias that makes the latent space's job easier — but they are
not strictly required there the way they are for the IF.

Honest limits on the LSTM side (these belong with the physics caveats below
but apply to the detector, not the estimate):

- It only "knows" correlations that were *consistently present and
  well-covered in training*. A regime-dependent correlation (co-move at idle,
  decouple at WOT) needs that regime covered, or it misses the break or
  false-alarms benign decoupling — another reason for operating-mode
  segmentation and the coverage requirements in
  `behavior-model-inference.md`.
- An over-capacity autoencoder can learn a near-identity map and reconstruct
  anomalies too, partially "seeing through" the break. `latent_dim`,
  `dropout`, and `sequence_length` are the levers; a tighter bottleneck forces
  reliance on correlation structure at some cost to reconstructing
  rare-but-normal patterns.
- The signal collapses if the *partner* channel is dropping out (OBD
  round-robin gaps at 1–5 Hz). Correlation-break detection needs both columns
  populated in the sequence buffer.

## Rate of change and curvature

The 1st derivative is already built and built correctly. `compute_features`
(`src/preprocessing/features.py`) appends a `__roc` column per channel that is
`(value − value_shifted_by_window) / window_seconds` — a *windowed average
slope*, not a naive sample-to-sample difference. That distinction is the whole
game: a raw finite difference of the 1–5 Hz jittery OBD trace is mostly noise;
a windowed slope is the usable form. Because residual/relational columns land
before `compute_features`, they inherit `__roc` for free.

The 2nd derivative does **not** exist, and should be added *targeted, not
blanket*:

- Blanket 2nd derivatives on every channel are mostly noise columns. They
  inflate dimensionality and specifically hurt the IF (more axis-aligned
  splitting on near-noise — weaker isolation, not stronger).
- Add curvature only where it *is* the fault signature, computed as the ROC of
  the smoothed ROC (or a Savitzky-Golay 2nd-order term), not a double finite
  difference:
  - **coolant-warmup curvature** — a sticking thermostat is a *flattening* of
    the warmup curve, which is a 2nd-derivative signature (the failure mode
    `coolant_warmup` in `behavior_models.yaml` already calls out).
  - **RPM jerk** — misfire / stumble.
  - **IMU jerk** — d(accel)/dt, already planned in `roadmap/imu-gps-integration.md`.

Model-split, same as correlations: rate and curvature matter most for the
**IF**, which is memoryless and per-row — without them it sees no dynamics at
all. The **LSTM** ingests the ordered sequence over `sequence_length`, so it
can represent rate and curvature internally; explicit derivative columns there
are a helpful inductive bias, not a necessity, and noisy ones cost the IF more
than they help the LSTM.

## Integration plan

1. Add a preprocessing step after resampling/segmentation and before
   `append_deviation_columns` / `compute_features` that appends selected
   non-cumulative `derived:*` channels.
2. Add equation-backed `physics_residual:*` columns for the high-value
   relationships above.
3. Add a small set of `relational:*` ratio/difference columns for
   physically-coupled pairs nobody has modelled yet (cheap IF correlation
   coverage).
4. Let `compute_features` add rolling mean/std/ROC for those columns the same
   way it already handles raw values and behavior deviations.
5. Add **targeted** 2nd-derivative (curvature) columns for coolant warmup, RPM,
   and IMU jerk only — as ROC-of-smoothed-ROC, not blanket.
6. Keep cumulative trip channels out of `trainable_feature_columns`.
7. Surface top residual contributors *and* the LSTM per-channel
   `anomalous_channels` in anomaly events and dashboard panels so an alert can
   say "MAF is low for RPM/MAP/IAT" rather than only "LSTM score high."

The first useful implementation should be conservative: fuel flow, MAF
speed-density residual, boost/vacuum, fuel-trim behavior deviation, and timing
behavior deviation. Add power/torque and IMU/GPS cross-checks once those inputs
are reliably present.

## Calibration and limits

Physics estimates carry assumptions:

- Volumetric efficiency is approximate and varies with RPM/load.
- MAF and MAP sensors are filtered and may lag each other.
- Fuel-rate estimates depend on commanded lambda and fuel constants.
- Thermal efficiency and drivetrain loss are coarse assumptions.
- Grade, wind, torque converter behavior, and accessory load affect power
  estimates.

Because of that, residual thresholds should be learned from clean sessions and
segmented by operating mode where possible. Treat physics residuals as
high-signal features, not absolute truth.

