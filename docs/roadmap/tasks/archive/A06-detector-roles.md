# A06 — what the Isolation Forest and sequence detector are for

> **Archived 2026-11-08.** The decisions are recorded and the other items landed.
> A06.3's remainder (the next IF retrain) continues as **D03.51**, its nested
> LSTM-without-memory retrain as **D03.52**, and A06.6 (the Pi measurement and
> the operator's cadence) as **D03.53**, all in `../D03-open-work.md`.

**Depends on:** the operator for every decision here. A02.11's baseline table
supplies the evidence. Items marked *decision* stay `[~]` with the options
named until the operator chooses; an implementer does not choose. Follow
[A00](../A00-anomaly-detectors-README.md).

The two detectors were built to say "something is unusual" across hundreds
of features. The record so far, detailed in the
[review](../../../../data/analyses/2026-09-22-anomaly-detector-review.md):

- **No confirmed fault catch.** No written record credits either detector
  with a real vehicle fault. The behavior validation report states "there is
  no real fault recall to claim".
- **Nuisance rate.** The live and replayed 2026-09-10 drive produced about
  117 sequence events per hour and 0 from the Isolation Forest, against a
  budget of 1 per engine-hour.
- **Weak on gross faults.** On 2026-09-20 the Isolation Forest caught 26 of
  250 injected faults, at ~1.9 alerts/h on clean data.
- **What they flagged was change, not faults.** Altitude, a PID that came
  alive after training, counter channels, and corrected bindings, which
  blinded the IF twice.
- **Cost.** Sequence scoring took a 251.5 ms median on the 2026-09-10
  drive. D01.7 measured the whole tick at a 439 ms median at idle.
- **Shelf life.** The supercharger build, weeks away, invalidates every
  learned normal.

On one vehicle with a moving corpus, "unusual" mostly means "outside the
training envelope". That is useful to know. It is not a diagnosis.

## Checklist

- [x] **A06.1 — Decision: does the Isolation Forest alert?** **Decided by
  the operator, 2026-09-23: (b) evidence only.** `DetectorRolesConfig`
  now defaults `isolation_forest` to `evidence`, and the three example
  configs agree (`tests/test_detector_roles.py::test_roles_default_to_the_operators_decision`,
  `::test_shipped_example_configs_carry_the_operators_roles`). The decision
  rests on the A02.11 table: 1.2 [0, 3.9] false alarms per engine-hour with
  81 % of rows withheld, and A04.1 (the IF is blind to a single-quantity
  shift of any size). A05's plausibility checks own the gross single-channel
  faults.
  - Options:
    - (a) keep alerting after A04's fixes;
    - (b) score and store as low-weight evidence, but never alert;
    - (c) retire it.
  - *Recommendation:* (b) now, since A05 covers its useful job (gross
    single-channel faults) by construction. Revisit (a) only if A02.4 shows
    it beating A02.5's baselines.
  - *Evidence now available (2026-09-23, awaiting the operator's choice):*
    A02.11's held-out table,
    [`2026-09-23-a02-detector-baseline.md`](../../../../data/analyses/2026-09-23-a02-detector-baseline.md).
    It is **provisional**: it is being re-run after a harness fix, so re-read
    it before deciding. First-run overall false alarms per engine-hour,
    against a budget of 1: Isolation Forest 1.45 [0.00, 4.29] with 78 % of
    rows withheld; sequence (per-channel) 42.78 [22.58, 62.13]; sequence
    global 3.38; fused 0.00. A05's deterministic plausibility checks are
    committed (`06a3188`, [A05](A05-plausibility-checks.md)). The role
    mechanism the decision would switch is in A06.5.
- [x] **A06.2 — Decision: what the sequence detector is for.** **Decided
  by the operator, 2026-09-23: (b) a non-alerting novelty screen.** The
  non-alerting half is in force: `sequence` now defaults to `evidence`. It
  rests on the A02.11 table: 45.4 false alarms per engine-hour as served,
  3.0 on the global score.
  - *Novelty-screen report, done 2026-09-23 (code and tests):*
    - `TrainingEnvelope` (`src/models/inference.py`) is built once from the
      manifest's `training_envelope` (A03.8). For each scored window,
      `SequenceInferenceEngine.envelope_check` compares the window's
      [min, max] of each recorded quantity (baro, IAT, speed) with the
      trained [min, max] for the window's mode. It returns `inside`,
      `outside`, `mode_not_trained`, or `not_compared` (no fresh value, or no
      mode, with the reason). It is carried as `SequenceInferenceResult.envelope`.
      An artifact without an envelope gives `envelope: None`, never "inside".
    - The window buffer keeps the envelope columns beside the window whether
      or not they are model inputs, and `_sequence_observe` passes them
      through the feature reconciliation. The scoring child uses the same
      path.
    - Each sequence event carries `detail.envelope` and
      `detail.novelty_summary`, e.g. "Outside the learned envelope in city on
      BAROMETRIC_PRESSURE (63 kPa vs trained 80–82 kPa); largest deviations:
      …" (`sequence_envelope_summary`, `src/alerts/events.py`).
    - `/status` `detectors.inference.sequence_envelope`
      (`EnvelopeCoverageMeter`, `src/daemon/inference_metrics.py`) is the
      coverage meter: windows inside and outside, `mode_not_trained`,
      `not_compared` and `not_recorded`. The outside share is taken of the
      compared windows only, by mode and by quantity, and the counts restart
      when the served artifact changes.
    - The dashboard's anomaly row shows an envelope badge with the sentence
      as its title, and the LSTM evidence card shows the sentence.
    - Cost: 39 µs per scored window on the laptop, for the 30-row, 3-quantity
      check.
    - Tests: `tests/test_sequence_novelty_screen.py`, 12 passed. It fails on
      HEAD, where `sequence_envelope_summary` does not exist. It covers the
      altitude case, inside, mode not trained, not compared, not recorded,
      buffer condition columns, the engine and event end to end on a stub
      model, `_sequence_observe` passing the columns through, the meter by
      mode and quantity, an unrecorded envelope with a restart on a new
      artifact, and `_apply` feeding the meter. `ModelEvidenceHealth.test.ts`
      has 2 new cases.
    - Checked against the real candidate manifest
      (`models/_candidates/2026-09-23-na/lstm_autoencoder`): a 63–65 kPa,
      30–90 km/h city window reads "Outside the learned envelope in city on
      BAROMETRIC_PRESSURE (63 kPa vs trained 80–82 kPa), SPEED (90 km/h vs
      trained 0–84 km/h)". The deployed 2026-09-20 manifest has
      `training_envelope: null`, so it reports "not recorded".
  - *Served since 2026-09-24:* the r2 LSTM, which records the envelope, is
    deployed (A06.3). **Remaining:** read `/status`
    `detectors.inference.sequence_envelope` over a real drive.
    *2026-09-24 16:04–16:25Z, engine running:* the meter stayed at 0 windows,
    and so did A01.7's `sequence_windows` and the lstm events, although
    `stages.sequence` ran every tick (300 calls, median 344 ms). It was a
    serving defect, not a quiet model. **Fixed in `6892c2c` (2026-09-24):**
    `run_tick` read `busy` before awaiting the snapshot. A score that
    finished during the snapshot could then send rows to the sequence engine
    out of order. Since `f15ec38` a backwards step clears the window, so on
    the Pi it never passed 2 rows. The defect had been present since D01.9
    (2026-09-11). `busy` is now judged after the snapshot, and `_dispatch`
    refuses any tick not newer than the last one dispatched. A laptop replay
    went from 7 to 114 scored windows out-of-process (15 to 87 in-process).
    `tests/test_live_sequence_ordering.py` fails on the old code.
    The Pi has run `6892c2c` since 2026-09-24 16:53:24Z
    ([scan-tool note](../../../../data/analyses/2026-09-24-scan-tool-bcm-ecm-session.md)),
    but CANary was passive in that session, so it says nothing about
    scoring. *Closed 2026-10-04 drive (Pi `a1da058`, daemon up 19:43:17Z, profile `default`; engine running 19:43–20:16Z, passive 19:48:23–~20:00Z by another caller, non-passive otherwise):* `sequence_envelope` counted **1,739 windows**
    (`envelope_recorded: true`, r2 LSTM): 367 inside, 904 outside, 468
    `not_compared`, 0 `mode_not_trained`. Outside by mode: highway 631/634
    (IAT outside on all 631 compared, baro on 283), warmup 99/167 (IAT),
    city 108/629, idle_transient 66/232, idle 0/77. The meter is nonzero.
    The highway IAT result says the trained highway envelope is narrower in
    intake temperature than an October afternoon. *Was:* read
    `detectors.inference.sequence_envelope` and A01.7's `sequence_windows`
    over an engine-running, non-passive drive on `default`. Only a nonzero
    meter closes the box.
  - Options:
    - (a) keep alerting after A01–A03 and A02's recalibration;
    - (b) a non-alerting *novelty screen* reporting "outside the learned
      envelope; largest deviations on X, Y" against A03.8's envelope, stored
      as low-weight evidence and used as a corpus-coverage meter (after the
      supercharger it shows where the new normal lacks data);
    - (c) retire it.
  - *Recommendation:* (b). The "D01.7 kept it because it produces 60 % of
    events" argument measures volume, not usefulness.
  - Whatever is chosen, A01.1–A01.4 are correctness fixes and land anyway.
  - *Evidence now available (2026-09-23, awaiting the operator's choice):*
    A02.11's held-out table,
    [`2026-09-23-a02-detector-baseline.md`](../../../../data/analyses/2026-09-23-a02-detector-baseline.md).
    It is **provisional**: it is being re-run after a harness fix, so re-read
    it before deciding. First-run overall false alarms per engine-hour,
    against a budget of 1: Isolation Forest 1.45 [0.00, 4.29] with 78 % of
    rows withheld; sequence (per-channel) 42.78 [22.58, 62.13]; sequence
    global 3.38; fused 0.00. A05's deterministic plausibility checks are
    committed (`06a3188`, [A05](A05-plausibility-checks.md)). The role
    mechanism the decision would switch is in A06.5.
- [~] **A06.3 — Decision: scope of the owed retrain (D03.25).** → **carried to D03.51** (2026-11-08). **Decided
  by the operator, 2026-09-23: (a) a full naturally aspirated retrain now**,
  with the A02–A04 fixes and the A04.10 tree count (600). *Retrained and
  evaluated 2026-09-23; not deployed.*
  [Report](../../../../data/analyses/2026-09-23-a06-3-retrain-evaluation.md)
  (the `.json` beside it has every number).
  - **Corpus.** The operator's choice: sessions starting at or after the
    ZS44A-20260920 flash (2026-09-21T03:08:45Z, `era_resolver`), from
    `~/canary-data/sessions/nissan-xterra-2008` only.
    - 40 live sessions and 13 captures. 13 no-OBD sessions
      (`classify_sessions.py`) and the 11-hour parked session
      `2026-09-23T032755` were left out of the explicit list.
    - That leaves 6 physical drives. `min_training_drives_per_mode` (3) has
      no override, so the split was set explicitly:
      - evaluation: the newest drive, D6 (09-23);
      - calibration: 2 drives (D5, D3) by the picker's own rule;
      - training: D1, D2, D4.
    - `--accept-thin-calibration "operator chose current-calibration-only
      corpus (post ZS44A-20260920), 2026-09-23"` is stamped in both manifests.
    - Training used `canary.example.yaml`, as the deployed artifacts did.
      `canary.dev.example.yaml` disables the LSTM.
  - **Candidates** (gitignored, `models/_candidates/2026-09-23-na/`):
    - IF `model.joblib` sha256 `69463c07…52b3c`, 600 trees, 204 features. It
      serves city, idle_transient and warmup. idle and highway were refused
      at 2 training drives.
    - `seed_agreement` mean Jaccard at 600 trees: city 0.57, warmup 0.68,
      idle_transient 1.00 (one flagged row of 14).
    - LSTM `model.pt` sha256 `3edfd8fe…99f2`: 250 columns, `arrived_sample`
      mask, global threshold 1.0575.
    - `training_envelope` records about 0.73 engine-h of moded data (city
      0.26 h, highway 0.16 h). `suppress_channels` is empty.
  - **Held out, same rows, one drive.** Scored on D6 with
    `scripts/evaluate_detectors.py` for both the deployed 2026-09-20
    artifacts and the candidates: 4,825 rows, 1.06 engine-h.
    - Each deployed detector was paired with a candidate so that the harness
      grades injections: the deployed pair alone is cross-era on post-flash
      drives.
    - Injection windows and each artifact's results are identical across
      runs. Batched/served parity: 0 of 171 windows differ.
    - One drive gives no bootstrap. Intervals are Poisson on episode counts.
  - **Findings, candidate vs deployed:**
    - **Sequence global, live sessions:** 0 episodes against 29. It caught a
      10 % bank imbalance in 4 of 6 windows; the deployed model masked every
      window.
    - **Family path:** 54.9 FA/eh [41.7, 70.9] against 5.7 [2.1, 12.4].
      The deployed flood is one run over 82 % of rows; the candidate flags
      37 %.
    - **Tuning captures:** they carry most sequence episodes for both
      artifacts. Captures never train.
    - **IF:** the candidate withholds 91 % of the drive's rows (deployed
      72 %). Neither IF raised an alarm or caught any fault, and both fail the
      A04.1 shift.
  - **Code fixes the retrain needed** (each with a default-tier test, named
    in the report):
    - captures now bridge drive grouping in `drives_of_sessions`;
    - the IF coverage floor ignores `unknown`-mode rows, which had left the
      first candidate IF with 8 columns;
    - a column seen only in validation drives no longer crashes the sequence
      trainer's scaler; it is recorded as `unobserved_in_training`.
  - The operator chose to deploy the candidate LSTM only and keep the
    deployed IF (2026-09-23). After the drive-grouping fix the LSTM was re-run
    as r2 (and r3 with parked rows dropped; r3 was noisier on D6, so r2 was
    chosen, per the evaluation's recommendation). The candidate IF's
    idle_transient rests on 2 physical drives, not 4 as first counted.
  - **Deployed 2026-09-24 16:03:22Z (10:03 MDT):** the Pi serves
    `lstm_autoencoder_2026-09-24T035125.284392Z` (`model.pt` sha256
    `f7389acf…1f7a`, manifest `cc034239…c6b6`, hash-checked on the Pi) beside
    the unchanged `isolation_forest_2026-09-20T054703`. The prior LSTM
    (`c77165ad…1112`) is backed up at
    `models/nissan-xterra-2008/_prior/2026-09-24-pre-r2/`. `/status`:
    no version mismatches; both detectors effectively `evidence`.
  - **Remaining:** the next IF retrain once idle and highway clear the
    3-drive floor. The 600-tree tick cost waits for an IF that is deployed.
    The IF replay diff is done (D03.25, 2026-10-03). On D6 the candidate scores
    422 of 782 ticks and none on highway, against the deployed IF's 748.
    Events are identical, and neither IF crosses its threshold.
  - [~] **LSTM retrain without memory features (operator-approved
    2026-10-08; B08.4's owed retrain).** → **carried to D03.52** (2026-11-08). *Trained and evaluated 2026-10-08;
    not deployed; the feature-set acceptance failed (below).* Candidate
    `models/_candidates/2026-10-08-na-r2-nomem/` (gitignored), set aside by
    the A02.3 gate as `_refused-unbudgeted-20261008T210426Z-lstm_autoencoder/`
    (`model.pt` sha256 `7a0d4833…3903`, manifest `c3270b0f…8353`,
    `lstm_autoencoder_2026-10-08T210004.303855Z`). Commands, logs, every
    evaluation JSON and the feature diff are in its `evaluation/`.
    - **Recipe.** r2's: same 26 sessions, same split (training D1/D2/D4,
      calibration 10 sessions of D3/D5, evaluation D6), same
      `--accept-thin-calibration` reason, `canary.example.yaml`, LSTM
      hyperparameters identical, CUDA, `--skip-if --skip-benchmark`. Input
      rows 11,084, as r2. Two deviations the current code makes mandatory:
      - the r3 parked-row filter (operator, 2026-09-24): 6,869 rows dropped,
        600/158 windows (r2 1,479/671), tail-block validation, best epoch 31
        of 41;
      - A02.3 refused the alert level: D6 holds 0.40 engine-h, 1 needed. The
        artifact is set aside, not served, until a stated
        `--accept-unbudgeted-thresholds` reason.
    - **Feature diff vs r2: fails the "only the four memory features"
      acceptance.** 254 → 184 columns; 74 removed, 4 added, the kept 180 in
      the same order. Removed: the 4 `obd:mem:*` (`memory_channel`, 32aa13b;
      they held 4 non-null rows of 11,084, so D03.34's floor would have
      dropped them anyway); 28 Mode 01 columns of 7 PIDs (ABSOLUTE_LOAD,
      ABS_THROTTLE_POS_B, ACCEL_PEDAL_POS_D/E, COMMANDED_THROTTLE_ACTUATOR,
      EVAP_VAPOR_PRESSURE, RELATIVE_THROTTLE_POS: "not polled by the serving
      (default) capture profile"); 4 `gps:speed` (H00 decision 1, external
      sensor); 38 `sparse_for_grid` (D03.34: 8 DIDs ×3, AFR_BANK_BALANCE,
      TPS_VOLTAGE_PER_PCT, CATALYST_TEMP_B1S1/B2S1). Added:
      `derived:AFR_CENSORED_B2` ×4. Architecture/hyperparameters otherwise
      equal.
    - **Held out, D6 live (3 sessions, 2,396 rows, 0.397 engine-h), deployed
      IF beside each LSTM, same rows and the same 96 injection windows;
      batched/served parity 0 of 171 windows for all three:**

      | path | r2 (deployed) | r3 | no-mem candidate |
      |---|---|---|---|
      | per-channel episodes (FA/eh [Poisson 95 %]); flagged / 2,269 | 8 (20.2 [8.7, 39.7]); 131 | 16 (40.3 [23.0, 65.5]); 152 | **19 (47.9 [28.8, 74.7]); 167** |
      | global episodes; flagged rows | 0; 3 | 1 (2.5); 8 | **0; 0** |
      | `fp_benchmark` per-window FPR (family / global exceedances) | 5.77 % / 1 | 6.70 % / 1 | **7.36 % / 0** |
      | global recall, thermostat 10 / 15 °C | 1/6, 2/6 | 0/6, 1/6 | **0/6, 1/6** |
      | VUS-PR bank / lazy_o2 / maf / thermostat | 0.66 / 0.52 / 0.60 / 0.52 | 0.65 / 0.51 / 0.58 / 0.49 | **0.69 / 0.56 / 0.61 / 0.52** |
      | A04.1 shift, per-channel / global | fail (coolant) / fail (5) | fail (coolant) / fail (5) | fail (coolant) / fail (6, + af_sensor_b1) |

      Per-channel paths are masked on every other graded window for all
      three. The candidate sits with r3 (same recipe with memory), not
      clearly apart from r2 (intervals overlap); r2's 8 episodes reproduce
      the 2026-09-24 report. One drive: no bootstrap.
    - **Cost.** Retrain 8 min 21 s wall at nice 19 (training + calibration
      frames 3.3 min, LSTM fit + calibration 1.9 min on CUDA, the refused
      A02.3 level 2.7 min), peak RSS 2.2 GB; `fp_benchmark` ~65 s per model;
      `evaluate_detectors` ~4.9 min per model.
    - **Remaining:** the operator's choice of feature set and A02.3 reason
      (not the four-feature change this item asked for); then deploy, refresh
      `tests/fixtures/deployed_manifests`, delete the now-stale
      `manifest_unproducible_allowlist.yaml` entry, and rework
      `test_default_profile_b08_context.py::test_the_deployed_lstm_carries_the_spark_window_so_the_guard_can_fail`,
      which asserts the deployed LSTM learned the memory channels.

  The retrain was owed
  for three reasons: the binding retraction, the mode labels, and the new
  default-profile DIDs. The supercharger build will make the result obsolete
  within weeks.
  - Options:
    - (a) full naturally aspirated retrain now;
    - (b) retrain only what A06.1/A06.2's roles need, e.g. the envelope
      record for a novelty screen;
    - (c) skip it and start the boosted corpus at the build.
  - *Recommendation:* (b) or (c). Record the build as a hardware
    intervention and corpus boundary either way.
  - *Evidence now available (2026-09-23, awaiting the operator's choice):*
    A02.11's held-out table,
    [`2026-09-23-a02-detector-baseline.md`](../../../../data/analyses/2026-09-23-a02-detector-baseline.md).
    It is **provisional**: it is being re-run after a harness fix, so re-read
    it before deciding. First-run overall false alarms per engine-hour,
    against a budget of 1: Isolation Forest 1.45 [0.00, 4.29] with 78 % of
    rows withheld; sequence (per-channel) 42.78 [22.58, 62.13]; sequence
    global 3.38; fused 0.00. A05's deterministic plausibility checks are
    committed (`06a3188`, [A05](A05-plausibility-checks.md)). The role
    mechanism the decision would switch is in A06.5.
- [x] **A06.4 — Decision: where the primary detector lives.** **Decided by
  the operator, 2026-09-23: (a) the behavior models and the second brain's
  deterministic layer**, with A05 for gross faults. The IF and the sequence
  model are evidence (A06.1, A06.2). The work to grow the behavior models is
  the B-series (B03–B07). The literature
  (A02.5) and production OBD practice both point at per-quantity residuals:
  engine responses predicted from operating inputs, with per-regime bands,
  change detection and confirmation. That is what the behavior models
  (B-series) and the second brain's deterministic layer already are.
  - Options:
    - (a) grow the behavior models and the second brain into the primary
      detector, with A05 for gross faults;
    - (b) rebuild the sequence model as a conditioned forecaster
      (A03.2) and make it primary.
  - *Recommendation:* (a). It does not build a parallel framework, and each
    alarm names a component. (b) stays open as an A02-judged experiment.
  - *Evidence now available (2026-09-23, awaiting the operator's choice):*
    A02.11's held-out table,
    [`2026-09-23-a02-detector-baseline.md`](../../../../data/analyses/2026-09-23-a02-detector-baseline.md).
    It is **provisional**: it is being re-run after a harness fix, so re-read
    it before deciding. First-run overall false alarms per engine-hour,
    against a budget of 1: Isolation Forest 1.45 [0.00, 4.29] with 78 % of
    rows withheld; sequence (per-channel) 42.78 [22.58, 62.13]; sequence
    global 3.38; fused 0.00. A05's deterministic plausibility checks are
    committed (`06a3188`, [A05](A05-plausibility-checks.md)). The role
    mechanism the decision would switch is in A06.5.
- [x] **A06.5 — Fusion and the alert budget follow the roles.** Once
  A06.1/A06.2 are decided, change fusion so non-alerting detectors
  contribute evidence but cannot raise an alert on their own. Enforce ≤ 1
  alert per engine-hour on A02's evaluation drives at publish time, as a
  refusal, not a report.
  - *Done, decision-neutral (2026-09-23):* the role mechanism.
    `models.roles.{isolation_forest,sequence}: alert | evidence`
    (`DetectorRolesConfig`, `src/config.py`) was built defaulting both to
    `alert`. Since the operator's A06.1/A06.2 decision both default to
    `evidence`. It is enforced in the two shared
    paths, not per detector:
    - `AnomalyEventEmitter._emit` stamps `role: evidence` on the stored
      event, counts it in `emitted_evidence` rather than `emitted`, and never
      pushes it to ntfy;
    - `FusionScorer.fuse` counts an evidence source toward `agreement` and
      the boost, but only an alert-role source sets the base severity, and
      `is_anomaly` needs one. The `agreement >= 2` gate is unchanged.

    The daemon wires both from config (`build_components`,
    `LiveInferencePipeline`). The dashboard status strip never shows an
    evidence event as an active anomaly, and the Anomalies panel tags it
    `evidence`. The trip digest (`src/analysis/digest.py`, schema 2) counts
    only alert-role events in `anomaly_counts_by_source`, the "gated
    anomalies" figure the ntfy digest push and the Trip panel show. Evidence
    events go in a separate `evidence_counts_by_source`, shown on its own
    "N evidence event(s) (not alerts)" line, which is left out when it is
    zero. Schema-1 digests are read as they are: they predate roles, so
    every event they counted was an alert. They are not recomputed, because
    a late recompute's window ends at "now".

    Additive payload changes, all visible to callers:
    - `role` on every stored event (`"alert"` by default);
    - `emitted_evidence` in `/status` `emitter_counters`;
    - a per-source `role` in the fused event's `detail.sources`;
    - `evidence_counts_by_source` and `schema_version: 2` in `digest.json`. Tests: `tests/test_detector_roles.py`, 19 passed
    (config defaults and refusals, example configs, emitter storage and
    no-push, per-detector roles, fusion agreement, evidence-only agreement
    never alerts, daemon wiring end to end), and
    `src/dashboard/static/src/panels/StatusStrip.test.ts` (4 role cases);
    digest tests: `tests/test_digest.py::test_compute_digest_separates_evidence_role_events_from_alerts`
    (one alert and one evidence event give 1 and 1, and the push body does
    not count the evidence event as gated), 3 `format_digest_message` cases
    in `tests/test_notifier.py`, and 2 `TripPanel.test.ts` cases. The
    existing fusion, emitter, notifier, digest and trip-end suites pass
    unchanged.
  - *The role choice:* made by the operator on 2026-09-23 (A06.1, A06.2).
    A02's harness now builds `FusionScorer` with
    `roles=config.models.roles.by_source()` (`evaluation_detectors.py:446`,
    `synthetic_fault_eval.py:501`).
  - *Publish-time refusal, done 2026-09-23:*
    - `src/models/alert_budget.py` holds each published `alert`-role detector
      to ≤ 1 alert per engine-hour on A02's evaluation drives, scored by
      `scripts/evaluate_detectors.py`. Evidence roles are never gated. The
      fused verdict is gated too whenever an alert-role source is published.
    - **Upper bound, not estimate:** the larger of the harness's drive
      bootstrap and an exact Poisson bound on pooled episodes over pooled
      engine-hours, both 97.5 %. The bootstrap reads 0 whenever every drive
      was quiet and gives no interval on one drive. The estimate would pass
      A02.11's IF (1.18, interval 0–3.94) on noise. The stated cost: zero
      alarms need about 3.7 held-out engine-hours.
    - Also refused:
      - an alert-role detector nobody measured (no evaluation holdout, a TCN
        the harness cannot score, a failed evaluation, no engine time);
      - a report that counted it as `evidence`;
      - a report on other artifact bytes (sha256).
    - Every refusal names the 2026-09-10 incident.
    - `scripts/retrain.py` trains an alert-role artifact into
      `<models>/_staging-<type>/`, which the ModelManager never reads, so a
      refused artifact is never hot-swapped mid-gate. It publishes by moving
      the files in, binaries first and the manifest last. On refusal it moves
      the artifact to `_refused-alert-budget-<ts>-<type>/`, records a
      `failed` ledger run, writes `alert-budget-<ts>-verdict.json` and exits
      `ALERT_BUDGET_REFUSED` (4). `--accept-alert-budget "<reason>"` is the
      only override. A blank reason is rejected, and the reason is recorded
      in the verdict.
    - Tests: `tests/test_alert_budget.py`, 17 passed. It fails on HEAD, where
      the module does not exist. It covers:
      - the Poisson bound against Garwood's table (0 → 3.689, 1 → 5.572);
      - evidence roles not gated;
      - one alarm in 1 engine-h refused on its 5.57/h bound;
      - zero alarms over 1.06 engine-h refused, naming the 3.7 engine-h
        needed;
      - 5 quiet engine-h passing;
      - the bootstrap bound deciding;
      - cross-era drives excluded;
      - unmeasured, evidence-counted and stale-hash refusals;
      - fused gated;
      - the override needing a reason;
      - retrain end to end: evidence publishes without scoring, an alert-role
        flood is refused and set aside while the served artifact is kept, a
        quiet one is published from staging, the override publishes and
        records, and no evaluation drive is refused. The gate's harness argv
        parses under `evaluate_detectors.parse_args`.

      `tests/test_retrain_script.py` (foreign-dirty) passes unchanged.
  - *Operator decisions, 2026-09-23:*
    1. **The daemon refuses too (option b).** An `alert` role takes effect
       only for an artifact whose sha256 has a passing alert-budget verdict.
       Otherwise the detector serves as `evidence` and `/status` names why.
    2. **The bound stays the upper confidence bound**, as built above.
  - *The daemon gate, done 2026-09-23. A config flip of `models.roles` to
    `alert` is now gated.*
    - **The verdict travels with the binary.** Retrain writes
      `<artifact>.alert-budget.json`, e.g. `model.joblib.alert-budget.json`,
      beside every gated artifact (`verdict_record` / `write_verdict`,
      `src/models/alert_budget.py`). It carries:
      - the schema version and the artifact's sha256;
      - `passed` / `accepted`, the bound, per-detector numbers and
        violations;
      - the evaluation drives with their engine-hours;
      - the date, the override reason and the incident.

      This is necessary because models reach the Pi by `scp` and
      `data/analyses` never does. `_publish_staged` moves the verdict first,
      the binaries next and the manifest last. A refused artifact keeps its
      failed verdict in `_refused-alert-budget-*`.
    - **The daemon resolves effective roles** (`DetectorRoleGate`,
      `src/models/role_gate.py`, `components.role_gate`). An
      `alert`-configured detector is `alert` only when the verdict beside the
      served binary exists, is schema 1, names the binary's sha256, and
      passed with no violations. Otherwise it is `evidence`, with the reason:
      - "alert role refused: no passing alert-budget verdict for <sha12>";
      - "… (the verdict beside it is for <sha12>)";
      - "… failed: <violations with their numbers>";
      - "unreadable";
      - "no artifact loaded".

      A publish override (`--accept-alert-budget`) is **not** a passing
      verdict under the operator's wording. That artifact publishes but
      serves as `evidence`, and the reason says so.
    - **Applied where roles are resolved, not in `fusion.py`:**
      - the emitter holds the gate as its role mapping by reference, so each
        event is stamped with the effective role;
      - the IF and sequence detectors stamp their fusion sources through
        `_with_effective_role` (`src/daemon/inference.py`).

      `FusionScorer` never promotes an evidence source, so it needed no
      change.
    - **At a hot-swap:** resolution is cached per `LoadedModel` object. The
      first lookup after a swap re-resolves, so an artifact without a verdict
      demotes at the swap. `poll_model_managers` also calls
      `role_gate.refresh()` in the same worker thread as the poll: the
      hashing stays off the event loop, and a verdict that arrives after its
      binary is picked up on the next poll.
    - **`/status`:** `health.detector_roles` shows, per source, the
      configured and effective role, the reason, the artifact path and
      sha256, the model version, and the verdict's numbers.
    - Tests: `tests/test_role_gate.py`, 13 passed. It fails on HEAD, where
      the module does not exist. It covers:
      - evidence stays evidence;
      - alert with no verdict gives evidence and the named reason with the
        sha;
      - alert with a passing verdict for this sha gives alert;
      - alert with a verdict for a different sha gives evidence;
      - a failed verdict gives evidence with its numbers;
      - an override is not passing;
      - an unreadable verdict gives evidence;
      - a hot-swap to a binary without a verdict demotes at the swap and
        publishes to `/status`;
      - a late verdict is picked up by `refresh`;
      - the emitter stamps evidence after a swap;
      - the live fusion source is demoted;
      - the `health.detector_roles` snapshot;
      - `build_components` wiring.

      `tests/test_alert_budget.py` (17 passed) now also asserts:
      - a published artifact's verdict names its sha256, lists its 5
        evaluation drives, and resolves to `alert`;
      - a refused artifact's verdict stays with it, with `accepted: false`;
      - the served directory gets no verdict.
- [~] **A06.6 — Give back the tick budget if the sequence detector stops
  alerting.** → **carried to D03.53** (2026-11-08). A novelty screen does not need 1 Hz. Measure it at a lower
  cadence, or post-drive, on the Pi, and return the time to the collector and
  the live second-brain loop (Pi item).
  - *Desk half done 2026-10-05: the cadence route.* int8/ONNX was not taken:
    ONNX Runtime is not installed, and the item's own text already names the
    cheaper route, "append every tick, forward-pass every n".
    - **Config:** `models.sequence_forward_every_n_ticks` (default 1), declared
      in `canary.example.yaml` and `canary.pi.example.yaml`. `CanaryConfig`
      refuses a cadence the persistence gate could never latch at:
      `(min_consecutive_ticks − 1) × n` tick periods must be under
      `persistence_window_seconds`. With the shipped 3-in-8 s gate at 1 Hz, n ≤ 3.
    - **Engine:** `ForwardCadence` (`src/models/inference.py`). Every row is
      appended. The first full window after a (re)fill is forward-passed,
      then every n-th. A gap, a session change or a `reset()` restarts the
      count. A full window between passes returns `SequenceCadenceSkip`
      (`scored: false`). It has no score, threshold or verdict, so it is
      never a carried-over score. It still carries the envelope check (A06.2).
    - **Daemon:** the parent and the scoring child build the engine with
      the same config cadence (`_build_sequence_engine`, `_WorkerState`).
      `SequenceDetector.apply` publishes, gates, emits and fuses nothing for a
      skip. The persistence gate is left as it is, the same as a missing
      verdict. `/status` `detectors.inference.sequence_cadence` gives the
      cadence and the `scored` and `cadence_skipped` counts. A01.7's
      `sequence_windows` counts scored windows only. The envelope meter
      still counts every window.
    - **A02.3 budget level:** `score_budget_traces` scores the windows that
      serving forward-passes, using the engine's own rule
      (`SequenceScores.at_cadence` → `served_windows`). The level record
      stores `forward_every_n_ticks`. `_build_sequence_engine` refuses to
      serve an artifact whose level was chosen at a different cadence
      (`require_budget_cadence`, logged once). A record without the key was
      chosen on every window, which is cadence 1.
    - **Graded as served, not only scored as served.** This closes the gap
      the verifier found: the A02 harness and the A06.5 verdict still
      graded every window.
      - `DeployedStack.score_sequence` applies
        `at_cadence(models.sequence_forward_every_n_ticks)`, so the
        harness's `sequence`, `sequence_global` and `fused` are graded on the
        served windows. `sequence_parity` stays at n = 1.
      - `scripts/evaluate_detectors.py` records
        `sequence_forward_every_n_ticks` in the report and the Markdown. The
        P02.5 sequence score-cache identity names the cadence
        (`sequence_identity`), so an n = 1 entry is never read at n = 3.
      - `check_alert_budget(forward_every_n_ticks=)` fails `sequence`,
        `sequence_global` and `fused` on a report scored at another cadence,
        naming A06.6. A report without the key counts as n = 1.
        `scripts/retrain.py` passes the publishing config's n.
      - `verdict_record` stores the cadence. `DetectorRoleGate` and
        `resolve_role` get the config's n from `build_components`. They
        demote to `evidence`, naming A06.6, when the verdict was measured
        at another cadence. This applies to the sequence source, and to any
        verdict that graded the fused detector.
      - The synthetic-fault harness builds its engine at the config cadence.
        Its report records `sequence_forward_every_n_ticks`.
      - A serving refusal (an A02.3 level chosen at another n) is on
        `/status` as `health.sequence_cadence_refusal`, not only in the log.
      - `benchmark_gate.incomparable_detectors` (A02.3's mechanism) names
        `sequence`, `sequence_global` and `fused` incomparable, citing
        A06.6, when two reports' `sequence_forward_every_n_ticks` differ. A
        missing key reads as 1. The IF and the baselines are still diffed.
      - Each of these ten guards was removed in turn, and in every case a
        test in `tests/test_sequence_cadence.py` failed (scratch mutation
        run). The suite is 41 tests, all passing.
    - **Parity on a held-out real drive** (`2026-10-04T194342`, served r2 LSTM
      `lstm_autoencoder_2026-09-24T035125`, engine level, every tick against
      n = 3):
      - 1,940 rows and 1,911 windows: 637 scored, 1,274 skipped.
      - **0** scored results differ. `to_dict` is identical apart from
        `latency_ms`, and the largest |Δ global_score| is 0.0.
      - 0 skips fall on a tick with no window, and 0 skips carry a different
        envelope verdict.
      - The batched `at_cadence` schedule equals the engine's.
    - **Tests:** `tests/test_sequence_cadence.py`, 41 passed (31 from the first pass). They cover:
      - the rule, refill and reset;
      - `at_cadence`;
      - budget traces and the recorded cadence;
      - the cadence refusal, including logged once;
      - both build paths;
      - a skip publishes nothing;
      - the recorder;
      - config acceptance and refusal;
      - engine parity at n = 2, 3, 5 with a window-dependent stub model;
      - *slow*: the committed drive slice through the installed artifact;
      - *slow*: the live pipeline at n = 3, in the scoring child (pickled)
        and in process.
    - **Laptop cost, before/after:** the same held-out drive, the two engines
      interleaved row by row. Load average ~16 on 12 cores, so the absolute
      numbers are high. `observe` took a median of **12.18 ms** every tick
      (total 24.2 s). At n = 3 it took a median of 2.42 ms over all ticks
      (total 11.2 s, −54 %): 12.20 ms on a scored tick and 2.29 ms on a
      skipped one (append and envelope check).
    - **Laptop pipeline replay** (`tools/replay_event_diff.drive_session`,
      240 s of the same drive, in process): `stages.sequence` median went
      from 1,118 ms to 925 ms. The engine's share went from a median of
      125 ms to 0.93 ms. `reconcile_feature_frame_to_spec`, which still runs
      every tick, stayed at a median of 736 / 718 ms. On this laptop the
      per-tick reconciliation, not the forward pass, is most of the stage.
      The Pi measurement below decides how much the cadence actually returns.
      `/status` read `sequence_cadence {n: 3, scored: 71, cadence_skipped: 141}`.
  - *Remaining (Pi):*
    - `stages.sequence` and missed ticks on the truck at n = 1 against n = 2
      or 3, on the same drive conditions;
    - the operator's choice of n (at most 3 under the shipped gate);
    - any artifact that later carries an A02.3 level must have the level
      chosen at that n.
  - *Was:* the whole item. It depends on A06.2 and needs a measurement
    on the Pi. No lower-cadence option was added. The window is state
    advanced by every row (in-process and in the scoring child), so "score
    every n ticks" means "append every tick, forward-pass every n". That
    change touches `src/models/inference.py` and the worker, not just config.
    *Note (2026-09-23):* the novelty-screen report does not need the forward
    pass. The envelope check reads the buffer's condition columns, at 39 µs
    per window on the laptop. A lower-cadence screen could keep the check
    every tick and run the forward pass every n.
  - *Option added 2026-09-26 ([P02](../P02-raw-reads-evaluation-and-pi-costs.md)):*
    keep the forward pass on every tick but make it cheaper, using int8
    dynamic quantization or an ONNX Runtime export. The gain is unmeasured
    here. It is judged by replay score parity: scores within a stated
    tolerance and identical verdicts on a held-out session. The latest
    measurement is `stages.sequence` at a median of 344 ms per tick on the
    truck (2026-09-24, above).
- [x] **A06.7 — Update the docs that describe the detectors' role.**
  *Done 2026-09-23*, citing A02.11
  (`2026-09-23-a02-detector-baseline.md`: IF 1.18 [0.00, 3.94] per
  engine-hour with 81 % of rows withheld; sequence 45.35 [24.51, 61.28];
  global 2.96 [1.66, 5.04]) and the A06.3 retrain evaluation (candidate
  global score 0 episodes against 29; family path 54.9 [41.7, 70.9]):
  - `CANary-overview.md`: the FR-07 "decision pending" note is replaced by
    the decided roles, the novelty screen and its `/status` meter, the
    primary detector and the alert-budget refusal. The config sample's
    `roles:` now reads `evidence`/`evidence`.
  - `per-mode-detection-and-mode-taxonomy.md`: a "Role" paragraph.
  - `online-training-strategy.md`: a new section, "What the retrained
    detectors are for". It covers what the central danger means for
    evidence-role detectors, the envelope record as the retrain's product,
    and the publish gate beside the promotion gate's FP/hr caveat.
  - Anomalies panel (`AnomalyPanel.tsx`): a caption saying the IF and the
    sequence model are evidence by default and naming the primary detector.
    It also has the envelope badge (A06.2). `pnpm test` 357 passed,
    `pnpm typecheck` clean.
  Update, citing A02's numbers:
  - [`CANary-overview.md`](../../../CANary-overview.md);
  - [`per-mode-detection-and-mode-taxonomy.md`](../../../per-mode-detection-and-mode-taxonomy.md);
  - [`online-training-strategy.md`](../../online-training-strategy.md);
  - the dashboard's anomaly panel copy.

## Acceptance

Each decision item is closed by the operator's choice, recorded here with
its date, plus the A02.11 table it was based on. A06.5 and A06.6 follow the
decisions. None of this removes code before A02 has measured the
replacement.
