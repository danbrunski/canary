# Per-mode detection & operating-mode taxonomy — adopted and shipped

The investigation that lived here concluded both detectors were
mode-blind and recommended an escalation ladder (per-mode contamination →
mode-as-feature → full per-mode models). The first rungs have since
shipped; this stub records where the behavior now lives.

- **IsolationForest**: per-mode contamination is real —
  `_contamination_for_mode` (`src/models/isolation_forest.py`), per-mode
  calibrated thresholds in the manifest, and
  `recommended_per_mode_contamination` produced by
  `src/models/isolation_forest_evaluation.py`. Config:
  `models.isolation_forest.per_mode_contamination`,
  `target_flag_rate` (per-mode quantile thresholds).
- **Sequence detector (LSTM/TCN)**: per-mode reconstruction-error
  thresholds are computed at training (`src/models/sequence_training.py`
  `per_mode_errors`), persisted in the manifest, and applied at inference
  (`src/models/inference.py`) with a global fallback.
- **Mode taxonomy**: config-driven, rule-based, first-match segmentation
  (`src/preprocessing/segmenter.py`); `single_operating_mode` shortcut in
  `src/config.py`. Calibrate thresholds out-of-sample (held-out
  sessions), not on the training window — in-sample calibration once
  masked the TCN entirely.

**Role (operator decision 2026-09-23, [A06](roadmap/tasks/A06-detector-roles.md)).**
Per-mode calibration did not make either detector fit to alert. On A02.11's
held-out drives (`data/analyses/2026-09-23-a02-detector-baseline.md`, false
alarms per engine-hour against a budget of 1) the Isolation Forest read 1.18
[0.00, 3.94] with 81 % of rows withheld, and the sequence detector as served
45.35 [24.51, 61.28] (its global score alone 2.96 [1.66, 5.04]). Both now
default to `models.roles: evidence`: they score, store and vote in fusion,
but never alert on their own. The sequence detector is a novelty screen: each
event states whether its window lies outside the artifact's per-mode training
envelope (baro, IAT and speed ranges, A03.8), per mode, which is what its
per-mode thresholds mostly measured. The primary detector is the behavior
models and the deterministic layer (A06.4).

Full per-mode *models* (one detector per mode) remain not-adopted; the
shipped per-mode calibration was judged sufficient. The original
findings, the correction history, and the design reasoning are in git
history for this file.
