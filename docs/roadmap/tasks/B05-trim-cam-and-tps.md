# B05 — activate trim, bank-balance, cam and TPS relationships

**Depends on:** B01/B02; B03 supplies artifact lifecycle/publication.
These recipes already exist in `default_recipes`; inspect and extend them
rather than building a second model system. Follow
[B00](B00-behavior-models-README.md).

## Fuel trim and bank balance

- [ ] **B05.1 — Inventory viable inputs per recipe.** Report fresh, paired
  STFT/LTFT for both banks, fuel status, RPM/load and required thermal context
  across independent drives. Validate signal units/identities before fitting.
  Keep missing-bank coverage distinct from zero bank difference.
- [ ] **B05.2 — Enforce fuel-state semantics.** Apply B01's persisted
  closed-loop and steady-state requirements to total-trim fits and serving.
  Exclude or separately model warmup, enrichment and fuel cut. The current
  `TOTAL_TRIM` is an additive percentage-point index; retain that label unless
  an independently justified calibration-specific multiplier is implemented.
- [ ] **B05.3 — Fit total-trim B1/B2 and useful bank-balance baselines.**
  Use existing recipes and consistent calibration cohorts; compare robust
  simple baselines against surfaces using B02. Test bank-specific changes,
  symmetric changes, normal STFT oscillation and LTFT adaptation. Count two
  banks/common ECU corrections appropriately in fusion; do not claim a bank
  asymmetry identifies a particular injector, leak or sensor by itself.
- [ ] **B05.4 — Replace STFT-only alerting with evidence.** Compare qualified
  replacements and the old `fuel_trim_vs_rpm_load` on identical eligible rows
  and fault cases, including coverage outside the intersection. Demote the
  old alert after the replacement qualifies; keep raw STFT for inspection.
  If data is insufficient, expose that state instead of claiming replacement
  complete or silently continuing to present the old surface as validated.

## Cam command tracking

- [ ] **B05.5 — Validate command/measured identities and pairing.** Reuse the
  catalogued command and two measured channels. Use acquisition-time comparisons
  and a known-answer control; keep measured-bank names neutral until independently
  established. Persist steady command/RPM limits and freshness via B01.
- [ ] **B05.6 — Fit and verify the existing cam recipes end to end.** Validate
  steady tracking residuals on independent drives, then inject stuck/offset
  response cases and ordinary command transitions. Prove a fitted artifact
  produces live and replay predictions with its derivative gates. Characterize
  response lag separately; transition lag is not a steady tracking fault.

## TPS and AFR sensor balance

- [ ] **B05.7 — Validate TPS track consistency.** Reuse
  `tps_sensor_1_vs_sensor_2_did`, with actual paired acquisition observations
  across KOEO sweeps and running conditions. Verify slope/offset, per-sample
  residuals/ratios where defined, resolution and negative controls. Demonstrate
  offset, stuck-track and dropout detection without confusing sequential-read
  skew during rapid movement with sensor disagreement.
- [ ] **B05.8 — Qualify AFR bank balance separately.** Assess the existing
  `afr_bank_balance_robust` recipe with both bank measurements, fuel state,
  lag and relevant steady-state gates. Verify voltage-to-mixture semantics;
  equal voltages do not constitute an independently calibrated lambda reference.
  Keep this recipe unpublished if a useful bank-asymmetry claim cannot be
  supported; B06 owns target-versus-measured delivery, not raw voltage mapping.
- [ ] **B05.9 — Publish the qualified subset.** Archive B02 reports and run
  B03 compatibility/replay/rollout steps per accepted model. Record explicit
  reasons for each unfitted recipe and the smallest missing capture. Dashboard
  and MCP must distinguish unavailable evidence from a healthy subsystem.

**Done when:** each selected relationship works through fitting and actual
serving with documented sensitivity, and each remaining recipe has a named
data or validation gap. Recipe existence alone is not completion.
