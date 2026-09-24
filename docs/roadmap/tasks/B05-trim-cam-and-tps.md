# B05 — activate trim, bank-balance, cam and TPS relationships

**Depends on:** B01/B02; B03 supplies artifact lifecycle/publication.
These recipes already exist in `default_recipes`; inspect and extend them
rather than building a second model system. Follow
[B00](B00-behavior-models-README.md).

## Fuel trim and bank balance

- [x] **B05.1 — Inventory viable inputs per recipe.** Report fresh, paired
  STFT/LTFT for both banks, fuel status, RPM/load and required thermal context
  across independent drives. Validate signal units/identities before fitting.
  Keep missing-bank coverage distinct from zero bank difference.
  *Done 2026-09-22:* `src/analysis/behavior_input_inventory.py` +
  `scripts/inventory_behavior_inputs.py` count, per session and per recipe,
  through the recipe's **own serving contract**: channels never recorded,
  stale rows, named contract refusals and eligible rows, then group sessions
  into physical drives and count qualifying independent drives. Per-bank facts
  are separate (`paired_b1`/`paired_b2`/`paired_both`/`missing_bank`), and a
  missing bank is never reported as a zero bank difference. Run over 134
  unique local sessions / 53 drives: total trim accepts 10,082 (b1) and 9,542
  (b2) rows over 30 drives, 11 of them with ≥30 rows; both banks are paired
  on 23,524 running rows (19,949 of them closed loop, 946 in `warmup` mode
  and now excluded), exactly one bank on 7,399, and the STFT bank difference
  is exactly zero on 5,476 — three numbers that were previously one.
  `ve_manifold_vs_rpm_load` has **zero** eligible rows in 134 sessions (no
  MAP sensor: unavailable, not substituted). Two cautions recorded: a
  resampled row is not an observation (a DID sample stays fresh up to 2 s —
  `tps_sensor_1_vs_sensor_2_did`'s 1,920 "rows" are ~70 real pairs, §6), and
  **the only densely-polled trim captures (2026-09-21, ~2.2 Hz) have every
  LTFT sample at exactly 0.0** after the 09-20 flash, so a total-trim fit
  there would be an STFT fit under another name. Units validated: all trim
  samples lie on the 100/128 % lattice inside the ±25 % clamp
  (`trim_unit_checks`, which reports an all-zero channel separately from an
  off-lattice one). PID 0x03's two fuel-system states disagree on 0.99 % of
  the 77,797 payload-carrying samples across 137 sessions. Tests:
  `tests/test_behavior_trim_contracts.py`. Evidence:
  [report §5](../../../data/analyses/2026-09-22-b04-b05-measurements.md).
- [x] **B05.2 — Enforce fuel-state semantics.** Apply B01's persisted
  closed-loop and steady-state requirements to total-trim fits and serving.
  Exclude or separately model warmup, enrichment and fuel cut. The current
  `TOTAL_TRIM` is an additive percentage-point index; retain that label unless
  an independently justified calibration-specific multiplier is implemented.
  *Done 2026-09-22 (contract only; no fit):* both total-trim recipes persist
  closed loop = J1979 `0x02` only, the new contract field
  `excluded_operating_modes = ["warmup"]` (refusal
  `operating_mode_excluded:warmup`; an unlabelled mode is refused as
  `operating_mode_unknown`, never assumed warm), and steady-state gates
  `obd:RPM ≤ 10 rpm/s` / `obd:ENGINE_LOAD ≤ 0.5 %/s` measured on the dense
  2026-09-21 capture (the cam recipes' 75/5 passed 724 of 801 closed-loop
  rows, 443 locally transient, STFT σ 3.47 %; 10/0.5 passes 247, 74 locally
  transient, σ 1.16 %). Label kept: `output_unit` is now
  "percentage points (additive STFT+LTFT index)", matched in
  `behavior_validation.yaml`, with "an additive index, not a calibrated
  injector multiplier" in the recipe limitations. Two named gaps, both
  recorded rather than papered over: a rate gate is a **net-change** gate over
  the 30 s feature horizon (an up-and-back excursion passes: 74 of 247 rows),
  and **bank 2 is gated on fuel system 1's state** because `decode_fuel_status`
  keeps only PID 0x03 byte A — the fix is at collection, and the stored
  payloads can backfill the corpus. Tests:
  `tests/test_behavior_trim_contracts.py`. Evidence:
  [report §4](../../../data/analyses/2026-09-22-b04-b05-measurements.md).
- [~] **B05.3 — Fit total-trim B1/B2 and useful bank-balance baselines.**
  Use existing recipes and consistent calibration cohorts; compare robust
  simple baselines against surfaces using B02. Test bank-specific changes,
  symmetric changes, normal STFT oscillation and LTFT adaptation. Count two
  banks/common ECU corrections appropriately in fusion; do not claim a bank
  asymmetry identifies a particular injector, leak or sensor by itself.
  *Fitted 2026-09-23 on ZS44A-20260920; none qualifies.* STFT bank-balance
  criteria were declared in `behavior_validation.yaml` before the refit
  (constant baseline, 5-point target, max MAE 2.0, p95 3.75). Results on
  untouched drives ([report](../../../data/analyses/2026-09-23-behavior-refit-validation-report.md) §2, §5):
  - *Total trim B1/B2:* skill 0.11 / −0.01 < 0.2, warning width 12.1 / 10.7
    > 5, symmetric +5 %/120 s detection 0.04 / 0.07. Failed.
  - *STFT bank balance city/highway:* MAE 0.94 / 1.16 %, p95 2.49 / 3.13,
    bank-specific ±5 % steps detected 0.93–0.96, so they meet the accuracy
    and sensitivity criteria. Normal STFT oscillation still raises 2.86 /
    13.9 control events/h (> 2). Failed.
  - *STFT bank balance idle:* bias +1.77 %, held-out warning rate 18 %. Not
    published.
  - *LTFT:* **every LTFT sample on this calibration is exactly 0.0**, so the
    total-trim fits are STFT fits under another name, LTFT bank balance
    cannot fit (zero-width domain), and LTFT adaptation is untestable.

  **Fusion (done 2026-09-23):** total trim B1/B2, STFT/LTFT per bank and
  both bank balances vote as one `behavior:fuel_trim` voice
  (`src/alerts/fusion.py::behavior_fusion_group`, derived inside
  `FusionScorer.behavior_source`). Before, `fuel_trim_vs_rpm_load` (STFT B1)
  and `total_trim_b1` (STFT B1 + LTFT B1) were separate groups over the same
  reading. Evidence: `tests/test_fusion.py::test_one_measurement_is_one_voice_across_its_derived_channels`.
  **Remaining:**
  - LTFT content (relearn) on this calibration;
  - bank-balance event persistence that ordinary oscillation does not trip.
- [~] **B05.4 — Replace STFT-only alerting with evidence.** Compare qualified
  replacements and the old `fuel_trim_vs_rpm_load` on identical eligible rows
  and fault cases, including coverage outside the intersection. Demote the
  old alert after the replacement qualifies; keep raw STFT for inspection.
  If data is insufficient, expose that state instead of claiming replacement
  complete or silently continuing to present the old surface as validated.
  *Compared 2026-09-23; no replacement qualifies.* On the 1405 rows all three
  surfaces evaluate:

  | surface | MAE | bias | events | synthetic +5 %/120 s |
  |---|---|---|---|---|
  | published STFT v2 | 5.29 % | −3.98 | 17 | 0.00 |
  | candidate STFT v3 | 3.81 % | +0.37 | 2 | 0.03 |
  | total trim B1 | 3.48 % | +0.04 | 1 | 0.03 |

  Outside the intersection, STFT v3 has 4721 rows that total trim refuses:
  rate gates 3030, open loop/fuel cut 971, warmup 349. The old surface stays
  diagnostic-only (legacy, no contract, cannot alert); the candidate STFT v3
  also failed B02. Nothing is demoted or claimed replaced ([report](../../../data/analyses/2026-09-23-behavior-refit-validation-report.md) §5).
  **Remaining:** a qualifying replacement (B05.3).

## Cam command tracking

- [x] **B05.5 — Validate command/measured identities and pairing.** Reuse the
  catalogued command and two measured channels. Use acquisition-time comparisons
  and a known-answer control; keep measured-bank names neutral until independently
  established. Persist steady command/RPM limits and freshness via B01.
  *Done 2026-09-22 (no fit):* `scripts/validate_command_pairing.py` (new) runs
  a known-answer control, the pair under test on all rows and on a steady
  subset, a negative control and the per-sample ratio, all through
  `compare_channels` with an explicit `PairingPolicy`. Known answer:
  `obd:RPM → did:ECM_RPM` r²=0.9995, slope 0.996, n=292 (09-17T201000); in the
  Mode 22-only captures it refuses with "unavailable input" rather than
  returning a number. Negative control: commanded cam → `ECM_AF_SENSOR_V_B1`
  r²=0.05–0.12. Steady tracking (command held ≥ 5 s, RPM range ≤ 200), four
  captures: slope 0.967–1.022, 1:1 residual +0.11 … +0.94° with spread
  0.51–1.20°, per-sample ratio 0.97–1.04 with ±3.6–6.5 % spread, n=63–913 per
  pair. On all rows (transitions included) the same pairs read r² 0.90–0.96
  with ~3° spread and ±35–50 % ratio spread — response lag, not a tracking
  fault, and the reason the recipes gate on a steady command. Bank names stay
  neutral. Freshness/steady limits are already persisted on both cam recipes
  (`did:ECM_INTAKE_CAM_ANGLE_COMMANDED ≤ 0.25 °/s`, `did:ECM_RPM ≤ 75 rpm/s`),
  with the caveat recorded in B05.2 that a rate gate is a 30 s net-change
  gate. Evidence: [report §6](../../../data/analyses/2026-09-22-b04-b05-measurements.md).
- [~] **B05.6 — Fit and verify the existing cam recipes end to end.** Validate
  steady tracking residuals on independent drives, then inject stuck/offset
  response cases and ordinary command transitions. Prove a fitted artifact
  produces live and replay predictions with its derivative gates. Characterize
  response lag separately; transition lag is not a steady tracking fault.
  *Attempted 2026-09-23 — data-blocked on the current calibration.* Both cam
  recipes skip with "missing `did:ECM_INTAKE_CAM_ANGLE_COMMANDED__roc`". The
  commanded angle is polled in **no** ZS44A-20260920 session: the rom-trace
  captures poll only the two measured channels, and the tuning profile polls
  neither. That leaves 0 eligible rows (B05.1 inventory on the manifest
  corpus). **Smallest missing capture:** a normal drive polling
  `ECM_INTAKE_CAM_ANGLE_COMMANDED` with both measured channels in one round
  (as the 2026-08-06/-07 tuning-did captures did), across ≥ 3 drives. B05.5
  supplies the identities and steady subsets.

## TPS and AFR sensor balance

- [~] **B05.7 — Validate TPS track consistency.** Reuse
  `tps_sensor_1_vs_sensor_2_did`, with actual paired acquisition observations
  across KOEO sweeps and running conditions. Verify slope/offset, per-sample
  residuals/ratios where defined, resolution and negative controls. Demonstrate
  offset, stuck-track and dropout detection without confusing sequential-read
  skew during rapid movement with sensor disagreement.
  *Partial 2026-09-22 — blocked on a capture, not on a decision.* Same tool as
  B05.5. `did:ECM_THROTTLE_POS_SENSOR_2` is never polled densely: best local
  coverage 122 samples in one session, 71 in the next, **zero** in every
  high-rate capture. On `2026-09-18T213818` (known-answer control
  `obd:THROTTLE_POS → did:ECM_THROTTLE_POS_SENSOR_1` r²=0.995, slope
  0.0408 V/%, n=70) the tracks read r²=0.9897, slope 0.9918, offset
  +0.0100 V, 1:1 residual +0.0004 ± 0.0436 V on 71 pairs; on
  `2026-09-20T152758` r²=0.9988, slope 0.9933, offset +0.0090 V on 67 pairs —
  but there the known-answer control could not be paired at all, so that
  session is not evidence, and no session reached 30 pairs for a negative
  control. The two DIDs are read 125–250 ms apart, up to ~0.4 V of real
  throttle travel, so skew and disagreement are not separable at this cadence.
  **Smallest missing capture:** a KOEO throttle sweep (idle → WOT → idle,
  slowly) polling `0x120F` and `0x1210` in the same round at ≥ 5 Hz, plus the
  same pair on a normal drive. Detection of offset/stuck-track/dropout stays
  untested until then. Evidence: [report §6](../../../data/analyses/2026-09-22-b04-b05-measurements.md).
- [~] **B05.8 — Qualify AFR bank balance separately.** Assess the existing
  `afr_bank_balance_robust` recipe with both bank measurements, fuel state,
  lag and relevant steady-state gates. Verify voltage-to-mixture semantics;
  equal voltages do not constitute an independently calibrated lambda reference.
  Keep this recipe unpublished if a useful bank-asymmetry claim cannot be
  supported; B06 owns target-versus-measured delivery, not raw voltage mapping.
  *Assessed 2026-09-23 — kept unpublished.* The fit on the current calibration
  is refused publication:
  - held-out critical rate 18 %
  - 97 events in 2.4 h
  - MAE 0.102 V against a fixed 0.05 V warning band
  - no criteria declared

  The existing recipe has **no fuel-state, mode or steady-state gate**, so
  enrichment and fuel-cut rails are graded as bank asymmetry. The
  voltage→AFR transfer is verified (`afr_calibration.yaml`, both banks agree
  on held-out logs), but a voltage difference is non-linear in lambda
  (≈ 2 AFR/V rich of 14.7, ≈ 3.9 AFR/V lean). No asymmetry claim is
  supported ([report](../../../data/analyses/2026-09-23-behavior-refit-validation-report.md) §5). **Remaining:** closed-loop + steady gates on
  the recipe contract (a `default_recipes` change), a residual in lambda
  rather than volts, and declared criteria.
- [~] **B05.9 — Publish the qualified subset.** Archive B02 reports and run
  B03 compatibility/replay/rollout steps per accepted model. Record explicit
  reasons for each unfitted recipe and the smallest missing capture. Dashboard
  and MCP must distinguish unavailable evidence from a healthy subsystem.
  *2026-09-23: the qualified subset is empty.* B02 report archived
  ([report](../../../data/analyses/2026-09-23-behavior-refit-validation-report.md)). Per-recipe failure and unfitted reasons, and the smallest
  missing captures, are in §2/§5:
  - cam commanded DID
  - the KOEO throttle sweep + drive for TPS (B05.7; 244 rows)
  - LTFT relearn
  - AFR balance contract gates

  B03.8's status surfaces already show "none recorded"/failed evidence as
  not alerting. **Remaining:** publication once any model qualifies.

**Done when:** each selected relationship works through fitting and actual
serving with documented sensitivity, and each remaining recipe has a named
data or validation gap. Recipe existence alone is not completion.
