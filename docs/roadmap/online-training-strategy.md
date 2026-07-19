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
| Reactive operator verdicts feeding retrain | `src/alerts/feedback.py` | Done — `true_positive/false_positive/ignored/needs_review`, `data/control/anomaly_feedback.jsonl` |
| Observed-vs-FSM-spec range scoring | `src/analysis/fsm.py`, `src/analysis/range_match.py` | Done — built for DID binding; **reusable as the validation screen** |
| Held-out threshold calibration in retrain | `scripts/retrain.py` (`--calibration-sessions`, automatic holdout via `_pick_calibration_holdouts`) | Done — and ml-hardening Phase 3 makes in-sample calibration a loud refusal at the library layer |
| Per-vehicle data partitioning | `src/profiles.py` (`scope_config`) | Done — everything this doc adds is per-vehicle state and must live under the profile scope |
| Scheduled trigger + training-data validation | — | **Missing. This doc.** |

The delta to "online" is small in mechanism (a scheduler + a rolling
window) and entirely in the guard model.

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
  `review_by` and the golden-baseline divergence signal are the only
  defenses; neither is automatic. This is inherent to unsupervised
  detection, not a fixable bug.
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
| FSM physical limits | `src/analysis/fsm.py` (extract abs min/max alongside nominal) |
| Disposition + overrides | `data/accepted_baselines.yaml` (new), loader beside `src/decode/behavior_models.py` |
| Directional deviation | `src/decode/behavior_models.py` (`DeviationConfig` variant) |
| Disposition lineage | `src/models/training_ledger.py` (add `dispositions` to the run record) |
| Champion/challenger promotion | `src/models/manager.py` + the ledger lifecycle |
| Reactive verdicts (existing) | `src/alerts/feedback.py` — the override's reactive cousin |
| Regime gate (existing) | `src/preprocessing/segmenter.py` |

## Validity check vs the 2026-07 ML-hardening review (2026-07-19)

This doc predates the five-subsystem ML review
(`ml-correctness-hardening.md`) and the vehicle-profiles migration. The
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
   (`eval-longitudinal-and-dtc.md` Phase 2) measures recall on injected
   faults directly — the challenger must show **recall non-regression
   vs golden on the injected-fault suite** plus held-out FP/hr within
   budget, not just score agreement.
4. **Everything here is profile-scoped.** `scope_config`
   (`src/profiles.py`) partitions all vehicle-specific state by VIN.
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

## Implementation checklist

Task-level truth for the overnight queue lives here. Phases map 1:1 to
"Phased rollout" above; the design sections are the spec each task is
verified against, as amended by the validity check above.

### Phase 1 — data-quality gate + disposition report (no auto-retrain)

- [ ] **1.1 — FSM physical limits.** Extend `src/analysis/fsm.py` to
  extract absolute physical min/max per channel alongside nominal
  ranges, where the FSM data actually carries them. **Fallback if it
  doesn't** (likely — sensor-open/-short and decode-rollover values are
  decode-level knowledge, not FSM spec): curate an explicit
  physical-limits table in code (typed dict beside the FSM loader),
  seeded with the doc's examples (coolant −40 °C = sensor open, RPM
  16383 = decode rollover, negative MAF, IAT ≥ 250 °C) plus obvious
  bounds for the other polled channels; leave channels you're unsure
  about out of the table entirely rather than guessing tight bounds —
  an absent limit means "no reject", which is safe. Emit in the same
  structure `range_match.py` consumes. Tests over the table.
- [ ] **1.2 — Reject gate (train *and* score).** New module (e.g.
  `src/preprocessing/quality_gate.py`) that NaNs
  physical-impossibility values **per cell** before the scaler in
  training *and* before scoring live — the NaN then flows through the
  existing missingness machinery (`__gap` flags, GRU-D mask, IF
  missing-channel policy) so garbage is a data-quality failure, never an
  anomaly alert and never a whole-row drop. Per-channel reject counts
  are logged and returned, never silently swallowed. Wire into the
  training loaders and the live feature path; tests for both.
- [ ] **1.3 — `accepted_baselines.yaml` schema + loader.** Strict-Pydantic
  loader beside `src/decode/behavior_models.py` implementing the schema
  in "Directional overrides" (channel/behavior_model target, optional
  `operating_mode` scope, `accepted_center`, directional band,
  provenance with `declared_by/declared_at/review_by/reason`). The file
  is **profile-scoped** — resolved through `scope_config` under the
  active vehicle's data tree, not a flat `data/` path. Ship it with
  `schema_version: 1` and **zero active overrides** (commented worked
  example only — authoring real overrides is an operator task).
  Round-trip + validation-failure tests.
