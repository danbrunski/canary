# ZS44A-Tuned-20261111: full-pedal targets to AFR 12.3 — edit, evidence, frozen predictions

Written 2026-11-11 ~17:40Z, **before the flash**, at the operator's request ("do
we want to try a flash with leaner AFR targets": yes, targets only, one modest
step). The file is `Tuned ROMs/ZS44A-Tuned-20261111.OsirisROM`, built by CANary
from `ZS44A-Tuned-20261110.OsirisROM` (the running tune). The operator reviews
it in UpRev and flashes it. Nothing here wrote to the ECU.

- Container sha256 `c919ff9612304e5f7c1a93a7ca2a28b40f1b0cfb0b54007efab358a40a22f386`
- Image sha256 `2b42c6effb795edff69d49284f5a86d78de9340b9d1e7256fc4676f82d9ab412`
- Checksum words: sum `0x0C0604CC`, xor `0x69CB4D4C` (parent `0x0C0604D4`, `0x69CB4D48`)
- Build script, review file, baseline script:
  `~/canary-data/analysis-scripts/2026-11-11/build_rom.py`,
  `~/canary-data/rom-candidates/2026-11-11/review.json`,
  `~/canary-data/analysis-scripts/2026-11-11/baseline.py`
- Checks: the same build path reproduces UpRev's own `ZS44A-Tuned-20260920.OsirisROM`
  byte for byte from k106 (a Fuel Targets edit, the same table); checksum verify;
  container round trip; an independent re-read of the copied file differs from
  20261110 at four image bytes only (the two cells, the low byte of each checksum
  word); Fuel Targets in slots 2–5 unchanged.

## What changed

**Fuel Targets, Map 1, two cells of the last column** (the column the ECM reads at
full pedal at every rpm). Raw is φ × 128; AFR = 1881.6 / raw.

| Row | rpm | Old | New | Fuel at full pedal |
|---|---|---|---|---|
| 6 | 4,400 | 155 (AFR 12.14, φ 1.211) | 153 (AFR 12.30, φ 1.195) | −1.3 % |
| 7 | 5,200 and above | 159 (AFR 11.83, φ 1.242) | 153 (AFR 12.30, φ 1.195) | −3.8 % |

The full-pedal column now reads 12.89 / 12.63 / 12.63 / 12.63 / 12.63 / 12.30 /
12.30 / 12.30 AFR at 1,200 … 5,200 rpm. In UpRev: the right-most column of Fuel
Targets, the 4,400 and 5,200 rpm rows.

Nothing else: Fuel Compensation, K, timing, the axes and Maps 2–5 are
byte-identical to 20261110.

## Why

- **Operator decision (2026-11-10/11):** home is 79–82 kPa and the truck runs 89/91
  octane in preparation for the supercharger; 11.8 at the top is rich of best power
  (roughly 12.5–13.0 for an NA engine). The expected gain is small, a few percent of
  power at most. The value is learning the response before the build.
- **Delivery is already on target** above 4,400 rpm (`2026-11-10-fuel-comp-wot-cut-prediction.md`,
  scored: +1.2 / +0.6 / +0.4 % at 4,400–4,800 / 4,800–5,200 / 5,200–5,600, negative =
  rich). A target edit therefore carries through to the exhaust.
- **Why one step, and only to 12.3.** The 20261110 write-up deferred leaning the
  targets to the KS1 harness repair. It is still pegged: knock control is in its
  fallback (−5° retard target), and HI DET is latched, so timing comes from trim B.
  Timing that held back is what makes 12.3 reasonable without knock feedback.
  Going past 12.3 waits for the repair.

## What was not changed, and why

- **Fuel Compensation: no edit.** It corrects delivery to the commanded target,
  and delivery is within about 1 % of it. Leaning it as well would lean the
  mixture twice and make two edits in one flash, which can't be attributed.
- **Full pedal at 4,000 rpm and below: no edit.** The 4,000 row is already 12.3,
  and 12.6–12.9 at 3,600 and below is already lean of 12.5.
- **The part-pedal cells of the 4,400 and 5,200 rows (columns 0–6): no edit.** At
  5,200 rpm columns 2–4 hold 157 / 160 / 163 (AFR 12.0 / 11.8 / 11.5). Below the
  pedal switch, high load near the top therefore stays richer than full pedal
  does: the safe side.

