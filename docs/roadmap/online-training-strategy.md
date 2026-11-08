# Online training strategy — continual retrain, and the guards that make it safe

How the system updates its anomaly models while it keeps collecting, and —
the part that actually needs design — how training data is validated so a
continually-retrained detector doesn't quietly learn the engine's faults as
"normal."

Conclusion up front: the *mechanism* (retrain over recent data, hot-swap
into the running daemon with no restart) is ~80 % built. The *unsolved*
part is the guard model. Filtering training data on factory-spec nominal
ranges feels like a guard but is the specific trap that produces either no
data or a false-positive death spiral. The right model is a **disposition
pipeline** with **directional, provenanced overrides** for known-accepted
faults. This doc specs both.

## What "online" means here — and what it doesn't

Not per-sample streaming SGD. Two reasons, both about *these* detectors:

* **`IsolationForest` has no `partial_fit`.** `fit_isolation_forest`
  (`src/models/isolation_forest.py`) does a wholesale `model.fit(x)` on a
  batch tree ensemble. The only incremental form it has is *rebuild on a
  rolling window*.
* **The LSTM autoencoder's threshold is post-hoc calibrated** over a held
  reconstruction-error distribution (`LSTMThresholdCalibration`,
  `ReconstructionErrorDistribution` in `src/models/lstm_training.py`).
  Online weight updates make that calibrated threshold stale immediately,
  on top of catastrophic-forgetting risk against a drifting target.

So "online" here = **scheduled rolling-window batch retrain + hot-swap**
(continual learning at minute-to-hour cadence), which is exactly the
cadence `ModelManager`'s docstring says it was built for. True streaming
(`learn_one`/`score_one`, e.g. River `HalfSpaceTrees`) is only sensible as
an *additive* fast detector alongside the batch IF, never a replacement,
and is out of scope until the guards below exist.

## What already exists

| Capability | Where | State |
|---|---|---|
| Continuous data capture during operation | collection → Parquet | Done |
| Hot-swap a new model into the live daemon, no restart | `src/models/manager.py` (`ModelManager`) | Done — atomic, manifest-schema-validated, rejects bad swaps |
| Side-by-side IF + LSTM retrain over recent sessions | `scripts/retrain.py` | Done — "Manual trigger in v1; cron later" |
| Training run lineage + promotion lifecycle + rollback | `src/models/training_ledger.py` | Done — `unpublished → active`, latest-line-wins per `run_id` |
| Reactive operator verdicts feeding retrain | `src/models/feedback.py` | Done — `true_positive/false_positive/ignored/needs_review`, `data/control/anomaly_feedback.jsonl` |
| Observed-vs-FSM-spec range scoring | `src/signals/fsm.py`, `src/analysis/range_match.py` | Done — built for DID binding; **reusable as the validation screen** |
| Held-out threshold calibration in retrain | `scripts/retrain.py` (`--calibration-sessions`, automatic holdout via `src.models.evaluation.pick_calibration_holdouts`) | Done — and ml-hardening Phase 3 makes in-sample calibration a loud refusal at the library layer |
| Per-vehicle data partitioning | `src/vehicle/profiles.py` (`scope_config`) | Done — everything this doc adds is per-vehicle state and must live under the profile scope |
| Scheduled trigger + training-data validation | — | **Missing. This doc.** |

The delta to "online" is small in mechanism (a scheduler + a rolling
window) and entirely in the guard model.

## What the retrained detectors are for (A06, 2026-09-23)

The operator decided the learned detectors' roles on 2026-09-23
([A06](tasks/archive/A06-detector-roles.md)), on A02.11's held-out table
(`data/analyses/2026-09-23-a02-detector-baseline.md`; false alarms per
engine-hour against a budget of 1):

- **Isolation Forest: evidence only.** 1.18 [0.00, 3.94] with 81 % of rows
  withheld, and blind to a single-quantity shift of any size (A04.1).
- **Sequence detector: a non-alerting novelty screen.** 45.35 [24.51, 61.28]
  as served, 2.96 [1.66, 5.04] on its global score alone. Its events say
  whether the window was outside the artifact's training envelope, and
  `/status` `detectors.inference.sequence_envelope` meters how much of the
  driving falls outside it, by mode and quantity.
- **Primary detector: the behavior models and the deterministic layer**, with
  A05's plausibility checks for gross faults.

