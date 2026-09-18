## Fuel-block analysis: `_capture-fuel-target-research-2026-09-18T140241`

Script: `analyze_fuel_blocks.py` (scratchpad). Cross-mode tolerance within 350 ms, reference=auto, ties=earlier; rate gate |d ln x/dt| ≤ 5%/s; stepwise channels require equal neighbours.

**1. Known answer (obd:RPM → did:ECM_RPM):** FAIL — analysis stopped. n_pairs 628, slope 0.9998, offset +3.5259, r² 0.9997, drift 8.52 % [drifting], b−a +3.1346 ± 14.9854, sep p95 79 ms

- stability windows: [{'n': 210, 'rpm_min': 725.0, 'rpm_max': 775.0, 'slope': 0.9197809278350539, 'slope_se': 0.022107228953494044}, {'n': 209, 'rpm_min': 725.0, 'rpm_max': 2937.5, 'slope': 0.9984473561613684, 'slope_se': 0.0020562888899197016}, {'n': 209, 'rpm_min': 1975.0, 'rpm_max': 3137.5, 'slope': 1.0027460573661169, 'slope_se': 0.0022554932443071946}]

**Known answer FAILED its gate and was OVERRIDDEN by --override-known-answer; everything below is conditional on the operator accepting the reason above.**

**2. D03.8, 0x1134 vs PID 0x44 (both φ):** unresolved: no enriched pairs

- gated: REFUSED: did:ECM_TARGET_EQUIV_RATIO_CANDIDATE is effectively constant over these 204 pairs (relative span 0.00e+00). Any scale fits a constant, so a fit here would describe the session, not the signals — compare over a window where it moves.
- ungated: REFUSED: did:ECM_TARGET_EQUIV_RATIO_CANDIDATE is effectively constant over these 630 pairs (relative span 0.00e+00). Any scale fits a constant, so a fit here would describe the session, not the signals — compare over a window where it moves.
- pairs gated 204, enriched (0x44 > ENRICH) 0; agree within quantisation (±0.0079) 0.00980392156862745
- 0x1134 raw census: {64: 632}
- 0x44: 232 distinct values, smallest step 0.0001220703125; top: {'0.979584': 420, '1.020386': 84, '1.016296': 51, '1.016449': 43, '1.016174': 18, '1.016052': 15, '0.983795': 14, '0.983917': 14, '0.984070': 14, '1.016724': 13, '1.016846': 12, '1.012787': 10}
- closed-loop check (0x44 == 1.000): {'n_pairs_0x44_in_stoich_band': 204, 'n_pairs_0x44_exactly_1': 0, 'n_0x1134_eq_64': 204, 'other_0x1134_raw': {}}
- residual (0x1134 − 0x44) by regime:

| regime | n | mean | median | p10 | p90 |
|---|---|---|---|---|---|
| stoich band ±0.025 | 204 | 0.01399 | 0.02042 | -0.02039 | 0.02042 |

**3. 0x1134 vs measured φ = 1/λ (PID 0x24), steady:** unresolved: no steady enriched pairs — {"n_pairs_steady": 617, "n_pairs_target_enriched": 0, "target_eq_1": {"n": 617, "median_phi_meas/target": 1.0056160810188737, "p10": 0.982796283711452, "p90": 1.022977022977023}}

**4. D03.9, register vs fuel actually delivered** (fuel/inj = MAF·20/rpm · (1/λ) / 14.7; fit on steady closed loop, validate on enriched):

- **0x1206**: did:ECM_INTAKE_PRESSURE_1206 never captured in this session
- **0x1207**: did:ECM_INTAKE_PRESSURE_1207 never captured in this session
- **0x1208** {'n_register_samples': 626, 'n_dfco': 0, 'n_steady': 299, 'n_closed_loop': 299, 'n_enriched': 0}; DFCO median None (None per g air); per g air by regime {'closed_loop': 63662.57142857143}
  - CL fit: a = 954324 counts/g, b = -160.2 counts (-1.602 ms if 0.01 ms/count), r² 0.9907, [drifting], implied flow 8 cc/min if 0.01 ms/count
  - **unresolved: no steady enriched rows to validate on**

| regime | n | median | p10 | p90 |
|---|---|---|---|---|
| closed_loop | 299 | -0.16 | -1.88 | 2.36 |

  By fuel per injection:

| fuel_bin | regime | n | median | p10 | p90 |
|---|---|---|---|---|---|
| (0.0, 0.01] | closed_loop | 286 | -0.21 | -1.89 | 2.34 |
| (0.01, 0.015] | closed_loop | 10 | 1.43 | -1.35 | 3.94 |
| (0.015, 0.02] | closed_loop | 3 | 0.31 | -0.12 | 1.99 |

  By rpm:

| rpm_bin | regime | n | median | p10 | p90 |
|---|---|---|---|---|---|
| (0, 900] | closed_loop | 279 | -0.23 | -1.84 | 2.35 |
| (2000, 2500] | closed_loop | 4 | 1.04 | -1.46 | 2.67 |
| (2500, 3000] | closed_loop | 14 | 0.62 | -1.41 | 3.34 |
| (3000, 3500] | closed_loop | 2 | -1.18 | -2.03 | -0.34 |

