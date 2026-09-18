## Fuel-block analysis: `_capture-fuel-pw-research-2026-09-18T174742`

Script: `analyze_fuel_blocks.py` (scratchpad). Cross-mode tolerance within 350 ms, reference=auto, ties=earlier; rate gate |d ln x/dt| ≤ 5%/s; stepwise channels require equal neighbours.

**1. Known answer (obd:RPM → did:ECM_RPM):** PASS. n_pairs 2192, slope 0.9992, offset +6.2438, r² 0.9984, drift 0.32 % [stable], b−a +4.0502 ± 18.7127, sep p95 80 ms

- stability windows: [{'n': 731, 'rpm_min': 1837.5, 'rpm_max': 5662.5, 'slope': 0.9992889571688902, 'slope_se': 0.0010987898314788603}, {'n': 731, 'rpm_min': 1962.5, 'rpm_max': 5512.5, 'slope': 0.9982074240788816, 'slope_se': 0.001500821800654728}, {'n': 730, 'rpm_min': 1912.5, 'rpm_max': 3900.0, 'slope': 1.0013968358330325, 'slope_se': 0.002423778119819196}]

**2. D03.8, 0x1134 vs PID 0x44 (both φ):** 10 enriched pairs

- gated: n_pairs 633, slope 0.0198, offset +0.9990, r² 0.0967, drift n/a [unresolved], b−a +0.7891 ± 0.3952, sep p95 213 ms
- ungated: n_pairs 2183, slope 0.0077, offset +0.9974, r² 0.0173, drift 182.15 % [drifting], b−a +0.2416 ± 0.4245, sep p95 124 ms
- pairs gated 633, enriched (0x44 > ENRICH) 10; agree within quantisation (±0.0078) 0.06951026856240126
- 0x1134 raw census: {64: 2141, 65: 4, 66: 1, 68: 1, 69: 1, 71: 1, 73: 3, 74: 7, 75: 1, 76: 5, 77: 5, 78: 5, 79: 9}
- 0x44: 220 distinct values, smallest step 6.103515625e-05; top: {'0.000000': 1022, '1.000000': 287, '0.870728': 172, '1.020386': 127, '0.998352': 60, '1.000122': 55, '0.999298': 54, '0.999573': 54, '0.996460': 53, '1.000519': 52, '1.002838': 50, '0.998749': 50}
- closed-loop check (0x44 == 1.000): {'n_pairs_0x44_in_stoich_band': 70, 'n_pairs_0x44_exactly_1': 26, 'n_0x1134_eq_64': 70, 'other_0x1134_raw': {}}
- residual (0x1134 − 0x44) by regime:

| regime | n | mean | median | p10 | p90 |
|---|---|---|---|---|---|
| enriched>1.05 | 10 | -0.00703 | -0.00781 | -0.00781 | -0.00703 |
| other | 61 | 0.12927 | 0.12927 | 0.12927 | 0.12927 |
| stoich band ±0.025 | 70 | -0.00437 | 0.0 | -0.02039 | 0.02042 |
| zero(DFCO/n.a.) | 492 | 1.0 | 1.0 | 1.0 | 1.0 |

**3. 0x1134 vs measured φ = 1/λ (PID 0x24), steady:** see enriched stats — {"n_pairs_steady": 1414, "n_pairs_target_enriched": 11, "target_eq_1": {"n": 1403, "median_phi_meas/target": 0.9992071720436665, "p10": 0.9836990783825162, "p90": 1.0314197786295536}, "target_enriched": {"n": 11, "median_phi_meas/target": 0.9814488260907004, "p10": 0.9711096149090873, "p90": 0.9920856509492961}, "enriched_fit": {"r_squared": 0.9262862690024447, "slope": 1.0868959369977342, "offset": -0.12660791541694807, "slope_se": 0.10220409433897548, "drift_pct": 46.19479354793589, "scale_verdict": "unresolved"}}

**4. D03.9, register vs fuel actually delivered** (fuel/inj = MAF·20/rpm · (1/λ) / 14.7; fit on steady closed loop, validate on enriched):

