# A04 — Isolation Forest defects and what to replace it with

**Depends on:** A02 for evaluation. A06.1 decides whether the IF keeps
alerting. Correctness fixes (A04.2, A04.6, A04.8, A04.9, A04.12) are worth
landing whatever that decision is, because a non-alerting IF still writes
evidence. Model-changing items take effect at the next retrain (deferred by
the operator on 2026-09-22). Follow [A00](A00-anomaly-detectors-README.md).

Reviewed artifact: `models/nissan-xterra-2008/isolation_forest/`, created
2026-09-20 from 19 sessions. It has 295 features and one model per
operating mode, each using 173–255 of them (`unknown` uses 20). This is not
the 88-feature July artifact the D02.5 blindness counts refer to. A02.11
confirms which artifact the Pi runs.

## Checklist

- [ ] **A04.1 — Put single-quantity sensitivity in the harness as a known
  answer.** Shifting one quantity on held-out, unflagged rows flagged **0 %**
  of rows at every size tried:
  - Shift sizes: +3, +10, +30 and +100 standard deviations of the mode.
  - Shifted quantities: STFT_1 alone, the fuel-trim family, and all four
    coolant sources (raw plus `__mean`).
  - Sample: city n = 917, highway n = 1,585.
  - Score movement: +0.002 to +0.007, at most 0.15 of the held-out spread.
    The threshold sits about 2.4 spreads above the median.
  - Control: shifting every feature by 3σ flags 100 %, so the harness works.

  Mechanism: `max_samples` = 256 caps trees at 8 levels, so with ~250
  features any one feature lies on about 3 % of a point's path. A value
  beyond the training range follows the same branch as the training maximum,
  so size stops mattering. This matches the July benchmark (precision
  0.006–0.008 at 214 alerts/h) and the 2026-09-20 one (26 of 250 injected
  faults caught).
  - Add the shift test to A02.4 as a known-answer case every candidate
    detector must pass or be recorded as failing.
- [ ] **A04.2 — Attribute over the scoring model's own features, by effect on
  the score.** `_rank_feature_contributors` (`src/daemon/inference.py`)
  ranks |z| over `engine.feature_columns`, the 295-column union
  (`src/alerts/emitter.py`), not the mode's feature set.
  - The top-1 name was not a feature of the scoring model on 60 of 62 city,
    52 of 62 highway and 39 of 60 idle_transient rows.
  - Restricted to the mode's own features, the |z| top-1 matched the
    occlusion top-1 on only 0–7 of ~61 rows.

  Rank per mode by occlusion: replace each feature with the mode's median
  and score all variants in one batched `decision_function` call. Or use
  `shap.TreeExplainer`, which supports `IsolationForest`.
  - **Test:** every reported contributor belongs to the scoring model.
- [ ] **A04.3 — Stop quarantine from reducing training to the median
  moments.** Quarantine sets cells to NaN (`scripts/retrain.py`). Its
  6 × MAD screen quarantines every non-median value wherever MAD = 0
  (`training_disposition.py`); accelerator pedal at rest is the example. The
  IF then drops any row with a NaN in its features. Measured on real
  sessions:
  - 4.5 % of cells are quarantined, but 71.6 % of rows are lost.
  - Usable rows fall: city 69 % → 12 %, highway 80 % → 40 %,
    idle_transient 96 % → 54 %.

  That is the "train only on moments where everything sits at its median"
  trap `online-training-strategy.md` warns against.
  - Handle MAD = 0 explicitly: use a floor, or exempt the channel.
  - Never let a quarantined cell remove a whole row silently; record rows
    lost per mode per step in the manifest.
  - Put the calibration frame through the same feedback-label and
    quarantine policy as training, or record why not. Today it skips both,
    so thresholds include operator-labelled anomalies.
- [ ] **A04.4 — Record and enforce per-mode provenance.** Greedy joint-
  completeness feature selection keeps only sessions that polled every
  selected channel, so each mode quietly narrows:
  - city: 2,174 rows from 11 sessions, all on 2026-09-18;
  - highway: 1,348 rows from 7 sessions, all on 2026-09-18;
  - warmup: 192 rows from two August sessions.

  The header still says 19 training sessions. The idle (154 rows) and warmup
  (328) calibration holdouts come entirely from one session, `134345`, at
  71–73 kPa against a training median of 79.
  - Record per-mode training and calibration sessions and dates in the
    manifest.
  - Enforce `MIN_CALIBRATION_SESSIONS` per mode, not only globally, and
    refuse a mode model resting on fewer independent drives than the
    configured minimum.
