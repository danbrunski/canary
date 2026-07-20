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
eval-longitudinal-and-dtc.md 2.6 — done — regression-compare pre-deploy gate: diff_reports() vs baseline, wired into retrain.py's _run_benchmark_gate, ledger records report path regardless of regression (commit 17b43a8)
eval-longitudinal-and-dtc.md 2.7 — done — already satisfied by pre-existing Phase 7.5 test (test_evaluate_detectors_grades_each_fault_kind_and_keeps_nominal_fp_low); checkbox caught up, no new code
ml-correctness-hardening.md 2.1-2.4 — done — IF blindness surfacing (edge-triggered, /status), min_samples_per_mode floor + manifest.skipped_modes, non-emitting uncalibrated modes, scaler leakage fix (mode-gated-column crash caught + fixed in review) (commit 3eda014)
OVERNIGHT-QUEUE.md Q3 — done — ml-correctness-hardening.md Phase 8 (8.1-8.4) + eval-longitudinal-and-dtc.md Phase 2 (2.1-2.7) all landed; baseline benchmark report archived to data/analyses/ (commit c5c9558)
ml-correctness-hardening.md 2.5+2.6 — done — feature-redundancy and ECOD trials on fixed-seed synthetic corpus; both keep current defaults (full 4x feature set wins, pyod/ECOD dropped after losing decisively) (commit 5a4a0c5)
ml-correctness-hardening.md 3.1-3.3 — done — calibration enforcement moved to library level (raise unless allow_in_sample_calibration), threshold_quantile defaults to 0.995, sequence_bakeoff auto-carves held-out calibration sessions (commit 29e25e3)
ml-correctness-hardening.md 5.1+5.2 — done — mode taxonomy fall-throughs closed (idle_high, suburban, city.speed_min) + dwell hysteresis in the shared segmenter (batch/live agree); a new 0-5km/h dead zone caught by verifier and fixed before landing (commits 23413bb, 9636926)
ml-correctness-hardening.md 5.3 — done — offline mode-second before/after comparison + real --rebuild + smoke retrain, all confirm 5.1/5.2 fix works on real sessions; 3 out-of-scope corpus findings logged for morning review (commit 5b51c14)
OVERNIGHT-QUEUE.md Q5 — done — ml-correctness-hardening.md Phase 5 (5.1-5.3) complete; Pi config/deploy half blocked-overnight per queue note
ml-correctness-hardening.md 4.1-4.7 — done — behavior-surface publish gates (r_squared/coverage floors), schema-enforced sigma-floor, frozen 2D bin edges across versions, bilinear serve interpolation, version-bump-on-change, warmup band widening (real corpus has only 2 cold-start sessions, under the floor of 8), segmenter-fallback /status diagnostic (commit 5b68b9f)
ml-correctness-hardening.md 6.1-6.4 + 9.1 + 9.4 — done — M-of-N persistence (wall-clock bound, uniform missing-verdict semantics), agreement-gated fusion (min_agreement=2), tuner override replaces built-in gate, stable fused dedupe key, LSTM backbone default confirmed, relative->sigma rename with back-compat alias (commit eb6f3f8). 9.3 left open, needs a follow-up.
ml-correctness-hardening.md 9.3 — done — documented (accept+document, no new machinery) the cold-start Delta-t distortion at SequenceWindowBuffer's accumulator init; confirmed real and bounded/self-correcting (commit pending in this same batch)
ml-correctness-hardening.md 6.5 — done — new intermittent AnomalyKind + before/after benchmark: 6.1's M-of-N gate detects fire-gap-fire faults 10/30->21/30 vs strict-consecutive at unchanged clean-drive FP, numbers independently reproduced by verifier (commit de126c4)
ml-correctness-hardening.md 7.1-7.3 — done — replay in-memory isolation (PersistenceGate/LiveFeatureSource resets, emitter snapshot/restore), 409 refusal while engine_running, integration test with teeth; a real unmatched-exit_replay_mode dedupe-wipe bug caught by review and fixed before landing (commit c1efc75)
OVERNIGHT-QUEUE.md Q7 — done — ml-correctness-hardening.md Phases 6 (6.1-6.5), 7 (7.1-7.3), and 9 (9.1, 9.3, 9.4) complete; 9.2 (optional DAMP spike) skipped this run. This completes ml-correctness-hardening.md except Pi-deploy verifies.
eval-longitudinal-and-dtc.md 1.1-1.7 — done — DTC reader backend: Mode 07/0A reads, PID 0x01 MIL/count, periodic dtc-kind poll (caught + fixed a real DID-gating starvation bug + an identifier=0 wire-corruption bug), DtcState cache, GET /obd/dtc, source="dtc" events bypassing PersistenceGate, atomic dtc.json sidecar (commit f578cc0)
eval-longitudinal-and-dtc.md 1.8-1.9 — done — get_dtc MCP tool + DtcPanel.tsx (MIL banner, stored/pending/permanent lists, stale indicator, no clear-codes control) (commit df87965)
eval-longitudinal-and-dtc.md 1.10 — blocked-overnight — needs Pi/truck for live KOEO/engine-running verify; 1.1-1.9 all landed and ready for morning deploy
OVERNIGHT-QUEUE.md Q8 — done — eval-longitudinal-and-dtc.md Phase 1 (1.1-1.9) complete; 1.10 blocked-overnight per this entry's own note
eval-longitudinal-and-dtc.md 3.1-3.5 — done — vehicle ledger store/config/MCP/CLI/DTC-auto-entries/corpus-boundary integration; 3.6 blocked-overnight (needs operator history), scaffolded with one clearly-labeled placeholder entry (commit b18fc48)
eval-longitudinal-and-dtc.md 4.1-4.3 — done — session fingerprint computation (skip-not-zero contract), sidecar hooked into both offline and daemon classification paths, frozen reference epoch with per-stat sample-scoped summaries; a real fabricated-voltage-at-crank bug caught by review and fixed before landing (commit 966b55a)
