# A — anomaly detectors: correctness, honest evaluation, and their role

Written 2026-09-22 from the
[anomaly detector review](../../../data/analyses/2026-09-22-anomaly-detector-review.md),
which holds the measurements and their limits. These are implementation
checklists. All work starts unchecked. Mark `[x]` only with linked evidence;
use `[~]` when code is complete but named validation, deployment or an
operator decision remains.

| File | Scope | Dependencies | Vehicle/Pi work |
|---|---|---|---|
| [A01](A01-scoring-correctness.md) | Serve-time defects: worker replies applied to the wrong tick, stale fallback, windows that straddle gaps and replays, suppression altering encoder input | None | Hot-swap timing; dropped-tick measurement |
| [A02](A02-calibration-and-evaluation.md) | Disjoint calibration/evaluation, coverage-checked thresholds, drive-grouped harness, baselines, realistic and real fault labels | Harness now; recalibration waits for the retrain | Induced reversible faults; Pi artifact hashes |
| [A03](A03-sequence-inputs-and-training.md) | Mask meaning "sample arrived", conditioning on driving conditions, de-duplicated inputs, scaler/validation/CLI fixes, envelope record | A02 to judge; retrain to take effect | None |
| [A04](A04-isolation-forest.md) | Single-quantity blindness, attribution, quarantine and completeness, per-mode provenance, feature policy at load, served `unknown` model, evaluation and hyperparameters | A02 to judge; A06.1 for its future | None |
| [A05](A05-plausibility-checks.md) | Deterministic range, stuck, freshness and slew checks with confirmation and evidence records | A03.1 for arrived-sample flags; A02 to judge | Per-tick cost on the Pi |
| [A06](A06-detector-roles.md) | Operator decisions: whether the IF alerts, what the sequence model is for, retrain scope before the supercharger, where the primary detector lives | Operator; A02.11 baseline | Tick budget if the role changes |

## Implementation order and decisions

1. **A01.1–A01.4 first.** They are defects in live scoring, independent of
   any model decision or retrain.
2. **Then A02's harness and A02.11's baseline table.** Nothing in the repo
   currently measures fault detection on drives a model has not seen, so
   every later change needs this yardstick.
3. **A05 alongside.** It is cheap, deterministic, needs no retrain, and
   covers the gross faults the IF misses.
4. **A06's decisions** use A02.11's table.
5. **A03/A04 model changes** take effect at the next retrain, which the
   operator deferred on 2026-09-22.

No task here changes queue order in `OVERNIGHT-QUEUE.md`.

Findings to carry forward, not re-derive:

- **Neither detector has a confirmed real-fault catch.** Their events so far
  were altitude, pipeline changes, and corrected bindings.
- **The IF cannot see a single-quantity fault of any size** (A04.1). Its
  "top feature" is usually not even one of the scoring model's features
  (A04.2).
- **The deployed sequence thresholds rest on one session.** They flag
  38–75 % of windows on unseen drives, and 16–100 % on the model's own
  training sessions. Two families have a threshold of zero because they
  were never observed in calibration (A02.12). The retrain's false-alarm
  check scored the calibration sessions, so it could not see any of this
  (A02.1, A02.2).
- **The calibration is a fixed holdout, not leave-one-out** (A02.10).
- **The supercharger build invalidates every learned normal within weeks**,
  so retraining the naturally aspirated engine is a scoped decision (A06.3),
  not a default.

## Shared acceptance rules

- **Same rows.** A before/after comparison scores the same drives and the
  same rows with both artifacts, and reports per drive. Never compare pooled
  totals across different drives (AGENTS.md, "never compare two signals by
  their aggregates").
- **Held-out means unseen.** A held-out number comes from drives the
  detector neither trained nor calibrated on, grouped by physical drive and
  ordered by `session_started_at`.
- **Empty results say which kind of empty.** Name what a result rests on
  (rows, windows, drives), and separate:
  - withheld (inputs missing);
  - skipped (not scorable);
  - scored and quiet;
  - flagged.
- **Measured constraints become refusals.** A calibration that does not
  cover its modes, a feature outside the policy, a context-only mode model:
  refuse it, don't document it.
- **No hard-coded vehicle.** Channel limits, poll tiers, sentinels and
  thresholds are profile and catalog data; the Xterra's values live in its
  files.
- **Gate and commits.** Before implementation commits, run the whole-repo
  venv gate (ruff lint and format, mypy, full pytest). The full pytest run
  rewrites `models/` manifests, so restore them and commit explicit paths
  only. Iterate on targeted tests. Work on `main`.
- **Live work.** Read `/status.vehicle_state` first. Use known recordings
  for controlled replay, and `scripts/restart_canary.sh` for restarts; never
  end an unrelated capture.

## Existing task ownership

| Existing item | Relationship |
|---|---|
| D03.25 (with D02.5, D03.22) | The owed retrain. A06.3 decides its scope. A02's recalibration and A03/A04's model changes ride on it rather than adding a fifth reason. |
| D03.20 | Altitude out-of-distribution events. A03.2/A03.3 (driving conditions as inputs, not targets) and A06.2 (a novelty screen) are the IF/sequence side of it; B04 keeps the behavior-surface side. |
| B-series | Behavior models are the residual layer A06.4 recommends as the primary detector. A-series does not duplicate their validation (B02) or lifecycle (B03). |
| [ECU second brain](../ecu-second-brain.md) | A05's evidence records and A06.4's direction are its deterministic layer. Build them once. |
| [Supercharger build](../supercharger-build.md) | A hardware intervention and corpus boundary. A02.7 labels it; A06.3 scopes the retrain around it. |