## What else the edit moves

- **Nothing on the load axis.** Fuel Targets does not feed the base fuel schedule
  (BFS), so BFS, the timing trims' and Fuel Comp's axis, does not move.
- **The enrichment gate still latches.** 153 is above the gate threshold (130,
  cal 0x8B39), so full pedal still enriches. The multiplier `0xFFFF26C0` ramps to
  the new `0xFFFF174B << 7`.
- **The enrichment retard `0xFFFF174A` reads `0xFFFF174B`.** It is subtracted in
  the base timing sum, so a leaner target could change timing through it. It read
  0 on every one of 4,038 reads on 2026-11-11 (KOEO and unloaded full-pedal stabs,
  lookup byte 146–149), with its cap at 0–3 counts. No loaded read exists before
  this flash unless a pull on 20261110 lands first. If it is 0 under load at the
  old target, it is at its floor and leaning can only leave it there or add retard.
  The timing prediction below is the check either way.
- **HI DET fuel scaling:** the curve 0xD247 is 128 (×1.0) in all cells, so the
  latch does not change the target.

## Predictions (frozen before the flash)

Scored on the next warm drive with full-pedal pulls in 2nd or 3rd gear. Full pedal
is ENGINE_LOAD ≥ 85 %, rpm ≥ 2,500 and the paired RAM target ≥ 1.10 (the ECM
enriching), as in `baseline.py`. The baseline is `_capture-tuning-2026-11-10T235310`.

| rpm band | RAM target before (median) | Predicted | Timing before (median, n) |
|---|---|---|---|
| 2,500–3,500 (control) | 1.164 | 1.164, unchanged | 9° (17) |
| 4,000–4,400 | 1.203 | 1.195 | 10° (6) |
| 4,400–4,800 | 1.219 | 1.195 | 12° (18) |
| 4,800–5,200 | 1.234 | 1.195 | 13° (20) |
| 5,200–5,600 | 1.238 | 1.195 | 18.5° (12) |

- **Commanded:** `mem:ECM_TARGET_EQUIV_RATIO` at full pedal reads no more than
  1.195 above 4,400 rpm (ramps aside), and `mem:ECM_TARGET_LOOKUP_GATED` reads 153
  from 4,000 rpm up. Control bands unchanged.
- **Delivery error against the RAM target** (same sampler and gates as the
  20261110 scoring, bank 1) stays within ±2.5 points of the scored values: +1.2 /
  +0.6 / +0.4 % at 4,400–4,800 / 4,800–5,200 / 5,200–5,600. The error belongs to
  delivery, not to the target.
- **Measured AFR at the top** (PID 0x24, baro gain applied): about 12.35, between
  12.0 and 12.65; it was 11.9. UpRev's AFR display skips the barometric gain and
  reads 1.5–4.5 % lean at altitude, so expect about 12.5–12.9 there.
- **Mechanism check, independent of the A/F sensor:** at full pedal, the injected
  pulse minus dead time per unit air ((DID 0x1206 − Ts(PID 0x42)) × rpm / MAF)
  falls by about 1.9 % at 4,400–4,800, 3.2 % at 4,800–5,200 and 3.8 % above 5,200
  rpm, each ±1.5 points; BFS · rpm / MAF does not move (±1.5).
- **Timing:** median full-pedal timing per band within ±1° of the table above,
  with HI DET still latched and the knock fallback still active.
- **Enrichment retard `0xFFFF174A`:** 0 at full pedal.
- **Idle and cruise trims do not move because of this flash.** The flash resets
  long-term trim to 0.00 and the readiness monitors again, so the key-on MIL blink
  returns until they complete.

**Falsifiers:**

- A full-pedal RAM target above 1.20 above 4,400 rpm: the ECM is not reading the
  edited cells (wrong slot, or the flash did not take).
- Median full-pedal timing up by more than 1° in any band from 4,400 rpm with HI
  DET still latched, or `0xFFFF174A` reading 0 now where a pull on 20261110 showed
  it above 0: leaning removed retard. Flash 20261110 back.
- Any band measuring leaner than +2.5 % of the new target: flash 20261110 back.

## Scored 2026-11-11 (first drive after the flash)

