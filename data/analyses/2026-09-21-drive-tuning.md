# 2026-09-21 evening drive — first `tuning` capture

Session `_capture-tuning-2026-09-21T223734`, 17.6 min, daemon `2c709b5`, calibration
`ZS44A-20260920` (manifest). Cold start (coolant 47 → 92 °C), baro 80–81 kPa and
IAT 25–35 °C **measured** this time. Pulled to the laptop by tar stream; the Pi
also logged `/status` every 20 s (`poll-tuning-20260921.jsonl`, pulled beside it).

## Capture health

Every profile channel at its target rate: Mode 01 4 Hz, trims 2 Hz, baro/IAT/
coolant 1 Hz, 8 DIDs 2 Hz, 4 RAM channels 1 Hz. 0 dropped records on every sink.
Quality verdict **fail**, on three minor counts: PID 0x0E backed off for the first
76 s (3 timeouts during cranking, then 100 %), two ~3 s gaps each on `ECM_RPM` and
bank-2 A/F volts (the D03.18 stall shape), and 67 MCP2515 overflow frames on the
broadcast bus. Coverage 99.4 %; the analysis below is unaffected.

## Known answers first

| pair | n | result |
|---|---|---|
| RAM timing vs PID 0x0E | 892 | median Δ 0.0°, 84 % within 1° |
| RAM target φ vs PID 0x44 | 822 | ratio median 1.004 (p5 0.983, p95 1.148) |
| RAM engine speed vs PID rpm | 960 | ratio median 1.0000 |

## WOT fuelling: ~3 % lean on three instruments

27 load ≥ 85 % events, all 2–6 s tip-ins again (peak 4,888 rpm); no steady gear
pull. `delivery_error` (λ, PID 0x24) gives +2.2 % pooled on a 1 s grid and
+3.0 % on 250 ms. That grid dependence looked like sensor lag, so the error was
recomputed per sample, dropping the first *t* seconds of each enrichment event:

| settle after onset | n | events | median error |
|---|---|---|---|
| 0 s | 120 | 12 | +3.08 % |
| 0.5 s | 92 | 10 | +2.87 % |
| 1.0 s | 72 | 10 | +2.84 % |
| 1.5 s | 57 | 8 | +2.86 % |

It does not shrink with settle time, so it is not transport lag. Per event
(settled ≥ 1 s): +2.2 to +4.0 %, 8 events. The A/F sensor voltage path, settled
≥ 1 s against the same commanded φ: **bank 1 +4.1 %** (n 23, 7 events), **bank 2
+3.3 %** (n 17). Three instruments, both banks, same sign and size — and the
same +3.0 % August measured and attributed to the 48-vs-51 psi supply offset.

## Closed loop: trims remove fuel

Warm (coolant ≥ 80 °C) closed loop, 1,228 samples: STFT median **−2.3 %** on both
banks, and −4 to −7 % at 2,000–5,000 rpm with 8–40 g/s MAF, returning to ≈ 0 above
40 g/s. Idle median −2.3 % (mean +1, σ 8 — normal oscillation).

**LTFT is exactly 0.00 % on both banks for the whole drive.** Not a small number:
a constant. The 2026-09-20 flash reset the learned table; it was active again by
2026-09-18 after the June disconnect, which took weeks. STFT is carrying all
correction until it relearns.

## What the two findings together say

A fuel-supply shortfall makes the engine lean **everywhere** — positive trims at
part throttle and lean at WOT. This drive has negative trims at part throttle and
lean at WOT, so a supply offset alone does not explain it. The pattern that does
fit is an error that depends on airflow: rich-correcting at 8–40 g/s, neutral near
40 g/s, lean at 40–130 g/s under WOT. That is the shape of a MAF transfer curve
that reads high at mid flow and low at high flow — or of fuel delivery that sags
only at high flow (pressure falling under WOT demand) layered on a base that is a
little rich. The data cannot separate those two.

**Do not raise K to fix the WOT lean.** K scales every cell; it would push the
part-throttle cells — already being pulled 4–7 % — further rich.

The measurement that separates them: **fuel pressure logged through a pull.**
Steady pressure at WOT points at the MAF curve (fix the MAF scaling at high
flow); pressure that drops under WOT points at delivery (pump, regulator, filter).
A steady WOT gear pull is still owed — every event so far is a tip-in.

## Timing and airflow

WOT timing (PID 0x0E): medians 8–12° BTDC from 1,500 to 5,000 rpm, same as this
morning, against 24.6° at 3,000–3,500 on the stock calibration. Low outliers
(−1° at 2,500–3,000, 3° elsewhere) are tip-ins.

VE with measured baro and IAT: 0.78 (1,500–2,000) → 0.81 → 0.87 → 0.88 → 0.90 →
**0.91 (4,000–4,500)** → 0.88 (4,500–5,000, n 5). Healthy, smooth, no restriction.
Air per cylinder-event peaks at 0.57 g.
