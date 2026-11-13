# Behavior Model Inference

CANary's anomaly detector needs to know what the engine should be doing right
now so it can flag when actual diverges from expected. This document describes
how we build those expected-behavior models from runtime observation, why we
*don't* read the ECU's factory calibration tables, and the implementation tasks
to land it.

The target vehicle is a 2008 Nissan Xterra (VQ40DE engine) that has been out of
production since 2015. Approach, scope, and risk profile are sized for that
specific vehicle and its owner-operator diagnostic context.

Status as of 2026-05-16: the schema, 1D/2D fitters, deviation decoder,
preprocessing integration, refit CLI, and dashboard feed are implemented. The
remaining blockers are data, not modeling code: OBD-backed drive captures now
run through the Canable-native streaming collector, and injector pulse width
still needs Mode 22 DID identification.

## Goals

1. **Inputs to anomaly detection.** For every signal we care about (fuel pulse
   width, ignition timing, MAF, AFR, etc.) produce an `expected_value(operating_point)`
   surface. The runtime decoder evaluates the surface at the current operating
   point; the deviation `actual − expected` becomes a feature the IsolationForest
   and LSTM consume alongside raw and rolling features.

2. **Performance monitoring.** The dashboard's performance panel (task 5.12)
   renders actual-vs-expected curves directly: fuel surface health heatmap,
   coolant warmup vs. baseline, VE drift over months, idle stability trends.
   This is the diagnostic value beyond a single "anomaly score" — it shows
   *how* the engine has drifted and which subsystem is responsible.

3. **Drift detection.** Models are re-fit periodically (weekly? monthly?) on
   clean recent sessions. Comparing successive fits surfaces slow degradation
   that the IsolationForest, trained on a single baseline, can't see — e.g.
   thermostat slowly opening earlier as it ages.

## Why inferred surfaces, not factory calibration tables

The obvious alternative is reading the ECU's flash and extracting the actual
fuel/ignition tables. We deliberately don't do this, for three reasons:

1. **The factory map is 17 years old.** The ECU's LTFT (long-term fuel trim)
   has been compensating for installed-state factors — sensor offsets, fuel
   composition, slight injector flow variations — for the engine's entire
   life. The raw factory map doesn't capture any of that. The runtime
   behavior does.

2. **For drift detection, "healthy" = "what the engine has been doing."**
   On a 200k-mile engine the question isn't "does it match a 2007 calibration
   target," it's "is it behaving like the engine has been behaving over the
   last N months." The runtime-inferred surface is exactly that baseline.

3. **The inferred surface is cheaper than a memory dump, not forbidden
   from one.** Mode 23 ReadMemoryByAddress behind Mode 27 SecurityAccess is
   available to CANary and is a legitimate way to get at the factory map.
   It is simply more work — and a dump gives you the *shipped* calibration,
   whereas this baseline gives you what the engine is doing now, which is
   the more useful comparand on a 200k-mile engine.

The factory map remains interesting as **named calibration constants** —
some Mode 22 DIDs expose those directly without security. See the
"Calibration DID scanner" section below.

## Scope guardrails

What we will do:

- Passive broadcast-CAN capture
- Standard Mode 22 ReadDataByIdentifier requests (the *intended* diagnostic
  interface; what every workshop scanner uses)
- Mode 01 PID polling via the Canable-native client or the legacy ELM327 path
- Fit statistical models to the captured data
- Persist the fitted models, version them, evaluate drift between versions

What this modeling workflow explicitly won't do:

- Write ECU non-volatile memory or reflash a module
- Mode 23 / Mode 27 / flash dumps / EEPROM read-back
- Bypassing security access
- Anything that modifies engine behavior
- Tuning (which is *modifying* a fuel map, distinct from modeling it)

## What we infer

Surfaces, listed roughly in order of expected diagnostic value:

