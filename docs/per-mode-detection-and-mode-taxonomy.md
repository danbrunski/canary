# Per-mode detection & operating-mode taxonomy (findings + roadmap)

Findings and design reasoning from the per-mode Isolation-Forest
discussion. Records what is *actually* true in the deployed artifacts (not
the plan), a correction to an earlier wrong claim, the design concerns, and
the recommended escalation path — so these are not re-derived or
re-litigated. Companion to Phase 9 in `docs/implementation-tasks.md`
(selectable sequence detector) and Phase 7 (feature-spec safety / fusion).

## Status

Investigation + recommendation only. No code changed. The proposal that
started this — "one IsolationForest per operating mode, route inference by
mode label" — is **not** adopted as-is; see the escalation path below.

## The original proposal

A single global IF scores Idle/City/Highway/WOT frames with one
contamination rate. "Normal" differs across modes, so the global rate is
either too high (transition FPs) or too low (within-mode misses). Proposed
fix: train one `sklearn.IsolationForest` per mode, route each inference
frame to the model for its current mode label — claimed as "no
architectural complexity" since mode segmentation already exists.

## What is actually true in the code (verified)

- **Modes are config-driven, rule-based, first-match.**
  `src/preprocessing/segmenter.py`: modes evaluated in mapping order, the
  first whose thresholds all match wins, else `unknown`. Configured set
  (all `canary*.yaml`): `idle {rpm_max:900, speed_max:0}` →
  `city {speed_max:50}` → `highway {speed_min:80}` → `wot {throttle_min:80}`.
- **The IF is mode-blind.** `normalization._resolve_columns` skips
  `{SESSION, TIMESTAMP, MODE}` from the feature matrix — operating mode is
  not a feature. The global IF scores a mode-multimodal distribution with
  no mode conditioning. (Confirms the proposal's core diagnosis.)
- **`per_mode_contamination` is scaffolded but unused.** Present in
  `config.py` (`Field(default_factory=dict)`) and `canary.example.yaml`
  (`per_mode_contamination: {}` with a "task-2.8 tuning output" comment) —
  i.e. the design *already anticipated* per-mode calibration on a single
  global model as the first lever. It is empty/untested.
- **The deployed LSTM has NO per-mode calibration.**
  `models/lstm_autoencoder/model.manifest.json`: `thresholds.channel_errors`
  is keyed by *channel* (e.g. `dbc:BRAKE_ACTIVITY_29E_7 → {count, p50,
  p95, threshold}`), one global distribution per feature. `per_mode` /
  `by_mode` occurrences in the manifest: **0**. The mode names
  `["city","highway","idle","wot"]` appear only as recorded schema
  metadata (the configured segmenter modes), not as per-mode thresholds or
  sub-models. The LSTM is also mode-blind.
- **`single_operating_mode` exists as an escape hatch.** `config.py`
  documents that the OBD mode signals (RPM/SPEED/THROTTLE) can be "too
  sparse to segment reliably live," producing train/serve skew where
  serving emits a mode the trainer never produced — and offers collapsing
  to a constant `operating_mode = "all"`. The maintainers already hit the
  live-mode-label-reliability problem.

> Not separately audited: the internal per-mode structure of
> `models/isolation_forest/model.manifest.json` (only that the string
> `"wot"` appears in a recorded mode-name list, mirroring the LSTM). Treat
> "IF has no per-mode thresholds" as expected-but-unconfirmed pending a
> direct check.

## Correction recorded

Earlier in the discussion it was asserted that "the LSTM trainer already
does per-mode calibration if sample counts support it — do the same for
IF." That came from the *plan document's* description of the trainer and
was stated as fact about the running system. The deployed LSTM manifest
shows it is **false for the shipped artifact** (global per-channel
thresholds, zero per-mode entries). Both detectors are currently
mode-blind; there is no per-mode precedent in the running system to copy.
Logged so the wrong premise is not reused.

## Design concerns the proposal under-prices

1. **Per-mode data sparsity, worst at `wot`.** First-match precedence
   means `wot` only catches frames that are *not* idle, *not* city
   (speed > 50), *not* highway (speed < 80), *and* throttle ≥ 80 — a
   narrow band (≈ 50–80 mph at ≥ 80 % throttle). Real WOT pulls from a
   stop or highway passing get labeled `city`/`highway` instead. A
   per-mode `wot` model would train on almost nothing → high-variance,
   plausibly worse than the pooled model. Any per-mode scheme needs a
   **global fallback for modes below a clean-sample threshold**.
2. **The FP budget is global; per-mode multiplies calibration.** ≤1 FP/hr
   is a single rate. Four individually-"fine" models can sum worse, and
   the sparse-mode model tends to dominate FPs.
3. **Contract ripple.** One IF artifact → four sub-models touches
   manifest schema, `ModelManager`, hot-swap, `model_version` display,
   training-ledger comparison, and the model-evidence panel — the model
   contract surface, not a localized `isolation_forest.py` edit.
4. **Live mode-label accuracy at transitions.** Routing inference by
   per-frame *live* mode label moves the boundary problem from "pooled
   density" to "mode-label accuracy at the boundary." Jittery live labels
   can *introduce* FPs (genuinely-Idle frame momentarily labeled Highway →
   scored by the wrong model). Per-mode models do **not** fix transition
   FPs and can worsen them. This is the `single_operating_mode` skew
   concern in another form.

## Recommended escalation (cheapest lever first, measured each step)

Gate every step on the synthetic-fault + false-positive harness, per mode.

1. **Populate `per_mode_contamination` on the single global IF.** Hours of
   calibration, no artifacts, no contract change, no sparsity problem.
   Directly addresses "contamination wrong per mode." May suffice.
2. **If within-mode *sensitivity* is still the bottleneck** (calibration
   only moves thresholds, it cannot un-mask an anomaly hidden by another
   mode's density): add **mode (or mode one-hot) as a feature** to the
   single IF — conditioning without partitioning data. One artifact, one
   calibration, no sparsity.
3. **Only if 1–2 leave a real gap:** per-mode models — but only for modes
   with enough clean data, with a **global fallback for sparse modes
   (wot)**, and accepting the manifest/ManagerLedger multiplicity as
   explicit scope.
4. **Separately, regardless of 1–3:** fix the mode taxonomy itself —
   precedence so `wot`/bands are reachable and not starved, plus
   mode-label dwell/hysteresis or a transition class for the
   transition-FP and live-segmentation-skew problem. Per-mode models are
   downstream of a *trustworthy* mode taxonomy, which the system does not
   currently rely on for thresholds at all.

## Open questions

- Should mode-conditioning apply to the sequence detector (LSTM/TCN) too,
  or IF only? (The multimodal-normal critique applies to both.)
- Is live per-frame mode labeling reliable enough to route on, or is
  `single_operating_mode` / window-smoothed labeling the safer baseline?
- Actual clean-sample counts per mode on the recorded corpus — the input
  to the sparsity/fallback threshold in step 3.

## Non-goals

- Adopting the 4-model "no architectural complexity" proposal as the
  first step.
- Per-mode models before `per_mode_contamination` is tried and the mode
  taxonomy/precedence is validated.
- Removing the global model (it is the fallback for sparse modes).

## Cross-references

- `docs/implementation-tasks.md` — Phase 7 (feature-spec safety, fusion),
  Phase 9 (selectable sequence detector).
- `src/preprocessing/segmenter.py`, `src/config.py`
  (`OperatingModeSpec`, `single_operating_mode`, `per_mode_contamination`),
  `src/preprocessing/normalization.py` (`_resolve_columns` mode skip).
