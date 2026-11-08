# Fuelling proposals after the 2026-11-08 drive

Desk analysis on the laptop. Calibration ZS44A-20261004, new pump (fitted
between 10-10 and 10-26). Sessions: leg 1 `_capture-ltft-gate-2026-11-08T182054`
(town), leg 2 `_capture-ltft-gate-2026-11-08T191721` (town and highway; the
first 50 min, pulled mid-drive). **Nothing here writes a ROM.** Every flash is
the operator's, in UpRev, with the engine off and an explicit per-flash
decision. Scripts: `~/canary-data/analysis-scripts/2026-11-08-drive/`
(`decompose.py`, `trim_map.py`).

## Answer

1. **Find the low-load lean before tuning it out.** With purge off, warm idle
   runs +7.0 / +3.8 % total trim (B1 / B2) on leg 1 and +9.4 / +6.1 % on
   leg 2. The lean shrinks with airflow about as 1/MAF, which is the shape of
   unmetered air. It is new: post-pump idle on 10-26 and 10-27 ran −3.9 to
   +1.6 %. A fuel-table edit would hide an air leak, and once the leak is fixed
   the edit would be wrong. The FSM's lean order (EC-181) is: exhaust leak
   ahead of the A/F sensor, intake leak after the MAF (boots, PCV), A/F sensor,
   fuel pressure.
2. **Bank 1 runs about 3 % leaner than bank 2, roughly evenly across load.**
   On every drive from 08-03 to 10-27 the banks agreed within 1 % (one drive
   −3.1 %). Today the gap is +3.2 / +3.3 % at idle on both legs, and +1.5 to
   +3.6 % across 0–60 g/s with purge on. A constant percentage across load is
   a bank-specific scaling error, meaning a bank-1 injector or the bank-1 A/F
   sensor. A bank-1 leak would shrink with load. The data cannot tell injector
   from sensor, because closed loop only knows the sensor. **Swapping the two
   A/F sensors settles it.** If the gap moves to bank 2, it was the sensor.
3. **Mid-load closed loop is fine. Leave it.** At 25–60 g/s, total trim is
   −1.0 to +1.3 % with purge on (n 4–14 per band), and LTFT is learning again
   (`2026-11-08-ltft-learning.md`). A static edit there would chase the
   drive-to-drive offset the learner is built to absorb.
4. **Leave WOT alone for now.** Neither leg has a pull: leg 2 peaks at 96 g/s
   for one read, and leg 1 has no high-load moment longer than 1.2 s. On
   10-26/27, open loop ran 2.0–2.7 % richer than target (median). With KS1 in
   fallback (−5° target, P0328), rich is the safe side. Revisit after the KS1
   harness repair and a 3rd-gear pull with rail pressure logged (NA1/NA2).
5. **Rail pressure drifts, and the ECM cannot see it.** Today it fell steadily
   from 379 kPa (55.0 psi) at 18:05 to 348 kPa (50.5 psi) at 20:05, with the
   sensor volts tracking it. The leg medians were 52.5 psi on leg 1 and
   50.8 psi on leg 2. On 10-27 it read 53.7–55.7 psi. The ECM assumes a fixed
   350 kPa (`0x8A89`). Across that range injector flow changes by up to about
   ±2 % (√ of injector ΔP at idle vacuum). That is a slice of the within- and
   between-drive offset that no ROM table can remove. Whether it is the
   regulator or pump as fuel heats, or thermal drift in the sensor, needs a
   mechanical gauge on the rail's Schrader valve, read cold and after a long
   drive. Any before/after comparison has to normalise trims by the measured
   pressure, sample by sample.

## The evidence

Warm (ECT ≥ 80 °C), closed loop, steady (MAF within 15 % of 2 s earlier).
STFT/LTFT reads are paired to RPM/MAF within 0.25 s, and to purge (DID
0x1110) within 0.6 s. Total trim TT = (1+STFT)(1+LTFT)−1, median.

| Band | Leg 1 B1 / B2, purge off (n) | Leg 1, purge on | Leg 2 B1 / B2, purge off (n) | Leg 2, purge on |
|---|---|---|---|---|
| Warm idle (< 1,000 rpm, < 7 g/s) | +7.0 / +3.8 (1,027) | +5.5 / +2.7 (49) | +9.4 / +6.1 (1,110) | +5.4 / +0.8 (42) |
| Off idle, 0–8 g/s | +7.2 / +3.9 (173) | +2.3 / +0.8 | +6.9 / +3.5 (243) | +3.1 / 0.0 |
| 8–15 g/s | +6.8 / +3.1 (37) | 0.0 / −1.7 | +5.4 / +2.9 (44) | +0.7 / −1.7 |
| 15–25 g/s | n 3–4 | +0.5 / −2.3 | +6.7 / +0.8 (7) | +1.2 / −2.4 |
| 25–40 g/s | n 1 | +0.2 / −0.6 | +2.7 / −0.9 (7) | +0.7 / −1.0 |
| 40–60 g/s | n 1 | +1.3 / −0.5 | none | +1.3 / −0.1 |

- **Purge is not the idle lean.** Idle purge is off on 95 % of reads, and the
  lean is there with it off. With purge on, light cruise reads 5–7 points
  richer: vapour plus the ECM's α cut (`docs/ecu-firmware-analysis.md`, EVAP
  purge factor). That is why the learned light-cruise cells sit at −5 to
  −6 %. They were learned while purging, so with a clean canister or purge
  off, light cruise needs about +12 points of STFT.