- [ ] **A04.5 — Make feature selection survive polling mix, and count
  blindness per mode.** On `134345` no spec column is absent, yet the IF
  withholds 99.7 % of city rows, 83.5 % of idle_transient and 100 % of
  unknown. `derived:STFT_BANK_BALANCE` (4 variants) and `TOTAL_TRIM` are NaN
  under that session's polling. With 4 variants per channel, three missing
  base channels blind a 255-feature model.
  - `structural_blindness` (`feature_spec_health.py`) divides by the global
    295, but withholding is decided per mode. Compute it per mode.
  - Choose features so a common polling profile cannot blind a mode, or
    state in the manifest which profiles a mode needs.
- [ ] **A04.6 — Enforce the feature policy at load, and treat sentinels as
  states.** 83 of the 295 features come from `model_feature: false` bindings
  (TPMS, steering, brake, `MOTION_SIGNAL_*`, wheel-speed pairs, HVAC). 44 come
  from `hypothesized` bindings. The policy (`a4a58a4`) runs only at training;
  `manager.py` and `test_manifest_producibility.py` do not check it. These
  columns dominate the attribution.
  - TPMS reads 0 (sensors not yet transmitting) on 35 % of `134345`,
    including every warmup row. A pooled scaler with μ = 100.6, σ = 56 treats
    that sentinel as a measurement.
  - Check the policy at load, and name violations in `/status`.
  - Decode sentinels to a state channel, per AGENTS.md's "a sentinel is a
    state, not a number".
- [ ] **A04.7 — Impute per mode, or not at all.** One scaler is fit over all
  modes, so `fillna(0)` imputes the *pooled* mean. Against each mode's own
  spread, that mean sits a median 9.4 standard deviations away for idle
  features (idle_transient p90 16.5). Imputing 5 % of features raised the
  mean score in 3 of 4 modes; per-mode medians lowered it.
  - Fit the scaler per mode, or impute per-mode medians.
  - Correct the docstring that says imputation pushes toward normal.
  - Low impact today only because of A04.1.
- [ ] **A04.8 — Do not serve context-only mode models.** The artifact carries
  an `unknown` model over 20 context features (coolant, IAT, baro, GPS
  speed), thresholded on 91 rows at a 1.1 % achieved flag rate. Scoring
  routes to it, and the loader does not reject it; the comment saying it is
  "withheld at serve anyway" is false for this artifact.
  - Refuse such models at training and at load.
  - **Test:** such an artifact is refused.
- [ ] **A04.9 — Fix the offline IF evaluation.** `isolation_forest_evaluation.py`
  scores against the sklearn `contamination` label, counts withheld rows
  (label −1) as detections, and recommends contamination values. None of
  that reflects what is served: emits use the calibrated threshold, and
  `contamination` only sets `offset_`. Evaluate at the served threshold,
  count withheld rows as withheld, and drop the contamination
  recommendation, or fold the module into A02.4.
- [ ] **A04.10 — Choose hyperparameters by measured stability.**
  `n_estimators` = 200 is seed-sensitive. Refitting the same highway rows
  with seeds 0–3 flagged 147–214 rows (pairwise Jaccard 0.68); 1,000 trees
  gave 0.86. `max_samples` and `max_features` are sklearn defaults, never
  tuned.
  - Pick the ensemble size by seed stability on A02's evaluation drives.
  - Record seed-to-seed agreement in the manifest.
- [ ] **A04.11 — Count exceedance episodes, not rows.** The rule of at least
  10 exceedances per calibration treats 1 Hz rows as independent, despite
  30 s rolling windows. On `134345` the warmup top 1 % was 4 rows forming 1
  episode. Count clustered exceedances as one episode for both calibration
  minimums and alert rates.
- [ ] **A04.12 — Record the calibration actually used.** The manifest
  records `calibration_holdout_fraction` 0.2 while external calibration forced
  it to 0 (`isolation_forest.py`). Record the effective values.
- [ ] **A04.13 — Evaluate a replacement for the multivariate-outlier role.**
  If A06.1 keeps any multivariate outlier detector, the candidate is per-mode
  robust z or Ledoit-Wolf Mahalanobis over 10–20 curated physics residuals
  and core engine channels. Calibrate it per mode on held-out drives and
  count clustered exceedances as one event.
  - Note: the reviewed artifact has **zero** `deviation:*` columns
    (`behavior_model_columns: []`), so the docs' reliance on residual
    features to cover IF's axis-parallel limit does not hold today.
  - A naive max-|robust z| over the same 249 raw columns needed a threshold
    of ~80 and caught only a +20σ STFT shift. The gain comes from curating
    the inputs, not from swapping the algorithm.

## Acceptance

Correctness items land with tests. Every model-changing item is judged on
A02's harness against A02.11's baseline, per drive and on the same rows.
A04.13 is an experiment whose result feeds A06.1, not a commitment to ship a
new detector.
