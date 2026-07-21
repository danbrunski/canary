# Disposition report (20260721-042451)

14 session(s) screened. Regime gate (segmenter) -> data-quality gate (physical limits) -> four-tier disposition. No retrain occurred; this is a pure screen.

## Totals

| Tier | Cells |
|---|---:|
| Reject | 0 |
| Accept-recenter | 0 |
| Quarantine | 11,947 |
| Accept | 447,259 |

## Reject — physically-impossible cells, by channel

None.

## Persistently outside this-engine baseline

| Channel | Mode | Accept-recenter | Quarantine | Accept | Baseline band |
|---|---|---:|---:|---:|---|
| `decoded:ECM_RPM` | unknown | 0 | 715 | 1,537 | [5.68e+03, 6.57e+03] |
| `decoded:ECM_CALC_LOAD_A` | unknown | 0 | 708 | 1,530 | [217, 271] |
| `decoded:ECM_CALC_LOAD_B` | unknown | 0 | 706 | 1,536 | [217, 271] |
| `decoded:ECM_INTAKE_VACUUM_OR_IAC` | unknown | 0 | 618 | 1,620 | [46.7, 171] |
| `decoded:BRAKE_ACTIVITY_29E_7` | unknown | 0 | 589 | 1,617 | [99.2, 135] |
| `decoded:ECM_AIRFLOW_PROXY_23D` | all | 0 | 554 | 721 | [0, 0] |
| `obd:TIMING_ADVANCE` | unknown | 0 | 474 | 665 | [17, 17] |
| `decoded:MOTION_29E_5` | city | 0 | 394 | 549 | [-141, 179] |
| `obd:RPM` | unknown | 0 | 374 | 455 | [540, 985] |
| `obd:LONG_FUEL_TRIM_1` | unknown | 0 | 300 | 449 | [3.12, 3.12] |
| `obd:COMMANDED_EQUIV_RATIO` | unknown | 0 | 250 | 604 | [0.995, 0.995] |
| `obd:CONTROL_MODULE_VOLTAGE` | unknown | 0 | 249 | 885 | [12.3, 15.2] |
| `decoded:MOTION_29E_5` | suburban | 0 | 239 | 514 | [-55.3, 69.3] |
| `decoded:ECM_ENGINE_STATE_B` | city | 0 | 218 | 725 | [132, 274] |
| `decoded:ECM_ENGINE_STATE_C` | city | 0 | 217 | 726 | [132, 274] |
| `decoded:ECM_ENGINE_STATE_A` | city | 0 | 214 | 729 | [142, 266] |
| `decoded:MOTION_SIGNAL_590_2` | unknown | 0 | 194 | 1,963 | [0, 0] |
| `decoded:WHEEL_SPEED_PAIR_A` | unknown | 0 | 194 | 2,028 | [0, 0] |
| `decoded:MOTION_SIGNAL_354_1` | unknown | 0 | 192 | 1,982 | [0, 0] |
| `decoded:WHEEL_SPEED_PAIR_B` | unknown | 0 | 192 | 2,014 | [0, 0] |
| `obd:EVAPORATIVE_PURGE` | city | 0 | 189 | 205 | [0, 0] |
| `decoded:MOTION_SIGNAL_590_5` | unknown | 0 | 187 | 1,970 | [143, 161] |
| `obd:CATALYST_TEMP_B1S1` | unknown | 0 | 167 | 185 | [12, 12] |
| `obd:CATALYST_TEMP_B2S1` | unknown | 0 | 164 | 218 | [12, 12] |
| `decoded:ECM_AIRFLOW_PROXY_23D` | unknown | 0 | 161 | 2,081 | [0, 0] |
| `decoded:ECM_ENGINE_STATE_C` | all | 0 | 161 | 1,105 | [112, 308] |
| `decoded:ECM_ENGINE_STATE_B` | all | 0 | 160 | 1,115 | [112, 308] |
| `obd:MAF` | unknown | 0 | 160 | 1,024 | [0.387, 6.61] |
| `decoded:ECM_ENGINE_STATE_A` | all | 0 | 156 | 1,119 | [112, 308] |
| `obd:MAF` | city | 0 | 142 | 535 | [-17.9, 31.7] |
| `obd:FUEL_LEVEL` | warmup | 0 | 132 | 210 | [82.7, 82.7] |
| `decoded:STEERING_RATE_002` | city | 0 | 127 | 816 | [-7.9, 9.9] |
| `obd:FUEL_LEVEL` | idle | 0 | 127 | 160 | [75.4, 89.3] |
| `obd:COMMANDED_EQUIV_RATIO` | city | 0 | 108 | 305 | [0.88, 1.1] |
| `obd:RELATIVE_THROTTLE_POS` | unknown | 0 | 103 | 1,038 | [-0.351, 6.63] |
| `obd:FUEL_LEVEL` | unknown | 0 | 101 | 268 | [79.3, 86.2] |
| `obd:THROTTLE_POS` | unknown | 0 | 98 | 969 | [-2.66, 11.3] |
| `obd:EVAPORATIVE_PURGE` | highway | 0 | 96 | 531 | [5.83, 131] |
| `obd:COMMANDED_EQUIV_RATIO` | suburban | 0 | 82 | 236 | [0.878, 1.12] |
| `obd:O2_B2S2` | unknown | 0 | 79 | 753 | [0.191, 0.369] |
| `obd:O2_B1S2` | unknown | 0 | 78 | 592 | [0.191, 0.369] |
| `obd:COMMANDED_EQUIV_RATIO` | warmup | 0 | 75 | 427 | [0.993, 0.998] |
| `decoded:ECM_CALC_LOAD_A` | idle_transient | 0 | 74 | 177 | [170, 312] |
| `decoded:ECM_CALC_LOAD_B` | idle_transient | 0 | 74 | 177 | [170, 312] |
| `decoded:ECM_ENGINE_STATE_A` | suburban | 0 | 71 | 682 | [14.2, 388] |
| `obd:ENGINE_LOAD` | unknown | 0 | 65 | 1,030 | [-0.931, 40.9] |
| `decoded:ECM_ENGINE_STATE_C` | idle_transient | 0 | 64 | 187 | [211, 211] |
| `decoded:ECM_ENGINE_STATE_A` | idle_transient | 0 | 63 | 188 | [211, 211] |
| `decoded:ECM_ENGINE_STATE_B` | idle_transient | 0 | 63 | 188 | [211, 211] |
| `obd:ENGINE_LOAD` | idle | 0 | 59 | 921 | [15.7, 22.7] |
| `obd:EVAPORATIVE_PURGE` | unknown | 0 | 58 | 703 | [0, 0] |
| `obd:RELATIVE_THROTTLE_POS` | city | 0 | 57 | 620 | [-13.9, 21] |
| `obd:EVAPORATIVE_PURGE` | idle_transient | 0 | 49 | 51 | [0, 0] |
| `obd:COMMANDED_EQUIV_RATIO` | highway | 0 | 36 | 616 | [0.9, 1.1] |
| `obd:MAF` | idle | 0 | 36 | 1,084 | [2.72, 4.05] |
| `obd:FUEL_LEVEL` | city | 0 | 34 | 74 | [72.7, 93.6] |
| `decoded:ECM_ENGINE_STATE_C` | suburban | 0 | 33 | 720 | [5.3, 397] |
| `obd:O2_B1S2` | idle_transient | 0 | 33 | 82 | [0.315, 1.2] |
| `decoded:BRAKE_ACTIVITY_29E_7` | idle_transient | 0 | 30 | 221 | [165, 201] |
| `decoded:ECM_AIRFLOW_PROXY_23D` | idle_transient | 0 | 30 | 221 | [0, 0] |
| `decoded:ECM_ENGINE_STATE_B` | suburban | 0 | 30 | 723 | [5.3, 397] |
| `decoded:ECM_RPM` | idle_transient | 0 | 28 | 223 | [4.45e+03, 8e+03] |
| `decoded:MOTION_29E_5` | idle_transient | 0 | 28 | 223 | [-13.8, 21.8] |
| `decoded:ECM_AIRFLOW_PROXY_23D` | city | 0 | 24 | 919 | [-2.63e+04, 3.3e+04] |
| `decoded:MOTION_SIGNAL_590_2` | idle_transient | 0 | 24 | 227 | [0, 0] |
| `decoded:WHEEL_SPEED_PAIR_A` | idle_transient | 0 | 24 | 227 | [0, 0] |
| `decoded:WHEEL_SPEED_PAIR_B` | idle_transient | 0 | 24 | 227 | [0, 0] |
| `decoded:MOTION_SIGNAL_354_1` | idle_transient | 0 | 23 | 228 | [0, 0] |
| `decoded:ECM_AIRFLOW_PROXY_23D` | highway | 0 | 22 | 1,483 | [-7.73e+03, 3.34e+04] |
| `decoded:MOTION_SIGNAL_590_5` | idle_transient | 0 | 22 | 229 | [143, 161] |
| `obd:THROTTLE_POS` | city | 0 | 22 | 632 | [-20.1, 35.8] |
| `obd:SHORT_FUEL_TRIM_1` | unknown | 0 | 21 | 759 | [-6.95, 6.95] |
| `decoded:MOTION_SIGNAL_2D1_1` | idle_transient | 0 | 20 | 231 | [0, 0] |
| `obd:RPM` | idle_transient | 0 | 20 | 191 | [540, 985] |
| `decoded:MOTION_SIGNAL_280_5` | idle_transient | 0 | 19 | 232 | [0, 0] |
| `obd:THROTTLE_POS` | idle_transient | 0 | 18 | 184 | [0.825, 7.8] |
| `obd:RELATIVE_THROTTLE_POS` | highway | 0 | 16 | 1,050 | [-14.2, 41.6] |
| `obd:SHORT_FUEL_TRIM_1` | city | 0 | 14 | 399 | [-20.8, 20.8] |
| `decoded:ECM_RPM` | warmup | 0 | 13 | 779 | [1.34e+03, 1.2e+04] |
| `obd:RELATIVE_THROTTLE_POS` | idle_transient | 0 | 13 | 155 | [-0.743, 6.23] |
| `obd:SHORT_FUEL_TRIM_2` | unknown | 0 | 13 | 837 | [-6.95, 6.95] |
| `obd:THROTTLE_POS` | highway | 0 | 13 | 1,140 | [-9.48, 46.3] |
| `obd:MAF` | idle_transient | 0 | 12 | 194 | [0.153, 8.25] |
| `obd:RPM` | warmup | 0 | 11 | 673 | [158, 1.49e+03] |
| `obd:ENGINE_LOAD` | warmup | 0 | 10 | 637 | [15.4, 29.3] |
| `obd:SHORT_FUEL_TRIM_2` | city | 0 | 10 | 401 | [-20.8, 20.8] |
| `obd:THROTTLE_POS` | suburban | 0 | 10 | 571 | [-33.2, 57.5] |
| `obd:CATALYST_TEMP_B1S1` | highway | 0 | 9 | 172 | [434, 790] |
| `obd:ENGINE_LOAD` | idle_transient | 0 | 9 | 157 | [2.21, 44.1] |
| `obd:MAF` | suburban | 0 | 9 | 570 | [-54.2, 81.6] |
| `obd:RELATIVE_THROTTLE_POS` | suburban | 0 | 8 | 584 | [-37.9, 52.8] |
| `obd:CATALYST_TEMP_B2S1` | highway | 0 | 6 | 176 | [399, 825] |
| `obd:TIMING_ADVANCE` | idle_transient | 0 | 6 | 174 | [8.1, 25.9] |
| `obd:SHORT_FUEL_TRIM_1` | suburban | 0 | 5 | 329 | [-13.9, 13.9] |
| `obd:CONTROL_MODULE_VOLTAGE` | highway | 0 | 4 | 1,164 | [13, 14.4] |
| `gps:speed` | highway | 0 | 3 | 1,459 | [70.1, 153] |
| `decoded:ECM_RPM` | highway | 0 | 2 | 1,503 | [1.32e+04, 3.09e+04] |
| `obd:SHORT_FUEL_TRIM_1` | idle | 0 | 2 | 726 | [-5.39, 8.51] |
| `obd:SHORT_FUEL_TRIM_2` | highway | 0 | 2 | 643 | [-21.6, 20.1] |
| `obd:SHORT_FUEL_TRIM_2` | suburban | 0 | 2 | 333 | [-20.8, 20.8] |
| `gps:speed` | suburban | 0 | 2 | 552 | [17.1, 132] |
| `obd:CONTROL_MODULE_VOLTAGE` | warmup | 0 | 1 | 653 | [12.7, 15.5] |

## Next steps

Quarantined channels are candidates for a reviewed `accepted_baselines.yaml` override (operator action — see `docs/roadmap/online-training-strategy.md`'s "Directional overrides" section). This report never authors one.
