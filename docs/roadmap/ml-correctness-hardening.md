# Roadmap — ML correctness hardening

Status: **planned** (2026-07-18). Straight to `main`, no feature branches.

## Why this exists

A five-subsystem skeptical review of the ML stack (IsolationForest,
sequence detectors, behavior surfaces, live inference/fusion, feature
pipeline/eval harnesses) on 2026-07-18 confirmed the foundations are
sound — threshold directions, quantile calibration, GRU-D decay math,
masking consistency, session-boundary isolation, scaler round-tripping,
physics formulas, and committee gating all verified correct — and
surfaced a ranked set of real gaps. The external literature check
(ADBench, TimeEval/VLDB, Wu & Keogh) supports the current portfolio
shape (physics residuals + GRU-D + cheap outlier screen) and argues
against a model replacement; the work is hardening, not rearchitecting.

Findings are grouped into phases by dependency and severity. Tier-1
items (Phase 1–2) undermine trust in every downstream score and go
first. Every fix that changes detection behavior must land with a
number attached: run the Phase-2 fault-injection benchmark from
`eval-longitudinal-and-dtc.md` (or its interim pieces) before/after.

## Guiding constraints

- Whole-repo gate before every commit: `ruff check . && ruff format
  --check . && mypy src && pytest`.
- Fix at source, quarantine bad data — no runtime tolerance machinery.
- Prefer principled solutions over threshold hacks; when a fix changes
  a distribution the models see (Phases 1, 5), plan a retrain and
  verify via replay (`start_replay`) before trusting live output.
- Don't over-engineer: this is a hobby system; the listed guards are
  cheap assertions and schema constraints, not new frameworks.
- Deploy note: model binaries are gitignored (scp, not pull);
  featurization changes need a daemon restart and usually a retrain.

## Review provenance

Severity tags below: **T1** (corrupts data/scoring trust), **T2**
(quality/config gap with a live instance), **T3** (design suitability).
Each task cites the code anchor the finding was confirmed at.

---

## Phase 1 — Stale-data visibility (T1)

**Problem.** `resample().last().ffill()` has no limit
(`src/preprocessing/resampler.py:91-92`), and slow channels
(COOLANT/IAT/BARO, `DEFAULT_SLOW_CHANNEL_HINTS` at `resampler.py:16`)
get no `__gap` flags — so a dead/stuck slow sensor forward-fills
forever, is never dropped by `_drop_fully_stale_rows`
(`feature_pipeline.py:387-412`), and coolant specifically gates
warmup/idle segmentation, silently misrouting every subsequent row's
operating mode. Separately, the live path scores stale rows training
drops, and inference never consults vehicle state.

- [x] **1.1 — Bounded forward-fill.** Add a per-channel-class ffill
  limit in `resample_wide`: fast channels limited to `max_gap_seconds`
  (existing config), slow channels to a new
  `preprocessing.slow_channel_max_gap_seconds` (default generous, e.g.
  120 s — slow sensors legitimately update slowly). Beyond the limit
  the cell stays NaN. Unit-test: a channel that stops mid-session goes
  NaN after the limit, and `__mean`/`__std` windows shrink accordingly.
  — landed: `ResampleConfig.slow_channel_max_gap_seconds` (default
  120s) + bounded ffill for every value column in `resampler.py`.
- [x] **1.2 — Gap flags for slow channels.** Stop excluding slow
  channels from gap flagging (`resampler.py:96`, `_fast_channels` at
  `:121-124`); give them their own `max_gap` (same knob as 1.1) so a
  stuck coolant sensor is visible to `_drop_fully_stale_rows` and to
  the GRU-D mask. Verify the mask semantics flow through
  `sequence_dataset._frame_mask_delta` unchanged. — landed: every
  value column now gets a `__gap` column; `_drop_fully_stale_rows` and
  `_frame_mask_delta` both already scanned generically for `__gap`
  columns, so no code change was needed in either — verified via
  tests.
