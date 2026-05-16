# Corpus report (2026-05-16T23:44:20.457639+00:00)

## Sessions

18 session directory(ies) under `data/sessions/`; 15 with `frames.log`, 2 with `pids.jsonl`.

| Session | Duration | Frames | OBD PIDs | Tags |
|---|---:|---:|---:|---:|
| `2026-05-11T033338` | 5s | 8,144 | — | 0 |
| `2026-05-11T034516` | 752s | 1,230,000 | — | 11 |
| `2026-05-11T152820` | — | 0 | — | 0 |
| `2026-05-11T152858` | 369s | 589,613 | — | 17 |
| `2026-05-11T154648` | 240s | 389,823 | — | 2 |
| `2026-05-11T211230` | — | 0 | — | 0 |
| `2026-05-11T211240` | 341s | 561,577 | — | 10 |
| `2026-05-11T213407` | 193s | 314,180 | — | 0 |
| `2026-05-11T222447` | 693s | 1,147,645 | — | 11 |
| `2026-05-13T004818` | 123s | 206,306 | 8 | 0 |
| `2026-05-13T005522` | 259s | 281,074 | 8 | 17 |
| `2026-05-13T011123` | 1137s | 1,852,012 | — | 14 |
| `2026-05-15T020322` | 281s | 428,614 | — | 22 |
| `2026-05-16T202800` | 816s | 1,133,939 | — | 34 |
| `2026-05-16T214006` | — | 0 | — | 0 |
| `2026-05-16T223328` | 333s | 549,464 | — | 0 |
| `2026-05-16T225450` | 304s | 496,708 | — | 0 |
| `2026-05-16T231901` | 314s | 511,125 | — | 0 |

## CAN ID inventory

59 distinct CAN IDs across 9,700,224 total frames. Top 30 by frame count:

| CAN ID | Frames | Period (ms) | DLC | Sessions | Bound? |
|---|---:|---:|---:|---:|:---:|
| `0x2DE` | 614,312 | 9 | 8 | 15 | — |
| `0x2D1` | 614,296 | 9 | 7 | 15 | ✓ |
| `0x002` | 589,584 | 9 | 5 | 15 | ✓ |
| `0x2A5` | 581,610 | 9 | 7 | 15 | — |
| `0x6F7` | 581,576 | 9 | 8 | 15 | — |
| `0x1F9` | 570,880 | 9 | 8 | 15 | ✓ |
| `0x231` | 570,865 | 9 | 8 | 15 | ✓ |
| `0x233` | 570,856 | 9 | 8 | 15 | ✓ |
| `0x23D` | 570,854 | 9 | 8 | 15 | ✓ |
| `0x23E` | 570,854 | 9 | 8 | 15 | — |
| `0x551` | 570,789 | 9 | 7 | 15 | ✓ |
| `0x794` | 570,768 | 9 | 3 | 15 | — |
| `0x280` | 307,162 | 18 | 8 | 15 | ✓ |
| `0x215` | 306,681 | 18 | 6 | 15 | — |
| `0x216` | 306,680 | 18 | 1 | 15 | — |
| `0x284` | 290,684 | 18 | 8 | 15 | ✓ |
| `0x285` | 290,674 | 18 | 8 | 15 | ✓ |
| `0x29E` | 290,666 | 18 | 8 | 15 | ✓ |
| `0x2A0` | 290,664 | 18 | 5 | 15 | — |
| `0x354` | 145,343 | 37 | 8 | 15 | ✓ |
| `0x60D` | 61,859 | 92 | 8 | 15 | — |
| `0x35D` | 61,708 | 92 | 8 | 15 | — |
| `0x358` | 61,639 | 92 | 8 | 15 | ✓ |
| `0x5C5` | 61,440 | 92 | 8 | 15 | — |
| `0x625` | 61,350 | 92 | 6 | 15 | ✓ |
| `0x385` | 58,262 | 92 | 7 | 15 | ✓ |
| `0x590` | 57,954 | 92 | 6 | 15 | ✓ |
| `0x7E8` | 34,983 | 22 | 8 | 11 | — |
| `0x7E0` | 29,662 | 24 | 8 | 10 | — |
| `0x7DF` | 5,344 | 16 | 8 | 2 | — |

