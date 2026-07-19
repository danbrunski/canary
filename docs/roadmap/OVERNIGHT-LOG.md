# Overnight run log

refactoring.md 1.3 — done — single-sourced numeric_value_columns + resample_and_segment in src/preprocessing/loader.py, closed the GAP_SUFFIX drift (commit 4a46509)
refactoring.md 1.4 — done — run_with_reconnect shared across all 4 collectors in src/collection/reconnect.py; GPS now fails fast on ValueError instead of retrying
ml-correctness-hardening.md 1.1+1.2 — done — bounded ffill + gap flags for slow channels in resampler.py, new slow_channel_max_gap_seconds config (commit d5051b2)
ml-correctness-hardening.md 1.3 — done — segmenter treats gap-flagged coolant as absent for mode rules (commit e0c90ea)
ml-correctness-hardening.md 1.4 — done — LiveFeatureSource.tick() returns None on fully-stale + per-channel bounded ffill parity with offline resampler (commit f495cd2)
ml-correctness-hardening.md 1.5 — done — IF/LSTM scoring gated on vehicle_state in {engine_running, replay}; behavior overlay stays ungated; skipped_vehicle_state counter added (commit 41127f1)
ml-correctness-hardening.md 1.6 — laptop half done, blocked-overnight (IF replay diff + Pi deploy) — smoke retrain + LSTM replay diff, report + 2 pre-existing tooling bugs surfaced for Q3 (commit 2a3f8ae)
OVERNIGHT-QUEUE.md Q2 — done — ml-correctness-hardening.md Phase 1 (1.1-1.6) complete or annotated blocked-overnight
eval-longitudinal 2.1+2.2 — done — scripts/replay.py routed through featurize_wide_frame; build_feature_frame tail-identity verified + locked with a parity test (commit 05fc61a)
ml-correctness-hardening.md 8.4 — done — __roc divides by actual elapsed time, not nominal window_seconds, across dropped-row seams (commit b84da4c)
ml-correctness-hardening.md 8.3 — done — k-sweep branches to a quantile sweep for threshold_quantile-calibrated models (commit d2ca42e)
ml-correctness-hardening.md 8.1 — done — AnomalyInjector.dropout() reproduces resample_wide's coupled hold-then-NaN+gap signature instead of raw NaN (commit 6123d45)
ml-correctness-hardening.md 8.2 — done — post-window detection grace: late hits distinguished from misses, latency still from window.start (commit b5bd740)
eval-longitudinal-and-dtc.md 2.4 — done — time-to-detect latency already landed incidentally in commit 825ae0a; checkbox caught up
eval-longitudinal-and-dtc.md 2.3 — done — evaluate_detectors gains sequence-detector coverage via real observe() contract, generalized N-detector persistence gate (commit 719908e)
eval-longitudinal-and-dtc.md 2.5 — done — scripts/detector_benchmark.py corpus-wide grid CLI + fixed latent AnomalyInjector.drift() float32 dtype bug (commit 90118c1)
