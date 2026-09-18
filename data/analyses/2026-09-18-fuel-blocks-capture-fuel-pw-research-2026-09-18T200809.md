## Fuel-block analysis: `_capture-fuel-pw-research-2026-09-18T200809`

Script: `analyze_fuel_blocks.py` (scratchpad). Cross-mode tolerance within 350 ms, reference=auto, ties=earlier; rate gate |d ln x/dt| ≤ 5%/s; stepwise channels require equal neighbours.

**1. Known answer (obd:RPM → did:ECM_RPM):** PASS. n_pairs 2290, slope 0.9992, offset +6.0942, r² 0.9976, drift 0.69 % [stable], b−a +4.1118 ± 24.7681, sep p95 130 ms

- stability windows: [{'n': 764, 'rpm_min': 1537.5, 'rpm_max': 3312.5, 'slope': 0.9993055594942161, 'slope_se': 0.0021606038147448684}, {'n': 763, 'rpm_min': 1875.0, 'rpm_max': 3737.5, 'slope': 0.9924250063351634, 'slope_se': 0.0019488456890611825}, {'n': 763, 'rpm_min': 0.0, 'rpm_max': 4450.0, 'slope': 0.9993213776177947, 'slope_se': 0.0018574032682936396}]

**2. D03.8, 0x1134 vs PID 0x44 (both φ):** 203 enriched pairs

- gated: n_pairs 875, slope 0.0690, offset +0.9865, r² 0.2600, drift 114.85 % [drifting], b−a +0.3287 ± 0.4532, sep p95 247 ms
- ungated: n_pairs 2336, slope 0.0375, offset +0.9826, r² 0.0740, drift 103.55 % [drifting], b−a +0.1323 ± 0.3323, sep p95 127 ms
- pairs gated 875, enriched (0x44 > ENRICH) 203; agree within quantisation (±0.0079) 0.352
- 0x1134 raw census: {64: 2102, 65: 1, 69: 1, 74: 225, 75: 4, 76: 4}
- 0x44: 241 distinct values, smallest step 0.0001220703125; top: {'0.000000': 569, '1.164062': 388, '0.870728': 293, '1.000000': 237, '0.992096': 208, '1.020386': 205, '0.994965': 98, '0.979584': 87, '0.998749': 55, '0.999573': 53, '1.001068': 52, '1.000519': 50}
- closed-loop check (0x44 == 1.000): {'n_pairs_0x44_in_stoich_band': 272, 'n_pairs_0x44_exactly_1': 42, 'n_0x1134_eq_64': 272, 'other_0x1134_raw': {}}
- residual (0x1134 − 0x44) by regime:

| regime | n | mean | median | p10 | p90 |
|---|---|---|---|---|---|
| enriched>1.05 | 203 | -0.00867 | -0.00781 | -0.00781 | -0.00781 |
| other | 128 | 0.12927 | 0.12927 | 0.12927 | 0.12927 |
| stoich band ±0.025 | 272 | 0.003 | 0.00504 | -0.02039 | 0.01917 |
| zero(DFCO/n.a.) | 272 | 1.0 | 1.0 | 1.0 | 1.0 |

**3. 0x1134 vs measured φ = 1/λ (PID 0x24), steady:** see enriched stats — {"n_pairs_steady": 1763, "n_pairs_target_enriched": 198, "target_eq_1": {"n": 1565, "median_phi_meas/target": 1.0001526111772425, "p10": 0.9759061314111898, "p90": 1.047650735705745}, "target_enriched": {"n": 198, "median_phi_meas/target": 0.9970760311090137, "p10": 0.9720089138390688, "p90": 1.0111278682707254}, "enriched_fit": {"r_squared": 0.0032420373758577092, "slope": 0.477159373918388, "offset": 0.5969269683493996, "slope_se": 0.5976147013460471, "drift_pct": null, "scale_verdict": "unresolved"}}

**4. D03.9, register vs fuel actually delivered** (fuel/inj = MAF·20/rpm · (1/λ) / 14.7; fit on steady closed loop, validate on enriched):

- **0x1206** {'n_register_samples': 2290, 'n_dfco': 309, 'n_steady': 419, 'n_closed_loop': 222, 'n_enriched': 188}; DFCO median 55.0 (930.4249291784703 per g air); per g air by regime {'closed_loop': 2094.521227694815, 'enriched': 2412.0841313858555}
  - CL fit: a = 29665 counts/g, b = +30.1 counts (+0.301 ms if 0.01 ms/count), r² 0.9949, [drifting], implied flow 271 cc/min if 0.01 ms/count
  - **enriched median residual +1.4 %**