- [ ] **1.4 — Disposition classifier.** Implement the four-tier table
  (Reject / Accept-recenter / Quarantine / Accept) as a pure function
  over a wide frame + limits + overrides; record a per-run `dispositions`
  summary in the training-ledger record
  (`src/models/training_ledger.py`). Tests cover each tier and the
  regime-gate-then-disposition ordering.
- [ ] **1.5 — Disposition report CLI.** `scripts/disposition_report.py`
  (or a `scripts/retrain.py --disposition-report` mode) that runs the
  screen over stored sessions and writes a per-channel
  persistently-outside-nominal report to `data/analyses/` — the
  artifact the operator disposes from. Run it over the current corpus
  and commit the report.

### Phase 2 — directional overrides surfaced

- [ ] **2.1 — Directional `DeviationConfig`.** Add a
  `kind: directional` variant to `DeviationConfig`
  (`src/decode/behavior_models.py:201`), against the post-9.4 naming
  (`relative`→`sigma` rename lands in queue Q7 before this): recenter
  on `accepted_center`, `tolerance` wander band, `worse_direction`
  warning/critical thresholds, `better_direction: ignore`, and the same
  σ-floor schema rule ml-hardening 4.2 enforces for `sigma` when the
  band is std-scaled. For `behavior_model`-targeted overrides the
  *center* comes from the surface refit on accepted data — the override
  contributes only the one-sided band; `accepted_center` applies to
  raw-channel overrides. Runtime deviation evaluation honors the
  one-sided band; symmetric configs unchanged. Tests for both
  directions and the ignore side.
- [ ] **2.2 — Override → alert path.** Deviation/alert evaluation
  consults active overrides: recentered band applied, worsening side
  trips, improving side never alerts. An override past `review_by` is
  **inert** (stops recentering/suppressing) and is surfaced as
  needs-re-confirmation rather than silently continuing. Events
  suppressed or recentered by an override carry the override's `reason`.
- [ ] **2.3 — Dashboard surfacing.** "N accepted-baseline overrides
  active" always visible (StatusStrip or Alerts context), expandable to
  per-override provenance + review_by, expired ones flagged. FE tests.
- [ ] **2.4 — Docs.** Update `docs/behavior-model-inference.md` +
  runbook entry for authoring/reviewing overrides.

### Phase 3 — closing the loop (only after 1–2 are proven)

- [ ] **3.1 — Rolling-window selection.** `scripts/retrain.py --since` /
  sliding-window session selection beside `--days`, disposition
  pipeline applied to the selected window. The window selection must
  always reserve held-out calibration sessions (the existing
  `_pick_calibration_holdouts` path); refuse to schedule-retrain a
  window too small to spare one.
- [ ] **3.2 — Scheduler.** Config-gated daemon `asyncio` task
  (`online_training:` block, **default disabled**) that fires the
  retrain flow on cadence; a systemd-timer alternative documented, not
  installed. No training on the capture hot path. The scheduler never
  passes `--no-calibration-holdout` — a scheduled retrain that can't
  calibrate out-of-sample skips and logs rather than degrading.
- [ ] **3.3 — Champion/challenger gate.** `ModelManager` + ledger:
  a scheduled retrain lands `unpublished`; promotion to `active`
  requires the validation gate — **recall non-regression vs golden on
  the fault-injection benchmark** (queue Q3), held-out FP/hr within
  budget, holdout score agreement with golden within tolerance,
  contamination sanity — never auto-`active`. An artifact whose
  manifest is stamped `calibration: in_sample` (ml-hardening 3.1) is
  categorically ineligible. Tests for gate pass/fail/rollback.
- [ ] **3.4 — Frozen golden baseline.** Ledger support for declaring one
  run `golden` **per vehicle profile** (an operator assertion, recorded
  as such); golden is never auto-updated; a live-vs-golden divergence
  metric is computed and surfaced ("normal has drifted — why?").
  Declaring the actual golden run is an operator morning task.
- [ ] **3.5 — Docs + runbook.** Online-training runbook: enable,
  observe, promote, roll back, re-declare golden.
