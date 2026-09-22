# B02 — reproducible behavior validation and publication gates

**Depends on:** B01 for final serving parity; evaluator/corpus inventory can
start immediately. Follow [B00](B00-behavior-models-README.md).

The existing holdouts reproduced, but some cover only 47–79 rows. A 2D model
can pass with negative predictive R², and a legacy 1D artifact can pass on
training R² with no held-out samples. Quiet alerting is not proof of accuracy.

## Checklist

- [ ] **B02.1 — Make the review reproducible from the repo.** Turn the
  temporary review evaluator into a bounded offline command using shared
  preprocessing/evaluation code. Inputs explicitly name profile, config,
  artifact and sessions; output records their hashes/versions and selection
  rules. Do not rely on `/tmp` helpers or an implicit local config. Check mode
  taxonomy against current operator-approved config (including the existing
  decision to fold `suburban` into `city`); distinguish historical reproduction
  from evaluation with corrected configuration.
- [ ] **B02.2 — Pin known answers and counterexamples.** Preserve a suitably
  sized real-data fixture or reproducible evidence manifest for the July
  holdout reproduction and September failures. Verify the ground-truth chain
  first. Include off-map cooler-IAT data, legacy catalyst evidence absence,
  a broad-band poor predictor and a useful constant idle baseline. Do not
  relabel unverified September exceedances as false positives.
- [ ] **B02.3 — Split by independent physical drive.** Replace string-sorted
  last-session holdout with explicit grouped train/calibration/test splits.
  Use parsed timestamps and retain parent-drive identity across captures and
  `#segment` pieces. No pieces of one drive may cross folds. Fit bin edges,
  normalization, context selection and thresholds from training/calibration
  data only; final test drives cannot choose the winning model or bands.
- [ ] **B02.4 — Enforce provenance and adequate evidence.** Validate tune
  boundaries, source/decoder identity, mode labels and capture recipe grants.
  Unknown revisions and sessions crossing a flash need a named resolution,
  not an assumed calibration. Require multiple independent validation drives
  for an alerting model; record per-recipe minimum drives, eligible observations
  and operating-envelope coverage. If the corpus cannot meet them, report the
  missing evidence and retain diagnostic-only status.
- [ ] **B02.5 — Report denominators and practical accuracy.** Per model,
  drive and relevant regime, emit input-present/fresh/eligible/evaluated counts,
  refusal reasons, coverage, MAE, signed bias, error quantiles, R² where useful,
  and warning/critical half-widths in physical units. Report actual event rates
  after persistence/deduplication separately from per-row bands. Use drive-level
  uncertainty where supported; do not treat adjacent seconds as independent.
- [ ] **B02.6 — Gate on useful skill and sensitivity.** Require predictive
  surfaces to improve on an appropriate training-fitted simple baseline on
  untouched drives and resolve a declared practically meaningful deviation.
  Give robust constant baselines their own error/coverage/sensitivity criteria;
  never impose a universal R² floor. Specify each criterion and physical noise
  floor in recipe/profile data before final testing. Include no-fault controls
  and explicitly labelled synthetic fault magnitude/duration cases; distinguish
  synthetic sensitivity from real fault recall.
- [ ] **B02.7 — Apply gates to every publication route.** Fresh fit, partial
  refit, carry-over, model load/hot reload and promotion must agree about
  trusted versus diagnostic-only artifacts. Missing held-out evidence cannot
  fall back to training R² for alerting. Reject/flag incompatible contract,
  decoder or context changes even if the training-row hash is unchanged.
  Preserve old evidence instead of rewriting historical validation stamps.
- [ ] **B02.8 — Test the actual failures.** Assert refusal of the broad-band
  useless surface, missing holdout, leaked drive fragments and prefix-sorted
  chronology. Assert the constant idle model can pass appropriate criteria.
  Exercise CLI report output, partial-refit carry-over and hot reload, plus
  B01's no-prediction versus valid-observation distinction.

## Code and evidence

Start at `fit_recipe`, `_validate_held_out`, `_check_publish_gate`, `refit_all`
in `src/analysis/refit.py`, `scripts/refit_behavior_models.py`,
`src/decode/behavior_models.py`, `src/models/promotion_gate.py` and
`src/control/sessions.py`. Reuse existing evaluation/report mechanisms where
they fit; do not create a second publication system.

**Done when:** a reproducible report shows accuracy, applicability and useful
sensitivity, and all publication/carry-over routes enforce the same evidence
contract. Insufficient data remains an explicit incomplete validation item.