- [x] **1.3 — Segmentation guard on stale coolant.** In the segmenter
  path, treat a gap-flagged coolant value as absent for mode rules
  (don't classify warmup/idle off a frozen reading) — rows fall to
  `unknown` rather than a confidently wrong mode. — landed:
  `_coalesced_series(gap_masked=...)` in `segmenter.py`, applied only
  to `coolant_temp`.
- [x] **1.4 — Live parity: stop scoring fully-stale ticks.**
  `LiveFeatureSource.tick` (`src/preprocessing/live.py:303-354`) has no
  equivalent of `_drop_fully_stale_rows`; `_current_values` holds last
  values indefinitely. Add the same rule: when every gap-flagged channel
  is stale, `tick()` returns None (detectors already treat None as
  no-op, `daemon/inference.py:105`). Also NaN out individual channels
  past their ffill limit so serve rows match Phase-1.1 training rows. —
  landed: fully-stale gate before buffer append + per-channel bounded
  ffill via `_bounded_values`, fast/slow ceiling parity with the offline
  resampler; `vehicle_state_inputs()` added as no-I/O prep for 1.5
  (commit `f495cd2`).
- [x] **1.5 — Vehicle-state gating for IF/LSTM.** `run_live_inference`
  scores every tick; `classify_vehicle_state`
  (`src/dashboard/status.py:73-143`) is display-only (confirmed: zero
  references in `daemon/inference.py`). Gate IF/LSTM scoring on
  `engine_running` (and optionally `ignition_only` if a koeo model
  exists for the mode); skip + count skipped ticks in `/status` for
  `engine_off`/`no_data`. Behavior surfaces keep their existing
  mode-gating. Replay state must still score (replay re-emits running
  data). — landed: `_live_vehicle_state` (no-I/O, built on 1.4's
  `vehicle_state_inputs`) gates IF + sequence scoring on
  `{engine_running, replay}`; behavior overlay left ungated;
  `EmitterCounters.skipped_vehicle_state` surfaced at `/status` (commit
  `41127f1`). No koeo model exists yet, so `ignition_only` stays gated
  out with the rest.
- [~] **1.6 — Retrain + verify.** Phase 1 changes the training
  distribution (rows that used to be ffilled/kept are now NaN/dropped).
  Retrain IF + sequence detector, replay a known-good session
  end-to-end, and compare event diff (`replay_window` `_diff_events`)
  before deploying. — laptop half landed: smoke retrain (12 sessions,
  reduced epochs) + LSTM replay diff against a real held-out session;
  findings + two pre-existing tooling bugs surfaced (not fixed, deferred
  to Q3) in `data/analyses/ml-hardening-1.6-retrain-replay-diff-2026-07-19.md`
  (commit `2a3f8ae`). **blocked-overnight**: the IF-side replay diff
  (needs Q3's `scripts/replay.py`/`build_feature_frame` CAN-decode
  parity fix) and the actual Pi deploy (needs the operator's truck +
  `scp` + daemon restart) are both out of scope for this run.

## Phase 2 — Missing-channel handling + IF hardening (T1/T2)

**Problem.** One permanently absent trained channel silently blinds IF
for that mode — `reconcile_feature_frame_to_spec` NaN-fills it
(`feature_pipeline.py:823-825`), `score()` marks all rows missing
(`isolation_forest.py:296-303`), `_score_and_emit` suppresses forever
with a once-per-process log (`daemon/inference.py:619-629`,
`_IF_MISSING_WARNED` at `:446`). Sparse modes also train meaningless
models that emit ungated.

- [x] **2.1 — Surface IF blindness.** Replace once-per-process warn
  with: a `missing_feature_rate` per-mode gauge already in `/status` —
  add an explicit `detectors.isolation_forest.blind_modes` list to the
  status payload + `get_status`, and log a fresh warning when a mode
  *transitions* into blindness mid-drive (edge-triggered, not
  per-tick). The dashboard SystemPanel shows it. — landed:
  `HealthMonitor` tracks an 8-tick per-mode rolling window, edge-fires
  once on the blindness transition (re-arms on recovery);
  `detectors.isolation_forest.{blind_modes,missing_feature_rate_by_mode}`
  wired into `build_status`/`get_status`; SystemPanel shows it.
- [x] **2.2 — `min_samples_per_mode` floor.** Add a training guard
  mirroring `_MIN_CALIBRATION_SAMPLES` (`isolation_forest.py`): modes
  with fewer fit rows than the floor (default ≥ 100) are skipped (no
  model persisted) rather than fitting a 200-tree forest on a handful
  of rows. Skipped modes are listed in the manifest. — landed:
  `IsolationForestConfig.min_samples_per_mode` (default 100); skipped
  modes get no persisted model and land in `manifest.skipped_modes`.
- [x] **2.3 — Retire the ungated legacy-label emit.** Uncalibrated
  modes currently bypass the PersistenceGate and fire on the sklearn
  contamination label (`daemon/inference.py:657-674` →
  `emitter.py:147-148`). Decision: make uncalibrated modes
  **non-emitting** (score recorded, no events) — consistent with 2.2's
  "no meaningless models" stance. Remove or gate the
  `emit_isolation_forest_row` legacy path at serve. — landed:
  `_score_and_emit` only emits when `calibrated_threshold is not None`;
  score still recorded via `_publish_score`/health regardless; old
  once-per-process `_IF_MISSING_WARNED` path removed.
- [x] **2.4 — Scaler leakage nit.** Fit `ZScoreScaler` on `fit_index`
  rows only (`isolation_forest.py:388` currently fits pre-split).
  Cosmetic-scale effect; cheap to do during 2.2's edit. — landed:
  scaler now fits only on the union of surviving modes' `fit_index`
  rows, restricted to columns those modes actually use (a mode-gated
  column exclusive to a 2.2-skipped mode gets an inert identity
  placeholder instead of starving the fit — caught by review, fixed
  before landing).
- [x] **2.5 — Feature redundancy for IF.** Each channel expands 4×
  (raw + `__mean` + `__std` + `__roc`, `features.py:14-15,34-36`),
  diluting IF's random-split mechanism. Trial in the benchmark: IF on
  raw+`__roc` only (or raw-only) vs current. Adopt whichever wins on
  detection-rate/FP; keep the full set for the sequence detector
  (masking makes it cheap there). — landed: trial machinery is
  `restrict_isolation_forest_expansion` (`feature_pipeline.py`) — drops
  requested suffixes from an already-resolved IF column list, fed
  straight into `fit_isolation_forest`'s existing `feature_columns`
  override (no new config knob needed). Trialed full-4x vs raw+`__roc`
  vs raw-only on a fixed-seed synthetic two-mode corpus (3 seeds,
  20 injections/fault kind) through `evaluate_detectors`: full-4x won
  on both axes — mean detection rate 0.97-0.99 vs raw+roc's 0.96-0.99
  (comparable) and raw-only's 0.89-0.93 (notably worse), AND full-4x's
  nominal FP/hr (451-499) was *lower* than raw+roc's (499-559), not
  higher as the dilution theory predicted. The gap concentrates in the
  "stuck" fault kind (full-4x 0.85-0.95 vs raw-only 0.45-0.65): a
  frozen sensor's primary signature is `__std`/`__roc` collapsing to
  ~0, not a duplicate of the raw signal — dropping them removes
  information IF needs, it doesn't reduce redundancy. **Decision: kept
  the current full 4x set as the production default** (no change to
  `scripts/train_anomaly_model.py`); the restriction helper stays in
  the tree (tested, unused by the default path) for a future retrial
  against a real multi-session corpus. Sequence detector untouched.
- [x] **2.6 — ECOD comparison (optional, cheap).** Add `pyod`'s ECOD as
  a benchmark-only alternative screen in `synthetic_fault_eval` /
  `detector_benchmark` — parameter-free, top-tier in ADBench. Promote
  only if it beats IF on the benchmark; otherwise drop the dependency.
  — landed: wired ECOD as a fourth optional `evaluate_detectors`
  detector (per-mode fit + held-out calibration mirroring IF's split,
  in a benchmark-only `src/models/ecod_eval.py`, never imported by
  `src/daemon/` or `model_manager.py`), trialed it against IF on the
  same fixed-seed synthetic corpus/seeds as 2.5 (full 4x feature set
  for both). **ECOD lost decisively**: mean detection rate 0.55-0.76 vs
  IF's 0.97-0.99, and it missed "dropout" almost entirely (0.0-0.05 vs
  IF's 1.0 — ECOD's empirical-CDF scoring doesn't pick up a
  hold-then-NaN-gap signature the way IF's random splits do). FP/hr was
  also inconsistent across seeds (174-403) despite the same calibration
  target. Per the task's own instruction, **dropped `pyod` entirely**
  (reverted the `evaluate_detectors`/`ecod_eval.py` wiring, the
  `pyproject.toml` `bench` extra + mypy override, and uninstalled it
  from the dev venv) rather than leave a losing dependency half-adopted;
  `synthetic_fault_eval.py`'s module docstring keeps a short note of
  what was tried so this isn't re-litigated blind next time.

## Phase 3 — Calibration enforcement (T2)

**Problem.** Held-out threshold calibration is enforced only by the CLI
wrappers; library entry points default to in-sample
(`lstm_training.py:340-342`, `tcn_training.py:337-339`) — the exact
mechanism behind the earlier FPR≈1.0 flood. The default threshold is
also `mean + k·σ` on right-skewed errors instead of the calibrated
quantile.

- [x] **3.1 — Hard requirement or loud refusal.** Make
  `train_lstm_autoencoder`/`train_tcn_autoencoder` raise (or require an
  explicit `allow_in_sample_calibration=True` escape hatch that stamps
  `calibration: in_sample` into the manifest) when no calibration
  dataset is provided. Load-time: daemon logs a warning when serving an
  `in_sample`-stamped artifact. — landed: both trainers raise unless
  `allow_in_sample_calibration=True`; manifest gains a
  `calibration: "held_out"|"in_sample"` field; `ModelManager._maybe_load`
  warns on an `in_sample`-stamped artifact (via manifest `extras`); CLI
  wrappers (`train_lstm.py`/`train_tcn.py`) thread the flag + log a
  warning for their pre-existing documented no-flag fallback.
- [x] **3.2 — Quantile threshold as default.** Flip
  `threshold_quantile` from `None` to a concrete default (e.g. 0.995)
  in `config.py:273,313` so the calibrated-FPR path is the default and
  k·σ is the opt-out. Re-verify `false_positive_benchmark`'s k-sweep
  provenance note (see 8.3). — landed: both trainers' `threshold_quantile`
  default to 0.995; `false_positive_benchmark`'s `_quantile_band` already
  handled an arbitrary calibrated quantile correctly (exact-match
  special case), no code change needed there; k-sigma-specific test
  fixtures pinned `threshold_quantile: None` explicitly.
- [x] **3.3 — Bake-off defaults.** `sequence_bakeoff` calibrates
  in-sample unless `--calibration-sessions` is passed — make the flag
  required (or auto-carve held-out sessions) so a default bake-off is
  production-faithful. — landed: auto-carves a held-out split (reusing
  `retrain.py`'s `_pick_calibration_holdouts`) when the flag is omitted;
  errors (exit 1) instead of silently calibrating in-sample when the
  corpus has fewer than 3 sessions to spare a holdout.

## Phase 4 — Behavior-surface publish gates + serve quality (T2/T3)

**Problem.** No fit-quality gate actually runs (docstrings claim one;
zero `r_squared` references in `refit.py` — confirmed), the σ-floor fix
is convention-only (`DeviationConfig.sigma_floor` optional and may be
0, `behavior_models.py:222`) with a live hair-trigger instance
(`tps_voltage_per_pct`, `refit.py:452-472`, no floor), 2D serve is
nearest-cell over per-refit auto-recomputed edges, and warmup surfaces
model f(RUN_TIME) with load/ambient confounding.

- [x] **4.1 — Publish gate.** In `fit_recipe`/`refit_all`
  (`refit.py:495-706`): refuse to publish a surface whose `r_squared`
  is below a per-recipe floor (default e.g. 0.6, overridable) or whose
  populated-cell coverage is below a floor (kills the silent all-empty
  2D surface, `surface_2d.py:89-114`). Refusals are `RecipeResult`
  skips with reasons, visible in the refit CLI output. — landed:
  `min_r_squared` (1D, default 0.6) + `min_populated_cell_fraction` (2D,
  default 0.3, denominator is the full grid incl. empty cells) both
  route through the existing skip-with-reason path.
- [x] **4.2 — Schema-enforce the σ-floor.** Pydantic validator on
  `BehaviorModel`/`DeviationConfig`: `kind="relative"` requires
  `sigma_floor` present and > 0. Migration: add floors to every shipped
  relative recipe — including `tps_voltage_per_pct` (pick a floor from
  the fitted residual distribution, e.g. max(fit std, sensor LSB)). —
  landed: `DeviationConfig` validator rejects `sigma_floor` None/≤0 for
  `kind="relative"` only; `tps_voltage_per_pct` gets `sigma_floor=0.005`
  (one LSB of `ECM_THROTTLE_POS_SENSOR_1`, verified against
  `dids.yaml`'s `scale: 0.005`).
- [x] **4.3 — Freeze 2D bin edges across versions.** `_auto_edges`
  (`refit.py:778-791`) recomputes quantile edges each refit → versioned
  cells cover different operating rectangles. On first fit, persist the
  edges into the model; subsequent refits reuse stored edges unless a
  recipe explicitly re-grids (which bumps a `grid_version`). This makes
  cross-version drift comparison valid. — landed: refits reuse the
  prior model's edges by default (verified against genuinely drifted,
  not identical, data); `regrid=True` (new `--regrid <recipe>` CLI
  flag) recomputes edges and bumps `grid_version`.
- [x] **4.4 — Interpolate at serve (1D-style smoothness for 2D).**
  Replace nearest-cell `evaluate_2d` (`surface_2d.py:158-180`) with
  bilinear interpolation over populated neighbor cells (fall back to
  nearest when neighbors are empty). Kills severity flips from
  operating-point drift across a bin edge. — landed: real bin-center-
  distance-weighted 2×2 blend; falls back to nearest containing cell
  when a full populated quad isn't available.
- [x] **4.5 — Version bump only on change.** Consult
  `training_data_hash` (`behavior_models.py:114-121`) before bumping
  `version` in `refit_all` (`refit.py:662-663`); identical fit → same
  version, no churn. — landed: hash comparison happens after 4.1's
  publish gate (a gated-out fit never reaches version logic), so no
  interaction bug between the two.
- [x] **4.6 — Warmup confounding.** Either (a) add a context axis to
  the warmup recipes (2D: RUN_TIME × engine load or ambient IAT), or
  (b) widen bands + document the confound. **Decision rule
  (operator-approved, see OVERNIGHT-QUEUE.md):** count cold-start
  sessions with usable load/IAT spread first; ≥ 8 → do (a), fewer →
  do (b), and log which path was taken. The same lesson already
  retired the load-blind timing surface (`refit.py:186-190`). — landed:
  real local corpus has only 2 cold-start sessions (verified), far
  under the floor of 8 → took path (b), widened
  coolant_warmup/cat_warmup/injector_pw_warmup bands ~50% with
  documented rationale.
- [x] **4.7 — Segmenter-fallback diagnostic.** When the runtime
  segmenter falls back to constant `"all"` mode (`live.py:333-335`),
  every mode-gated surface silently never fires — log it and surface a
  flag in `/status` (complements the existing
  `unknown_operating_modes` load-time warning). — landed:
  `detectors.segmenter.fallback_active` (edge-triggered, mirrors 2.1's
  `if_blind_modes` pattern), only for the runtime input-missing
  fallback (not an explicit `single_operating_mode` config).

## Phase 5 — Operating-mode taxonomy fixes (T2)

**Problem (verified in `canary.example.yaml`).** `city` constrains only
`speed_max: 50` so warm idle at >900 rpm falls through to it; 50–80
km/h is an unclassified dead zone (`highway` floors at 80); no
hysteresis → 1 Hz mode flapping contaminates per-mode models. The
segmenter *code* is correct; the rule set leaks.

- [x] **5.1 — Close the fall-throughs.** Add `speed_min` (e.g. 5) to
  `city`; raise idle `rpm_max` (or add an `idle_high` mode for
  A/C-load/fast-idle up to ~1200 rpm); add a `suburban` (50–80 km/h)
  mode or extend `city` to meet `highway`. Update all `canary*.yaml`
  variants + the Pi config (deploy note). — landed: `city.speed_min: 1`
  (started at 5, lowered after review caught a new 0-5km/h creep dead
  zone), new `idle_high` (900-1200rpm) and `suburban` (50-80km/h)
  modes, applied across all tracked `canary*.yaml` variants; Pi config
  needs the same edit hand-synced at deploy (noted in
  `canary.pi.example.yaml`, not touched on the Pi itself).
- [x] **5.2 — Hysteresis/debounce.** Add a dwell requirement to mode
  transitions (e.g. new mode must hold N consecutive rows before the
  label switches; `recent_speed` at `segmenter.py:261-294` is the
  existing precedent for causal smoothing). Keep it in the shared
  segmenter so batch and live agree. — landed:
  `PreprocessingConfig.mode_dwell_ticks` (default 1 = legacy immediate
  switch; all configs set 3); `_apply_mode_dwell`/`_debounce_sequence`
  inside the shared `segment_operating_modes`, called by both the batch
  loader and live's rolling-buffer re-segmentation — one function, not
  two implementations, verified to agree including at buffer-eviction
  boundaries.
- [x] **5.3 — Re-segment + refit.** Mode labels feed per-mode IF
  models, sequence per-mode thresholds, behavior-surface gating, and
  session classification. After 5.1/5.2: re-run `label_sessions.py
  --rebuild`, retrain, and compare mode-second distributions
  (`get_corpus_mode_summary`) before/after to confirm the dead zone and
  fall-throughs actually closed. — landed: offline mode-second
  comparison (no live daemon for `get_corpus_mode_summary`) confirms
  the fix on real sessions — `suburban` picked up 1.28pp of previously-
  `unknown` time, `city`'s over-broad share dropped 1.61pp, `unknown`
  decreased overall. Real `--all --rebuild` run + a bounded IF smoke
  retrain both completed cleanly. Full report + 3 out-of-scope findings
  (thin local corpus, un-quarantined schema-broken sessions, 87%
  unknown baseline) in `data/analyses/ml-hardening-5.3-resegment-retrain-2026-07-19.md`.

## Phase 6 — Persistence, fusion, and emit semantics (T3)

**Problem.** `PersistenceGate` hard-resets on any non-firing tick
(`fusion.py:100-103`) — K=3-consecutive at 1 Hz structurally cannot
alert on intermittent faults; gate keys ignore operating mode; tick
semantics differ between IF (freezes on missing) and LSTM/behavior
(reset); fusion is OR-with-boost that double-emits with per-source
events; tuner debounce stacks on the built-in gate; fused dedupe key
churns on hot-swap.

- [x] **6.1 — M-of-N persistence.** Replace N-consecutive with an
  M-of-N sliding window per key (e.g. 3-of-5 default, config under
  `alerts.`), preserving flood protection while catching flicker.
  Wall-clock-bound the window (N ticks *or* T seconds, whichever is
  shorter) so streaks can't accumulate across long data gaps
  (fixes the freeze inconsistency too — define observe-on-every-tick
  semantics for all three detector paths, including IF missing ticks).
  — landed: `PersistenceGate` keeps a trailing (timestamp, hit) window,
  latches on M hits within N ticks or T seconds (default 3-of-5, 8s);
  `window=required` is provably equivalent to the old strict-N-
  consecutive default; `firing=None` freezes the window (uniform
  missing-verdict semantics for IF blindness, LSTM buffer-not-full, and
  behavior off-mode ticks).
- [x] **6.2 — Decide what fusion is.** Either (a) make it a real
  agreement gate: fused `is_anomaly` requires `agreement >= 2` (single
  sources still emit their own events), or (b) keep OR-with-boost but
  stop double-emitting: suppress the per-source event when a fused
  event covering the same tick fires, and rename the config/docs to
  "escalation score". **Decision made (operator-approved, see
  OVERNIGHT-QUEUE.md): implement (a).** Update config docs accordingly.
  — landed: `FusionConfig.min_agreement` (default 2); `is_anomaly`
  requires `severity >= threshold AND independent_groups >= min_agreement`;
  per-source events unchanged/unsuppressed.
- [x] **6.3 — Tuner/gate stacking.** Document (or unify) the two
  debounce layers — built-in PersistenceGate (keyed by source) and
  `AlertTuner` `min_consecutive` (keyed by source+mode,
  `tuning.py:319-346`). Simplest: tuner debounce *replaces* the
  built-in requirement for that key rather than stacking. — landed:
  `AlertTuner.has_debounce_override` bypasses the built-in gate
  entirely (not stacked) at all three detector call sites when a tuner
  override exists for that key.
- [x] **6.4 — Stable fused dedupe key.** Drop model versions from the
  fused event's dedupe identity (`daemon/inference.py:216`,
  `emitter.py:263`) so a hot-swap can't burst a duplicate alert inside
  the dedupe window; keep versions in the event payload. — landed:
  `AnomalyEvent.dedupe_key` is a stable `"fused"` for fused events;
  `model_version` still carries full per-model provenance in the
  payload.
- [x] **6.5 — Benchmark the gate change.** Re-run the FP benchmark and
  (once built) the fault-injection benchmark with intermittent-fault
  cases (fire/gap patterns) to demonstrate 6.1 catches them at
  unchanged clean-drive FP. — landed: new `"intermittent"`
  `AnomalyKind` (fire-gap-fire, benchmark-only — excluded from
  `DEFAULT_FAULT_KINDS` since it structurally can't clear the CI
  guard's strict-gate floor). Same seeded injection placements through
  strict-3-consecutive vs the 3-of-5/8s M-of-N default: detection rate
  10/30 (33%) → 21/30 (70%), median time-to-detect 8.0s → 4.0s, clean-
  drive gated alerts 0 under both. Independently reproduced by the
  verifier, incl. a calibration sweep confirming the comparison point
  isn't cherry-picked toward maximal apparent improvement.

## Phase 7 — Replay in-memory isolation (T3)

**Problem.** Control-plane replay injects into the same live queue
(`replay_control.py:166`) and shares `LiveFeatureSource` buffers,
`PersistenceGate` streaks, and emitter dedupe/counters with live
collectors — storage/JSONL isolation is complete
(`replay_control.py:11-24`) but in-memory state is not.

- [x] **7.1 — Reset on replay boundaries.** On `enter_replay_mode` /
  `exit_replay_mode`: clear `LiveFeatureSource` (`_current_values`,
  buffer), reset `PersistenceGate._streaks`, and snapshot/restore or
  segregate emitter dedupe + counters, so live→replay→live is clean.
  — landed: `PersistenceGate.reset()`, `LiveFeatureSource.reset()`,
  and `AnomalyEventEmitter.enter_replay_mode()`/`exit_replay_mode()`
  (counters/dedupe snapshotted-then-reset on entry, restored on exit —
  a running live total, not hard-reset like the other two);
  `ReplayController` fires all three on both entry and the single exit
  path (`.stop()` and natural completion funnel through the same
  reaper). A real bug (unmatched `exit_replay_mode()` silently wiping
  live dedupe state instead of no-op'ing) caught by review and fixed
  before landing.
- [x] **7.2 — Pause live collectors during replay (decide).** Either
  refuse `start_replay` while `vehicle_state` is `engine_running`
  (replay is a bench/desk tool), or gate live collector records out of
  the queue while a replay is active. **Decision made
  (operator-approved, see OVERNIGHT-QUEUE.md): refuse — return 409
  with a reason string while `engine_running`.** — landed:
  `post_replay_start` refuses with 409 only for `engine_running`
  (ignition_only/engine_off/no_data/replay/unknown all still allowed);
  the MCP `start_replay` tool routes through the same HTTP endpoint,
  no bypass.
- [x] **7.3 — Test.** Integration test: live ticks → start replay →
  stop → live ticks; assert no cross-contaminated streaks/dedupe and
  the live buffer restarts empty. — landed:
  `tests/test_replay_isolation.py` (`-m integration`), drives a real
  `run_daemon` through live→replay→live with a stub IF model; confirmed
  it has teeth (fails when a reset is reverted). Not part of the
  standard fast-tier gate; run explicitly tonight and passed
  consistently (3/3).

## Phase 8 — Eval-harness realism (T2 — feeds the benchmark roadmap)

**Problem.** The synthetic harness overstates dropout recall (injects
NaN post-resample; production produces ffill + gap flag), undercounts
late detections (only flags strictly inside `[start, end)` count,
`synthetic_fault_eval.py:228-231`), and the k-sweep reparameterizes
thresholds that production may have set by quantile.

These merge into **Phase 2 of `eval-longitudinal-and-dtc.md`** (tasks
2.1–2.7 there); listed here for completeness:

- [x] **8.1 — Realistic dropout injection.** Inject dropout *pre-
  resample* (or emulate: hold last value + set gap flags) so the
  benchmark measures the production-visible signature, not an
  auto-detected NaN. (Depends on Phase 1 landing first — the
  production signature changes.) — landed: `AnomalyInjector.dropout()`
  emulates `resample_wide`'s bounded-ffill exactly (held value +
  `__gap=False` until a required `max_gap_seconds` ceiling, then NaN +
  `__gap=True` together past it, coupled the same way
  `resampler._gap_flags` couples them); callers derive the real
  fast/slow ceiling instead of guessing one. Verified against a live
  `resample_wide` run — both transition at the identical row (commit
  `6123d45`).
- [x] **8.2 — Post-window detection grace.** Count detections within
  `window_seconds` after injection end as (late) hits with recorded
  latency, not misses. — landed: `_first_sustained_detection` takes a
  separate `grace_end` and returns `(timestamp, is_late)`; `late_flags`/
  `late_hits` added to `_DetectorMetrics`, latency still measured from
  `window.start` for late hits (commit pending).
- [x] **8.3 — Threshold provenance in the k-sweep.** When the manifest
  used `threshold_quantile`, sweep quantiles (not k·σ) so
  `recommended_k`-style outputs share production's parameterization. —
  landed: `FalsePositiveBenchmarkReport.threshold_provenance` +
  `_quantile_sweep`/`_quantile_band` (limited to the 4 quantiles the
  manifest persists, p50/p90/p95/p99, plus the calibrated quantile
  itself); k-sigma path unchanged for non-quantile models (commit
  `d2ca42e`).
- [x] **8.4 — ROC-across-gap seams.** `__roc` divides a fixed row-shift
  by `window_seconds` on a grid with dropped-row holes
  (`features.py:150-154`) — compute RoC from actual timestamp deltas
  (or NaN it across seams) so gap edges don't produce spurious slopes.
  — landed: divide by the row pair's actual elapsed time (NaN when
  non-positive); zero behavior change on a dense grid, regression test
  proves the old code reported 300 instead of the true 100/sec slope
  across a 15s dropped-row seam (commit `b84da4c`).

## Phase 9 — Model-portfolio actions (T3, informed by literature)

- [x] **9.1 — Settle the backbone bake-off.** The TCN decoder
  reconstructs from a single broadcast latent (`tcn.py:314-316`) —
  materially weaker temporal-shape modeling than the GRU decoder.
  **Decision made (operator-approved, see OVERNIGHT-QUEUE.md): make
  GRU-D LSTM the default `sequence_detector` and document TCN as an
  NPU-export fallback, not an equal.** No further bake-off needed to
  proceed. — confirmed: `sequence_detector` already defaulted to
  `"lstm"` in `src/config.py`; added the decision docstring + a config
  test asserting the default so it can't silently drift.
- [ ] **9.2 — DAMP spike (optional).** Prototype matrix-profile
  left-discords (DAMP, KDD'22) offline over a few sessions'
  per-channel streams as a model-free third opinion; evaluate in the
  benchmark. Adopt only if it adds detections the current stack
  misses; it shares no failure modes with reconstruction/isolation
  methods and is computationally trivial at 1 Hz.
- [x] **9.3 — Cold-start Δt note.** Mid-session daemon restart
  under-reports Δt for channels last seen pre-restart
  (`inference.py:139-141,237`) — bounded distortion on the first
  window(s). Either persist last-observation timestamps across restart
  or accept + document; don't build machinery beyond a comment unless
  benchmark shows impact. — landed: accept + document (no cross-restart
  persistence machinery). Confirmed `SequenceWindowBuffer` really does
  rebuild fresh on every daemon (re)start with no persisted last-
  observed state, and confirmed the distortion is genuinely bounded —
  a channel's Δt resets to 0 correctly on its next real observation and
  accumulates truthfully from there.
- [x] **9.4 — "Relative" naming.** `DeviationConfig.kind="relative"` is
  σ-scaled (z-score), not percent-of-expected
  (`behavior_decoder.py:291-304`). Rename to `sigma` (with backward
  compat alias) or document loudly; today the name invites
  misconfigured bands. — landed: canonical literal is now `"sigma"`;
  a before-validator aliases legacy `"relative"` with a deprecation
  warning. Real shipped `data/nissan-xterra-2008/behavior_models.yaml`
  (uses `kind: relative`) confirmed still loads unchanged.

---

## Cross-cutting acceptance

- [ ] Whole-repo `ruff` + `mypy` + `pytest` green on every commit.
- [ ] Every phase that changes featurization, segmentation, or
  thresholds: retrain affected models, replay a pinned known-good
  session, and compare event diffs before Pi deploy.
- [ ] FP benchmark (and the fault-injection benchmark once built) run
  before/after Phases 1, 2, 5, 6 — no unexplained regression in
  clean-drive alerts/hour or detection rate.
- [ ] `docs/` updated where behavior changes (data-sources for
  ffill/gap semantics, per-mode stub for taxonomy changes,
  mcp-server/headless-api for new status fields).
- [ ] Pi deploy verified live per phase (pull + scp binaries +
  restart; check `vehicle_state` before interpreting detector output).

## Code anchor index (from the 2026-07-18 review)

| Finding | Anchor |
|---|---|
| Unbounded ffill | `src/preprocessing/resampler.py:91-92`; slow hints `:16`; fast-only gap flags `:96,121-124` |
| Stale-row drop (batch-only) | `src/preprocessing/feature_pipeline.py:387-412` (called `:379`); no live equivalent in `live.py:303-354` |
| Vehicle state unused in inference | `src/dashboard/status.py:73-143` vs `src/daemon/inference.py` (no refs) |
| IF missing-feature suppression | `isolation_forest.py:296-303` → `daemon/inference.py:619-629`, once-warn `:446` |
| IF holdout/threshold | fit/holdout split `isolation_forest.py:432-444`, quantile `:465`, scaler pre-split `:388` |
| Ungated legacy emit | `daemon/inference.py:657-674` → `emitter.py:142-148` |
| Sequence calibration defaults | `lstm_training.py:340-342`, `tcn_training.py:337-339`; guard precedent `lstm_training.py:496-502` |
| Threshold default k·σ | `config.py:273,313`; distribution `sequence_training.py:463-466` |
| PersistenceGate reset | `src/alerts/fusion.py:97-110`; keys `:83-85` |
| Fusion OR + boost | `fusion.py:144-234`; behavior severities `:196-204` |
| R² never gated | `src/analysis/refit.py` (zero refs; fit paths `:495-706`) |
| σ-floor optional | `behavior_models.py:213-234`; hair-trigger recipe `refit.py:452-472` |
| 2D nearest-cell + auto-edges | `surface_2d.py:158-196`; `refit.py:753,778-791` |
| Version churn | `refit.py:662-663`; hash `behavior_models.py:114-121` |
| Mode-rule leaks | `canary.example.yaml:99-147` (city `:139`, highway `:141`, idle `:127`) |
| Segmenter smoothing precedent | `segmenter.py:261-294` (`recent_speed`) |
| Replay shared queue | `src/control/replay_control.py:166`; isolation contract `:11-24` |
| Synthetic-eval accounting | `synthetic_fault_eval.py:223,228-231`; dropout NaN `anomaly_injection.py:201` |
| TCN broadcast-latent decoder | `src/models/tcn.py:314-316`; causal conv OK `:135-158` |
| STFT double-smoothing | `analysis/derived.py:70,111-141,343-353` + rolling re-smooth in `compute_features` |