- **0x1206** {'n_register_samples': 2163, 'n_dfco': 522, 'n_steady': 172, 'n_closed_loop': 161, 'n_enriched': 6}; DFCO median 56.0 (1047.0913461538462 per g air); per g air by regime {'closed_loop': 2108.5834127740704, 'enriched': 2491.7672266370464}
  - CL fit: a = 30909 counts/g, b = +0.6 counts (+0.006 ms if 0.01 ms/count), r² 0.9687, [drifting], implied flow 261 cc/min if 0.01 ms/count
  - **enriched median residual -1.2 %**

| regime | n | median | p10 | p90 |
|---|---|---|---|---|
| closed_loop | 165 | 0.22 | -3.76 | 5.4 |
| enriched | 6 | -1.21 | -2.11 | 0.92 |
| open_loop_other | 1 | 72.68 | 72.68 | 72.68 |

  By fuel per injection:

| fuel_bin | regime | n | median | p10 | p90 |
|---|---|---|---|---|---|
| (0.0, 0.01] | closed_loop | 6 | 22.26 | 14.14 | 28.28 |
| (0.0, 0.01] | open_loop_other | 1 | 72.68 | 72.68 | 72.68 |
| (0.01, 0.015] | closed_loop | 4 | 4.66 | 0.64 | 14.65 |
| (0.015, 0.02] | closed_loop | 20 | -0.19 | -8.59 | 5.58 |
| (0.02, 0.025] | closed_loop | 43 | -1.6 | -6.95 | 7.27 |
| (0.025, 0.03] | closed_loop | 59 | 0.43 | -1.26 | 2.13 |
| (0.03, 0.035] | closed_loop | 33 | -0.12 | -0.78 | 1.6 |
| (0.035, 0.04] | enriched | 6 | -1.21 | -2.11 | 0.92 |

  By rpm:

| rpm_bin | regime | n | median | p10 | p90 |
|---|---|---|---|---|---|
| (1500, 2000] | closed_loop | 2 | -1.88 | -9.5 | 5.73 |
| (2000, 2500] | closed_loop | 70 | 0.37 | -1.06 | 3.04 |
| (2500, 3000] | closed_loop | 45 | -0.46 | -3.83 | 3.2 |
| (3000, 3500] | closed_loop | 46 | 0.46 | -5.7 | 17.43 |
| (3000, 3500] | enriched | 1 | 1.77 | 1.77 | 1.77 |
| (3000, 3500] | open_loop_other | 1 | 72.68 | 72.68 | 72.68 |
| (3500, 4000] | closed_loop | 2 | 5.29 | 2.94 | 7.64 |
| (4000, 5000] | enriched | 1 | 0.07 | 0.07 | 0.07 |
| (5000, 7000] | enriched | 4 | -1.67 | -2.29 | -1.04 |

- **0x1207** {'n_register_samples': 2166, 'n_dfco': 530, 'n_steady': 163, 'n_closed_loop': 153, 'n_enriched': 6}; DFCO median 56.0 (1047.0856434199009 per g air); per g air by regime {'closed_loop': 2097.312656520902, 'enriched': 2541.519226575435}
  - CL fit: a = 31967 counts/g, b = -34.3 counts (-0.343 ms if 0.01 ms/count), r² 0.9558, [unresolved], implied flow 252 cc/min if 0.01 ms/count
  - **enriched median residual -0.7 %**

| regime | n | median | p10 | p90 |
|---|---|---|---|---|
| closed_loop | 156 | 0.39 | -3.56 | 3.3 |
| enriched | 6 | -0.67 | -1.69 | 1.86 |
| open_loop_other | 1 | 106.9 | 106.9 | 106.9 |

  By fuel per injection:

| fuel_bin | regime | n | median | p10 | p90 |
|---|---|---|---|---|---|
| (0.0, 0.01] | closed_loop | 4 | 36.37 | 34.24 | 40.51 |
| (0.0, 0.01] | open_loop_other | 1 | 106.9 | 106.9 | 106.9 |
| (0.01, 0.015] | closed_loop | 2 | 7.8 | 6.02 | 9.59 |
| (0.015, 0.02] | closed_loop | 18 | 0.47 | -9.24 | 3.63 |
| (0.02, 0.025] | closed_loop | 37 | 0.24 | -8.08 | 8.77 |
| (0.025, 0.03] | closed_loop | 57 | 0.69 | -1.06 | 1.84 |
| (0.03, 0.035] | closed_loop | 38 | -0.13 | -1.3 | 0.81 |
| (0.035, 0.04] | enriched | 6 | -0.67 | -1.69 | 1.86 |

  By rpm:

