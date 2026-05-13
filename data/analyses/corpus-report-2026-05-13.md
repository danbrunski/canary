# Corpus report (2026-05-13T22:50:19.986826+00:00)

## Sessions

12 session directory(ies) under `data/sessions/`; 10 with `frames.log`, 2 with `pids.jsonl`.

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

## CAN ID inventory

59 distinct CAN IDs across 6,580,374 total frames. Top 30 by frame count:

| CAN ID | Frames | Period (ms) | DLC | Sessions | Bound? |
|---|---:|---:|---:|---:|:---:|
| `0x2DE` | 409,779 | 10 | 8 | 10 | — |
| `0x2D1` | 409,765 | 10 | 7 | 10 | ✓ |
| `0x002` | 401,155 | 10 | 5 | 10 | ✓ |
| `0x2A5` | 395,882 | 10 | 7 | 10 | — |
| `0x6F7` | 395,851 | 10 | 8 | 10 | — |
| `0x1F9` | 388,424 | 10 | 8 | 10 | ✓ |
| `0x231` | 388,413 | 10 | 8 | 10 | ✓ |
| `0x233` | 388,404 | 10 | 8 | 10 | ✓ |
| `0x23D` | 388,401 | 10 | 8 | 10 | ✓ |
| `0x23E` | 388,400 | 10 | 8 | 10 | — |
| `0x551` | 388,338 | 10 | 7 | 10 | ✓ |
| `0x794` | 388,317 | 10 | 3 | 10 | — |
| `0x280` | 204,898 | 19 | 8 | 10 | ✓ |
| `0x215` | 204,619 | 19 | 6 | 10 | — |
| `0x216` | 204,617 | 19 | 1 | 10 | — |
| `0x284` | 197,918 | 19 | 8 | 10 | ✓ |
| `0x285` | 197,911 | 19 | 8 | 10 | ✓ |
| `0x29E` | 197,904 | 19 | 8 | 10 | ✓ |
| `0x2A0` | 197,901 | 19 | 5 | 10 | — |
| `0x354` | 98,962 | 39 | 8 | 10 | ✓ |
| `0x60D` | 41,321 | 97 | 8 | 10 | — |
| `0x35D` | 41,190 | 97 | 8 | 10 | — |
| `0x358` | 41,125 | 97 | 8 | 10 | — |
| `0x5C5` | 40,987 | 97 | 8 | 10 | — |
| `0x625` | 40,933 | 97 | 6 | 10 | ✓ |
| `0x385` | 39,671 | 97 | 7 | 10 | ✓ |
| `0x590` | 39,462 | 97 | 6 | 10 | ✓ |
| `0x7E8` | 29,793 | 77 | 8 | 8 | — |
| `0x7E0` | 24,472 | 95 | 8 | 7 | — |
| `0x7DF` | 5,344 | 16 | 8 | 2 | — |

## Bound-vs-unbound coverage

* By CAN ID:    15 / 59 (25.4%)
* By frame count: 3,770,559 / 6,580,374 (57.3%)

## Binding candidates (highest-variance unbound bytes)

Bytes with high observed variance that don't yet have a binding. Ranked candidates for `tools/broadcast_signals.py` differential or `tools/cross_reference_broadcast.py` regression next.

| CAN ID | Byte | Variance | Unique values | Samples |
|---|:---:|---:|---:|---:|
| `0x2A0` | 2 | 12879.9 | 100 | 36,250 |
| `0x2DE` | 5 | 6662.8 | 256 | 36,499 |
| `0x551` | 1 | 6549.2 | 256 | 36,489 |
| `0x2DE` | 4 | 6443.4 | 256 | 36,499 |
| `0x285` | 7 | 5489.1 | 256 | 36,250 |
| `0x284` | 7 | 5484.6 | 256 | 36,250 |
| `0x285` | 6 | 5437.3 | 256 | 36,250 |
| `0x284` | 6 | 5437.2 | 256 | 36,250 |
| `0x002` | 4 | 5290.8 | 253 | 36,504 |
| `0x7E8` | 4 | 4160.2 | 230 | 22,878 |
| `0x23D` | 5 | 3408.8 | 255 | 36,488 |
| `0x29E` | 2 | 2695.2 | 2 | 36,250 |
| `0x2DE` | 7 | 2652.0 | 10 | 36,499 |
| `0x7E8` | 3 | 1813.4 | 153 | 22,878 |
| `0x2D1` | 5 | 1728.8 | 171 | 36,499 |
| `0x7E8` | 5 | 1497.3 | 168 | 22,878 |
| `0x285` | 3 | 1424.1 | 129 | 36,250 |
| `0x284` | 5 | 1359.3 | 256 | 36,250 |
| `0x284` | 3 | 1344.8 | 129 | 36,250 |
| `0x280` | 6 | 1334.3 | 146 | 36,250 |

## 16-bit signal candidates (adjacent-byte correlation)

Adjacent unbound byte pairs whose values correlate (|r| ≥ 0.95). Strong correlation suggests the pair is the low+high bytes of a 16-bit big-endian signal — exactly the form `decode_raw(byte_length=2, byte_order=big)` expects. Worth running `cross_reference_broadcast.py` against PIDs / FSM names.

| CAN ID | Bytes | \|r\| | Samples |
|---|:---:|---:|---:|
| `0x2A5` | 2-3 | 1.000 | 36,499 |
| `0x2A5` | 5-6 | 1.000 | 36,499 |
| `0x794` | 0-1 | 0.996 | 36,489 |

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