For this document that changes what a continual retrain of the IF and the
sequence model buys. An evidence-role detector cannot silence an alarm,
because it raises none, so "the central danger" below applies to them only
as a quieter evidence stream. It applies in full to the behavior models,
which B03 refits. The retrained sequence model's useful product is its
recorded envelope, and the coverage meter is how the corpus after the
supercharger build shows where it still lacks data. The A06.3 retrain on the
current calibration (`data/analyses/2026-09-23-a06-3-retrain-evaluation.md`,
one held-out drive) quieted the global score to 0 episodes against the
deployed model's 29, but its per-channel path still read 54.9 [41.7, 70.9]
per engine-hour, so it stays evidence.

A detector flipped back to `alert` has to earn it at publish time:
`scripts/retrain.py` trains an alert-role artifact into a `_staging-*`
directory and publishes it only when the 97.5 % upper bound of its false
alarms per engine-hour on A02's evaluation drives is at most 1. The bound is
the larger of the drive bootstrap and an exact Poisson bound, so zero alarms
need about 3.7 held-out engine-hours. Otherwise it exits 4 and leaves the
artifact in `_refused-alert-budget-*`; `--accept-alert-budget "<reason>"` is
the only override (`src/models/alert_budget.py`, A06.5). The daemon holds the
same line: an `alert` role serves only with a passing
`<artifact>.alert-budget.json` verdict for the binary's sha256 beside it, so a
hot-swapped or scp'd artifact without one serves as evidence
(`src/models/role_gate.py`, `/status` `health.detector_roles`). Unlike the promotion
gate's FP/hr (caveat 3 below), this counts episodes inside a labelled `fault`
window as catches, not false alarms.

## The central danger

An unsupervised anomaly detector retrained on freshly collected data will,
unguarded, **adapt to a developing fault and stop flagging it**. The
vacuum leak grows; its sessions enter training; the model relearns "normal"
to include the leak; the alert goes silent precisely as the problem gets
worse. Nothing in the loss function objects. Every guard below exists to
break this loop.

## Three "normals" — keep them separate

The whole design hinges on not collapsing these:

1. **Spec normal** — what a *healthy* 2008 Xterra does. Vehicle-agnostic;
   lives in the FSM (`xterra_fsm/ec.pdf`, extracted by `fsm.py`).
2. **This-engine baseline** — what *this* truck does when it is "as good
   as it is going to get," known-bad parts included.
3. **Anomaly = departure from (2), not (1).** A known weak injector that
   has read +12 % fuel trim for two years is not an anomaly; it
   *worsening* to +18 % is.

The detector must be trained toward (2) and alert on Δ-from-(2). FSM spec
(1) is a screening and data-quality reference, **never** the training
filter.

## Why FSM-nominal-as-a-training-filter is the trap

Drop training rows that fall outside FSM nominal and you get one of two
failures:

* **No data** — on a high-mileage truck with a known issue, a large,
  *legitimately-baseline* fraction of every drive is outside nominal.
* **Death spiral** — you train only on the rare moments the bad component
  reads in-spec, so the model decides that sliver is normal and alerts
  every time the component does the thing it always does. Operators learn
  to ignore the system; the system is now worse than absent.

FSM ranges therefore split by *severity*, not into a single keep/drop:

* **Physical-impossibility limits → hard reject (train *and* score).**
  Coolant = −40 °C (sensor open), RPM = 16383 (decode rollover), negative
  MAF, 250 °C intake. These are *data-quality* failures — broken sensor /
  bad decode / bus glitch — not unhealthy-engine states. They are garbage,
  not anomalies; they must never reach the scaler and must not be emitted
  as alerts. **This guard does not exist yet and unambiguously should.**
* **Out-of-nominal but physically plausible → screen, force a human
  disposition, never silent auto-drop.** Reuse `range_match.py`'s
  observed-vs-FSM scoring to *report* "these channels sit persistently
  outside FSM nominal in this window." The system surfaces; the human
  disposes; the disposition becomes durable.

## The disposition pipeline

Every candidate training row resolves to a disposition, not a binary:

| Disposition | Condition | Train on it? | Score / alert on it? |
|---|---|---|---|
| **Reject** | Outside FSM *physical* limits, or decode-failure | No | No — it is garbage, not an anomaly |
| **Accept-recenter** | Outside FSM nominal **but** covered by a provenanced override | **Yes** — so the multivariate model learns the true joint baseline | Yes, against the **directional** override band |
| **Quarantine** | Outside this-engine baseline, no override, not tagged benign | No | **Yes** — this is the fault we are trying to catch |
| **Accept** | Within this-engine baseline | Yes | Yes, normal bands |

