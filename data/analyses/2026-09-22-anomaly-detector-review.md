# Anomaly detector review — Isolation Forest and sequence autoencoder

2026-09-22. Laptop-only review of the two unsupervised detectors: code,
training method, serving, and fitness for what CANary is doing. Nothing was
changed in the repo while reviewing; the checklists that act on it are the
A-series ([A00](../../docs/roadmap/tasks/A00-anomaly-detectors-README.md)).
The Pi and the canary MCP server were not reachable, so every serve-time
result below was reproduced offline with the repo's own serving classes.

**Evidence labels.**

- **checked** — the reviewing session read the code or data directly.
- **measured** — a review agent ran the repo's code on local data. The
  scripts lived in session scratch space, so each measurement below states
  how it was produced so it can be re-run.
- **inference** — reasoning, not a measurement.

## Artifacts reviewed

| Detector | Path | Created | Shape | Calibration |
|---|---|---|---|---|
| Isolation Forest | `models/nissan-xterra-2008/isolation_forest/` | 2026-09-20 | 295 features; one model per mode, 173–255 features each (`unknown`: 20) | 3 sessions: `2026-08-06T162657`, `2026-09-18T134345`, `2026-09-18T213818` (checked) |
| Sequence (LSTM/GRU-D) | `models/nissan-xterra-2008/lstm_autoencoder/` | 2026-09-20T06:46Z | 361 columns = 98 families × raw/`__mean`/`__std`/`__roc` plus duplicate sources | 1 session: `2026-09-18T134345`; threshold quantile 0.999; per-mode bands for city and warmup only (checked) |

The retrain notes (`~/canary-data/desk-0920/RESUME-retrain.md`) say these are
the models deployed to the Pi on 2026-09-20. That is not verified against the
Pi's hashes (A02.11). The retrain requested three calibration sessions for
both detectors. The sequence path used one, because two produced no windows;
nothing refused it. `a4a58a4` later made that a stamped WARNING, not a
refusal (checked).

## Headline

1. **No confirmed fault catch.** No written record credits either detector
   with a real vehicle fault. The behavior validation report says "there is
   no real fault recall to claim"
   ([2026-09-21](2026-09-21-behavior-validation-report.md), checked). What
   they flagged was change:
   - altitude: BAROMETRIC_PRESSURE was the top feature on every LSTM event
     at 63–73 kPa (D03.20);
   - a PID that came alive after training;
   - counter channels;
   - corrected bindings, which blinded the IF twice (D02.5).
2. **The Isolation Forest cannot see a single-quantity fault of any size**
   (measured, below).
3. **The sequence thresholds flood on drives they have not seen, and on
   drives the model trained on**, because they rest on one session. Across
   the 14 sessions it trained on, the family path flags 16–100 % of
   windows. Two families have a threshold of zero. The retrain's own
   false-alarm check scored the calibration sessions, so it could not
   notice (measured and checked).
4. **Four serve-time defects** mix rows or results that do not belong
   together (checked in code, reproduced).
5. **Both detectors' scores are dominated by driving conditions and
   duplicate or derived columns**, so their "top feature" is rarely
   diagnostic (measured).

## Track record (checked)

| Evidence | Result |
|---|---|
| Live and replayed drive, 2026-09-10 ([D02.6](2026-09-11-d026-replay-event-rates.md)) | 104 events in 1,910 s; sequence 62 (60 %, ~117/h), IF **0** |
| Project budget (`V1_ALERTS_PER_HOUR_BUDGET`, `src/models/false_positive_benchmark.py`) | ≤ 1 alert per clean engine-hour |
| Fault-injection benchmark, 2026-09-20 (`~/canary-data/desk-0920/bench_new.json`; 5 OBD channels × 2 sessions, 5 runs per fault kind) | IF caught spike 2/50, scale 4/50, dropout 5/50, stuck 7/50, drift 8/50; **fused 0/250**. Nominal IF alerts/h per cell: 0.0 or 6.07 |
| Same benchmark, first cell | an RPM spike of 5 × max + 1,000 was never flagged |
| Injected fault sizes (`synthetic_fault_eval._inject_one`) | spike 5 × max + 1,000; drift 10σ + 100; scale × 5, all caught by construction by a range check |
| Benchmark 2026-07-27 (`benchmark-20260727-031109.json`, one 0.48 h session) | sequence 33 clean alerts/h, IF 214, fused 102; precision ~3–6 % |
| Tick cost on the Pi | sequence stage 251.5 ms median on 2026-09-10 ([drive log](2026-09-10-drive-h1-h7-h9.md)); whole tick 439 ms median at idle (D01.7) |