Flashed 2026-11-11T17:44:34Z, verified from the flash capture
(`docs/skunkworks/uprev-flash-procedure.md`). Capture
`_capture-tuning-2026-11-11T174602` (17:46–18:09Z, 82–83 kPa, IAT 18–20 °C), ten
full-pedal pulls above 4,000 rpm, the first four at 73–85 °C coolant. Scripts in
`~/canary-data/analysis-scripts/2026-11-11/` (`score.py`, `ctx.py`, `mech.py`,
`fc-samples/`). Baseline throughout: `_capture-tuning-2026-11-10T235310`.

**Verdict: every prediction held on held pulls; keep the tune. One thing the
prediction did not know: in first-gear pulls the ECM does not read the edited
column at all.**

| Prediction | Result |
|---|---|
| RAM target ≤ 1.195 above 4,400 rpm; lookup byte 153 from 4,000 | **Held on held pulls.** Of 37 full-pedal reads with rpm rising < 700 rpm/s, 34 read raw 153 (φ 1.195), from 4,125 to 5,712 rpm; the other three are lift-offs (timing already at 39°, or MAF at 49 g/s). On 20261110 the same pulls stepped 153 → 155 → 159. Control 2,500–3,500 rpm: 1.164 on 27 of 27. |
| Delivery error within ±2.5 of +1.2 / +0.6 / +0.4 % | **Held, thin.** Frozen gates: +1.6 % (n 2) at 4,400–4,800, none at 4,800–5,200, +1.6 % (n 1) at 5,200–5,600, −0.3 % (n 1) above. The gates dropped most reads for a fuel cut in the previous seconds or coolant under the warm limit. |
| Measured AFR at the top about 12.35 (12.0–12.65) | **Held.** Band medians on held pulls, same-instant reads: 12.38 / 12.48 / 12.36 / 12.39 at 4,000–4,400 / 4,400–4,800 / 4,800–5,200 / 5,200–5,600 (n 8 / 8 / 6 / 6) against 12.30 commanded. It was 12.27 / 12.00 / 11.88 above 4,400. |
| (pulse − Ts) · rpm / MAF falls 1.9 / 3.2 / 3.8 % (±1.5) | **Held:** −2.6 / −3.1 / −3.3 % (n 8 / 6 / 6 against 18 / 13 / 5). |
| BFS · rpm / MAF unchanged (±1.5) | **Held:** −0.3 / +0.9 / +0.5 %. |
| Timing within ±1° per band | **Held at matched rpm, not by band median.** Each read against the 20261110 timing-versus-rpm curve at its own rpm: median −0.5° on 25 warm reads (≥ 85 °C), −1.8° on 9 reads below 85 °C, nothing advanced. The band medians read 9 / 11 / 13 / 21° against 10 / 12 / 13 / 18.5°: the +2.5° at 5,200–5,600 is rpm mix inside a band where timing climbs 13° → 21°. A band median was the wrong statistic to freeze for timing. |
| Enrichment retard `0xFFFF174A` = 0 at full pedal | **Held:** 0 on all 76 full-pedal reads, the first loaded read of it. Its cap `0xFFFF174C` read 0–7 counts. Leaning cannot have removed retard that was never applied. |

No band measured leaner than +2.5 % of the target.

### The falsifier that tripped as written

"A full-pedal RAM target above 1.20 above 4,400 rpm" occurred on six reads, pedal
at 255. One is a lift-off (raw 159 with MAF at 49 g/s). The other five read raw 157,
160, 160, 159 and 154 (φ 1.203–1.250) at 4,650–5,390 rpm. The falsifier's stated
meaning, that the ECM is not reading the edited cells, is refuted by the 34
held-pull reads. Those five are in three pulls where rpm rose 1,070–1,330 rpm/s
(first gear) and calculated load read 82–90 %, not 91–95 %. The values are the
part-pedal cells of the 4,400 and 5,200 rows (155–163), which this flash left
alone, so there the ECM was reading the load-indexed cell with the pedal on the
floor.

20261110 could not show this: its last column held 155 / 159, the same numbers as
the neighbouring part-pedal cells. Its three fast pulls read 155 → 159, which fits
either source. So "at full pedal the ECM reads the last Fuel Targets column at
every rpm" is true of held pulls and untested, now contradicted, for a fast
first-gear pull. What selects the column there (a delay, a load condition, a gear
or speed term) was not known when this was written. It is answered below.

