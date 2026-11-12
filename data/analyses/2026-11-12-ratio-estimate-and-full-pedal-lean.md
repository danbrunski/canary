# 2026-11-12: the ECM's gear-ratio estimate on the 4.08 axle, and full-pedal lean by gear

Desk analysis, overnight 2026-11-12, from the captures already on the laptop. No
vehicle contact. Scripts in `~/canary-data/analysis-scripts/2026-11-12/`. Every
polled row is placed on its wire-arrival time (`metadata_json.acquired_at`): in a
round-stamped `tuning` capture the stored `timestamp` of a RAM read is the round's
start, ~164 ms early (see the "Clock" note in
`2026-11-11-first-gear-cap-off-prediction.md`).

## 1. What `0xFFFF27D2` should read in each gear (frozen before any live read)

The firmware's ratio estimate is `0xFFFF27D2` = rpm × 1024 ÷ `w1180`, with
`w1180` = ECM speed word `w10C2` × 32768 ÷ cal `0x91F8` (25,491)
(`docs/ecu-firmware-analysis.md`, "What limits first gear"). If `w10C2` is the
ECM speed in 0.05 km/h (inferred), `w1180` = 25.709 × ECM speed in km/h.

**Inputs, measured** (`ratio_estimate.py`):

- ECM speed per 0x284 wheel-word count: PID 0x0D ÷ word, per pair, on two
  2026-10-27 default drives (1,957 pairs, PID ≥ 20 km/h): **0.005865 km/h per
  count** (p10–p90 0.00572–0.00597, the PID's 1 km/h resolution).
- Steady-gear frames on three tuning captures (2026-11-11 to 11-12): 0x23D rpm
  over the 0x284 word, stable within 1 % over 1 s, 1,500–4,500 rpm. Five clusters,
  one per gear. First gear is rarely held steady and is not among them; its
  value follows from the full-pedal frames (rpm/word 0.74–0.77).

| Gear | rpm / word | Frames | Predicted `0xFFFF27D2` (p10–p90) | As a ratio | Implied true ratio |
|---|---|---|---|---|---|
| 6th | 0.1325 | 16,961 | **893** (890–896) | 0.872 | 0.776 |
| 5th | 0.1725 | 24,114 | **1,158** (1,153–1,163) | 1.131 | 1.006 |
| 4th | 0.2175 | 3,491 | **1,484** (1,478–1,494) | 1.450 | 1.290 |
| 3rd | 0.2975 | 6,996 | **2,017** (2,003–2,032) | 1.969 | 1.753 |
| 2nd | 0.4275 | 1,870 | **2,907** (2,889–2,943) | 2.839 | 2.527 |
| 1st | ~0.76 | (pulls) | ~5,100 → **4,696** (clamped, cal `0x924A`) | 4.59 | ~4.4 |

"Implied true ratio" = rpm ÷ (wheel rpm × 4.08), with road speed = ECM speed ÷
0.92 (the ECM reads 0.92 × GPS, an earlier finding) and the 2.559 m rolling
circumference measured from GPS on 2026-10-04. Fifth comes out at 1.006, which is
what a direct-drive fifth gear should give, so the chain (4.08 axle, 2.559 m, the
0.92 speed bias) hangs together. The ratios relative to fifth need none of those
assumptions: 0.771 / 1 / 1.282 / 1.743 / 2.512 / ~4.4.

**Prediction for the live read:** in steady fifth gear `0xFFFF27D2` reads
**1,158 ± 10**. (Stock axle and stock tire would read about 1,024.) A reading
far from it (e.g. ~580 or ~2,300) means the `w10C2` unit is not 0.05 km/h. Read it
with service 0x23 at `0xFFFF27D2`, 2 bytes, in steady fifth on a level road; one
read in second gear (expect ~2,907) checks the scale.

**What the error is, and why it matters which way 0x91F8 is corrected.** The
estimate is engine rpm per wheel speed, normalized by a constant that bakes in
the stock 3.692 axle and stock tire. With the 4.08 axle it reads the
*transmission* ratio 12.3 % high (1.1235 × true). But engine rpm per wheel speed
(the *overall* ratio, ratio × 3.692 against the ECM's assumed tire) reads only
about 1.6 % high, because the regear is physically in the rpm-to-wheel relation
it measures. So:

- A consumer that uses the estimate as a **gear identifier** (thresholds such as
  the first-gear 3.50, gear-indexed maps) wants the true transmission ratio:
  0x91F8 → 25,491 ÷ 1.1235 ≈ **22,690**, sized properly from the live read as
  25,491 × 1,024 × r5 ÷ reading (r5 = 1.000 for a direct fifth).
- A consumer that uses it to **convert between engine and wheel quantities**
  (rpm per wheel speed, or engine torque per wheel force with an assumed axle)
  is already within ~1.6 % today and would be made 10.5 % wrong by that
  correction.

Which consumers exist is the firmware question in section 4. Gear
identification already works on the current word: second reads 2.84, first
clamps at 4.59, either side of 3.50.

## 2. Full-pedal lean of target grows with acceleration, gear by gear

`lean_vs_rate.py`. Per PID 0x24 read with the broadcast pedal at 255 for ≥ 0.45 s
and coolant ≥ 80 °C, error = (λ × 14.7) ÷ (1881.6 ÷ RAM target raw) − 1, with
the RAM target nearest on the wire-arrival clock. Three captures on the 20261111
fuel tables (`_capture-tuning-2026-11-11T174602`, `-11-12T010305`,
`-11-12T034249`); 87 reads. Gear from rpm ÷ 0x284 word (section 1's clusters).

| Gear | Reads | rpm rise, median | Error, median (p10 / p90) | rpm range |
|---|---|---|---|---|
| 5th | 23 | 52 rpm/s | −1.7 % (−3.3 / −0.7) | 2,569–2,934 |
| 4th | 3 | 94 rpm/s | +0.7 % | 3,550–3,678 |
| 3rd | 23 | 229 rpm/s | +0.8 % (−0.3 / +1.7) | 4,159–5,644 |
| 2nd | 17 | 510 rpm/s | +1.5 % (−0.3 / +3.9) | 2,919–5,550 |
| 1st | 21 | 1,250 rpm/s | +2.8 % (−1.9 / +5.4) | 1,709–5,550 |

(Positive = leaner than the target.) Taking the target 0.3 s earlier, to allow for
sensor and transport delay, leaves every median within 0.1 point: the target is
flat inside a pull, so delay in the sensor does not make this.

**Reading.** The error rises monotonically with how fast airflow is rising: about
−1.7 % when rpm creeps (5th, lugging at 2,600–2,900 rpm) to about +2.8 % in first,
where MAF climbs ~30 g/s every second. The cap-off flash did not cause it; it
made first-gear full pedal reach the last column, where it shows. AFR 12.5–12.9
against 12.3 at full load is not a lean condition, but it is the reason first
gear sits outside the 2.5 % floor of the frozen prediction.

**What cannot be separated on these data.** Gear and acceleration move together:
inside a gear the rise rate barely varies (second: Spearman −0.2 over 17 reads),
so "something about first gear" and "transient fuelling lags rising airflow"
predict the same table. Candidates for the second: the MAF's own lag against a
current rpm (air per stroke under-read while flow rises), the ECM's BFS filter,
and port-wall film. A 50 ms effective lag at first gear's ~30 g/s/s is about
1.5 % of ~100 g/s, the right size.

**The separating measurement (owed):** the ECM's base fuel schedule
(DID 0x1208, or its RAM word) against MAF ÷ rpm at the same instant during a
held full-pedal pull, by gear at matched rpm. The `tuning` profile reads DID
0x1208 at 0 of its full-pedal reads (its Mode 22 window never lands inside a
pull), so this needs a window that does. If BFS ÷ (MAF ÷ rpm) is lower in first
than in third at matched rpm, the ECM side lags; if it matches, the shortfall is
downstream (film or the sensor). A second, cheaper check: full pedal in second
gear up a steep grade against the same on the flat, which changes the rise rate
inside one gear.

## 3. Timing and knock in first-gear full-load pulls

`knock_by_gear.py`, same three captures and selection (pedal 255 ≥ 0.45 s,
coolant ≥ 80 °C, RAM reads on the wire-arrival clock), timing paired with the
broadcast rpm:

| rpm | 1st: n, median (range) | 2nd | 3rd |
|---|---|---|---|
| 3,600–4,200 | 3, 8° (7–10) | 2, 7° (6–8) | — |
| 4,200–4,800 | 5, 11° (9–13) | 6, 9.5° (9–11) | 11, 10° (9–12) |
| 4,800–5,400 | 4, 13° (10–15) | 4, 12.5° (11–13) | 7, 13° (11–13) |
| 5,400–6,200 | 1, 17° | 2, 17.5° (17–18) | 4, 19.5° (17–21) |

First gear gets the same full-load timing as second and third at matched rpm,
within about a degree. Knock retard and its target read −5° on all 82 reads (the
fixed fallback), `ECM_HIGH_DET_FLAGS` 0x40 on all 83 (High Det trim latched),
KS1 255 and KS2 127 on all 22 reads, and the KS fault flag 0x80. So the ECM has no
knock information in any gear: first-gear full load runs on the same fixed −5°
margin as the others, and nothing in these data could show knock if it occurred.
That is the state the KS1 harness repair changes.

## 4. Firmware: what else 0x91F8 moves

Traced read-only on 2026-11-12 (`docs/ecu-firmware-analysis.md`, "What else the
output-shaft constant `0x91F8` moves"). Three functions read the word and a fourth
multiplies by it. The consumers, by section 1's two kinds:

- **Gear identifier: one, and it is switched off.** The first-gear cap's test
  (estimate ≥ cal `0x9308`) is the only consumer that wants the true gearbox
  ratio. `0x9308` has been 32,767 since ZS44A-20261111-2, so the test never
  passes. Even at the stock 3,584 it separates first from second on the current
  word (first clamps at 4.59, second reads 2.84).
- **Engine-to-wheel conversion: one, cruise torque.** `0x0DAA62` turns the cruise
  drive-force command into an engine torque as force × cal `0x934E` × 2,048 ÷ the
  ratio latched at engagement. That is the second kind in section 1. The current
  word makes the estimate read overall reduction ÷ 3.692, which is right for the
  4.08 axle, provided `0x934E` carries the stock axle (inferred). Correcting
  `0x91F8` alone would make cruise ask for about 10 % more torque per unit of
  force. The cruise loop closes on speed, so that is not unsafe, only a change
  with nothing gained. Keeping cruise neutral would take a matching `0x934E` edit.
- **Nothing else sees it.** Vehicle speed, the speed limiter, the cruise
  gear-change cancel and the expected-rpm limit all cancel the word. On this
  calibration, pedal goes to torque through the torque map with no ratio: the
  Target Drive Force stage needs cal `0x8ADE` bit 7 clear, and here it is set.
  Idle, the full-load path and diagnostics do not read it, and no rev-matching
  code reads the ratio.

**Verdict: leave `0x91F8` as it is.** The one consumer that would want it
corrected is disabled, and the one live consumer is already right for the 4.08
axle because the high reading absorbs the regear. If the first-gear cap is ever
re-enabled, place its threshold in the word's own units (the "As a ratio" column
of section 1) rather than correcting the word.

Still worth reading live, now as checks rather than inputs to an edit:
`0xFFFF27D2` in steady fifth (expect 1,158 ± 10; it tests the speed word's unit),
and `0xFFFF2CA2`, `0xFFFF3B4A`, `0xFFFF475A` during cruise (they test the cruise
reading).

**A third cap trigger, found on the way.** When bit 3 of `0xFFFF4856` is set, the
same function applies the 3,138 cap in every gear. Its inputs are counters kept by
the CAN receive layer, so it reads as a limp cap on a lost message (inferred). It
is not set in normal driving: second to fifth gear at full pedal reach throttle
255, which the cap would prevent. If the truck is ever down on power in every gear
with no codes, read `0xFFFF4856` first. The other trigger, `0xFFFF2208`, cannot
fire here: its only writers are the automatic's message handlers, which return at
once on this calibration, and start-up zeroes it.