## Isolation Forest (measured on the artifact above)

Feature frames were built with the repo's pipeline for three training
sessions (`2026-09-18T180630`, `…225605`, `…232910`) and one calibration
session (`2026-09-18T134345`), then scored with the artifact's own per-mode
models and thresholds.

- **Single-quantity blindness.**
  - Setup: shift one quantity on unflagged training rows (city n = 917,
    highway n = 1,585) by +3, +10, +30, +100σ of the mode.
  - Result: STFT_1 alone, the fuel-trim family, and all four coolant
    sources (raw plus `__mean`) each flagged **0.00** of rows at every size.
    The score moved +0.002 to +0.007, at most 0.15 of the held-out spread;
    the threshold sits ~2.4 spreads above the median.
  - Control: shifting all features by 3σ flags 100 %.
  - Mechanism (inference, consistent with the measurement): with
    `max_samples` = 256, trees are ≤ 8 levels deep, so any one of ~250
    features lies on ~3 % of a path. A value past the training range follows
    the same branch as the training maximum, so its size stops mattering.
- **Attribution.** `_rank_feature_contributors` ranks |z| over the
  295-column union, not the scoring mode's features (checked). The top-1
  name was not a feature of the scoring model on 60/62 city, 52/62 highway
  and 39/60 idle_transient rows. Within the mode's own features, the |z|
  top-1 matched the occlusion top-1 on 0–7 of ~61 rows.
- **Quarantine versus completeness.** Quarantine blanks cells (MAD = 0 makes
  every non-median value an outlier), and the IF drops any row with a NaN
  (checked).
  - 4.5 % of cells are quarantined, but 71.6 % of rows are lost.
  - Usable rows: city 69 % → 12 %, highway 80 % → 40 %, idle_transient
    96 % → 54 %.
  - The calibration frame skips quarantine and feedback-label exclusion.
- **Per-mode provenance.**
  - city: 2,174 rows from 11 sessions, highway: 1,348 rows from 7 — both
    entirely 2026-09-18.
  - warmup: 192 rows from two August sessions.
  - The idle and warmup calibration rows come entirely from `134345`
    (71–73 kPa; training median 79).
- **Polling-mix blindness.** On `134345`, with no spec column absent, the IF
  withholds 99.7 % of city rows, 83.5 % of idle_transient and 100 % of
  unknown. `derived:STFT_BANK_BALANCE` and `TOTAL_TRIM` are NaN under that
  polling. `structural_blindness` divides by 295, while withholding is
  decided per mode.
- **Policy and sentinels.** 83 of 295 features come from
  `model_feature: false` bindings and 44 from `hypothesized` ones. The
  policy is checked only at training. TPMS reads 0 (not transmitting) on
  35 % of `134345`, scaled as a measurement (μ = 100.6, σ = 56).
- **Imputation.** The pooled-mode scaler means `fillna(0)` imputes a value a
  median 9.4 mode-standard-deviations away for idle features. Imputing 5 %
  of features raised the mean score in 3 of 4 modes.
- **Unknown model.** A context-only `unknown` model (20 features,
  thresholded on 91 rows) is served.
- **Hyperparameters.**
  - Seeds 0–3 on the same highway rows flagged 147–214 rows (Jaccard 0.68;
    1,000 trees: 0.86).
  - `contamination` does not affect what is served, but the offline
    evaluator scores against it and counts withheld rows as detections.

## Sequence autoencoder (measured on the artifact above)

As a harness check, the reviewer rebuilt the calibration session and
reproduced the manifest's threshold (1.2133), maximum (1.2267) and mean
(0.5116) exactly. Unseen drives were then scored through the real
`SequenceInferenceEngine` with serve-side suppression.

- **Flooding on unseen drives.** Every flag came through the per-family
  path; none through the global score.

  | Drive | Condition | Windows flagged |
  |---|---|---|
  | `2026-09-20T205117` | highway, ~75 kPa | 1,111 of 1,477 (75 %) |
  | `2026-08-07T155139` | idle, 80 kPa | 528 of 1,397 (38 %) |
  | calibration session | — | 0.47 % |

  Each of 98 families is cut at its 0.999 quantile over ~36 non-overlapping
  windows, which is that session's maximum. The calibration session is city
  and warmup, 71–73 kPa, 0–58 km/h. Beating the maximum of ~36 samples
  implies ~3 alert episodes per hour even on an identical drive (inference).