FSM ranges drive the **screen** (what surfaces for disposition) and the
**Reject** tier (physical limits). Operator disposition is required once
per persistently-flagged channel; thereafter it is a durable record. This
table is the guard model.

## Directional overrides — the schema gap

A known-accepted fault is **not a mute**. It is a *recentering with a
one-sided band*. Worked example:

> Weak #3 injector. STFT B1 sits at +12 % on this engine. Accepted, not
> being replaced. Baseline for STFT B1 = +12 %. Wander of ±3 % around +12 %
> is fine. Going **more positive is critical immediately** — the injector
> worsening. Going *toward* 0 % is improvement/noise — do not alert at all.

That asymmetry is the entire value, and the current schema cannot express
it: `DeviationConfig` (`src/decode/behavior_models.py:201`) is symmetric —
`warning_band` / `critical_band` straddle the surface equally. The
required addition is a **directional deviation**: recenter on the known
side, hard-trip on the worsening side, ignore the improving side.

Proposed override file — a new sibling to `behavior_models.yaml`, loaded
the same strict-Pydantic way:

```yaml
# data/accepted_baselines.yaml
schema_version: 1
overrides:
  - channel: obd:SHORT_FUEL_TRIM_1          # or: behavior_model: fuel_trim_vs_rpm_load
    operating_mode: idle                    # optional scope, same vocab as segmenter
    accepted_center: 12.0                   # this engine's recentered normal
    band:
      kind: directional
      tolerance: 3.0                        # ± wander around accepted_center: no alert
      worse_direction: positive             # which way is the fault worsening
      worse_warning: 3.0                    # > center + 3 → warning
      worse_critical: 6.0                   # > center + 6 → critical
      better_direction: ignore              # toward 0: never alert
    provenance:
      declared_by: dan
      declared_at: "2026-05-17"
      review_by: "2026-11-17"               # expires → forces re-confirmation
      reason: "~190k mi, known weak #3 injector; owner not replacing"
```

`better_direction: ignore` is what stops improvement/noise from generating
alerts; `review_by` is what stops the override from rotting (below).

## The multivariate subtlety

A known fault never moves one PID — it co-moves a correlated set (a weak
injector pulls STFT, nudges the O2 trace, idle quality, maybe timing).
This kills both naive fixes:

* **Filter the known-fault sessions out of training** → the IF / LSTM
  never see the joint pattern; their "normal" is a fiction; *every*
  healthy drive with the expected fault signature scores anomalous on
  every channel at once.
* **Blanket-mute the one obvious PID** → the joint signature in the *other*
  channels is unguarded, and a new, different fault that also touches that
  PID is invisible.

Resolution, and why the table above says "Accept-recenter → train: **yes**":
**include** known-fault sessions in the multivariate detectors so they
learn the true joint baseline, and pin the *directional worsening*
expectation at the behavior-model / channel level via the override. The
model learns "this engine's normal includes this signature"; the one-sided
band still catches the signature *intensifying*. Recentering preserves
information; filtering and muting both destroy it.

## Durability, provenance, and not rotting

The override is the **declarative cousin** of `feedback.py`. Feedback is
*reactive* — "that past alert was a false positive." An override is
*forward-looking* — "I declare this engine state known and accepted going
forward." It needs more than `anomaly_feedback.jsonl` carries:

* **Provenance** — who, when, why. The `reason` is read back to the
  operator when the override is what suppressed/recentered an alert, so a
  silenced channel always has a stated justification.
* **A `review_by` date.** Non-negotiable. A two-year-old "accept the weak
  injector" must not silently mask a *second*, newly-failing injector. An
  expiring override forces re-confirmation; a permanent mute is exactly how
  a known fault becomes an invisible fault.
* **Dashboard surfacing.** "3 accepted-baseline overrides active," always
  visible, each expandable to its provenance. The objective is the
  opposite of hiding the fault — it is making the fault *explicitly
  tracked* with a sharpened, one-sided expectation around it.

## One orthogonal axis — do not blur it

"Validate training data" is two independent questions:

1. **Is this value trustworthy and in an accepted health state?** —
   everything above (FSM gate + disposition + override).