| Model | Input axes | Output | Diagnostic value |
|---|---|---|---|
| Fuel surface | RPM, MAF | injector pulse width | Fuel filter clogging, injector wear, fuel quality issues |
| AFR surface | RPM, MAF | upstream O2 / lambda | O2 drift, closed-loop convergence problems |
| VE surface | RPM, MAF | volumetric efficiency proxy | Vacuum leaks, valve wear, top-end performance |
| Ignition surface | RPM, MAF | spark advance | Knock-retard activity, sensor faults, fuel quality |
| Coolant warmup | time since cold start | coolant temp | Thermostat health (sticking open / stuck closed) |
| Idle stability | engine-running-idle window | RPM std, idle PW std | Idle air control issues, vacuum leaks |
| Catalyst efficiency | upstream O2 | downstream O2 | Catalyst aging |
| Closed-loop convergence | post-cold-start time | STFT amplitude | Closed-loop entry timing, O2 sensor health |

Each surface is independently fitted, versioned, and stored. The daemon loads
all of them at startup and computes expected-value plus deviation per row.

## Inference techniques

Fitting strategy by surface complexity:

- **1D surfaces** (coolant warmup, idle stability): polynomial fit or local
  regression. `cross_reference.py` already does 1D linear regression — extend it
  to higher-order polynomials and LOWESS.
- **2D surfaces** (fuel, AFR, VE, ignition): start with a grid-binned mean
  per bin (`(RPM bin × MAF bin) → mean output`) — simple, interpretable,
  enough for v1. Move to radial basis functions or thin-plate splines if
  needed. Avoid neural-network smoothing for these — interpretability matters
  for diagnostics.
- **Per-mode fits**: the operating-mode segmenter already labels each row.
  Fitting a separate fuel surface per mode (idle, city, highway, WOT) gives
  cleaner anomaly signals than one global surface that has to span everything.

R² (or analogous goodness-of-fit metric) is persisted with the model.
Surfaces below a quality threshold (e.g. R² < 0.7) don't get used for anomaly
features in the daemon — partial coverage is better than misleading
expected values.

## Data requirements

For the fuel and AFR surfaces specifically:

| Signal | Source | Required for |
|---|---|---|
| RPM | OBD Mode 01 PID 0x0C | All engine surfaces |
| MAF (g/s) | OBD Mode 01 PID 0x10 | Fuel, AFR, VE, ignition |
| Upstream O2 voltage / wide-band lambda | OBD Mode 01 PID 0x14–0x1B | AFR, closed-loop |
| Throttle position | OBD Mode 01 PID 0x11 | Ignition, idle |
| Coolant temp | OBD Mode 01 PID 0x05 | Cold-start filtering, warmup curve |
| Injector pulse width | Mode 22 DID (TBD) | Fuel surface |
| Ignition timing advance | OBD Mode 01 PID 0x0E | Ignition surface |
| STFT B1 / LTFT B1 | OBD Mode 01 PID 0x06 / 0x07 | Fuel trim characteristics |
| Vehicle speed | OBD Mode 01 PID 0x0D | Operating-mode segmentation |

Most of this is standard Mode 01 — available through the Canable-native
streaming collector. Injector pulse width is the gap: it's typically exposed
via a manufacturer Mode 22 DID rather than standard Mode 01. Identifying that
DID is part of task 4.E.4 below.

For the runtime-inference path to converge on a good surface, we need:

- **Operating envelope coverage.** A pure highway cruise gives a thin slice;
  a varied drive (idle, city, highway, WOT) gives full coverage. Plan for
  multiple sessions spanning conditions.
- **Warm-engine filtering.** Cold-start enrichment overrides closed-loop fuel
  control; data captured before coolant > 70 °C should be excluded from the
  fuel surface fit but kept for the warmup-curve fit.
- **Steady-state vs. transient filtering.** For 2D fuel surfaces, large dRPM/dt
  windows are unreliable (the ECU is in transient acceleration enrichment).
  Filter to rows where RPM is reasonably stable.

## Output schema

New top-level section in `data/bindings.yaml`, parallel to `signals:`:

```yaml
behavior_models:
  - name: fuel_surface_idle
    type: surface_2d
    output: injector_pulse_width
    output_unit: ms
    axes:
      - input: rpm
        unit: rpm
        bins: [500, 700, 900, 1100, 1300]
      - input: maf
        unit: g/s
        bins: [2, 4, 6, 8, 10]
    operating_mode: idle
    fit:
      method: grid_binned_mean
      r_squared: 0.86
      n_samples: 4231
      sessions: [2026-05-11T152858, 2026-05-12T...]
      fitted_at: 2026-05-15T18:00:00Z
    values: |
      # 2D array, one row per RPM bin × MAF bin
      ...
    plausible_deviation:
      max_abs: 0.5  # ms; above this we flag anomaly
```