| regime | n | median | p10 | p90 |
|---|---|---|---|---|
| closed_loop | 228 | 0.52 | -2.53 | 3.23 |
| enriched | 188 | 1.38 | -0.46 | 3.97 |
| open_loop_other | 3 | -100.0 | -100.0 | -100.0 |

  By fuel per injection:

| fuel_bin | regime | n | median | p10 | p90 |
|---|---|---|---|---|---|
| (0.0, 0.01] | closed_loop | 62 | 2.58 | -0.34 | 4.22 |
| (0.01, 0.015] | closed_loop | 9 | -0.02 | -3.97 | 6.31 |
| (0.01, 0.015] | enriched | 2 | -1.25 | -1.78 | -0.72 |
| (0.015, 0.02] | closed_loop | 8 | -6.53 | -12.22 | 2.6 |
| (0.02, 0.025] | closed_loop | 8 | -2.49 | -13.22 | 7.35 |
| (0.025, 0.03] | closed_loop | 30 | -1.37 | -5.5 | 0.63 |
| (0.03, 0.035] | closed_loop | 88 | 0.45 | -0.63 | 1.87 |
| (0.035, 0.04] | closed_loop | 23 | 0.19 | -0.92 | 0.76 |
| (0.035, 0.04] | enriched | 45 | 3.66 | 1.9 | 4.83 |
| (0.04, 0.05] | enriched | 141 | 0.94 | -0.47 | 2.65 |

  By rpm:

| rpm_bin | regime | n | median | p10 | p90 |
|---|---|---|---|---|---|
| (0.0, 900.0] | closed_loop | 54 | 2.51 | -0.28 | 4.01 |
| (0.0, 900.0] | enriched | 2 | -1.25 | -1.78 | -0.72 |
| (2000.0, 2500.0] | closed_loop | 74 | 0.4 | -2.8 | 3.65 |
| (2000.0, 2500.0] | enriched | 68 | 2.75 | 0.97 | 4.63 |
| (2500.0, 3000.0] | closed_loop | 100 | 0.1 | -3.45 | 1.25 |
| (2500.0, 3000.0] | enriched | 108 | 0.57 | -0.7 | 2.2 |
| (3000.0, 3500.0] | enriched | 9 | 2.5 | 1.05 | 4.41 |
| (3500.0, 4000.0] | enriched | 1 | 2.62 | 2.62 | 2.62 |

- **0x1207** {'n_register_samples': 2290, 'n_dfco': 311, 'n_steady': 422, 'n_closed_loop': 223, 'n_enriched': 187}; DFCO median 55.0 (928.9056603773585 per g air); per g air by regime {'closed_loop': 2105.6569889254715, 'enriched': 2408.1839460278125}
  - CL fit: a = 29799 counts/g, b = +23.2 counts (+0.232 ms if 0.01 ms/count), r² 0.9910, [drifting], implied flow 270 cc/min if 0.01 ms/count
  - **enriched median residual +1.3 %**

| regime | n | median | p10 | p90 |
|---|---|---|---|---|
| closed_loop | 232 | 1.47 | -5.41 | 5.49 |
| enriched | 187 | 1.29 | -0.38 | 3.71 |
| open_loop_other | 3 | -100.0 | -100.0 | -100.0 |

  By fuel per injection:

| fuel_bin | regime | n | median | p10 | p90 |
|---|---|---|---|---|---|
| (0.0, 0.01] | closed_loop | 65 | 3.61 | 0.47 | 9.08 |
| (0.01, 0.015] | closed_loop | 8 | 0.89 | -2.24 | 6.17 |
| (0.01, 0.015] | enriched | 2 | -0.15 | -0.18 | -0.12 |
| (0.015, 0.02] | closed_loop | 8 | -5.73 | -19.7 | 1.68 |
| (0.02, 0.025] | closed_loop | 9 | -5.96 | -12.82 | 2.54 |
| (0.025, 0.03] | closed_loop | 28 | -2.76 | -12.18 | 2.22 |
| (0.03, 0.035] | closed_loop | 90 | 0.9 | -2.14 | 2.52 |
| (0.035, 0.04] | closed_loop | 24 | 1.69 | 0.31 | 2.3 |
| (0.035, 0.04] | enriched | 45 | 2.92 | 1.24 | 4.25 |
| (0.04, 0.05] | enriched | 140 | 0.95 | -0.51 | 3.02 |

  By rpm:

| rpm_bin | regime | n | median | p10 | p90 |
|---|---|---|---|---|---|
| (0.0, 900.0] | closed_loop | 55 | 3.6 | 0.73 | 9.08 |
| (0.0, 900.0] | enriched | 2 | -0.15 | -0.18 | -0.12 |
| (2000.0, 2500.0] | closed_loop | 75 | -0.43 | -7.68 | 2.62 |
| (2000.0, 2500.0] | enriched | 67 | 1.92 | -0.08 | 3.98 |
| (2500.0, 3000.0] | closed_loop | 100 | 1.57 | -3.61 | 2.9 |
| (2500.0, 3000.0] | enriched | 108 | 1.02 | -0.5 | 2.98 |
| (3000.0, 3500.0] | enriched | 9 | 3.86 | 2.38 | 4.85 |
| (3500.0, 4000.0] | enriched | 1 | 2.39 | 2.39 | 2.39 |
| (900.0, 1500.0] | closed_loop | 2 | -9.47 | -10.31 | -8.64 |

- **0x1208** {'n_register_samples': 2290, 'n_dfco': 318, 'n_steady': 454, 'n_closed_loop': 257, 'n_enriched': 183}; DFCO median 3889.5 (64003.32643911068 per g air); per g air by regime {'closed_loop': 63777.68329718004, 'enriched': 64068.459503160375}
  - CL fit: a = 941363 counts/g, b = -32.3 counts (-0.323 ms if 0.01 ms/count), r² 0.9985, [stable], implied flow 9 cc/min if 0.01 ms/count
  - **enriched median residual -13.0 %**

| regime | n | median | p10 | p90 |
|---|---|---|---|---|
| closed_loop | 268 | -0.23 | -2.29 | 1.48 |
| enriched | 183 | -13.04 | -14.97 | -10.54 |
| open_loop_other | 3 | -100.0 | -100.0 | -24.16 |

  By fuel per injection:

| fuel_bin | regime | n | median | p10 | p90 |
|---|---|---|---|---|---|
| (0.0, 0.01] | closed_loop | 79 | -1.28 | -13.97 | 1.19 |
| (0.01, 0.015] | closed_loop | 7 | 0.21 | -0.88 | 1.93 |
| (0.01, 0.015] | enriched | 2 | -7.14 | -7.33 | -6.94 |
| (0.01, 0.015] | open_loop_other | 1 | -5.2 | -5.2 | -5.2 |
| (0.015, 0.02] | closed_loop | 11 | 1.0 | -0.71 | 5.84 |
| (0.02, 0.025] | closed_loop | 15 | 1.04 | -3.38 | 6.57 |
| (0.025, 0.03] | closed_loop | 37 | 0.08 | -1.56 | 1.89 |
| (0.03, 0.035] | closed_loop | 97 | -0.01 | -0.98 | 0.86 |
| (0.035, 0.04] | closed_loop | 22 | -0.43 | -0.82 | 0.25 |
| (0.035, 0.04] | enriched | 53 | -11.83 | -13.33 | -9.96 |
| (0.04, 0.05] | enriched | 128 | -13.6 | -15.12 | -11.39 |

  By rpm:

| rpm_bin | regime | n | median | p10 | p90 |
|---|---|---|---|---|---|
| (0.0, 900.0] | closed_loop | 56 | -1.62 | -9.42 | 0.19 |
| (0.0, 900.0] | enriched | 2 | -7.14 | -7.33 | -6.94 |
| (0.0, 900.0] | open_loop_other | 1 | -5.2 | -5.2 | -5.2 |
| (1500.0, 2000.0] | closed_loop | 3 | 0.45 | 0.32 | 1.47 |
| (2000.0, 2500.0] | closed_loop | 86 | 0.05 | -1.27 | 1.36 |
| (2000.0, 2500.0] | enriched | 64 | -12.35 | -14.16 | -11.14 |
| (2500.0, 3000.0] | closed_loop | 119 | -0.05 | -1.64 | 1.83 |
| (2500.0, 3000.0] | enriched | 108 | -13.64 | -15.12 | -10.94 |
| (3000.0, 3500.0] | enriched | 8 | -10.38 | -11.92 | -8.9 |
| (3500.0, 4000.0] | enriched | 1 | -11.36 | -11.36 | -11.36 |
| (900.0, 1500.0] | closed_loop | 4 | -14.18 | -18.29 | -3.82 |

