# B01 — behavior fit/serve contracts

**Depends on:** nothing. **Unlocks:** every conditioned candidate in B04–B07.
Follow [B00](../B00-behavior-models-README.md)'s shared rules.

The review reproduced a steady model returning no live prediction: behavior
evaluation precedes the `__roc` inputs it requires. Closed-loop and cold-start
training filters also lack a complete persisted serving equivalent.

## Checklist

- [x] **B01.1 — Pin the complete failure path.** Add a regression through
  `featurize_wide_frame` and `build_prepared_live_tick`: 40 seconds of constant
  RPM, a model requiring its rate of change, and a finite expected output.
  Assert both live observation and batch deviation exist after the required
  history. Missing history, a stale dependency and excessive rate must refuse.
  Supplying an already-computed derivative directly to the decoder is not
  sufficient coverage.
  *Done 2026-09-21:* `tests/test_behavior_serving_contracts.py::test_b01_1_batch_deviation_exists_after_required_history`
  (40 s constant RPM through `featurize_wide_frame`: deviation 12.0 from row 30,
  `rate_insufficient_history:obd:RPM` before),
  `::test_b01_1_live_observation_exists_after_required_history` (same through
  `prepare_tick` → `build_prepared_live_tick` → `_observe_behavior`; overlay
  expected 10.0) and `::test_b01_1_stale_dependency_and_excess_rate_refuse`
  (`rate_stale:obd:SPEED`, `rate_exceeded:obd:RPM`).
- [x] **B01.2 — Compute prerequisite context before behavior evaluation.**
  Refactor the shared transform so required base-channel derivatives are
  available to both `append_deviation_columns` and the live operating point.
  Preserve rate units, rolling horizon, acquisition/freshness semantics and
  session resets. Avoid derivative-of-derivative columns, circular behavior
  dependencies or two competing implementations of the rolling calculation.
  *Done 2026-09-21:* `features._rate_of_change_block` is now the only ROC
  implementation (`compute_features` and the new `compute_rate_of_change`
  both call it; input order preserved, per-session, same horizon/units,
  float32). `behavior_decoder.append_behavior_prerequisites` runs inside
  `append_deviation_columns`, so every caller (featurize, fingerprint) gets
  the `__roc` gates' derivatives; `__roc`-of-`__roc` and `deviation:*` gates
  are refused, `context:*` columns are skipped by `compute_features` and
  excluded from trainable features. Evidence: `tests/test_behavior_serving_contracts.py::test_append_deviation_columns_builds_prerequisites_without_mutating_input`;
  unchanged feature schemas in the benchmark (332/372/377 columns before and after).
- [x] **B01.3 — Persist eligibility as a typed model contract.** Extend the
  strict behavior schema to represent the actual requirements of each recipe:
  engine-running or KOEO-compatible state, fuel-control state, rate limits,
  cold-start/initial-temperature history and required measured context.
  Missing required context must produce a named ineligible result. Do not
  globally require engine-running: TPS consistency legitimately supports KOEO.
  Separate training-only selection rules from per-observation serving rules.
  *Done 2026-09-21:* `behavior_models.ServingEligibility` (schema 2):
  `EngineStateRequirement` (`engine_running` | `any`), `FuelControlRequirement`
  (explicit J1979 codes), `ColdStartRequirement`, `RequiredContext` (any-of),
  and provenance-only `TrainingSelection` (session events, run-time
  continuity, min sessions); rate limits stay on `conditions`. Recipes build
  it via `_RecipeBase.serving_eligibility()`; TPS stays `any`; VE now
  requires measured IAT. Evidence: `tests/test_behavior_models.py::test_yaml_round_trip_preserves_serving_contract`,
  `tests/test_behavior_serving_contracts.py::test_engine_state_running_vs_koeo_compatible`.
- [x] **B01.4 — Use one eligibility evaluator.** Apply the same contract to
  fitting rows, held-out scoring, vectorized decoding, scalar live decoding
  and replay. Persist recipe requirements in `fit_recipe`; sweep callers in
  the refit CLI, preprocessing, daemon, dashboard and MCP. Cover open-loop
  versus closed-loop status, fuel cut, warm restart versus cold start, absent
  status and stale values. Define fuel-status membership explicitly rather
  than accepting any nonzero status as closed loop.
  *Done 2026-09-21:* `src/decode/behavior_eligibility.py` (one array
  implementation for frames and single points, named `code:detail`
  refusals). Used by `fit_recipe` (training rows), `_validate_held_out`,
  `explain_expected_column`/`append_deviation_columns`, scalar
  `deviation()` (new `DeviationResult.reason`), `_observe_behavior`,
  session analysis (`refusal_reasons` in the MCP deviation payload), replay,
  and capture-profile input warnings (`required_channel_groups`). Closed loop
  is `{0x02}` only (the old bit test also accepted 0x10, closed loop with a
  feedback fault). Refused ticks are a missing verdict in the behavior
  detector, not a healthy miss. Evidence: `tests/test_behavior_serving_contracts.py::test_fuel_status_membership_is_explicit`
  (closed/open loop, fuel cut, feedback fault, invalid, absent, stale),
  `::test_fit_recipe_persists_fuel_and_cold_start_contract`.
  Frontend rendering of the new reasons is B03.8.