- **Circular check.** `_run_benchmark_gate` (`scripts/retrain.py`) scores
  false alarms on `calibration_sessions`, the sessions the thresholds were
  cut from (checked). The code holds out a fixed set of sessions
  (`_pick_calibration_holdouts`); it is not leave-one-out, though D03.22,
  D03.25 and an operator memory note say "LOO" (checked).
- **Straddling windows.** The live session label is always `"live"`, and
  `prepare()` returns early without clearing when scoring is not allowed.
  So the window survives stops and gaps (checked). Feeding a drive's tail
  and then the next drive 20 minutes later flagged 11 of 29 straddling
  windows. Replay start and stop do not reset either engine (checked).
- **Worker reply mismatch.** `InferenceWorker.compute` never compares
  `reply.index` with the tick (checked). After one timeout, a fake child
  reproduced every later tick receiving the previous tick's result. The
  in-process fallback scores the parent's own engine, whose buffer holds
  only fallback ticks (checked).
- **Mask semantics.** The mask is `¬__gap`, meaning "no sample within 2 s
  (fast) or 120 s (slow)", not "sample arrived". Baro had 52 samples in
  1,085 s but mask = 1 on 90.8 % of rows. Δt was nonzero on 10 % of baro
  cells.
- **Score composition.** Four coolant families give 19.2 % of the global
  numerator from 7.1 % of observed cells. Even on in-distribution windows,
  the top-1 attributions are `INTAKE_TEMP__mean` (102 of 1,056) and
  `BAROMETRIC_PRESSURE__std` (96), a step detector with σ 0.21.
- **Suppression.** Masking suppressed channels changes encoder input: a
  median shift of 0.024 on other channels, p99 0.31, against a typical
  channel threshold of 1.57.
- **Minor, all checked.**
  - The scaler is fit on train plus validation.
  - The validation shuffle is overridden by a size sort, so validation is
    the smallest sessions.
  - The CLI keeps held-out sessions in explicit lists, and trains on
    everything when all sessions are held out.
- **Checked and fine.**
  - eval mode, and effectively zero dropout;
  - fp32 serving of a bf16-trained model;
  - offline versus live Δt;
  - masked MAE the same in calibration and serving;
  - per-mode threshold keyed on the last row on both sides.

## Both detectors across 27 local sessions (measured)

**How this was produced.** A review agent scored 27 local sessions with the
two artifacts above, then stopped at an account spend limit before writing
its own summary. The reviewing session aggregated the per-session outputs
with the agent's own analysis script. The session roles come from each
manifest's `training_sessions`/`calibration_sessions`.

**Harness checks.**
- The IF's calibration was reproduced exactly: every mode's held-out row
  count and achieved flag rate match the manifest.
- `tools/check_manifest_producibility.py` reports 0/295 (IF) and 0/361
  (sequence) features unproducible from the catalog.

**Unit.** Window-level flags, not live events. Live alerts also pass the 3-of-5
persistence gate and de-duplication, so live event counts would be lower.
Live behavior was not observed.

| Session | IF role | Sequence role | IF rows scored | IF flag rate | Seq. windows | Global flags | Family flags | Family flags without the two zero-threshold families |
|---|---|---|---|---|---|---|---|---|
| `2026-08-06T002019` | train | train | 220 | 2.3 % | 227 | 8.4 % | 100 % | 100 % |
| `2026-08-07T161543` | train | train | 1,156 | 4.6 % | 1,337 | 0 % | 58.9 % | 58.9 % |
| `2026-09-18T180630` | train | train | 1,208 | 0.2 % | 1,277 | 0 % | 76.6 % | 76.5 % |
| `2026-09-18T200256` | train | train | 149 | 3.4 % | 150 | 0 % | 94.0 % | 94.0 % |
| `2026-09-18T231144` | train | train | 996 | 0 % | 1,005 | 0 % | 32.0 % | 32.0 % |
| `2026-09-18T134345` | cal | cal | 512 | 0.4 % | 1,056 | 0.2 % | 0.2 % | — |
| `2026-09-18T213818` | cal | held out | 4,407 | 0.1 % | 4,464 | 0 % | 53.1 % | 53.1 % |
| `2026-08-07T164617` | held out | held out | 1,233 | 0 % | 1,307 | 0 % | 54.6 % | 54.6 % |
| `2026-09-18T160921` | held out | held out | 1,468 | 1.8 % | 4,186 | 0 % | 7.2 % | 7.2 % |
| `2026-09-20T205117` | held out | held out | 55 of 1,506 | 0 % | 1,477 | 0.7 % | 75.6 % | 75.6 % |
| `2026-09-20T225927` | held out | held out | 1,134 | 0.1 % | 1,464 | 0 % | 71.9 % | — |
| `2026-09-21T010459` | held out | held out | 100 of 1,642 | 29.0 % | 1,613 | 0 % | 29.9 % | 4.3 % |
| `2026-09-22T163415` | held out | held out | 610 of 1,771 | 9.0 % | 1,742 | 28.6 % | 36.9 % | — |
| `2026-09-22T171544` | held out | held out | 227 of 699 | 0 % | 670 | 15.8 % | 26.4 % | 26.4 % |
| `_capture-tuning-2026-09-21T232153` | held out | held out | 0 (no mode labels) | — | 1,601 | 1.2 % | 97.1 % | 69.5 % |

