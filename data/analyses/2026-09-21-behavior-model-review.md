# Behavior-model accuracy review — 2026-09-21

Reviewed the eight models in `data/nissan-xterra-2008/behavior_models.yaml`,
the 23 recipes in `src/analysis/refit.py`, the fit/serve paths, and local
recordings. Code inspected at `427c18a`, with the unrelated capture-profile
change `2c709b5` landing during the review. No model, recipe, or runtime code
was changed, refitted, published, or deployed.

Verdict: keep the behavior-model approach, but do not treat the current set
as eight validated engine-health detectors. Air charge has the strongest
predictive evidence. Warm idle is a useful simple baseline. Timing needs
better conditioning and much tighter demonstrated resolution. Retire the
standalone RPM/IAT timing alert and quarantine the legacy catalyst alert
pending validation. Prefer fewer validated relationships over more surfaces.

## Method and limits

- The Pi's compact status endpoint at `192.168.137.2:8000` was unreachable,
  including a network-enabled retry. Current `vehicle_state`, deployed model
  identity, overrides, and emitted alerts therefore were not verified.
- All analysis ran on the laptop. Selected stored OBD/DID/memory rows and
  the three stored decoded mode inputs (RPM, coolant, wheel speed), plus GPS
  speed, were loaded. DID decoding, quality gates, 1 Hz resampling,
  segmentation, derived channels, and model evaluation use existing code.
  Raw CAN was not re-decoded. This is an offline operating-point evaluation,
  not an exact replay of live polling or an acquisition-time signal-binding
  experiment. No novel raw-channel identity was inferred.
- Evaluated only rows with OBD RPM >= 400, the model's mode where required,
  present inputs, and no dependency gap flags. Coverage below means the
  fraction of those fresh-input rows on which the surface can evaluate;
  missing inputs and off-map results are not counted as successful predictions.
- Reproduced the July 26 held-out R², coverage, and warning/critical rates
  exactly for the five OBD-backed 2D models, and the idle baseline's held-out
  sample count/rates. This checks the evaluator against a known answer before
  applying it to September recordings.
- The September drives are later than training, but are not independently
  labelled healthy/faulty. Reported warning/critical fractions are per-row
  band exceedances, **not false-positive rates or emitted event counts**.
  Adjacent seconds are correlated; no confidence intervals or fault-detection
  recall are claimed. Old July fits also precede calibration changes.
- 147 existing tests passed: ground truth, behavior schema/decoder/detector,
  refitting, and the refit CLI. A separate controlled pipeline probe exposed
  a gap those tests do not cover, described below.
- Reproduction helper: `/tmp/canary_behavior_review.py`. Per-session JSON,
  per-model residual CSVs, and intermediate frames are under
  `/tmp/canary-behavior-review-with-modes/`. These are temporary local review
  artifacts; the measurements and interpretation are retained here.

Sessions used for the reported results:

| Label | Local session | Purpose |
|---|---|---|
| J26 | `sessions/nissan-xterra-2008/2026-07-26T225058` | Reproduce persisted holdout metrics |
| S17 | `sessions-0917/_capture-tuning-hybrid-research-2026-09-17T184821` | Later hybrid capture |
| S18 | `sessions-0918/2026-09-18T134345` | Cold start and higher altitude; baro 71–73 kPa, IAT 13–21 °C |
| S21 | `sessions-0921/_capture-fuel-target-research-2026-09-21T195844` | Latest pulled capture, after September calibration edit |

All paths above are beneath `/home/dan/canary-data/`. A May training session
was also inspected as a historical control; it is not independent evidence
for the catalyst model and is excluded from the conclusions below.

## Model decisions