Versioning: each refit increments a `version` field. Older versions stay on
disk for drift comparison. Manifest tracks which version the daemon is
currently serving.

Resolved (task 4.E.1): the surfaces live in a sibling
`behavior_models.yaml`, not in `bindings.yaml`. With vehicle profiles
the live file is profile-scoped — e.g.
`data/nissan-xterra-2008/behavior_models.yaml` — via
`scope_config` (`src/vehicle/profiles/scope.py`); the unscoped `data/behavior_models.yaml`
is the single-vehicle default.

## Integration with anomaly detection

For every behavior model the daemon loads, it adds two new columns to the
preprocessed frame:

- `<model>__expected` — surface value at the current operating point
- `<model>__deviation` — `actual − expected`, the diagnostic signal

These get featurized by `compute_features` exactly like raw values: rolling
mean of deviation, rolling std of deviation, rate of change of deviation.
Then scaled by `ZScoreScaler`. The IsolationForest sees deviation features
alongside the original ones; a healthy engine produces near-zero deviation,
faults produce sustained nonzero deviation.

This is the path that turns "we recovered the fuel table" into actual
anomaly-detection value.

Equation-backed expectations use the same idea but come from physical
relationships rather than fitted behavior surfaces. Those features and their
limits are documented in `docs/physics-informed-anomaly-features.md`.

### Directional deviation and accepted-baseline overrides

`DeviationConfig.kind` (`src/behavior/behavior_models.py`) has a third variant
alongside `absolute` and `sigma`: `directional` — a one-sided band around a
center, for a known, *accepted* fault that shouldn't be muted, only
recentered (see `docs/roadmap/online-training-strategy.md`, "Directional
overrides — the schema gap"). `tolerance` is the ± wander that never
alerts; past that, `worse_direction` picks the sign of `actual − center`
that means "worsening" and grades it against `warning_band`/
`critical_band` (in output units, or std-scaled via `std_scaled` +
`sigma_floor` — same degenerate-std guard ml-hardening 4.2 enforces for
`sigma`); the other direction (`better_direction: ignore`, the only value
today) never alerts no matter how far it drifts, which is what stops
improvement/measurement noise from generating false alarms.

A model's own `deviation:` can be authored `directional` directly, but the
common path is `data/<vehicle-slug>/accepted_baselines.yaml`
(`src/behavior/accepted_baselines.py`) — an operator-authored, provenanced
override (who/when/why/`review_by`), never an unattended one. Each
override targets either a raw `channel` (needs `accepted_center` — there's
no fitted surface to read a center from) or a `behavior_model` by name (the
center is always that surface's own fit; the override contributes only the
band).
`AcceptedBaselineOverride.to_deviation_config()` converts an override into
the runtime `DeviationConfig` shape so one severity-grading implementation
(`src.behavior.behavior_decoder`) serves both statically-authored directional
models and dynamically-applied overrides.

The live behavior detector (`src.daemon.inference._observe_behavior`)
consults `AcceptedBaselines.active_override_for` every tick: the worsening
side still trips warning/critical (with a fired event's
`detail.override_reason` echoing the declared `reason`), the improving
side never alerts, and an override past its `review_by` is inert —
`active_override_for` already excludes it, so the model falls back to
grading against its own static config (if any) exactly as if the override
never existed, rather than silently continuing to mask a fault.
`GET /status` → `accepted_baseline_overrides` (`{"active": N, "overrides":
[...]}`) is always present so the dashboard can show "N accepted-baseline
overrides active" and flag an expired one as needing re-confirmation
instead of dropping it from view — see `docs/runbooks.md`, "Author or
review an accepted-baseline override".

## Integration with the dashboard

The performance panel (Phase 5 task 5.12) renders this data directly:

- **Fuel-surface heatmap** showing current deviation across the RPM × MAF
  grid. Healthy = mostly zero. Clogged filter = uniformly positive.
- **Coolant warmup curve overlay** showing today's curve vs. the baseline
  curve from the fitted model.
- **VE drift over time** plotted as the surface's mean shifting across
  monthly refits — slow degradation visible at a glance.
- **STFT/LTFT decomposition** showing fuel-trim contributions per operating
  mode, highlighting whether trim is consistently positive (lean fault) or
  negative (rich fault).

These views are valuable independently of the IsolationForest score.

## Calibration-DID scanner (complementary)

The runtime-inferred surface tells us what the engine has been doing. A
calibration-DID scanner tells us what the ECU is *configured* to do. They're
complementary, not redundant.

Method: sweep Mode 22 DID space, separate runtime-varying DIDs from
constants, cross-reference constants against the FSM where it documents
calibration IDs.

This requires sending Mode 22 requests, so it changes our setup from
listen-only to active. Before running, verify that the Canable application
won't accidentally send other traffic (the slcan interface is hardware-
bidirectional; the discipline is application-level). Once verified, the
scanner is safe — Mode 22 reads are routine workshop traffic.

