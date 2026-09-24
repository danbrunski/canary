# B04 — timing resolution and airflow consolidation

**Depends on:** B01/B02; B03 governs artifact status and publication.
Addresses the behavior-model part of D03.20. Follow
[B00](B00-behavior-models-README.md).

## Timing

- [x] **B04.1 — Reproduce the timing baseline.** Record held-out errors,
  coverage and physical warning widths for RPM/load and RPM/IAT, including
  the cooler September drives. Preserve the roughly 24° median warning width
  as an observed limitation, not a desired threshold. Separate calibration,
  temperature, load and steady/transient regimes before comparing candidates.
  *Done 2026-09-22 (no fit):* 31 independent September sessions (10 grouped
  drives contributed evaluated timing rows) incl. the cool 09-18/09-20 ones,
  pinned by content hash in
  `tests/fixtures/behavior_validation/evidence_b04_timing.yaml`; run through
  `scripts/evaluate_behavior_models.py --mode corrected`. RPM/load: 41,317
  eligible → 36,169 evaluated, MAE 5.86°, bias +0.93°, p95 18.4°, R² 0.323,
  **median warning half-width 23.9° (recorded as the observed limitation)**,
  skill 0.228, 2.19 events/h, envelope 1.00; drive MAE 0.05–8.23°, bootstrap
  90 % 3.83–7.35°. RPM/IAT: 5,088 of 34,887 eligible evaluated (coverage
  0.15), MAE 5.52°, skill −0.083, 58 events/h. Regime strata are now data
  (`behavior_validation.yaml: report_regimes`) and reported per model by the
  evaluator (`per_context`): by calibration (k106 5.77° vs post-flash
  ZS44A-20260920 6.94°), IAT, coolant, load (15.3° below 20 % load vs 1.8° at
  20–30 %), baro (+6.99° bias on the single 50–70 kPa drive) and
  steady/transient (4.24° vs 7.51°). Evidence: [report §1](../../../data/analyses/2026-09-22-b04-b05-measurements.md).
- [~] **B04.2 — Build a load-aware context candidate.** Retain RPM and load
  as essential explanatory inputs; assess IAT/coolant and relevant air-density
  context on independent drives. Prefer the smallest interpretable conditional
  model justified by coverage. If additional axes cannot be supported, keep
  the model exploratory with named missing coverage rather than substituting
  an RPM/IAT-only fit or broadening bands until it passes.
  *Assessed 2026-09-23 on the current calibration; stays exploratory.* The
  RPM/load surface was refit on ZS44A-20260920 (candidate v3) and judged on 2
  untouched drive groups. Strata ([report](../../../data/analyses/2026-09-23-behavior-refit-validation-report.md) §4):
  - IAT: [20,30) °C MAE 6.08°, [30,40) 2.89°, [40,50) 0.20°. High IAT is
    idle/low airflow (stagnation), so IAT stands in for operating point, not
    context.
  - Coolant: < 60 °C 8.88° (249 rows, 2 cold starts) against 4.21° at
    90–100 °C.
  - Load: **< 20 % load 10.9°** (overrun/decel) against 1.18° at 20–30 %.
  - Baro: only 76–90 kPa.

  Neither IAT nor coolant explains the dominant error, and no axis is added
  (the fitter is 2-D). **Missing coverage:** any drive below 76 kPa on this
  calibration (no air-density axis is assessable); more cold starts for a
  coolant axis; and a way to model low-load overrun, which is outside what
  IAT/coolant context addresses.
