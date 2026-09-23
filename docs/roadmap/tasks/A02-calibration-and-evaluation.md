# A02 — calibration and honest evaluation

**Depends on:** nothing for the evaluation harness. Recalibrating a deployed
model needs the retrain the operator deferred on 2026-09-22 (D03.25). Every
decision in A06 is judged on this protocol, so land the harness first.
Follow [A00](A00-anomaly-detectors-README.md).

Today no number in the repo says how well either detector finds a fault on a
drive it has not seen:

- The retrain's post-training benchmark scores false alarms on the same
  sessions the thresholds were cut from.
- The deployed sequence thresholds rest on one 18-minute session.
- The fault-injection benchmark injects faults a range check would catch
  (a spike of 5× the channel maximum plus 1,000).
- No simple baseline has ever been compared.

This checklist builds the yardstick.

## Checklist

- [ ] **A02.1 — Separate calibration sessions from evaluation sessions.**
  `_run_benchmark_gate` (`scripts/retrain.py`) scores clean-drive alerts and
  precision on `calibration_sessions`, the same held-out sessions the
  thresholds were cut from. The sequence detector's clean-alert rate is
  therefore ~0.1 % by construction, which is why A02.2's flood was invisible.
  - Split the holdout into disjoint *calibration* and *evaluation* sessions,
    grouped by physical drive. The gate scores evaluation sessions only.
  - Record both lists in the manifest and the benchmark report.
  - **Test:** a session in both lists is refused.
- [ ] **A02.2 — Refuse thresholds that do not cover what they will score.**
  The deployed sequence artifact (2026-09-20) was calibrated on one session,
  `2026-09-18T134345`: city and warmup only, 71–73 kPa, 0–58 km/h. The
  retrain was asked for three; two produced no windows. `a4a58a4` now stamps
  that into `calibration_holdout` and logs a WARNING, but still publishes.
  Measured with the real `SequenceInferenceEngine` and serve-side channel
  suppression:

  | Drive | Condition | Windows flagged |
  |---|---|---|
  | `2026-09-20T205117` | highway, ~75 kPa | 1,111 of 1,477 (75 %) |
  | `2026-08-07T155139` | idle, 80 kPa | 528 of 1,397 (38 %) |
  | calibration session | — | 0.47 % |

  All of those flags came through the per-family path, none through the
  global score. The same path flags **16–100 % of windows on the model's own
  training sessions** (14 sessions); only the calibration session is quiet.
  The thresholds are one session's maxima, not a model of normal (27-session
  table in the review).
  - Find out why the two requested sessions produced no windows, and fix or
    name the cause.
  - Refuse to publish (not warn) when the calibration sessions do not cover
    every operating mode the model will score, or number fewer than
    `MIN_CALIBRATION_SESSIONS`. Coverage comes from A03.8's envelope record.
  - An operator override must be explicit and stamped.
- [ ] **A02.3 — Set thresholds from the alert budget, over independent
  windows.** Each of the 98 families is cut at its 0.999 quantile over about
  36 non-overlapping windows, which is that session's maximum. Beating the
  maximum of ~36 samples gives about 3 alert episodes an hour on a drive
  identical to calibration. So the project's own budget of ≤ 1 alert per
  engine-hour (`V1_ALERTS_PER_HOUR_BUDGET`) cannot be met by construction.
  - Compute thresholds over non-overlapping windows or per-episode maxima,
    pooled across calibration sessions.
  - Choose the level that meets the budget on the *evaluation* sessions.
  - Give the per-family path per-mode bands, or remove it if A02.5's
    ablation shows it adds nothing over the global score.
- [ ] **A02.4 — Build the evaluation harness.** One command that scores any
  detector, including the baselines in A02.5, on the same rows:
  - folds grouped by physical drive and ordered by `session_started_at`;
  - tune-revision and hardware-intervention boundaries respected, with no
    fold mixing sides;
  - results stratified by baro and ambient temperature;
  - confidence intervals from a bootstrap over drives, never over correlated
    seconds;
  - metrics: event-level recall, time to detect, and false alarms per
    engine-hour at the deployed threshold. No point-adjust scoring. VUS-PR
    only for threshold-free comparisons;
  - a per-drive table in the report, not pooled totals;
  - operating modes labelled by the same segmenter the daemon runs, not by
    sidecars. Sessions without a mode sidecar, such as captures, cannot be
    scored offline today, while live serving labels them itself.

  Reuse `src/models/false_positive_benchmark.py` and
  `src/models/synthetic_fault_eval.py` where they fit; do not build a second
  benchmark beside them.