Consequence: a first-gear pull above about 4,600 rpm still gets AFR 11.8–12.0
commanded (measured 11.5–12.2 on those reads), not 12.3. That is the richer side.
Bringing first gear to 12.3 as well means editing the part-pedal cells of the
4,400 and 5,200 rows, a separate flash.

### Why first gear does not read the last column (firmware, same day)

The switch is not on the pedal. The ECM compares its own **target throttle
opening** with a flat threshold of 78 % (on PID 0x11's scale, where fully open
reads 88.6 %), with 3 % of hysteresis, and only above it replaces the load input
with the last column (`docs/ecu-firmware-analysis.md`, "The enrichment switch
tests target throttle"). In first gear the ECM does not open the throttle fully at
full pedal:

| Pull | Gear (rpm ÷ wheel) | rpm | Pedal | Throttle, PID 0x11 | Target raw |
|---|---|---|---|---|---|
| 233 s | 1st (0.73) | 3,425 → 4,950 | 255 | 41 → 59 % | 134, 149, 157, 160 |
| 375 s | 1st (0.76–0.82) | 4,775 → 5,100 | 255 | 53 → 62 % | 154, 160 |
| 470 s | 1st (0.72–0.76) | 3,588 → 5,513 | 255 | 43 → 73 % | 141, 153, 159, 149 |
| all others | 2nd (0.42–0.46), 3rd (0.30), 5th (0.17) | 2,525 → 5,638 | 255 | 88–89 % once the pull is established | last column: 149 below 3,500 rpm, 153 from 4,125 on the 34 held reads |

Each row pairs a RAM target read with the 0x23D frame within 0.1 s (pedal and
throttle bytes from one frame; the throttle byte reads 108–206 of 255 in the
first-gear rows and 253–255 in the others) and Mode 01 rpm and throttle within 0.3 s
(`~/canary-data/analysis-scripts/2026-11-11/throttle_switch.py`). The rpm rise
rate and the 82–90 % load that marked these pulls were symptoms of first gear at
part throttle, not causes.

This is not new with either November flash. Across every local session since
2026-05, at pedal ≥ 250, first gear holds the throttle byte at 98–130 of 255 from
2,000 to 4,500 rpm and reaches 255 only from about 5,600 rpm (8,878 frames, up to
21 sessions per band); second to fifth gear sit at 255 throughout (130,000 frames;
`throttle_by_gear.py` beside the script above).

The cause is a first-gear torque cap in the calibration. While the ECM's
estimate of the transmission ratio (rpm ÷ vehicle speed) is 3.50 or more, which
only first gear reaches, it limits its torque demand to a flat 3,138 (314 N·m if
the unit is N·m × 10) and opens the throttle only as far as that torque needs at
the current rpm. The engine's full-load torque in the ECM's own map crosses that
value at 1,705 rpm and again at 5,906 rpm, and the measured first-gear throttle
leaves 255 between 1,700 and 1,900 rpm and is back near it from 5,600. The
throttle even dips at 4,000 rpm, where the torque map peaks
(`docs/ecu-firmware-analysis.md`, "What limits first gear";
`first_gear_cap_check.py`). The cap word itself has not been read live.

So the last column is the full-throttle column for second gear and up. First-gear
full pedal is a part-throttle condition to the ECM from about 1,800 to 5,600 rpm,
and is fuelled from the load-indexed cells. Two ways to change that, both separate
flashes: lean the part-pedal cells of the 4,400 and 5,200 rpm rows, or raise the
cap (cal words `0x8EBC` and `0x9308`, a main-calibration edit that also gives
first gear more torque, which is presumably what the cap is there to prevent).

### What this drive did not cover

- Full pedal at 3,000–4,000 rpm (3 reads). The cells there were not edited.
- A fully warm set under the frozen delivery gates: four reads qualified. A drive
  with pulls made after 20 s of steady cruise, not straight out of a lift, would
  fill it.
- The step to 12.5 still waits for the KS1 harness repair: knock control stayed in
  its fallback (−5° target on every full-pedal read).