- **Battery voltage is not it either.** At idle Vb sits at 12.96–13.36 V.
  Within each idle stop, TT moves +1.1 %/V on B1 (95 % CI −2.3..+3.9) and
  +2.2 %/V on B2 (+0.8..+3.0). Leg 1 had too little voltage spread to test
  dead time. NA4's electrical-load steps are still the right test.
- **Leak versus dead time.** Leg 2's purge-off bank-mean is +7.75 % at idle
  (3.4 g/s, Ti 2.29 ms) and +0.9 % at 25–40 g/s. That 8.6× ratio matches the
  airflow ratio (8.7×), not the pulse ratio (~2.7×). A dead-time error would
  shrink with pulse width, so the shape points to unmetered air. This rests on
  only 7 purge-off reads at 25–40 g/s; purge is almost always on at cruise.
- **The lean grows through a drive.** Leg 1's idle stops, bank 1: +3.9
  (18:21), +0.8 (18:32), +4.7 (18:36), then +7.0 to +9.3 from 18:48 to 19:09.
  Coolant was flat at 91–92 °C, target φ 1.000 and λ 0.992–0.997. Rail
  pressure fell from 53.2 to 52.3 psi, which accounts for about 0.9 points.
  The rest is unexplained. Hot fuel in the rail at idle is a candidate, and
  IAT is the only heat proxy logged (41 → 50 °C).
- **Fuel Comp cannot be localised at idle.** The N02 sampler places 942 of
  leg 1's 958 eligible closed-loop reads ambiguously, spanning rpm rows 0–3 and
  load columns 0–4, because the idle-air and purge-area inputs are uncertain.
  Any idle edit has to be a block.

## If you want a flash to try anyway

**F1. Fuel Comp +3 % (+4 counts) over rows 0–4 × columns 0–5.** This is
N01.1's design run in the enrichment direction.

- **Where.** Rows 0–4 are 400–2,000 rpm. Columns 0–5 are load bytes 16–96,
  the alpha-N fill. In UpRev the labels are swapped (`rom_viewer.yaml`
  `editor_disagreement`): pick cells by position. UpRev's "VFR" columns 0–4
  are the rpm rows. Its "RPM" rows 0–5 are the load columns. Current values
  are 125–133. Add 4 to each.
- **Controls, which must not move:** rpm ≥ 2,400 (rows ≥ 5), load byte ≥ 112
  (columns ≥ 6) and the WOT columns.
- **What else moves.** Fuel Comp multiplies into BFS (`BFS ≈ 4·Tp·FC/128`), so
  the timing-trim and Target AFR lookups shift +3 % along their load axes in
  those cells. A timing change of under 1° is expected. Measure it.
- **Frozen predictions,** scored on the next warm drive. Each reading is
  pressure-normalised: trim + 100·(√((P+7.3)/(50.8+7.3)) − 1), with P in psi.
  - Warm-idle purge-off STFT falls 3.1 ± 1.5 points on both banks, against
    today's legs normalised the same way.
  - Purge-off 0–15 g/s moves the same amount.
  - Control bands (≥ 25 g/s) move ≤ ±1.5 points.
  - The B1 − B2 gap is unchanged (the table is bank-common).
  - **Falsifier:** idle STFT unchanged (within ±1.5), meaning Fuel Comp is not
    on the idle path, or a move of more than 2× the edit.
- **Order matters.** Do F1 or the leak/sensor checks, not both before one
  drive. If a leak is found and fixed, F1 becomes wrong and should be flashed
  back. The leak check is the better first move.
- Record the flash in `tune_revisions.yaml`, with SHA-256 and the cell list,
  before driving (`project_recording_a_flash`).

## Not proposed, and why

- **0x9254 (injector-flow scaler) or K, globally.** Today's error is
  load-shaped, so a global factor would leave a ±3 % residual at the ends.
  N02.7's held-out test of a global factor made one drive worse (4.48 % vs
  2.90 % RMS). K also moves every load axis.
- **Dead time (0x8C41 / 0x84F2).** Today's data cannot separate it from a
  leak, and the shape favours the leak. NA4's load steps separate them.
- **Per-cylinder trim (`D605[c]`, 128 for all six) to close the bank gap.**
  The sensor or injector question comes first. If the cause is a sensor bias,
  enriching bank 1 would make it actually rich in open loop.
- **Lowering the purge-gate threshold (cal 0x9048) or the idle-learn purge
  compare (0x86D4) so the idle cell learns.** The ECM would then learn
  vapour into its idle cell, as it already has into the light-cruise cells.
  The constants are also unverified on the bench.

## What the next drive needs

- After any mechanical fix, or after F1: the same `ltft-gate` capture, warm,
  with long idles and steady light cruise. Score the predictions above.
- **Purge-off cruise samples are rare** (7–44 per band). Long steady cruise
  produces some.
- **NA4 load steps at warm idle:** headlights, blower on high, rear defrost,
  30 s on and 30 s off, three times. That tests dead time against leak
  directly.
- **NA1/NA2:** one 3rd-gear pull from ≤ 2,500 rpm with the pressure ADC
  connected, but only after the KS1 harness repair.