## Bound-vs-unbound coverage

* By CAN ID:    16 / 59 (27.1%)
* By frame count: 5,621,858 / 9,700,224 (58.0%)

## Binding candidates (highest-variance unbound bytes)

Bytes with high observed variance that don't yet have a binding. Ranked candidates for `tools/broadcast_signals.py` differential or `tools/cross_reference_broadcast.py` regression next.

| CAN ID | Byte | Variance | Unique values | Samples |
|---|:---:|---:|---:|---:|
| `0x2A0` | 2 | 9837.4 | 101 | 56,250 |
| `0x2DE` | 5 | 6952.4 | 256 | 56,499 |
| `0x2DE` | 4 | 6816.8 | 256 | 56,499 |
| `0x280` | 6 | 6605.9 | 183 | 56,250 |
| `0x551` | 1 | 6590.7 | 256 | 56,489 |
| `0x2D1` | 5 | 6304.8 | 233 | 56,499 |
| `0x285` | 7 | 5482.5 | 256 | 56,250 |
| `0x284` | 7 | 5479.2 | 256 | 56,250 |
| `0x002` | 4 | 5478.3 | 255 | 56,504 |
| `0x284` | 6 | 5443.5 | 256 | 56,250 |
| `0x285` | 6 | 5443.4 | 256 | 56,250 |
| `0x2DE` | 7 | 4678.1 | 15 | 56,499 |
| `0x7E8` | 4 | 3890.9 | 230 | 28,068 |
| `0x7E8` | 5 | 2804.4 | 226 | 28,068 |
| `0x23D` | 5 | 2760.7 | 255 | 56,488 |
| `0x29E` | 2 | 1790.2 | 2 | 56,250 |
| `0x7E8` | 3 | 1519.0 | 153 | 28,068 |
| `0x23D` | 0 | 1284.3 | 12 | 56,488 |
| `0x7E0` | 3 | 1184.5 | 59 | 22,745 |
| `0x285` | 3 | 951.3 | 129 | 56,250 |

## 16-bit signal candidates (adjacent-byte correlation)

Adjacent unbound byte pairs whose values correlate (|r| ≥ 0.95). Strong correlation suggests the pair is the low+high bytes of a 16-bit big-endian signal — exactly the form `decode_raw(byte_length=2, byte_order=big)` expects. Worth running `cross_reference_broadcast.py` against PIDs / FSM names.

| CAN ID | Bytes | \|r\| | Samples |
|---|:---:|---:|---:|
| `0x2A5` | 5-6 | 1.000 | 56,499 |
| `0x2A5` | 2-3 | 1.000 | 56,499 |
| `0x794` | 0-1 | 0.997 | 56,489 |
| `0x280` | 6-7 | 0.953 | 56,250 |

## OBD-II PID coverage across the corpus

2 session(s) carry OBD telemetry; 8 distinct PIDs observed.

| PID | Seen in N sessions |
|---|---:|
| `CONTROL_MODULE_VOLTAGE` | 2 |
| `COOLANT_TEMP` | 2 |
| `ENGINE_LOAD` | 2 |
| `INTAKE_TEMP` | 2 |
| `MAF` | 2 |
| `RPM` | 2 |
| `THROTTLE_POS` | 2 |
| `TIMING_ADVANCE` | 2 |

## Suggested next analyses

* **For each binding candidate**, run `tools/broadcast_signals.py <session>` against a tagged session to see if the byte's value clusters by tag — that's the simplest differential and the same idiom that pinned `ECM_RPM`.
* **For each 16-bit pair**, run `tools/cross_reference_broadcast.py <session>` over the pair against the OBD PID set; high r² names the signal + scaling.
* **Behavior models**: `scripts/refit_behavior_models.py --all` over the OBD sessions to fit the surfaces that have the data (coolant warmup, idle-RPM-vs-coolant, timing-vs-RPM).
* **Anomaly baseline**: `scripts/train_anomaly_model.py` over the longest clean session, then `scripts/fp_benchmark.py` on a held-out one for the v1 done-definition's FPR claim.
* **DID inventory** (vehicle): `tools/scan_constant_dids.py` to bring the Mode 22 calibration / live-DID space into the corpus.