A dash in the last column means the agent's script did not produce it for
that session. The full 27-session table lives only in the review's scratch
output. The rows above are chosen to show every pattern:

- **The family path fires on the model's own training data.** Across all 14
  training sessions it flags 16.3–100 % of windows; only the one
  calibration session is quiet (0.2 %). The thresholds are that single
  session's maxima, not a model of normal.
- **Two families have a threshold of zero.** `did:ECM_INJECTOR_PULSE_1` and
  `_2` had no observations in the calibration session (1,056 windows,
  maximum error 0.0), so their threshold is 0.0 and any nonzero error flags.
  These DIDs joined the default profile on 2026-09-21, so they drive most
  family flags on the newest drives (1,525 of 1,554 on the tuning capture)
  and part of the global flags there (injector pulse is the top global
  channel).
- **The global score starts firing on the newest drives.** It flags 28.6 %
  and 15.8 % of windows on the two 2026-09-22 drives, against ≤ 0.7 % on
  older ones. That is consistent with the polling-profile change D03.25
  lists as its fourth reason (inference).
- **The IF mostly withholds on unseen drives.** On held-out drives it scores
  as little as 3.7 % of rows, because required features are missing under
  the drive's polling (A04.5).
  - Where it does score, its flags come from `model_feature: false`
    channels: POWER_STATE, STEERING_RATE, BRAKE_PRESSURE and MOTION
    (A04.6).
  - Sessions without a mode-label sidecar (the capture,
    `2026-09-20T211925`) cannot be scored offline at all. Live, the
    segmenter labels modes itself, so the harness must do the same
    (A02.4).

## Literature

- **Unsupervised time-series detectors.** Deep detectors seldom beat simple
  baselines once evaluation is fair:
  - Wu & Keogh, IEEE TKDE 2021 (arXiv 2009.13807);
  - Kim et al., AAAI 2022, on point-adjust inflating scores;
  - Schmidl, Wenig & Papenbrock, PVLDB 15(9) 2022 (71 algorithms, "no clear
    winner");
  - TSB-AD (Liu & Paparrizos, NeurIPS 2024), where IF scores 0.20 mean
    VUS-PR on multivariate data against 0.31 for PCA;
  - Sarfraz et al., ICML 2024: deep detectors "effectively learn linear
    mappings".
- **Vehicle diagnosis.** Production OBD-II uses per-component monitors
  gated on operating conditions, confirmed on repeat (CARB 13 CCR §1968.2).
  Model-based residual diagnosis is the established method (Isermann 2005;
  Nyberg 2002; Frisk et al. 2017). Theissler (Knowledge-Based Systems 2017),
  a single car with on-road injected faults, found road driving inflated
  false positives.
- **Known weaknesses.**
  - IF suffers the curse of dimensionality with irrelevant attributes (Liu,
    Ting & Zhou, TKDD 2012).
  - IF has axis-parallel score artifacts (Hariri et al., TKDE 2021,
    Extended IF).
  - Autoencoders can reconstruct anomalies well (Gong et al., ICCV 2019).
  - Conditioning on exogenous inputs separates context from indicators
    (Song et al., TKDE 2007; Hundman et al., KDD 2018).

## What follows

Correctness defects (A01, and parts of A03 and A04) are worth fixing
whatever role the detectors keep. The evaluation harness (A02) comes before
any decision, because no current number measures fault detection on unseen
drives.

The evidence points to a narrower role (A06), for three reasons:

- gross sensor faults are caught better by deterministic plausibility
  checks (A05);
- the sequence model is more useful as a non-alerting "outside the learned
  envelope" screen;
- the primary detector belongs with the behavior models and the second
  brain's residuals.

The operator decides.

## Limits

- The laptop artifacts are assumed, not verified, to be the Pi's.
- The measured results use a handful of sessions each. They establish
  mechanisms and orders of magnitude, not rates to quote against a budget.
  A02 exists to produce those.
- Nothing here was measured on the truck.
