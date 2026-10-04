# B05 — activate trim, bank-balance, cam and TPS relationships

**Depends on:** B01/B02; B03 supplies artifact lifecycle/publication.
These recipes already exist in `default_recipes`; inspect and extend them
rather than building a second model system. Follow
[B00](B00-behavior-models-README.md).

## Fuel trim and bank balance

- [x] **B05.1 — Inventory viable inputs per recipe.** Report fresh, paired
  STFT/LTFT for both banks, fuel status, RPM/load and required thermal context
  across independent drives. Validate signal units/identities before fitting.
  Keep missing-bank coverage distinct from zero bank difference.
  *Done 2026-09-22:* `src/analysis/behavior_input_inventory.py` +
  `scripts/inventory_behavior_inputs.py` count, per session and per recipe,
  through the recipe's **own serving contract**: channels never recorded,
  stale rows, named contract refusals and eligible rows, then group sessions
  into physical drives and count qualifying independent drives. Per-bank facts
  are separate (`paired_b1`/`paired_b2`/`paired_both`/`missing_bank`), and a
  missing bank is never reported as a zero bank difference. Run over 134
  unique local sessions / 53 drives: total trim accepts 10,082 (b1) and 9,542
  (b2) rows over 30 drives, 11 of them with ≥30 rows; both banks are paired
  on 23,524 running rows (19,949 of them closed loop, 946 in `warmup` mode
  and now excluded), exactly one bank on 7,399, and the STFT bank difference
  is exactly zero on 5,476 — three numbers that were previously one.
  `ve_manifold_vs_rpm_load` has **zero** eligible rows in 134 sessions (no
  MAP sensor: unavailable, not substituted). Two cautions recorded: a
  resampled row is not an observation (a DID sample stays fresh up to 2 s —
  `tps_sensor_1_vs_sensor_2_did`'s 1,920 "rows" are ~70 real pairs, §6), and
  **the only densely-polled trim captures (2026-09-21, ~2.2 Hz) have every
  LTFT sample at exactly 0.0** after the 09-20 flash, so a total-trim fit
  there would be an STFT fit under another name. Units validated: all trim
  samples lie on the 100/128 % lattice inside the ±25 % clamp
  (`trim_unit_checks`, which reports an all-zero channel separately from an
  off-lattice one). PID 0x03's two fuel-system states disagree on 0.99 % of
  the 77,797 payload-carrying samples across 137 sessions. Tests:
  `tests/test_behavior_trim_contracts.py`. Evidence:
  [report §5](../../../data/analyses/2026-09-22-b04-b05-measurements.md).
- [x] **B05.2 — Enforce fuel-state semantics.** Apply B01's persisted
  closed-loop and steady-state requirements to total-trim fits and serving.
  Exclude or separately model warmup, enrichment and fuel cut. The current
  `TOTAL_TRIM` is an additive percentage-point index; retain that label unless
  an independently justified calibration-specific multiplier is implemented.
  *Done 2026-09-22 (contract only; no fit):* both total-trim recipes persist
  closed loop = J1979 `0x02` only, the new contract field
  `excluded_operating_modes = ["warmup"]` (refusal
  `operating_mode_excluded:warmup`; an unlabelled mode is refused as
  `operating_mode_unknown`, never assumed warm), and steady-state gates
  `obd:RPM ≤ 10 rpm/s` / `obd:ENGINE_LOAD ≤ 0.5 %/s` measured on the dense
  2026-09-21 capture (the cam recipes' 75/5 passed 724 of 801 closed-loop
  rows, 443 locally transient, STFT σ 3.47 %; 10/0.5 passes 247, 74 locally
  transient, σ 1.16 %). Label kept: `output_unit` is now
  "percentage points (additive STFT+LTFT index)", matched in
  `behavior_validation.yaml`, with "an additive index, not a calibrated
  injector multiplier" in the recipe limitations. Two named gaps, both
  recorded rather than papered over: a rate gate is a **net-change** gate over
  the 30 s feature horizon (an up-and-back excursion passes: 74 of 247 rows),
  and **bank 2 is gated on fuel system 1's state** because `decode_fuel_status`
  keeps only PID 0x03 byte A — the fix is at collection, and the stored
  payloads can backfill the corpus. Tests:
  `tests/test_behavior_trim_contracts.py`. Evidence:
  [report §4](../../../data/analyses/2026-09-22-b04-b05-measurements.md).
- [~] **B05.3 — Fit total-trim B1/B2 and useful bank-balance baselines.**
  Use existing recipes and consistent calibration cohorts; compare robust
  simple baselines against surfaces using B02. Test bank-specific changes,
  symmetric changes, normal STFT oscillation and LTFT adaptation. Count two
  banks/common ECU corrections appropriately in fusion; do not claim a bank
  asymmetry identifies a particular injector, leak or sensor by itself.
  *Fitted 2026-09-23 on ZS44A-20260920; none qualifies.* STFT bank-balance
  criteria were declared in `behavior_validation.yaml` before the refit
  (constant baseline, 5-point target, max MAE 2.0, p95 3.75). Results on
  untouched drives ([report](../../../data/analyses/2026-09-23-behavior-refit-validation-report.md) §2, §5):
  - *Total trim B1/B2:* skill 0.11 / −0.01 < 0.2, warning width 12.1 / 10.7
    > 5, symmetric +5 %/120 s detection 0.04 / 0.07. Failed.
  - *STFT bank balance city/highway:* MAE 0.94 / 1.16 %, p95 2.49 / 3.13,
    bank-specific ±5 % steps detected 0.93–0.96, so they meet the accuracy
    and sensitivity criteria. Normal STFT oscillation still raises 2.86 /
    13.9 control events/h (> 2). Failed.
  - *STFT bank balance idle:* bias +1.77 %, held-out warning rate 18 %. Not
    published.
  - *LTFT:* **every LTFT sample on this calibration is exactly 0.0**, so the
    total-trim fits are STFT fits under another name, LTFT bank balance
    cannot fit (zero-width domain), and LTFT adaptation is untestable.

  **Fusion (done 2026-09-23):** total trim B1/B2, STFT/LTFT per bank and
  both bank balances vote as one `behavior:fuel_trim` voice
  (`src/models/fusion.py::behavior_fusion_group`, derived inside
  `FusionScorer.behavior_source`). Before, `fuel_trim_vs_rpm_load` (STFT B1)
  and `total_trim_b1` (STFT B1 + LTFT B1) were separate groups over the same
  reading. Evidence: `tests/test_fusion.py::test_one_measurement_is_one_voice_across_its_derived_channels`.
  **Persistence rule (desk, 2026-09-26).** The 09-23 city events were all
  runs of at most 18 warning ticks just past the 3.1-point band, on both folds
  (training 5 in 0.81 h, test 5 in 1.75 h, peaks 3.1–5.7). One of them was
  warmup trims washing out of the 30 s mean. The daemon's 3-of-5 gate latches
  on every such run.
  - *Rule.* `BandExcessPersistence` (`src/decode/behavior_models.py`) is a
    two-sided CUSUM of the residual's excess over the warning half-width, per
    evaluated tick. The model is sustained while that sum is strictly above a
    limit. Refused ticks freeze the sum. A gap longer than 30 s restarts it
    (`STFT_BANK_BALANCE_SMOOTHING_SECONDS`, the balance's own memory).
  - *Fit.* The limit is fitted on the training fold only
    (`fit_band_excess_persistence`): the largest sum the training drives' own
    rows reach. It is persisted as `model.event_persistence`. A fold with no
    excursion refuses the recipe by name
    (`event_persistence_unfittable:no_training_excursion`).
  - *One arithmetic.* The daemon (`PersistenceGate.observe_band_excess` via
    `BehaviorDetector`) and the evaluator (`model_event_times`,
    `EVALUATOR_VERSION` 4) run the same code. The STFT balance recipes declare
    the rule; the LTFT ones keep the daemon gate.

  **Default-profile evidence only, enforced (2026-09-26).** This applies the
  recorded decisions: R03.12(a), and the standing rule that tuning captures
  never train, evaluate or score behavior deviations. They supersede the
  09-23 "captures included" choice for behavior models.
  - *Stamp.* `stamp_profile_exclusions` (`behavior_validation.py`) marks each
    row's session with R04.6's own `default_profile_exclusion`: a `_tuning-*`
    session, or a capture whose manifest says `corpus_eligible: false`.
  - *Refusal.* Stamped rows stay in the frame for drive grouping, so a
    bridging capture still keeps a drive whole. `evaluate_rows` and the
    refit's training rows refuse them as `profile_excluded:not_default_profile`.
  - *No silent inclusion.* A capture or tuning piece with no verdict is
    refused by name: `profile_unverified` in `fit_recipe` and `build_evidence`,
    and `profile_excluded:no_session_directory` when stamping.
  - *Refit CLI.* It stamps every frame and records `profile_exclusions` in the
    provenance. Its explicit-path grant and capture discovery reach
    corpus-eligible captures only. An explicit path to a tuning recording is
    named on stderr with its reason (`note: N session(s) are not
    default-profile evidence …`); when every path is one, the refit exits 1
    naming each, before building a frame.
  - *Evaluator.* `build_evaluation_frame` stamps by default. Only
    `evaluate_behavior_models.py --mode historical` opts out, with
    `profile_scope="every_recording"`, to reproduce numbers an artifact
    recorded before the rule.
  - *The guard is in the shared function.* `evaluate_rows` runs
    `profile_refusals` on every call, so fold metrics, synthetic sensitivity
    and transient controls cannot grade an unstamped capture. An unstamped
    frame holding a capture raises `UnverifiedProfileError`
    (`profile_unverified`). The keyword `profile_scope="every_recording"` is
    the only opt-out, and only `_historical` passes it. Before this change,
    the verifier's repro graded 2,561 capture rows of an unstamped real frame
    silently.
    Test: `tests/test_bank_balance_persistence.py::test_evaluate_rows_refuses_an_unstamped_frame_holding_a_capture`.
  - *Tests.* `tests/test_bank_balance_persistence.py::test_tuning_capture_rows_are_refused_by_name_in_training_and_held_out`
    shows a fit with stamped captures equals one without them (same training
    hash, same held-out rows). Also `::test_a_capture_with_no_directory_is_refused_not_assumed_eligible`
    and `::test_refit_cli_grants_nothing_to_a_non_default_capture`;
    `tests/test_refit_behavior_models_script.py::test_main_refuses_explicit_paths_that_are_all_tuning_recordings`
    and `::test_main_names_explicit_tuning_paths_and_never_trains_on_them`.

  **Held-out results, default-profile rows only.** Frozen 09-23 corpus, HEAD
  drive grouping; all 13 captures excluded. The frame first reproduced the
  published city/highway evidence exactly (2151 rows / 5 events, 1539 / 9).
  *Re-run 2026-09-26 (evening), from scratch:* frame rebuilt with
  `build_evaluation_frame`'s default stamping in a worktree of HEAD `cbad3aa`
  plus only the B05 files (so the uncommitted change-only CAN storage work
  did not reach it): 81,562 rows, 8,551 of them stamped
  `profile_excluded:not_default_profile` (10 captures with rows; 3 more have
  none), 13 min, 3.0 GB peak. Unstamped, it again reproduced 2151/5 and
  1539/9; stamped, the published models' own test folds shrink to 749 and
  163 rows (65 % and 89 % of them were captures). Every number below
  reproduced exactly, including the city training hash
  `sha256:2be84459f373ec4d`.
  - *City: `insufficient_evidence`.*
    - Train `2026-09-21T142126` + `2026-09-22T163415`: 430 rows, limit 0.52
      from a single excursion. That is a thin basis.
    - Test `2026-09-22T233855` + `2026-09-23T032755`: 515 rows / 0.39 h,
      MAE 1.46, bias +1.35, p95 3.00, width 4.00, 2 events.
    - Control exposure is under 0.5 h. With the default gate the result is
      the same: 2 events, `insufficient_evidence`.
    - Synthetic ±5 %/120 s: 1.00/0.90 (default gate 0.90/0.80).
  - *Highway: refused.* `event_persistence_unfittable:no_training_excursion`:
    one training drive, 322 rows. Under the default gate it fails anyway
    (synthetic 0.75/0.75, 0.09 h of control).
  - *Idle: refused by the declared rule.* 656 training rows never left the
    band. Under the daemon's default gate it would pass: 729 rows / 0.55 h,
    MAE 0.93, p95 2.80, 0 events, synthetic 0.87/0.80. Whether a fold with no
    excursion should fall back to the default gate is an operator decision.
    That option was noticed after seeing this test result, so it was not
    adopted here.
  - *Context: the captures-included runs this replaces.* On the 09-23 split
    city went from 5 test events to 0 in 1.75 h. On HEAD's split city
    `passed` (991 rows). 82 % of those held-out rows were tuning captures, so
    they are no longer evidence.

  The candidate was regenerated from default-profile rows only, and the
  capture-built one deleted:
  `data/nissan-xterra-2008/_candidates/2026-09-26-bank-balance-persistence/behavior_models.yaml`
  (gitignored; rewritten by the re-run, sha256 43104559…, identical to the
  first default-profile fit except its provenance note and timestamps). It
  holds `stft_bank_balance_city`, diagnostic-only, no capture piece in
  `fit.sessions`/`calibration_sessions`/split, and its evidence provenance
  lists the 13 excluded captures. It came from `fit_recipe` on the evaluator
  frame path, not the CLI. Still built from capture rows and **not** touched
  here (historical, or not B05's): `_candidates/2026-09-23/` (the 09-23
  report's candidate set), the two `_candidates/2026-09-23-warm-idle-*` sets
  (B07), and the committed published `behavior_models.yaml`, whose ten
  ZS44A-20260920 fits (all `failed`, diagnostic-only) list 6–10 capture
  pieces each. Tests: `tests/test_bank_balance_persistence.py` (gate,
  training-only fit, refusal, daemon/evaluator parity, profile enforcement).

  **Remaining:**
  - LTFT content (relearn) on this calibration (truck).
  - Default-profile exposure: at least 0.5 h more city, and more highway
    drives (truck).
  - The idle fallback decision above.
  - A CLI refit into `_candidates` before any B03 promotion (the 09-23 CLI
    refit took 1 h 17 min and 12.7 GB, over a desk session's budget).
  - Operator: what becomes of the capture-built artifacts listed above.
- [~] **B05.4 — Replace STFT-only alerting with evidence.** Compare qualified
  replacements and the old `fuel_trim_vs_rpm_load` on identical eligible rows
  and fault cases, including coverage outside the intersection. Demote the
  old alert after the replacement qualifies; keep raw STFT for inspection.
  If data is insufficient, expose that state instead of claiming replacement
  complete or silently continuing to present the old surface as validated.
  *Compared 2026-09-23; no replacement qualifies.* On the 1405 rows all three
  surfaces evaluate:

  | surface | MAE | bias | events | synthetic +5 %/120 s |
  |---|---|---|---|---|
  | published STFT v2 | 5.29 % | −3.98 | 17 | 0.00 |
  | candidate STFT v3 | 3.81 % | +0.37 | 2 | 0.03 |
  | total trim B1 | 3.48 % | +0.04 | 1 | 0.03 |

  Outside the intersection, STFT v3 has 4721 rows that total trim refuses:
  rate gates 3030, open loop/fuel cut 971, warmup 349. The old surface stays
  diagnostic-only (legacy, no contract, cannot alert); the candidate STFT v3
  also failed B02. Nothing is demoted or claimed replaced ([report](../../../data/analyses/2026-09-23-behavior-refit-validation-report.md) §5).
  **Remaining:** a qualifying replacement (B05.3).

## Cam command tracking

- [x] **B05.5 — Validate command/measured identities and pairing.** Reuse the
  catalogued command and two measured channels. Use acquisition-time comparisons
  and a known-answer control; keep measured-bank names neutral until independently
  established. Persist steady command/RPM limits and freshness via B01.
  *Done 2026-09-22 (no fit):* `scripts/validate_command_pairing.py` (new) runs
  a known-answer control, the pair under test on all rows and on a steady
  subset, a negative control and the per-sample ratio, all through
  `compare_channels` with an explicit `PairingPolicy`. Known answer:
  `obd:RPM → did:ECM_RPM` r²=0.9995, slope 0.996, n=292 (09-17T201000); in the
  Mode 22-only captures it refuses with "unavailable input" rather than
  returning a number. Negative control: commanded cam → `ECM_AF_SENSOR_V_B1`
  r²=0.05–0.12. Steady tracking (command held ≥ 5 s, RPM range ≤ 200), four
  captures: slope 0.967–1.022, 1:1 residual +0.11 … +0.94° with spread
  0.51–1.20°, per-sample ratio 0.97–1.04 with ±3.6–6.5 % spread, n=63–913 per
  pair. On all rows (transitions included) the same pairs read r² 0.90–0.96
  with ~3° spread and ±35–50 % ratio spread — response lag, not a tracking
  fault, and the reason the recipes gate on a steady command. Bank names stay
  neutral. Freshness/steady limits are already persisted on both cam recipes
  (`did:ECM_INTAKE_CAM_ANGLE_COMMANDED ≤ 0.25 °/s`, `did:ECM_RPM ≤ 75 rpm/s`),
  with the caveat recorded in B05.2 that a rate gate is a 30 s net-change
  gate. Evidence: [report §6](../../../data/analyses/2026-09-22-b04-b05-measurements.md).
- [~] **B05.6 — Fit and verify the existing cam recipes end to end.** Validate
  steady tracking residuals on independent drives, then inject stuck/offset
  response cases and ordinary command transitions. Prove a fitted artifact
  produces live and replay predictions with its derivative gates. Characterize
  response lag separately; transition lag is not a steady tracking fault.
  *Attempted 2026-09-23 — data-blocked on the current calibration.* Both cam
  recipes skip with "missing `did:ECM_INTAKE_CAM_ANGLE_COMMANDED__roc`". The
  commanded angle is polled in **no** ZS44A-20260920 session: the rom-trace
  captures poll only the two measured channels, and the tuning profile polls
  neither. That leaves 0 eligible rows (B05.1 inventory on the manifest
  corpus). **Smallest missing capture:** a normal drive polling
  `ECM_INTAKE_CAM_ANGLE_COMMANDED` with both measured channels in one round
  (as the 2026-08-06/-07 tuning-did captures did), across ≥ 3 drives. B05.5
  supplies the identities and steady subsets.

## TPS and AFR sensor balance

- [~] **B05.7 — Validate TPS track consistency.** Reuse
  `tps_sensor_1_vs_sensor_2_did`, with actual paired acquisition observations
  across KOEO sweeps and running conditions. Verify slope/offset, per-sample
  residuals/ratios where defined, resolution and negative controls. Demonstrate
  offset, stuck-track and dropout detection without confusing sequential-read
  skew during rapid movement with sensor disagreement.
  *Partial 2026-09-22 — blocked on a capture, not on a decision.* Same tool as
  B05.5. `did:ECM_THROTTLE_POS_SENSOR_2` is never polled densely: best local
  coverage 122 samples in one session, 71 in the next, **zero** in every
  high-rate capture. On `2026-09-18T213818` (known-answer control
  `obd:THROTTLE_POS → did:ECM_THROTTLE_POS_SENSOR_1` r²=0.995, slope
  0.0408 V/%, n=70) the tracks read r²=0.9897, slope 0.9918, offset
  +0.0100 V, 1:1 residual +0.0004 ± 0.0436 V on 71 pairs; on
  `2026-09-20T152758` r²=0.9988, slope 0.9933, offset +0.0090 V on 67 pairs —
  but there the known-answer control could not be paired at all, so that
  session is not evidence, and no session reached 30 pairs for a negative
  control. The two DIDs are read 125–250 ms apart, up to ~0.4 V of real
  throttle travel, so skew and disagreement are not separable at this cadence.
  **Smallest missing capture:** a KOEO throttle sweep (idle → WOT → idle,
  slowly) polling `0x120F` and `0x1210` in the same round at ≥ 5 Hz, plus the
  same pair on a normal drive. Detection of offset/stuck-track/dropout stays
  untested until then. Evidence: [report §6](../../../data/analyses/2026-09-22-b04-b05-measurements.md).
- [~] **B05.8 — Qualify AFR bank balance separately.** Assess the existing
  `afr_bank_balance_robust` recipe with both bank measurements, fuel state,
  lag and relevant steady-state gates. Verify voltage-to-mixture semantics;
  equal voltages do not constitute an independently calibrated lambda reference.
  Keep this recipe unpublished if a useful bank-asymmetry claim cannot be
  supported; B06 owns target-versus-measured delivery, not raw voltage mapping.
  *Assessed 2026-09-23 — kept unpublished.* The fit on the current calibration
  is refused publication:
  - held-out critical rate 18 %
  - 97 events in 2.4 h
  - MAE 0.102 V against a fixed 0.05 V warning band
  - no criteria declared

  The existing recipe has **no fuel-state, mode or steady-state gate**, so
  enrichment and fuel-cut rails are graded as bank asymmetry. The
  voltage→AFR transfer is verified (`afr_calibration.yaml`, both banks agree
  on held-out logs), but a voltage difference is non-linear in lambda
  (≈ 2 AFR/V rich of 14.7, ≈ 3.9 AFR/V lean). No asymmetry claim is
  supported ([report](../../../data/analyses/2026-09-23-behavior-refit-validation-report.md) §5).

  **Contract, residual and criteria (desk, 2026-09-26).** The remainders
  above are done; the recipe is still unpublished.
  - *λ residual.* The output is `derived:LAMBDA_BANK_BALANCE_AF_VOLTAGE`
    (`src/analysis/afr.py`). Each bank's A/F voltage goes through the
    verified transfer, is divided by stoich, and only then are the banks
    differenced. A censored bank (floor, or the fuel-cut ceiling) gives a
    missing value, not a rail difference. The `__gap` column carries both
    DIDs' freshness. Unit `lambda`, `_sigma(0.002)` (the DID LSB at 3.9 AFR/V).
  - *Gates.* Closed loop `0x02` only, `warmup` excluded,
    `TOTAL_TRIM_STEADY_RATES` (B05.2's measured limits), engine running.
    B05.3's band-excess persistence, with a 30 s gap (the rate gates' horizon).
  - *0x28 once verified, not assumed.* `derived:LAMBDA_BANK_BALANCE_WIDEBAND`
    (PID 0x24 − 0x28) exists only while `afr_calibration.yaml` carries a
    `wideband_bank_balance` record, which D03.34's owed `compare_channels`
    check fills. Switching the recipe is then a one-line output change, which
    flags `contract_changed:recipe` and so forces a refit. A model is never
    served the other instrument pair.
  - *Criteria.* Declared in `behavior_validation.yaml` before the fit: 0.05 λ
    target (the STFT balance's 5 %), max MAE 0.02, p95 0.0375, 2 events/h,
    ±0.05 λ/120 s.
  - *Lag.* The two DIDs are acquired 22–66 ms apart (p50; p90 33–92 ms) at
    ~2 Hz in the tuning captures. The 1 Hz row therefore pairs readings up to
    ~0.5 s apart, a phase of the closed-loop oscillation. That is part of the
    0.012 λ per-row σ.

  **Default-profile rows only (enforced, see B05.3): not fitted.**
  - `afr_bank_balance_robust` skips with "31 clean samples < min_samples=300",
    with or without the persistence rule.
  - The A/F DIDs are dense only in tuning captures, which are no longer
    evidence. `default` lists `ECM_AF_SENSOR_V_B1/B2` in `mode22` at the
    default DID cadence, which gave 13 samples each in `2026-09-23T150658`
    (`DID_1225`/`DID_1226` over an 82-minute drive, re-counted 2026-09-26).
    In the re-run frame the λ channel is finite on 230 default-profile rows
    against 6,049 capture rows; the fit counts 31 clean samples.
  - The earlier, now superseded, captures-included fit (98 % capture rows):
    406 rows, MAE 0.0102 λ, p95 0.032 and 1.24 events/h met their criteria,
    but synthetic 0.19/0.31 failed.
  - Closed-loop limitation, recorded in the recipe: the ECM drives each
    bank's measured λ to target, so a delivery asymmetry the trims absorb
    shows in the STFT balance, not here.

  **Detector features: kept out by the coverage floor today; not
  explicitly excluded (2026-09-26).**
  - The feature policy passes `derived:LAMBDA_BANK_BALANCE_AF_VOLTAGE`,
    because its inputs are serving-profile DIDs.
  - D03.34's floor (`MIN_DETECTOR_COVERAGE` = 0.5, `e6f4d54`) drops it as
    `sparse_for_grid`: grid density 0.026 on the 43 default-profile sessions
    of the re-run frame. It drops the siblings the same way:
    `did:ECM_AF_SENSOR_V_B1/B2` at 0.032 and `derived:AFR_BANK_BALANCE` at
    0.028. Measured with `sequence_feature_selection` and
    `isolation_forest_feature_selection` on that frame; neither keeps any of
    the four.
  - Captures included, the density would be 0.40, still under the floor.
    Captures never train anyway.
  - `engine_response` drops it too, as "no role assigned".
  - The deployed sequence model (2026-09-20, before the floor) does train on
    the siblings: the two voltages, `derived:AFR_BANK_BALANCE` and their
    `__mean`. The deployed IF does not.
  - **The floor stops excluding it if option (a) below is taken.** At ~1 Hz
    the λ balance, the voltages and `derived:AFR_BANK_BALANCE` all pass
    together. The λ balance is `derived:AFR_BANK_BALANCE` re-expressed
    through the transfer, so it would double-weight one measurement.
  - Taking (a) therefore needs an explicit detector exclusion. The place for
    it is the feature pipeline's static exclusions
    (`src/preprocessing/feature_pipeline.py`), which is held by uncommitted
    concurrent work today, so it is not added here.

  Tests: `tests/test_bank_balance_persistence.py` (λ channel, wideband
  refusal, recipe contract/criteria).

  **Open operator decision: A/F DID cadence on `default`.** Polling `0x1225`
  and `0x1226` at ~1 Hz in the default profile (a `mode22_rate_weights`
  entry) would give this recipe default-profile rows. It costs bus budget
  beside the ~1 Hz core Mode 01 set (D03.34). The capture profile is
  unchanged; this is the operator's call. Options:
  - (a) Weight both DIDs to ~1 Hz on `default`. That is about 2 req/s more,
    and each is paired within ~0.5 s. The re-run frame has 3,555
    closed-loop, engine-running default rows, and they yield 31 clean λ rows
    at today's cadence. At 1 Hz the same exposure would give an estimated
    ~1,100: that is B05.2's ~31 % steady-gate pass rate, and it is an
    estimate, not a measurement. It also lifts these channels over the
    detector coverage floor, so it needs the explicit exclusion described
    above.
  - (b) Leave `default` alone. The recipe then stays unfitted and says why;
    this is the status quo.

  A tuning profile cannot supply these rows: non-default profiles are never
  corpus-eligible (R03.12(a)).

  **Remaining:** that decision, then default drives and a refit. The D03.34
  0x28 verification is done (2026-10-04): `wideband_bank_balance` is recorded
  in `afr_calibration.yaml` (bank 2 PID/DID 0.9991 near stoich, n=51, vs
  bank 1's 1.0041 control), so `derived:LAMBDA_BANK_BALANCE_WIDEBAND` now
  exists and the recipe switch is available.
- [~] **B05.9 — Publish the qualified subset.** Archive B02 reports and run
  B03 compatibility/replay/rollout steps per accepted model. Record explicit
  reasons for each unfitted recipe and the smallest missing capture. Dashboard
  and MCP must distinguish unavailable evidence from a healthy subsystem.
  *2026-09-23: the qualified subset is empty.* B02 report archived
  ([report](../../../data/analyses/2026-09-23-behavior-refit-validation-report.md)). Per-recipe failure and unfitted reasons, and the smallest
  missing captures, are in §2/§5:
  - cam commanded DID
  - the KOEO throttle sweep + drive for TPS (B05.7; 244 rows)
  - LTFT relearn
  - AFR balance contract gates

  B03.8's status surfaces already show "none recorded"/failed evidence as
  not alerting. **Remaining:** publication once any model qualifies.

**Done when:** each selected relationship works through fitting and actual
serving with documented sensitivity, and each remaining recipe has a named
data or validation gap. Recipe existence alone is not completion.