2. **Is this the operating regime the model represents?** — already solved
   by `operating_mode` segmentation (`src/preprocessing/segmenter.py`).
   Don't train steady-state-idle expectations on cold-start, WOT, or the
   forthcoming IMU hard-accel rows *regardless of health*.

The disposition pipeline answers (1). The segmenter answers (2). Keep them
as separate stages; a row must pass both.

## Concrete design, mapped to modules

```
 sessions (rolling window)
        │
        ▼
 ┌─────────────────────┐  operating_mode filter (existing segmenter)
 │ regime gate          │  — wrong regime → not this model's training data
 └─────────┬───────────┘
           ▼
 ┌─────────────────────┐  FSM physical-limit check (fsm.py / range_match.py)
 │ data-quality gate    │  — impossible/decode-fail → REJECT (drop everywhere)
 └─────────┬───────────┘
           ▼
 ┌─────────────────────┐  vs this-engine baseline + accepted_baselines.yaml
 │ disposition          │  — override? ACCEPT-RECENTER. else outside? QUARANTINE.
 └─────────┬───────────┘     else ACCEPT.
           ▼
 retrain.py (rolling window)  → artifact + manifest
           ▼
 ModelManager.poll() hot-swap  → champion/challenger: promote only on
           ▼                     validation; ledger unpublished→active;
 live scoring                    frozen golden baseline never auto-updates
```

* **Scheduler** — daemon-internal `asyncio` task or systemd timer firing
  `retrain.py`; add `--since` / sliding-window selection beside `--days`.
* **Data-quality gate** — new module reusing `range_match.py` scoring at
  *physical* (not nominal) limits; emits the Reject set.
* **Disposition** — loads `data/accepted_baselines.yaml`; classifies the
  Quarantine vs Accept-recenter vs Accept tiers; writes a per-run
  disposition summary into the training ledger record (`input_sessions`
  already there — add `dispositions`).
* **Directional deviation** — extend `DeviationConfig` with a
  `kind: directional` variant; the runtime decoder and dashboard
  performance panel honor the one-sided band.
* **Frozen golden baseline** — the first human-validated model, never
  auto-updated. Divergence between live-adaptive and golden is itself an
  alert ("normal has drifted a lot — why?").
* **Champion/challenger** — `ModelManager` promotes a challenger only after
  a validation step (agrees with golden on a holdout; contamination hasn't
  collapsed). The ledger's `unpublished → active` already models this —
  wire the gate, don't auto-`active`.

## Phased rollout

1. **Data-quality gate + disposition report (no auto-retrain yet).** Run
   the screen over existing sessions; produce the disposition report;
   author the first `accepted_baselines.yaml`. Pure analysis, zero risk,
   and it tells you whether the guards even hold on real data.
2. **Directional deviation schema** + dashboard surfacing of active
   overrides. Still no auto-retrain — but now a known fault is explicitly
   tracked with a one-sided tripwire.
3. **Scheduler + champion/challenger + frozen golden baseline.** Only after
   1–2 are proven. This is the step that closes the loop, and it must not
   land before the guards do.

## Open questions / honest limits

* **First-baseline bootstrapping.** The frozen golden baseline must be
  declared on an engine whose state the operator can vouch for. There is
  no model-only way to establish "ground-truth normal" on a used vehicle —
  it is an operator assertion, and the system should record it as one.
* **Slow drift vs slow fault are the same shape.** A gradually widening
  band could be benign wear or an incipient fault. The override's
  `review_by` and the golden-baseline divergence signal are the main
  defenses; golden-anchored disposition baselines (Task 3.7) add a
  third, automatic one — a persistent shift quarantines against the
  frozen reference instead of redefining the window-local median. But
  *deciding* whether the shift is wear or fault remains an operator
  call. This is inherent to unsupervised detection, not a fixable bug.
* **Disposition is per-channel but faults are multivariate.** The override
  is authored on the obvious channel; the multivariate detector carries
  the rest by training on the recentered data. Whether that is sufficient
  for subtle correlated drift is an empirical question Phase 1 should test.
* **Threshold/override tuning is a data exercise, not a redeploy** — same
  philosophy as the behavior-model deviation bands; keep all of it in YAML.

## Where the code lives (proposed)