- [x] **B01.5 — Supply session context without fabricating it.** Establish
  initial coolant/start history from recorded evidence and maintain equivalent
  live state. Reset on session/replay/reconnect boundaries as appropriate.
  A mid-drive daemon start with no cold-start evidence cannot qualify a warmup
  model. A restart must not turn unknown initial temperature into a cold start.
  *Done 2026-09-21:* `src/preprocessing/session_context.py::EngineStartTracker`
  (evidence = observed stopped→running transition, or run-time clock ≤
  `max_start_runtime_s`; resets on engine stop, run-time rollback, session
  boundary, `LiveFeatureSource.reset`, and hot reload starts a fresh tracker).
  Batch folds it per session (`append_start_context`); live advances it per
  tick. Evidence: `tests/test_behavior_serving_contracts.py::test_cold_start_evidence_warm_restart_and_mid_drive`,
  `::test_start_tracker_runtime_reset_and_session_boundary`,
  `::test_start_context_batch_equals_live`,
  `tests/test_behavior_hot_reload.py::test_hot_reloaded_cold_start_contract_cannot_qualify_mid_drive`.
- [x] **B01.6 — Migrate without silently blessing old artifacts.** Version
  the changed contract, define how legacy artifacts load, and route missing
  validation/eligibility metadata through B03's diagnostic-only/quarantined
  handling. Keep existing API fields compatible while adding explicit reasons
  for no prediction; update schema round-trip and hot-reload tests.
  *Done 2026-09-21:* `CURRENT_SCHEMA_VERSION = 2`; schemas 1 and 2 load,
  others refuse. `serving_status()` reports `diagnostic_only` with
  `legacy_artifact:no_serving_eligibility_contract` and/or
  `unvalidated:no_held_out_evidence`; diagnostic-only models are still
  evaluated (overlay, session analysis, the `deviation:*` features existing
  detectors use keep their legacy gates) but `BehaviorDetector` never emits
  or fuses them. Refit carry-over keeps a legacy prior diagnostic-only.
  Additive API fields: live snapshot `reason`/`status`/`status_reasons`,
  `/models/behavior` `serving_status`, refit report `eligibility`/`serving_status`;
  daemon load/hot-reload log diagnostic-only models. B03.2 extends this
  minimal status into the typed permitted-use policy. **All eight shipped
  models are schema 1, so they stop alerting when this deploys** (still shown)
  until B03 refits them — `tests/test_behavior_models.py::test_shipped_artifact_loads_diagnostic_only`.
  Evidence: `tests/test_behavior_serving_contracts.py::test_legacy_artifact_loads_diagnostic_only`,
  `::test_contract_round_trips_and_unsupported_schema_rejected`,
  `::test_refit_carry_over_does_not_bless_legacy_prior`,
  `::test_diagnostic_only_model_is_shown_but_never_alerts`, hot-reload test above.
- [x] **B01.7 — Verify parity and cost.** Fit a conditioned model, serialize
  it, load it, and evaluate identical eligible/ineligible points through batch,
  live and replay paths. Check expected values, deviations, grading and
  reasons. Benchmark the shared feature transform on the existing benchmark
  workload; record any added work instead of assuming the new ordering is free.
  *Done 2026-09-21:* `tests/test_behavior_serving_contracts.py::test_b01_7_fit_serialize_load_parity_batch_live_replay`
  fits a rate-gated closed-loop model and a cold-start model with held-out
  validation (both `active`), dumps/loads YAML, then compares expected,
  deviation, severity and reason tick by tick across batch
  (`featurize_wide_frame`, scalar and vectorized), live (`LiveFeatureSource`)
  and replay (live source after `reset()`) for a cold start with a speed
  dropout, a warm restart and a mid-drive join. Cost, measured on the C05
  workload (`scripts/benchmark_live_pipeline.py` synthetic drive, 16 OBD
  channels, 20 CAN frames/tick, 120 warmup + 300 ticks; behavior models from
  `data/nissan-xterra-2008/behavior_models.yaml`, "conditioned" adds one
  2-gate rate-conditioned model), before (`3f3f5bf`) vs after, WSL laptop
  under concurrent load: tick median no models 56.7 → 54.0 ms, 8 legacy
  models 89.6 → 82.0 ms, +conditioned 90.4 → 90.5 ms (the conditioned model
  now evaluates on 121/181 window rows instead of 0), +cold-start contract
  93.0 ms; later repetitions were host-load contaminated (±2×) and are not
  quoted. Min-of-40 on one warm 181-row window: `featurize_wide_frame`
  84.0/84.4 ms before vs 87.1/84.2 ms after; `append_deviation_columns`
  (9 models) 31.2/32.9 vs 32.1/30.8 ms, of which the added prerequisite ROC
  work is 3.6–3.7 ms and eligibility evaluation 1.5 ms. Feature column counts
  unchanged (332/372/377).

## Code and evidence

Start at `src/analysis/refit.py::fit_recipe`,
`src/decode/behavior_models.py`, `src/decode/behavior_decoder.py`,
`src/preprocessing/feature_pipeline.py::featurize_wide_frame`,
`src/preprocessing/live.py::build_prepared_live_tick`, and
`src/daemon/inference.py::_observe_behavior`.

**Done when:** a persisted conditioned model evaluates identically wherever
it is used, and every refused point names the unmet condition. Link the
end-to-end regression and parity/benchmark results before checking completion.