| Model | Evidence | Recommendation |
|---|---|---|
| `air_charge_vs_rpm_throttle_did` | Persisted holdout R² 0.872, n=1,180. S17 R² 0.836, n=826, coverage 67.0%, MAE 2,317 raw counts. S21 R² 0.781, n=1,011, coverage 87.5%, MAE 2,029 counts. | Keep. Best-supported surface; expand independently validated coverage and condition/check air density. It is not a fuel-delivery measurement or unique mechanical-fault diagnosis. |
| `warm_idle_rpm_baseline` | Baseline 762.5 rpm; warning ±75 rpm. S18 n=154, MAE 13.6 rpm, 95th-percentile absolute error 25 rpm, no exceedances. | Keep. Negative R² is not a reason to reject a constant robust baseline. Validate across accessory loads; the `idle` gate does not cover every high-idle/roughness condition. |
| `timing_advance_vs_rpm_load` | J26 R² 0.356, n=57. S17 R² 0.328, MAE 4.80°, n=1,084, coverage 87.9%. S21 R² 0.279, MAE 6.71°, n=1,009, coverage 84.6%. Median warning half-width ~24° on both September captures. S18 performs better (R² 0.714), demonstrating dependence on operating mix. | Retain as exploratory visualization, rework before relying on subtle timing/knock alerts. Fit within calibration, separate transient/steady and temperature/load contexts. An unexpected timing residual does not identify knock. |
| `timing_advance_vs_rpm_iat` | J26 R² −0.046, MAE 8.80°, median warning half-width 31.3°. Stored IAT domain 29–68 °C; zero evaluable rows on S17 (1,234 fresh input rows, IAT 20–22 °C) and S18 (437 fresh rows, IAT 13–21 °C). | Remove as a standalone alerting surface. IAT should condition a load-aware timing model; RPM/IAT alone omits load. Preserve historical artifacts for comparison. |
| `fuel_trim_vs_rpm_load` | STFT B1 only. J26 R² 0.178, n=47. S18 R² −3.95, MAE 6.51 percentage points, median residual −6.33 points, n=142. No closed-loop-only gate in this recipe. | Replace its primary diagnostic role with total-trim B1/B2 and bank-balance models. STFT alone mixes feedback activity, adaptation, and fuel states. The observed shift is not proof of a fuel fault. |
| `air_mass_per_cycle_vs_rpm_load` | S17/S21 R² 0.736/0.756. On S18, 33 of 113 evaluable rows exceed bands, mostly idle/transient idle. | Candidate to demote from alerts once normalized VE has adequate coverage and an ablation shows no unique fault sensitivity. Keep the raw quantity available for interpretation. |
| `ve_vs_rpm_load` | J26 R² 0.551. S18 R² 0.661, MAE 4.71 percentage points, n=114. On the 113 identical timestamps evaluable by both air models: 27 air-only exceedances, 6 shared, zero VE-only. Missing pressure inputs prevent evaluation on S17/S21. | Prefer as the normalized airflow model, conditionally. One high-altitude session supports reduced nuisance sensitivity, not proven superior fault recall. Preserve pressure-source provenance: MAP-normalized and baro-normalized values are different physical references. |
| `cat_warmup` | May 25 artifact, no held-out validation. S18 genuine cold-start inputs: n=34 fresh warmup rows, R² −0.737, MAE 55.0 °C; 25 warnings + 1 critical = 76.5% exceedance. Persisted bands 30/80 °C versus recipe's newer 45/115 °C. | Remove current artifact from trusted alerting pending sensor/decoder and contextual validation. Temperature trajectory alone does not establish catalyst conversion efficiency. Keep the channel and warmup-analysis capability. |

The airflow comparison above counts both models on the same rows, rather
than comparing rates from different operating-point populations. Neither
surface should be treated as an independent instrument: VE is calculated
from the same MAF/RPM mass used by the other model. The calculation in
`src/analysis/derived.py` prefers MAP, falls back to baro, and can substitute
25 °C when IAT is absent. Model provenance/eligibility should distinguish
those cases rather than silently mix definitions.

## Correctness findings to address before adding models

1. **Steady-state-gated models cannot evaluate through the normal pipeline.**
   `src/analysis/refit.py:1109` persists `__roc` conditions for the cam recipes.
   `src/preprocessing/feature_pipeline.py:882` evaluates behavior deviations
   before `compute_features` creates those derivative columns. The live
   consumer receives `operating_frame`, which also lacks them
   (`src/preprocessing/live.py:126`). A controlled 40-second constant-RPM
   test yielded ROC=0 in the feature frame, expected=10 when given that
   feature row, but expected=None from the live operating row and zero
   nonempty behavior deviations. This affects future conditioned models;
   the eight saved models currently have empty condition lists. Build the
   required derivative context before evaluating behavior and test the
   complete fit-to-live path.