- [x] **B04.3 — Define and test useful timing resolution.** Put the intended
  sustained timing change and its required duration into the evaluation spec,
  justified by measured noise and the diagnostic question. Validate simulated
  shifts, transient retard controls and unmodified held-out data through actual
  eligibility and persistence. Report detection delay and covered envelope;
  an unexpected timing change must not be labelled proven knock retard.
  *Done 2026-09-22 (machinery + spec only; no fit):* `ResolutionSpec` on
  `ValidationCriteria` (sustained change, required duration, delay budget,
  **measured** noise with its basis, diagnostic question, `claim`,
  `not_identified`, transient controls) — declared for both timing recipes in
  `data/nissan-xterra-2008/behavior_validation.yaml`, justified by a measured
  2.19° steady-state within-cell spread (p95 1.0°, p99 6.0°, 6,221 rows), not
  the 0.5° PID LSB. The evaluator now injects each transient control at the
  window's first evaluated row and scores it through the same eligibility and
  persistence gate, and reports detection delay and covered envelope per
  synthetic case. On the untouched September drives the −3°/60 s step is seen
  in 210/718 clean windows (0.29) with a 2.0 s median delay and envelope 1.00,
  while the −6° transients (3 s and 10 s) raise events in 20 % / 22 % of clean
  windows against a declared 0 % ceiling: **the 3-of-5-ticks-in-8 s
  persistence gate cannot express "sustained"**, which is the named remaining
  gap (a per-model persistence requirement, not wider bands). Claims reworded:
  no timing recipe says knock retard any more, and
  `tests/test_behavior_resolution.py` fails if one does. That file also covers
  the spec's refusals (noise ≥ change, control ≥ duration, delay > duration,
  target ≠ meaningful_deviation, no long-enough fault) and the machinery
  end-to-end. **Along the way:** the evaluator's row clock read microsecond
  frames as `int64/1e9`, compressing every interval 1000x — events, dedupe
  and synthetic windows were all wrong before today (`EVALUATOR_VERSION` 2).
  Evidence: [report §0, §2](../../../data/analyses/2026-09-22-b04-b05-measurements.md).
- [x] **B04.4 — Publish only demonstrated improvement.** Use B02's untouched
  test drives, same eligible points and physical-unit errors to compare against
  the existing surface and simple baseline. Publish via B03 if qualified;
  otherwise leave the candidate diagnostic-only. RPM/IAT does not regain an
  independent alert solely because it supplies useful context.
  *Done 2026-09-23 — not qualified, left diagnostic-only.* Same 7976
  eligible rows on the untouched `2026-09-22T154716` + `2026-09-22T233855`
  drives:
  - published v2 → candidate v3: MAE 5.60 → 4.85°, median warning width
    23.1 → 13.2°, off-map 1000 → 715, events 3 → 3
  - skill against the training baseline 0.35 (passes ≥ 0.3)
  - fails: MAE and width > 3°, synthetic −3°/60 s detection 0.56 < 0.8,
    transient controls 0.30/0.38 > 0

  `behavior_policy.yaml` keeps it exploratory. RPM/IAT regains nothing
  (held-out coverage 24.7 %, failed, not published). Evidence: [report](../../../data/analyses/2026-09-23-behavior-refit-validation-report.md)
  §2–§4, `2026-09-23-behavior-refit-comparison.json`.

## Air charge and VE

- [x] **B04.5 — Preserve physical definitions in the contract.** Trace
  `compute_anomaly_signals` and `_theoretical_air_mass_per_cycle_g`: distinguish
  measured MAP, measured baro and missing/default IAT in model inputs and
  provenance. Do not silently train/serve one VE definition as another.
  Retain raw air-mass/air-charge quantities for inspection and restrict
  diagnostic claims to relationships the measurements actually identify.
  *Done 2026-09-22:* `_theoretical_air_mass_per_cycle_g` preferred MAP, fell
  back to baro and substituted 25 °C for a missing IAT, so one channel name
  meant two quantities (~85 % vs ~25 % at part throttle) depending on the
  capture profile. Split into `derived:VE_MANIFOLD_PERCENT` (measured MAP +
  measured IAT) and `derived:VE_AMBIENT_PERCENT` (measured baro + measured
  IAT) — no fallback between references, no default temperature, a missing
  input means no value. `derived:VE_PERCENT` is kept **bit-identical** and
  labelled legacy because tracked IF/LSTM manifests consume it (the 2026-09
  IF-blind incident); no recipe targets it. `derived.resolved_dependencies()`
  reports which input each group resolved to, and the evaluator records it per
  session (`derived_input_resolution`). `ve_vs_rpm_load` now targets the
  ambient channel and a new `ve_manifold_vs_rpm_load` the MAP one; the shipped
  artifact predates the split, so a carry-over is flagged
  `contract_changed:recipe` and the evaluator labels the substitution in
  `row_contract` (`refit.legacy_reproduction_spec`). Air-mass and VE notes no
  longer claim to identify leaks/restriction/MAF drift, and both VE recipes
  state they share air mass's MAF/RPM measurement. Tests:
  `tests/test_anomaly_signals.py` (four new cases). Evidence:
  [report §3](../../../data/analyses/2026-09-22-b04-b05-measurements.md).