| Layer | Module |
|---|---|
| Scheduler | daemon hook / systemd timer driving `scripts/retrain.py` (extend with `--since`) |
| Data-quality gate | new module reusing `src/analysis/range_match.py` at physical limits |
| FSM physical limits | `src/signals/fsm.py` (extract abs min/max alongside nominal) |
| Disposition + overrides | `data/accepted_baselines.yaml` (new), loader beside `src/decode/behavior_models.py` |
| Directional deviation | `src/decode/behavior_models.py` (`DeviationConfig` variant) |
| Disposition lineage | `src/models/training_ledger.py` (add `dispositions` to the run record) |
| Champion/challenger promotion | `src/models/manager.py` + the ledger lifecycle |
| Reactive verdicts (existing) | `src/models/feedback.py` — the override's reactive cousin |
| Regime gate (existing) | `src/preprocessing/segmenter.py` |

## Validity check vs the 2026-07 ML-hardening review (2026-07-19)

This doc predates the five-subsystem ML review
(`archive/ml-correctness-hardening.md`) and the vehicle-profiles migration. The
core design — three normals, the disposition table, directional
provenanced overrides, train-on-recentered-data — survives both intact;
the literature check even reinforces it (the review's verdict was
"hardening, not rearchitecting", and nothing in ADBench/TimeEval touches
the guard-model problem this doc solves). Four specifics are updated,
and the checklist below encodes them:

1. **Reject = per-cell NaN, not row drops.** ml-hardening Phases 1–2
   built real missingness machinery (bounded ffill, universal `__gap`
   flags, GRU-D masks, IF missing-channel policy). A physically
   impossible value should be NaN'd *per cell* so it flows through that
   machinery as missingness, with a counted reject reason — not dropped
   as a whole row (which would discard the other channels' good data)
   and not handled by parallel new plumbing. "Never reaches the scaler,
   never alerts" still holds; the mechanism is cell-level.
2. **Calibration is held-out, always.** The FPR≈1.0 flood traced to
   in-sample calibration; retrain already auto-carves a calibration
   holdout and ml-hardening Phase 3 (queue Q4) makes in-sample a
   refusal/loud-manifest-stamp. The scheduler must never pass
   `--no-calibration-holdout`, and an `in_sample`-stamped artifact is
   categorically ineligible for champion promotion. Rolling-window
   selection must therefore always reserve holdout sessions from the
   window.
3. **The promotion gate has teeth now: the fault-injection benchmark.**
   "Challenger agrees with golden on a holdout" is necessary but weak
   against the central danger (a model that quietly learned the fault
   still agrees on clean data). The Q3 benchmark
   (`archive/eval-longitudinal-and-dtc.md` Phase 2) measures recall on injected
   faults directly — the challenger must show **recall non-regression
   vs golden on the injected-fault suite** plus held-out FP/hr within
   budget, not just score agreement.
4. **Everything here is profile-scoped.** `scope_config`
   (`src/vehicle/profiles.py`) partitions all vehicle-specific state by VIN.
   `accepted_baselines.yaml`, dispositions, and the golden-baseline
   declaration are *this-vehicle* facts by definition (a second vehicle
   must not inherit the Xterra's weak-injector override) — the override
   file lives under the profile-scoped data tree, not a flat `data/`
   path, and golden is declared per profile.

Also noted, no change needed: `DeviationConfig.kind="relative"` becomes
`sigma` (alias kept) in ml-hardening 9.4 — the directional variant below
is added against the post-rename scheme and obeys the 4.2 σ-floor schema
rule; ECOD (2.6), if adopted, is batch-fit like IF, so the
rolling-window-rebuild mechanism covers it unchanged.

## Robustness amendments (2026-07-20 review)

A review pass over the landed Phases 1–2 and the in-flight Phase 3
surfaced five weaknesses. None undermine the core design; each is
encoded as a checklist task below (1.6, 3.6, 3.7) or an amendment to an
existing task (3.5).

1. **The disposition baseline is self-referential — it cannot catch the
   slow fault it was designed for.** `classify_dispositions` computes
   median ± scaled-MAD *from the same window being screened*. Robust
   statistics resist outliers, not persistent shifts: a slowly-worsening
   fault present in most of a sliding window moves the median with it
   and classifies **Accept** — exactly the central-danger scenario. As
   landed, the screen catches transient excursions only. Fix: once a
   golden run exists (3.4), anchor the per-`(channel, mode)` baseline
   statistics to the golden run's input sessions — frozen with the
   golden declaration — instead of the candidate window (Task 3.7).
   Until then, treat disposition output as a transient-excursion screen,
   not a slow-drift guard.

2. **Quarantine "train: No" is not yet enforced, and the promotion
   gate's power is bounded by the synthetic fault suite.** `retrain.py`
   currently *records* the disposition summary but still trains on
   Quarantine rows. Until row exclusion lands (3.6), the promotion gate
   carries the entire guard load — and its recall check runs on
   *injected* faults, so a novel real fault outside the suite's fault
   families can be learned and still pass. Two consequences: (a) wire
   the exclusion the table already specs; (b) give the gate a direct
   contamination signal the Reject-dominance proxy does not cover — the
   **quarantine-fraction trend** across consecutive scheduled runs (a
   sustained rise = the window's health is shifting under the model).
   Both in Task 3.6.

3. **The gate's "FP/hr" is really alert-rate/hr on an unlabeled
   holdout.** If the window genuinely contains a developing fault, the
   holdout does too, and *correct* alerts count against the FP budget —
   the gate can block a challenger precisely for detecting the fault.
   Keep the check (the symmetric collapse-vs-flood framing is right),
   but name it honestly and treat a budget breach as "operator inspects
   the alerts", not an automatic quality verdict (3.5 amendment).

4. **Golden re-declaration is a routine lifecycle event, not an anomaly
   response.** Legitimate seasonal drift (winter intake temps, fuel
   blends) will eventually push any honest challenger outside
   agreement-with-golden tolerance. The runbook (3.5) must document
   periodic golden re-declaration as expected maintenance — otherwise
   the gate quietly becomes a ratchet against the very adaptation online
   training exists to provide.

5. **Median/MAD is meaningless for discrete/bitmask channels.** The 1.5
   corpus run showed the Quarantine tier dominated by engine-state bytes
   and steering bitmasks whose band collapses to a point. The screen
   needs channel-type awareness before its Quarantine output is
   actionable (Task 1.6).

Minor, noted without a task: the scheduler's real cadence is
`interval_seconds + job duration` (it awaits the job before sleeping) —
harmless at hour-scale intervals, but don't read `interval_seconds` as a
wall-clock period when tuning.

## Implementation checklist

Task-level truth for the overnight queue lives here. Phases map 1:1 to
"Phased rollout" above; the design sections are the spec each task is
verified against, as amended by the validity check above.

### Phase 1 — data-quality gate + disposition report (no auto-retrain)

- [x] **1.1 — FSM physical limits.** Landed: curated `PHYSICAL_LIMITS`
  table in `src/signals/fsm.py`, each bound traced to the actual
  decode formula (`src/collection/can_obd_client.py`) — e.g. RPM's
  16383.75 ceiling at raw 0xFFFF, coolant's -40°C sensor-open value at
  raw 0. FSM-text extraction fallback confirmed empty on the real
  corpus, as the doc predicted; the curated table is the real source
  of truth. Uncertain channels left out (absent limit = no reject,
  safe default).
- [x] **1.2 — Reject gate (train and score).** Landed:
  `src/preprocessing/quality_gate.py::apply_quality_gate` NaNs
  physical-impossibility values per cell (never a row drop), wired
  into both training (`feature_pipeline.py`, between `to_wide` and
  `resample_wide` — a rejected cell is indistinguishable from any
  other gap to the resampler's existing `__gap`/ffill machinery) and
  live scoring (`preprocessing/live.py`'s `LiveFeatureSource._set_value`,
  the single choke point every observed value passes through — a
  rejected value ages out via the existing staleness path). Per-channel
  reject counts logged and returned, never swallowed. Verified both
  paths converge to identical gap semantics, not parallel plumbing.
- [x] **1.3 — `accepted_baselines.yaml` schema + loader.** Landed:
  strict-Pydantic schema in `src/decode/accepted_baselines.py` matching
  the doc's directional-override fields exactly. Profile-scoped via a
  new `DaemonConfig.accepted_baselines` path threaded through
  `scope_config` (verified against the exhaustive
  `test_scope_config_covers_every_daemon_path_field`). Shipped with
  `schema_version: 1` and zero active overrides — commented worked
  example only. No real override was authored (operator-only task).
- [x] **1.4 — Disposition classifier.** Landed:
  `src/analysis/training_disposition.py::classify_dispositions` — pure
  function, four mutually-exclusive/exhaustive tiers computed per
  `(channel, operating_mode)` group via median ± scaled-MAD (robust to
  the very outliers being screened for, unlike a quantile band). Sparse
  groups default to all-Accept rather than manufacturing false
  quarantines. Regime-gate-then-disposition ordering verified: baselines
  are computed strictly within each already-mode-gated group, never
  pooled across modes. `TrainingRunRecord.dispositions` field added
  (schema-ready; a real retrain run populates it in Phase 3).
- [x] **1.5 — Disposition report CLI.** Landed:
  `scripts/disposition_report.py` reuses the same decode/segment/
  feature-column functions training itself uses — screens exactly what
  training would consume, no parallel decode logic. Run over the real
  corpus (14/35 session dirs passed the discovery quality gate) and
  committed: 0 Reject, 0 Accept-recenter (expected — zero overrides
  exist to recenter against; the Accept-recenter code path itself is
  real and covered by a synthetic-override test), 40,865 Quarantine,
  418,341 Accept. Quarantine is dominated by discrete/bitmask channels
  (engine-state bytes, steering signals) whose median/MAD band
  collapses to a point — flagged for operator review rather than
  treated as uniformly actionable.
- [x] **1.6 — Channel-type-aware disposition screen.** (Amendment 5.)
  Landed: data-driven `_is_discrete` (small unique-value count, or
  integer-valued with a low unique-fraction — no hand-listed roster).
  A discrete channel's baseline is its observed value set. Re-ran the
  1.5 corpus report: Quarantine collapsed 40,865 → 11,947 (~71% drop);
  traced the residue against `data/bindings.yaml`'s own documented
  evidence trails (e.g. `ECM_ENGINE_STATE_*`'s low-nibble counter bits
  in active-driving modes, steering/motion channels' real
  parked-vs-driving cardinality jump) — genuine continuous-channel
  signal, not a detector miss. Superseded disposition report committed
  in place of the stale one. One disclosed, non-fatal risk: the
  absolute unique-value cap can misclassify a genuinely continuous but
  low-cardinality signal (exactly the heuristic's stated trade-off;
  no real corpus channel currently triggers it).

### Phase 2 — directional overrides surfaced

- [x] **2.1 — Directional `DeviationConfig`.** Landed: `kind:
  "directional"` variant on `DeviationConfig` (built against the
  already-landed post-9.4 `sigma` naming) — `accepted_center`,
  `tolerance`, `worse_direction`/`better_direction` (default
  `"ignore"`), `std_scaled` with the same σ-floor rule ml-hardening 4.2
  enforces for `sigma`. For `behavior_model` targets the center comes
  from the surface's own fit (override contributes only the one-sided
  band); `accepted_center` applies to raw-channel overrides. Verified:
  the improving side never alerts regardless of magnitude, `tolerance`
  is a genuine two-sided dead-zone, bands are measured from center.
  Symmetric `absolute`/`sigma` paths untouched.
- [x] **2.2 — Override → alert path.** Landed: deviation evaluation
  consults `active_override_for` for both `behavior_model`-targeted
  overrides (existing per-model branch) and raw-`channel`-targeted
  overrides (a second loop in `_observe_behavior`, added after
  adversarial review caught that `deviation_for_override` existed and
  was unit-tested but had zero live callers — the doc's own flagship
  `obd:SHORT_FUEL_TRIM_1` example was silently inert). Expired
  overrides (`review_by` past) are confirmed inert — filtered out by
  `active_override_for` before either branch ever sees them, falling
  back cleanly to the static config. Events carry `override_reason`
  through the existing emit path, present only when an override was
  genuinely active for that tick.
- [x] **2.3 — Dashboard surfacing.** Landed: `read_accepted_baselines()`
  returns active count + every override (with an `expired` flag),
  threaded through `GET /status`. New StatusStrip item "overrides: N
  active", expandable to per-override provenance/reason/`review_by`,
  expired ones flagged "needs re-confirmation" rather than hidden —
  consistent with 2.2's backend semantics. FE tests cover expand-on-click
  and expired-flagging.
- [x] **2.4 — Docs.** Landed: `docs/behavior-model-inference.md` gains
  a "Directional deviation and accepted-baseline overrides" section;
  `docs/runbooks.md` gains an "Author or review an accepted-baseline
  override" entry, corrected after review to accurately describe
  `deviation_for_override` as consulted by `_observe_behavior`'s
  channel-override branch (not an aspirational, uncalled entry point).

### Phase 3 — closing the loop (only after 1–2 are proven)

- [x] **3.1 — Rolling-window selection.** Landed: `scripts/retrain.py
  --since` sliding-window selection beside `--days`, reusing the
  existing `_pick_calibration_holdouts` path. Refuses
  (`WINDOW_TOO_SMALL_FOR_HOLDOUT` exit code) rather than degrading to
  in-sample calibration when the window can't spare a real holdout.
  Disclosed, pre-existing, out-of-scope inconsistency: the `--days`
  path's thin-corpus behavior (warn+fallback) is unchanged and doesn't
  get this hard refusal — not worsened by this task, but not unified
  either.
- [x] **3.2 — Scheduler.** Landed: `OnlineTrainingConfig.enabled:
  bool = False` (hard default, verified). New
  `src/daemon/online_training.py::run_online_training_scheduler`
  mirrors the retention sweeper's asyncio-cadence shape, submits a
  windowed `retrain_all` job through the existing `JobController` —
  never trains inline on the event loop. Verified structurally
  incapable of passing `--no-calibration-holdout`: no parameter or
  code path in the job-command builder accepts or forwards that flag.
  systemd-timer alternative documented in the runbook, not installed.
- [x] **3.3 — Champion/challenger gate.** Landed: new
  `src/models/promotion_gate.py::evaluate_promotion_gate` — calibration-
  stamp hard reject (unconditional, first-checked, can't be offset by
  other passing checks), contamination sanity, recall non-regression
  vs golden on the real fault-injection benchmark (then `detector_benchmark.py`'s
  report; since R06.5, 2026-09-25, the `evaluate_detectors` protocol report read
  through `src/models/benchmark_gate.py`), held-out FP/hr
  budget, holdout score agreement. `promote_challenger` verified as the
  ONLY path to `active` besides the pre-existing manual `publish` CLI —
  never called from `retrain.py` or the scheduler. Tests cover gate
  pass/fail/rollback.
- [x] **3.4 — Frozen golden baseline.** Landed: `TrainingLedger.
  set_golden`/`golden_run`, demote-on-append scoped strictly per
  `model_type` (no cross-type contamination), profile-scoped for free
  via ledger co-location. `compute_golden_divergence` for the
  live-vs-golden drift signal — a real per-detector computation, not a
  placeholder. Golden verified never auto-updated by any code path
  other than the explicit `ledger.py golden` CLI action. The real
  vehicle profile has no golden run declared, per the pre-made
  decision.
- [x] **3.5 — Docs + runbook.** Landed: `docs/runbooks.md` "Online
  training" entry — enable, observe, promote, roll back, re-declare
  golden. Amendments landed: FP/hr (and 3.6's quarantine-trend check)
  reframed as an alert-rate budget whose breach means "inspect the
  alerts," not an automatic verdict; golden re-declaration documented
  as routine seasonal maintenance, tied to the actual re-freeze
  mechanism 3.7 added.
- [x] **3.6 — Enforce Quarantine exclusion + quarantine-fraction
  trend.** (Amendment 2.) Landed: new `apply_quarantine_gate` in
  `quality_gate.py` — the identical cell-level NaN mechanism the
  Reject tier already uses, never a row drop. `retrain.py` masks
  Quarantine cells out of the real training matrix by default
  (`--skip-quarantine-exclusion` opt-out), excluded counts recorded in
  `TrainingRunRecord.dispositions`. Promotion gate gained a
  quarantine-fraction-trend check (sustained rise vs. recent
  same-model-type ledger runs blocks promotion) feeding the same
  shared eligibility mechanism as every other check — not a separate,
  bypassable path. Graceful no-check with fewer than 2 historical runs.
- [x] **3.7 — Golden-anchored disposition baselines.** (Amendment 1;
  after 3.4.) Landed: new `src/models/golden_baselines.py` sidecar,
  co-located with the profile-scoped ledger. `compute_golden_baselines`
  freezes per-`(channel, operating_mode)` median/MAD or discrete
  value-sets at golden declaration; `classify_dispositions` screens a
  covered group against the FROZEN reference, never recomputing from
  the candidate window — verified with a real, non-tautological test
  proving the exact self-referential-baseline blind spot: identical
  drifted data scores zero quarantine window-local vs. real quarantine
  golden-anchored. Falls back cleanly to window-local for an uncovered
  group. Re-declaring golden re-freezes the baselines.