| rpm_bin | regime | n | median | p10 | p90 |
|---|---|---|---|---|---|
| (1500, 2000] | closed_loop | 3 | 1.42 | -2.42 | 9.52 |
| (2000, 2500] | closed_loop | 73 | 0.36 | -1.5 | 1.8 |
| (2500, 3000] | closed_loop | 41 | 0.5 | -6.67 | 3.05 |
| (3000, 3500] | closed_loop | 38 | 0.24 | -6.27 | 20.87 |
| (3000, 3500] | enriched | 1 | 3.33 | 3.33 | 3.33 |
| (3000, 3500] | open_loop_other | 1 | 106.9 | 106.9 | 106.9 |
| (3500, 4000] | closed_loop | 1 | -1.32 | -1.32 | -1.32 |
| (4000, 5000] | enriched | 1 | -1.3 | -1.3 | -1.3 |
| (5000, 7000] | enriched | 4 | -0.68 | -1.84 | 0.25 |

- **0x1208** {'n_register_samples': 2187, 'n_dfco': 536, 'n_steady': 167, 'n_closed_loop': 156, 'n_enriched': 6}; DFCO median 3375.5 (63745.32814544291 per g air); per g air by regime {'closed_loop': 63972.1296461864, 'enriched': 64456.675455997465}
  - CL fit: a = 932120 counts/g, b = +188.4 counts (+1.884 ms if 0.01 ms/count), r² 0.9625, [unresolved], implied flow 9 cc/min if 0.01 ms/count
  - **enriched median residual -16.6 %**

| regime | n | median | p10 | p90 |
|---|---|---|---|---|
| closed_loop | 160 | 0.11 | -4.82 | 2.95 |
| enriched | 6 | -16.62 | -17.57 | -12.23 |
| open_loop_other | 1 | 16.14 | 16.14 | 16.14 |

  By fuel per injection:

| fuel_bin | regime | n | median | p10 | p90 |
|---|---|---|---|---|---|
| (0.0, 0.01] | closed_loop | 5 | -17.78 | -18.52 | -8.68 |
| (0.0, 0.01] | open_loop_other | 1 | 16.14 | 16.14 | 16.14 |
| (0.01, 0.015] | closed_loop | 1 | -0.18 | -0.18 | -0.18 |
| (0.015, 0.02] | closed_loop | 19 | 0.46 | -9.0 | 10.23 |
| (0.02, 0.025] | closed_loop | 37 | 0.35 | -6.22 | 8.04 |
| (0.025, 0.03] | closed_loop | 62 | 0.16 | -1.3 | 1.47 |
| (0.03, 0.035] | closed_loop | 36 | -0.32 | -1.3 | 1.34 |
| (0.035, 0.04] | enriched | 6 | -16.62 | -17.57 | -12.23 |

  By rpm:

| rpm_bin | regime | n | median | p10 | p90 |
|---|---|---|---|---|---|
| (1500, 2000] | closed_loop | 1 | 7.69 | 7.69 | 7.69 |
| (2000, 2500] | closed_loop | 72 | 0.08 | -1.66 | 1.72 |
| (2500, 3000] | closed_loop | 46 | -0.17 | -4.63 | 4.81 |
| (3000, 3500] | closed_loop | 40 | 0.35 | -13.27 | 2.41 |
| (3000, 3500] | enriched | 1 | -10.5 | -10.5 | -10.5 |
| (3000, 3500] | open_loop_other | 1 | 16.14 | 16.14 | 16.14 |
| (3500, 4000] | closed_loop | 1 | 0.86 | 0.86 | 0.86 |
| (4000, 5000] | enriched | 1 | -13.95 | -13.95 | -13.95 |
| (5000, 7000] | enriched | 4 | -16.83 | -17.8 | -16.61 |