- [~] **B04.6 — Validate normalization across conditions.** Keep the air-charge
  recipe, quantify its missing grid coverage, and compare density-aware
  alternatives on multiple independent low/high-pressure and temperature
  sessions. Record units and calibration dependence of raw DID counts. Missing
  pressure/IAT is an unavailable input, not evidence of stability.
  *Measured 2026-09-23; the pressure comparison is data-blocked.* On the
  current calibration:
  - Air charge (raw DID counts, scale unpinned, calibration-dependent; grid
    edges carried from the ZS44A-k106 prior): 41 of 49 cells populated,
    held-out MAE 1771 counts against a 1500 target. Fails.
  - Air mass and ambient VE: MAE 0.125 g/cycle and 3.17 %, relative
    resolution (MAE / warning width) 0.45 vs 0.40. Both fail B02.
  - Test rows span baro **79–82 kPa** and IAT 20–58 °C.

  **Remaining:** independent drives below 76 kPa (none on ZS44A-20260920).
  Until then, density normalization is not validated across pressure
  ([report](../../../data/analyses/2026-09-23-behavior-refit-validation-report.md) §4).
- [x] **B04.7 — Run the airflow ablation on identical observations.** Compare
  air-mass, VE and their combination using the intersection of eligible rows;
  separately report each model's exclusive coverage. Include no-fault controls
  and explicitly synthetic single-sensor/common-input and airflow-change
  cases. Count unique detections, misses and delays, not just output correlation
  or aggregate alert reduction. Label common-input blind spots honestly.
  *Done 2026-09-23* on the candidates' shared test drives. Coverage: 7139
  rows both; 99 air-mass-only (VE actual stale 51, off-map 48); 3 VE-only.
  No-fault control: 0 events for each on the intersection (2.47 h).
  Input-level synthetic cases, 142 clean 60 s windows each:

  | case | air mass | VE | VE-unique | OR / AND |
  |---|---|---|---|---|
  | MAF −10 % (common input; unmetered air looks the same) | 33 (delay 23 s) | 22 (24 s) | 0 | 33 / 22 |
  | baro −3 kPa (VE-only input) | 0 | 0 | 0 | — |
  | IAT +10 °C (VE-only input) | 0 | 0 | 0 | — |

  Blind spots: air mass cannot see baro/IAT errors by construction; VE's
  ~7.9 % warning width is wider than the 3.4–3.9 % such errors produce;
  neither separates MAF bias from unmetered air. Evidence: [report](../../../data/analyses/2026-09-23-behavior-refit-validation-report.md) §4,
  `2026-09-23-behavior-refit-comparison.json` `b04_7_air_mass_ve_ablation`.
- [~] **B04.8 — Make the conditional retirement decision.** Demote the
  air-mass alert only if validated VE/context coverage preserves useful unique
  sensitivity under the predeclared B02 criteria. Otherwise retain it with
  corrected context or diagnostic-only status and name the unresolved evidence.
  Check fusion grouping so transforms of the same MAF/RPM evidence are not
  treated as independent confirmation. Publish through B03 and link the
  behavior-side evidence back to D03.20.
  *Decided 2026-09-23: air mass is not retired.* VE adds **0** unique
  detections (B04.7) and fails B02, so no validated replacement exists. Air
  mass also fails B02 (MAE 0.125 > 0.08 g, synthetic detection 0.25) and
  stays diagnostic-only (promoted as part of the 2026-09-23 diagnostic-only
  refresh, B03.9).
  **Fusion grouping fixed 2026-09-23:** `FusionScorer.behavior_source` now
  takes the graded `channel` and derives the group itself
  (`src/alerts/fusion.py::behavior_fusion_group`). Air mass and all VE
  channels vote as one `behavior:airflow` voice, and the caller can no
  longer pass a per-channel group. Evidence:
  `tests/test_fusion.py::test_one_measurement_is_one_voice_across_its_derived_channels`.
  The behavior-side evidence is linked into D03.20 (2026-09-23 paragraph).
  **Unresolved:** the decision must be re-run with a < 76 kPa drive on the
  current calibration.

  [Report](../../../data/analyses/2026-09-23-behavior-refit-validation-report.md) §4.

**Done when:** timing has either demonstrated useful resolution or remains
explicitly exploratory, and airflow retention/removal is supported by an
independent-drive, same-observation ablation. Fewer alerts alone is not success.
