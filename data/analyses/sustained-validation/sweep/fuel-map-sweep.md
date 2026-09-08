### Budget sweep — `fuel-map` (synthetic bus)

| budget req/s | contract | verdict (adapter ledger) | limiting stage | worst achieved/requested | tuples | storage drops | named failures |
|---:|---|---|---|---:|---|---:|---|
| 4 | **failed** | fail | bus_budget_request_pacing | 0.985 | 33/40 | 0 | unanswered_input, acquisition_gap, unknown_loss_counters |
| 6 | **failed** | fail | bus_budget_request_pacing | 0.250 | 33/40 | 0 | rate_shortfall, unknown_loss_counters |
| 8 | held | pass | inference_feature_stage_on_loop | 0.998 | 34/41 | 0 | unknown_loss_counters |
| 12 | held | fail | inference_feature_stage_on_loop | 1.000 | 32/40 | 0 | acquisition_gap, acquisition_gap, unknown_loss_counters |
| 16 | held | fail | inference_feature_stage_on_loop | 1.000 | 33/41 | 0 | acquisition_gap, acquisition_gap, unknown_loss_counters |
| 20 | held | fail | inference_feature_stage_on_loop | 1.000 | 33/41 | 0 | acquisition_gap, acquisition_gap, unknown_loss_counters |
| 24 | held | fail | inference_feature_stage_on_loop | 1.000 | 32/40 | 0 | acquisition_gap, acquisition_gap, unknown_loss_counters |
| 32 | held | fail | inference_feature_stage_on_loop | 1.000 | 33/41 | 0 | acquisition_gap, acquisition_gap, unknown_loss_counters |

Baseline at 32 req/s (the most headroom any rung gets, so these are not about pacing): `acquisition_gap, acquisition_gap`.

Every rung from **8 req/s** to 32 req/s held its contract, and no rung failed after one that held. **The ladder found no ceiling — it ran out of rungs, which is not the same thing.** Raising the budget past the workload's own request demand cannot fail, so a ceiling for this workload would have to be sought by raising demand, which would no longer be the same workload.

Synthetic bus, workstation. These bound the daemon's software path at each budget; they establish no rate on the vehicle and license no change to a deployed default (C10.10).
