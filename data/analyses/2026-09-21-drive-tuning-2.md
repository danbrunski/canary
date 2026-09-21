# 2026-09-21 night drive — `tuning`, highway pulls (NA baseline)

Session `_capture-tuning-2026-09-21T232153`, 25.4 min, warm throughout (coolant
86–95 °C), baro 79–82 kPa, IAT 26–50 °C (measured). Calibration `ZS44A-20260920`.
First drive with WOT content above 5,000 rpm: 45 load ≥ 85 % events, 19 of them
≥ 2 s; longest 9.3 s (2,550 → 4,188 rpm); two reached 5,525 and 5,700 rpm. Still
not a steady 3rd-gear pull from 2,500 rpm (traffic). Counts toward baseline
items NA1/NA3 in `docs/roadmap/supercharger-build.md`.

Quality verdict "fail" was bookkeeping, not data loss: the capture ran 95 s past
key-off (ECU silent, every channel "gapped") and took 15 s to start; 108 MCP2515
overflow frames on the broadcast bus. See the verdict fix that followed.

## Known answers

| pair | n | result |
|---|---|---|
| RAM timing vs PID 0x0E | 1,431 | median Δ 0.0°, 80 % within 1° |
| RAM target φ vs PID 0x44 | 1,230 | ratio 1.0000 (p5 0.980, p95 1.148) |
| RAM engine speed vs PID rpm | 1,436 | ratio 1.0000 |

## WOT fuelling — flat ~3 % lean, 2,000 to 5,800 rpm

Per-sample error after the first 1 s of each enrichment event (16 events):

| rpm | λ (PID 0x24) | B1 volts | B2 volts |
|---|---|---|---|
| 2,500–3,000 | +3.0 % | +4.0 % | +5.3 % |
| 3,000–3,500 | +2.6 % | +3.6 % | +3.4 % |
| 3,500–4,000 | +3.2 % | +4.0 % | +2.3 % |
| 4,000–4,500 | +2.2 % | +4.3 % | +3.0 % |
| 4,500–5,000 | +3.0 % | +4.2 % | +3.1 % |
| 5,000–5,800 | +2.5 % | +3.7 % | +3.0 % |
| **all** | **+2.8 %** | **+4.0 %** | **+3.2 %** |

Independent of settle time (0 → 1.5 s: +2.73 → +2.88 %), per event +0.2 to +3.6 %.
Same size as the evening drive and August. It is **flat across 40–140 g/s and
2,000–5,800 rpm** — constant in percent, the multiplicative shape. The 5,000–5,800
band (where the 2026-09-20 flash edited a cell) reads the same as its neighbours:
the edit did not open a local hole.

## Closed loop

Warm closed loop, 2,519 samples: STFT median −1.6 % (B1) / −0.8 % (B2). By cell:
−2 to −5 % at 15–40 g/s across 900–5,000 rpm, ≈ 0 at < 15 g/s off idle and
0 to −3 % above 40 g/s. **Idle is the other way: median +4.7 % B1 / +2.3 % B2**
(σ 9–11 %). **LTFT exactly 0.00 % on both banks again** — still not relearned.

Read with the evening drive: WOT is lean by a constant ~3 %; part throttle is
pulled 2–5 % rich; idle is pushed lean. Positive at idle, shrinking with load, is
the fixed-quantity (additive) signature — an idle-only air leak, or injector
dead time — sitting on top of whatever makes mid-load rich and WOT lean.
Nothing here changes the evening conclusion: do not move K; log fuel pressure
through a pull (the AEM gauge on a second CAN channel will do it).

## Timing and airflow

WOT timing medians (PID 0x0E): 10–11° to 3,500, 8° at 3,500–4,000, 10° at
4,000–4,500, 12° at 4,500–5,000, **14° at 5,000–5,800** (n 18, 4 events).

VE (measured baro/IAT): 0.77 → 0.80 → 0.86 → 0.87 → 0.88 → **0.89 at
4,000–4,500** → 0.88 → 0.85 at 5,000–5,800. Air per cylinder-event peaks at
0.557 g (4,000–4,500). Agrees with the evening drive within 0.02 everywhere they
overlap. This is the NA breathing curve the supercharger will be compared with.