Output: a list of named calibration constants we can cross-reference against
the runtime surface. If FSM doc names DID `0x1280` as "idle target RPM" and
the runtime surface's RPM mode is 750 rpm, those should agree.

## Implementation status

These correspond to Phase 4.E in `docs/implementation-tasks.md`.

- [x] **4.E.1 — Behavior-model schema.** Implemented in
  `src/behavior/behavior_models.py` with `data/behavior_models.yaml` as the
  sibling artifact.
- [x] **4.E.2 — 1D surface inferer.** Implemented in
  `src/analysis/surface_1d.py`.
- [x] **4.E.3 — 2D surface inferer.** Implemented in
  `src/analysis/surface_2d.py`.
- [~] **4.E.4 — Injector pulse-width DID identification.** Selected Mode 22 DID
  polling now works through `scripts/record.py --obd-source canable --obd-dids`.
  Still pending: the actual candidate-DID drive and a broad Canable-native DID
  sweep CLI for exploratory scans.
- [x] **4.E.5 — Calibration-DID scanner.** Implemented in
  `tools/scan_constant_dids.py` / `src/analysis/did_scan.py` for the legacy ELM
  path.
- [x] **4.E.6 — Behavior-model decoder integration.** Implemented in
  `src/behavior/behavior_decoder.py`.
- [x] **4.E.7 — Deviation features in preprocessing.** Implemented via
  `append_deviation_columns` and `build_feature_frame`.
- [x] **4.E.8 — Refit CLI.** Implemented in `scripts/refit_behavior_models.py`.
- [x] **4.E.9 — Performance panel data feed.** Implemented via
  `GET /models/behavior` and the React behavior-model panel.
- [x] **Directional overrides (online-training-strategy.md Phase 2).**
  `DeviationConfig.kind="directional"` + `AcceptedBaselineOverride.
  to_deviation_config()` (schema + runtime grading), the live behavior
  detector consulting active overrides with `detail.override_reason` on
  fired events, and `GET /status` → `accepted_baseline_overrides` +
  StatusStrip surfacing ("N active", expandable, expired flagged).

The order assumes Phase 2 (IsolationForest baseline) lands first — behavior
models are most valuable as *additional* features feeding an existing
detector, not as a standalone detector.

## Open questions

- **Per-vehicle vs. per-VIN models.** If CANary ever runs on a second Xterra,
  do we share fitted surfaces or refit per vehicle? Probably refit per
  vehicle — installed-state factors vary too much.
- **Cold-start enrichment modeling.** Worth treating as its own model
  (input axis: time since cold start; output: open-loop fuel multiplier) or
  as filtering applied before the steady-state fuel surface fit?
- **VE proxy formulation.** Theoretical VE = (MAF / RPM) × constants, but
  the constants involve displacement, atmospheric conditions. A normalized
  proxy may be more useful diagnostically — TBD during 4.E.3.
- **Fit refresh cadence.** Refitting too often makes the baseline track
  faults (defeating drift detection); too rarely means real changes don't
  surface. Plausibly: weekly refit, with the IsolationForest seeing 2–4 weeks
  back so slow drift still registers.