2. **Publication/carry-over gates do not establish predictive usefulness.**
   `src/analysis/refit.py:1158` checks 2D cell coverage, not held-out predictive
   skill. `_validate_held_out` measures R² but only gates on coverage and
   warning/critical fractions. Broad bands can pass while resolving little.
   `_check_publish_gate` returned success for both the negative-R² timing/IAT
   artifact and the catalyst artifact with zero held-out samples. The latter
   falls back to training R²; `refit_all` can carry it forward unchanged.
   Require appropriate held-out evidence, physical-unit error and useful
   sensitivity against a simple baseline, with separate criteria for constant
   robust baselines. Do not apply an arbitrary universal R² threshold.

3. **Several fit-time conditions do not survive into serving.**
   Closed-loop-only filtering for total trim is at `src/analysis/refit.py:955`;
   only rate-of-change conditions are persisted at line 1109. Cold-start
   eligibility and initial-temperature requirements similarly live in fitting,
   not in a complete serving contract. Before activating these recipes,
   persist/enforce their required fuel state, cold-start context, freshness,
   and applicable engine state. A mode name alone is not a cold-start history.

4. **Holdout selection is too narrow and not chronological by contract.**
   `src/analysis/refit.py:1060` sorts session strings and holds out the last
   one. Capture prefixes can choose a different session than parsed start
   time would; virtual `#segment` sessions can also exaggerate independence.
   The July models share one short holdout with as few as 47–79 covered rows.
   Use physical-drive-grouped folds, parsed session times, several temperature/
   pressure regimes, and calibration boundaries. Seven of the eight saved
   fits have no recorded tune revision. Old-to-new residuals cannot be assigned
   to deterioration without accounting for known calibration changes.

## Additions and consolidation

Already implemented as recipes, but absent from the saved eight-model set:

- **Total fuel trim B1/B2 plus bank balance:** highest priority replacement
  for the STFT-only surface. Validate closed-loop steady operation and persist
  that gate. Current `TOTAL_TRIM` is an additive STFT+LTFT index; label it as
  such rather than claiming an exact injector multiplier.
- **Cam command tracking for the two measured channels:** valuable once the
  derivative-serving bug is fixed. Compare each measured angle with command
  under steady conditions, then separately characterize response delay. Keep
  physical bank assignment unspecified until independently established.
- **TPS track agreement:** useful independent-track consistency model; validate
  same-acquisition pairing, including KOEO sweeps. High correlation alone is
  insufficient. Existing recipe needs no new model architecture.
- **Coolant warmup / cold-start idle:** worth completing after collecting enough
  independent cold starts and representing initial temperature and load/ambient
  context. Neither currently has a published artifact.

The most useful genuinely new behavior relationship is **commanded-versus-
measured lambda delivery error**, building on existing `delivery_error`
analysis and the verified RAM target. Convert commanded phi to lambda using
its verified convention, exclude fuel-cut sentinels, account for transport
delay or require steady targets, and separate closed-loop from enrichment.
This is preferable to a raw AFR-voltage-vs-RPM/throttle surface that cannot
tell an intentional target change from a delivery error. Target/actual lambda
comparison is also the basis of the manufacturer's documented
[Hondata lambda overlay](https://hondata.com/help/kmanager/lambdaoverlay.htm).
That external reference supports the method, not validation of this Nissan's
specific sensors or thresholds.

A conditional cam command/response approach is consistent with the desired-
versus-actual position error described in
[Ford's VCT documentation](https://www.fordservicecontent.dealerconnection.com/pubs/content/~WVX2/~MUS~LEN/15/VX21028.HTM).
Again, Nissan-specific identity and response tolerances must come from this
project's own verified data.

Suggested order: fix the fit/serve contracts and validation gates; demote the
two weak legacy alerts; refit a calibration-consistent baseline with multiple
held-out drives; activate total trim/bank balance and cam/TPS consistency;
then promote lambda delivery error after lag-aware validation. Evaluate any
removal by unique detections on identical rows and injected/labeled faults,
not by correlated outputs or a quieter dashboard alone.