- [ ] **A02.5 — Compare against simple baselines on the same rows.**
  - per-channel robust z-scores per operating mode;
  - PCA reconstruction;
  - an untrained autoencoder of the same shape;
  - the A05 plausibility rules.

  The literature's consistent finding is that deep detectors rarely beat
  these on multivariate time series (Kim et al. AAAI 2022; Schmidl et al.
  PVLDB 2022; Sarfraz et al. ICML 2024; TSB-AD, NeurIPS 2024). A detector
  that does not beat them on A02.4 does not alert (A06).
- [ ] **A02.6 — Inject realistic faults, not only plumbing tests.** Today's
  `_inject_one` (`src/models/synthetic_fault_eval.py`) injects:
  - spike = 5 × max + 1,000;
  - scale × 5;
  - drift = 10σ + 100.

  Keep those as plumbing tests. Add severity sweeps a range check cannot
  catch, applied consistently across related channels:
  - MAF gain error from 0.90 to 0.97;
  - a lazy O2 sensor response;
  - a slow coolant or thermostat trajectory;
  - bank-to-bank fuel imbalance;
  - a small unmetered-air leak signature at idle.

  Report recall against severity, not a single rate.
- [ ] **A02.7 — Label the real events the project already has.**
  - the flashes in `tune_revisions.yaml`;
  - the LTFT reset after 2026-09-20;
  - the 2026-06-28 battery disconnect and relearn;
  - the altitude trips, labelled *novelty, not fault*;
  - the supercharger install, once it happens.

  Each is an interval with provenance, stored as data (vehicle ledger or the
  second-brain intervention record), and scored by A02.4.
- [ ] **A02.8 — Induce reversible faults on the parked truck.** *Truck and
  operator item.* A small metered vacuum leak, an unplugged sensor, or a
  restricted intake, each tagged as a physical action (key-on/engine-running
  as the procedure states). These are the only real fault labels the project
  can get short of a breakdown. The operator chooses which, if any, are
  acceptable. Record each as a capture with its own interval.
- [ ] **A02.9 — Explain why fused recall was zero.** In the 2026-09-20
  benchmark (`~/canary-data/desk-0920/bench_new.json`: 10 cells, 5 channels ×
  2 sessions, 5 runs per fault kind) the Isolation Forest caught 26 of 250
  injections and the fused detector 0 of 250. Establish whether fusion's
  severity threshold, a missing sequence detector in the benchmark run, or
  something else suppresses every event. Fix the cause or name it.
- [ ] **A02.10 — Fix the record of what the calibration is.** The code holds
  out a fixed set of up to `MIN_CALIBRATION_SESSIONS` sessions
  (`_pick_calibration_holdouts`); it is not leave-one-out. D03.22 and D03.25
  say "LOO", and so does an operator memory note. Correct the docs, or
  implement grouped cross-validation if A02.4 shows the fixed holdout is too
  noisy. Say which.
- [ ] **A02.11 — Baseline today's deployed models on the harness.** Before any
  retrain, score the current Pi artifacts on A02.4, with A02.5's baselines
  beside them. That table is the "before" every later change is compared
  against.
  - Record the artifact hashes, and verify them against the Pi rather than
    assuming the laptop copy matches (Pi item).

- [ ] **A02.12 — A family or channel with no calibration data must not
  score.** In the 2026-09-20 sequence artifact, `did:ECM_INJECTOR_PULSE_1`
  and `_2` had no observations in the calibration session (1,056 windows,
  maximum error 0.0). So their family threshold is **0.0**, and any nonzero
  error flags.
  - Those DIDs joined the default profile on 2026-09-21. On the tuning
    capture `_capture-tuning-2026-09-21T232153` they produced 1,525 of the
    1,554 family flags.
  - A threshold computed from zero observations must be recorded as
    *uncalibrated*, and the family excluded from scoring and attribution
    until a calibration covers it.
  - Refuse to publish an artifact in which a scored family or channel has no
    calibration observations.
  - Apply the same rule to channels rarely observed in training: they drive
    the global score on the newest drives (28.6 % and 15.8 % of windows on
    the two 2026-09-22 drives, injector pulse the top global channel).
  - **Test:** a calibration frame with an all-unobserved family yields no
    threshold for it and no flags from it.

## Acceptance

A02.1–A02.5, A02.9–A02.12 are desk work. A02.8 needs the truck and the
operator's choice of faults. Nothing here changes a deployed threshold until
the operator lifts the retrain deferral. A02.11's table plus A02.4's harness
are what A06's decisions cite.
