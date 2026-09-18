## Fuel-block analysis: `_capture-fuel-pw-research-2026-09-18T140805`

Script: `analyze_fuel_blocks.py` (scratchpad). Cross-mode tolerance within 350 ms, reference=auto, ties=earlier; rate gate |d ln x/dt| ≤ 5%/s; stepwise channels require equal neighbours.

**1. Known answer (obd:RPM → did:ECM_RPM):** PASS. n_pairs 4000, slope 0.9982, offset +7.7108, r² 0.9968, drift 0.17 % [stable], b−a +4.2360 ± 29.9642, sep p95 120 ms

- stability windows: [{'n': 1334, 'rpm_min': 725.0, 'rpm_max': 2837.5, 'slope': 0.9991238119656444, 'slope_se': 0.0010661523870540122}, {'n': 1333, 'rpm_min': 725.0, 'rpm_max': 2862.5, 'slope': 0.9974369737166009, 'slope_se': 0.0014963239292497491}, {'n': 1333, 'rpm_min': 725.0, 'rpm_max': 2625.0, 'slope': 0.998526881073711, 'slope_se': 0.0019648225631891513}]

**2. D03.8, 0x1134 vs PID 0x44 (both φ):** unresolved: no enriched pairs

- gated: REFUSED: did:ECM_TARGET_EQUIV_RATIO_CANDIDATE is effectively constant over these 607 pairs (relative span 0.00e+00). Any scale fits a constant, so a fit here would describe the session, not the signals — compare over a window where it moves.
- ungated: REFUSED: did:ECM_TARGET_EQUIV_RATIO_CANDIDATE is effectively constant over these 3997 pairs (relative span 0.00e+00). Any scale fits a constant, so a fit here would describe the session, not the signals — compare over a window where it moves.
- pairs gated 607, enriched (0x44 > ENRICH) 0; agree within quantisation (±0.0079) 0.051070840197693576
- 0x1134 raw census: {64: 3998}
- 0x44: 296 distinct values, smallest step 0.0001220703125; top: {'0.979584': 984, '1.020386': 336, '0.870728': 182, '1.000000': 127, '1.006927': 120, '1.007599': 99, '1.006775': 91, '1.008270': 85, '1.006256': 84, '0.992218': 81, '0.993195': 80, '0.992767': 78}
- closed-loop check (0x44 == 1.000): {'n_pairs_0x44_in_stoich_band': 493, 'n_pairs_0x44_exactly_1': 15, 'n_0x1134_eq_64': 493, 'other_0x1134_raw': {}}
- residual (0x1134 − 0x44) by regime:

| regime | n | mean | median | p10 | p90 |
|---|---|---|---|---|---|
| other | 83 | 0.12927 | 0.12927 | 0.12927 | 0.12927 |
| stoich band ±0.025 | 493 | 0.01084 | 0.02042 | -0.02039 | 0.02042 |
| zero(DFCO/n.a.) | 31 | 1.0 | 1.0 | 1.0 | 1.0 |

**3. 0x1134 vs measured φ = 1/λ (PID 0x24), steady:** unresolved: no steady enriched pairs — {"n_pairs_steady": 3817, "n_pairs_target_enriched": 0, "target_eq_1": {"n": 3817, "median_phi_meas/target": 1.0001526111772425, "p10": 0.9827550010496956, "p90": 1.0231878484792765}}

**4. D03.9, register vs fuel actually delivered** (fuel/inj = MAF·20/rpm · (1/λ) / 14.7; fit on steady closed loop, validate on enriched):

- **0x1206** {'n_register_samples': 3995, 'n_dfco': 46, 'n_steady': 625, 'n_closed_loop': 610, 'n_enriched': 0}; DFCO median 51.5 (910.110353130016 per g air); per g air by regime {'closed_loop': 2258.94578313253}
  - CL fit: a = 27747 counts/g, b = +41.2 counts (+0.412 ms if 0.01 ms/count), r² 0.9642, [drifting], implied flow 290 cc/min if 0.01 ms/count
  - **unresolved: no steady enriched rows to validate on**

| regime | n | median | p10 | p90 |
|---|---|---|---|---|
| closed_loop | 622 | -0.25 | -5.46 | 5.33 |
| open_loop_other | 3 | -7.79 | -10.12 | -4.96 |

  By fuel per injection:

| fuel_bin | regime | n | median | p10 | p90 |
|---|---|---|---|---|---|
| (0.0, 0.01] | closed_loop | 423 | -0.71 | -5.12 | 4.7 |
| (0.0, 0.01] | open_loop_other | 3 | -7.79 | -10.12 | -4.96 |
| (0.01, 0.015] | closed_loop | 172 | 1.29 | -5.53 | 9.67 |
| (0.015, 0.02] | closed_loop | 23 | -0.92 | -14.37 | 2.42 |
| (0.02, 0.025] | closed_loop | 4 | 0.55 | -4.1 | 2.9 |

  By rpm:

| rpm_bin | regime | n | median | p10 | p90 |
|---|---|---|---|---|---|
| (0, 900] | closed_loop | 358 | -0.77 | -5.07 | 4.22 |
| (1500, 2000] | closed_loop | 90 | 0.68 | -8.51 | 11.75 |
| (1500, 2000] | open_loop_other | 3 | -7.79 | -10.12 | -4.96 |
| (2000, 2500] | closed_loop | 141 | 1.26 | -4.45 | 5.7 |
| (2500, 3000] | closed_loop | 28 | -0.87 | -5.95 | 3.82 |
| (900, 1500] | closed_loop | 5 | 0.28 | -4.98 | 1.39 |

- **0x1207** {'n_register_samples': 3999, 'n_dfco': 47, 'n_steady': 651, 'n_closed_loop': 638, 'n_enriched': 0}; DFCO median 52.0 (899.964093357271 per g air); per g air by regime {'closed_loop': 2271.5537017726797}
  - CL fit: a = 27325 counts/g, b = +44.8 counts (+0.448 ms if 0.01 ms/count), r² 0.9621, [drifting], implied flow 295 cc/min if 0.01 ms/count
  - **unresolved: no steady enriched rows to validate on**

| regime | n | median | p10 | p90 |
|---|---|---|---|---|
| closed_loop | 648 | -0.45 | -5.62 | 6.28 |
| open_loop_other | 3 | -5.8 | -12.46 | -3.21 |

  By fuel per injection:

| fuel_bin | regime | n | median | p10 | p90 |
|---|---|---|---|---|---|
| (0.0, 0.01] | closed_loop | 436 | -0.86 | -5.2 | 4.7 |
| (0.0, 0.01] | open_loop_other | 3 | -5.8 | -12.46 | -3.21 |
| (0.01, 0.015] | closed_loop | 189 | 1.06 | -6.87 | 9.56 |
| (0.015, 0.02] | closed_loop | 19 | -1.82 | -15.29 | 2.25 |
| (0.02, 0.025] | closed_loop | 3 | -0.95 | -7.94 | 0.12 |
| (0.025, 0.03] | closed_loop | 1 | 9.15 | 9.15 | 9.15 |

  By rpm:

| rpm_bin | regime | n | median | p10 | p90 |
|---|---|---|---|---|---|
| (0, 900] | closed_loop | 368 | -1.16 | -5.2 | 3.92 |
| (1500, 2000] | closed_loop | 98 | 1.61 | -8.21 | 10.26 |
| (1500, 2000] | open_loop_other | 3 | -5.8 | -12.46 | -3.21 |
| (2000, 2500] | closed_loop | 141 | 0.67 | -8.17 | 6.28 |
| (2500, 3000] | closed_loop | 35 | 2.43 | -3.56 | 9.87 |
| (900, 1500] | closed_loop | 6 | 1.32 | -5.6 | 13.98 |

- **0x1208** {'n_register_samples': 3880, 'n_dfco': 42, 'n_steady': 630, 'n_closed_loop': 614, 'n_enriched': 0}; DFCO median 3668.5 (63984.01190985949 per g air); per g air by regime {'closed_loop': 63465.72357436622}
  - CL fit: a = 933579 counts/g, b = +17.5 counts (+0.175 ms if 0.01 ms/count), r² 0.9635, [unresolved], implied flow 9 cc/min if 0.01 ms/count
  - **unresolved: no steady enriched rows to validate on**

| regime | n | median | p10 | p90 |
|---|---|---|---|---|
| closed_loop | 628 | -0.52 | -4.06 | 3.3 |
| open_loop_other | 2 | -9.28 | -9.3 | -9.26 |

  By fuel per injection:

| fuel_bin | regime | n | median | p10 | p90 |
|---|---|---|---|---|---|
| (0.0, 0.01] | closed_loop | 438 | -0.8 | -3.39 | 1.92 |
| (0.0, 0.01] | open_loop_other | 2 | -9.28 | -9.3 | -9.26 |
| (0.01, 0.015] | closed_loop | 171 | 0.75 | -5.94 | 8.06 |
| (0.015, 0.02] | closed_loop | 17 | 0.27 | -13.04 | 4.79 |
| (0.02, 0.025] | closed_loop | 2 | 0.64 | -1.42 | 2.69 |

  By rpm:

| rpm_bin | regime | n | median | p10 | p90 |
|---|---|---|---|---|---|
| (0, 900] | closed_loop | 367 | -0.9 | -3.37 | 1.58 |
| (1500, 2000] | closed_loop | 93 | 1.12 | -7.17 | 11.08 |
| (1500, 2000] | open_loop_other | 2 | -9.28 | -9.3 | -9.26 |
| (2000, 2500] | closed_loop | 137 | 0.48 | -5.19 | 5.64 |
| (2500, 3000] | closed_loop | 27 | -0.02 | -3.54 | 5.4 |
| (900, 1500] | closed_loop | 4 | 2.71 | -2.71 | 13.06 |

