# B01 — behavior fit/serve contracts

**Depends on:** nothing. **Unlocks:** every conditioned candidate in B04–B07.
Follow [B00](B00-behavior-models-README.md)'s shared rules.

The review reproduced a steady model returning no live prediction: behavior
evaluation precedes the `__roc` inputs it requires. Closed-loop and cold-start
training filters also lack a complete persisted serving equivalent.

## Checklist

- [ ] **B01.1 — Pin the complete failure path.** Add a regression through
  `featurize_wide_frame` and `build_prepared_live_tick`: 40 seconds of constant
  RPM, a model requiring its rate of change, and a finite expected output.
  Assert both live observation and batch deviation exist after the required
  history. Missing history, a stale dependency and excessive rate must refuse.
  Supplying an already-computed derivative directly to the decoder is not
  sufficient coverage.
- [ ] **B01.2 — Compute prerequisite context before behavior evaluation.**
  Refactor the shared transform so required base-channel derivatives are
  available to both `append_deviation_columns` and the live operating point.
  Preserve rate units, rolling horizon, acquisition/freshness semantics and
  session resets. Avoid derivative-of-derivative columns, circular behavior
  dependencies or two competing implementations of the rolling calculation.
- [ ] **B01.3 — Persist eligibility as a typed model contract.** Extend the
  strict behavior schema to represent the actual requirements of each recipe:
  engine-running or KOEO-compatible state, fuel-control state, rate limits,
  cold-start/initial-temperature history and required measured context.
  Missing required context must produce a named ineligible result. Do not
  globally require engine-running: TPS consistency legitimately supports KOEO.
  Separate training-only selection rules from per-observation serving rules.
- [ ] **B01.4 — Use one eligibility evaluator.** Apply the same contract to
  fitting rows, held-out scoring, vectorized decoding, scalar live decoding
  and replay. Persist recipe requirements in `fit_recipe`; sweep callers in
  the refit CLI, preprocessing, daemon, dashboard and MCP. Cover open-loop
  versus closed-loop status, fuel cut, warm restart versus cold start, absent
  status and stale values. Define fuel-status membership explicitly rather
  than accepting any nonzero status as closed loop.
- [ ] **B01.5 — Supply session context without fabricating it.** Establish
  initial coolant/start history from recorded evidence and maintain equivalent
  live state. Reset on session/replay/reconnect boundaries as appropriate.
  A mid-drive daemon start with no cold-start evidence cannot qualify a warmup
  model. A restart must not turn unknown initial temperature into a cold start.
- [ ] **B01.6 — Migrate without silently blessing old artifacts.** Version
  the changed contract, define how legacy artifacts load, and route missing
  validation/eligibility metadata through B03's diagnostic-only/quarantined
  handling. Keep existing API fields compatible while adding explicit reasons
  for no prediction; update schema round-trip and hot-reload tests.
- [ ] **B01.7 — Verify parity and cost.** Fit a conditioned model, serialize
  it, load it, and evaluate identical eligible/ineligible points through batch,
  live and replay paths. Check expected values, deviations, grading and
  reasons. Benchmark the shared feature transform on the existing benchmark
  workload; record any added work instead of assuming the new ordering is free.

## Code and evidence

Start at `src/analysis/refit.py::fit_recipe`,
`src/decode/behavior_models.py`, `src/decode/behavior_decoder.py`,
`src/preprocessing/feature_pipeline.py::featurize_wide_frame`,
`src/preprocessing/live.py::build_prepared_live_tick`, and
`src/daemon/inference.py::_observe_behavior`.

**Done when:** a persisted conditioned model evaluates identically wherever
it is used, and every refused point names the unmet condition. Link the
end-to-end regression and parity/benchmark results before checking completion.
