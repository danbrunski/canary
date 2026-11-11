# ZS44A-Tuned-20261110: WOT Fuel Comp cut — edit, evidence, frozen predictions

Written 2026-11-10 ~17:45Z, after the morning's WOT pulls
(`_capture-tuning-2026-11-10T170119`, 17:01–17:28Z) and **before the flash**.
The file is `Tuned ROMs/ZS44A-Tuned-20261110.OsirisROM`, built by CANary from
`ZS44A-Tuned-20261004.OsirisROM`. The operator reviews it in UpRev and flashes
it. Nothing here wrote to the ECU.

- Container sha256 `28d0ff291e5812d30f6c2203bc68322aad53742c21ba3970c86e49826dc7560a`
- Image sha256 `dfe09508c9a3291794a31d4a24549b39798c19a32c67dccd1b513251855bf06a`
- Checksum words: sum `0x0C0604D4`, xor `0x69CB4D48`
- Build script and review file: `~/canary-data/analysis-scripts/2026-11-10-drive/build_rom.py`,
  `~/canary-data/rom-candidates/2026-11-10/review.json`

## What changed

**Fuel Compensation, Map 1, 35 cells: rows 9–15 (4,000–6,400 rpm) × columns
11–15 (load byte 192–255).** Nothing else: Fuel Targets, K, timing, the axes
and Maps 2–5 are byte-identical to 20261004. Only those cells and the two
checksum words differ in the image.

Raw counts (128 = 100 %). Columns 12–15 hold one value per row.

| Row | rpm | Col 11: old → new | Cols 12–15: old → new | Cut at WOT |
|---|---|---|---|---|
| 9 | 4,000 | 132 → 130 | 134 → 131 | −2.2 % |
| 10 | 4,400 | 133 → 130 | 134 → 130 | −3.0 % |
| 11 | 4,800 | 134 → 131 | 137 → 130 | −5.1 % |
| 12 | 5,200 | 134 → 131 | 137 → 130 | −5.1 % |
| 13 | 5,600 | 136 → 132 | 138 → 131 | −5.1 % |
| 14 | 6,000 | 137 → 133 | 139 → 132 | −5.0 % |
| 15 | 6,400 | 137 → 133 | 139 → 132 | −5.0 % |

In UpRev the cells are at the same positions (row 9–15 from the top, column
11–15 from the left, counting from 0), but UpRev's axis labels are swapped
(`rom_map.yaml` `editor_disagreement`). UpRev shows a cell as raw × 100 / 128:
the new cells read 101.56–103.91 %, where 20261004 reads 103.13–108.59 %.

## Why

1. **Measured today, full pedal.** The ECM asked for the last Fuel Targets
   column (φ 1.164 at 3,600 rpm rising to 1.242 above 5,200), and the engine
   ran richer than that above 4,000 rpm. Sampler: `tools/fuel_comp_samples.py`,
   open-loop route, 24 eligible reads with the load byte placed within 5
   counts; error is measured AFR (PID 0x24, bank 1) against the ECM's RAM
   target. Negative is rich.

   | rpm band | n | Error today | Fuel Comp there now |
   |---|---|---|---|
   | 3,000–4,000 | 8 | +2.0 % (0.0 to +4.1) | 132–134 |
   | 4,000–4,400 | 2 | −3.5 % | 134 |
   | 4,400–4,800 | 4 | −4.2 % (−7.0 to −2.7) | 135–136 |
   | 4,800–5,200 | 5 | −7.1 % (−7.5 to −6.8) | 137 |
   | 5,200–5,600 | 5 | −6.8 % (−7.5 to −5.7) | 137–138 |

   The repo's gated `delivery_error` (42 samples, PID 0x44 as the target) gives
   the same shape: −3.2 % at 4,000–4,500, −5.4 % at 4,500–5,000 and −7.0 %
   above 5,000 rpm. Measured AFR was 11.0 at the top against 11.8 commanded.
2. **History agrees.** The 2026-10-04 flash restored Fuel Comp to UpRev's
   base. At today's WOT points (load byte 207–216) that restore added 2.6 % at
   4,200 rpm rising to 5.6 % at 5,500 rpm. Before the restore, held-out WOT
   error was inside the 2.5 % floor. So about three quarters of today's rich
   error is the restore, and the rest fits the new pump.
3. **The cut is deliberately partial.** It takes back what the restore added
   and leaves the pump's share. Predicted residual is 1–2 % rich. The knock
   sensor is in fallback (KS1 pegged, −5° held, WOT timing 4–21°), and with
   timing that retarded a rich mixture is what protects exhaust temperature.

## What was not changed, and why

- **Fuel Targets: no edit.** The data says the engine missed its targets, not
  that the targets are wrong. Fixing delivery in the target table would hide
  the error and make the ECM's commanded φ stop meaning what it says. Leaning
  the WOT targets themselves (12.6 at mid rpm, 11.8 at the top) is a separate
  decision that should wait for the KS1 harness repair.
- **Idle and low load: no edit (F1 withdrawn).** Today's warm idle with purge
  off ran total trim 0.0 % on bank 1 and −1.6 % on bank 2 (n 640 / 671,
  `decompose.py`), against +7 to +10 % and +4 to +8 % on 2026-11-08. Today the
  intake air was 19–28 °C and the rail 53.1 psi. On 11-08 it was 41–65 °C and
  50–52.5 psi, and the lean grew through the afternoon. The idle lean follows
  heat, so it is not a fixed air leak, and a fixed +3 % in the table would run
  a cool engine 3 % rich. `2026-11-08-tuning-proposals.md` is corrected.
- **Mid-rpm WOT (rows 6–8): no edit.** +2 % lean of target at 3,000–4,000 rpm
  is inside the 2.5 % WOT noise floor on 8 reads.

## What else the edit moves

- Fuel Comp multiplies into the base fuel schedule (BFS), so BFS falls by the
  cut in these cells. BFS is the load axis of Fuel Targets and the timing trims.
  - **Fuel Targets:** at full pedal the ECM reads the last load column whatever
    BFS is, so the commanded φ does not move. Checked today: commanded φ
    1.242 at 5,500 rpm, where a BFS-indexed lookup would give 1.262.
  - **Timing trims:** flat within 1 count across BFS columns 96–128 at
    4,000–5,600 rpm, so a 5 % shift along that axis moves timing by under
    0.5°.
- The BFS ceiling (0xD8B3) is 255 at every rpm, so it does not clamp.

## Which copy the ECM reads (added ~18:10Z, before the flash; operator's question)

Two switches are easy to confuse here: UpRev's Map 1–5 slots, and the
Main / High Det pair.

- **HI DET was latched for the whole capture.** `mem:ECM_HIGH_DET_FLAGS` read
  0x40 on 3,283 of 3,283 reads. The cause is the knock-sensor fault flag
  (`mem:ECM_KS_FAULT_FLAGS` 0x80 on every read), not knock: the accumulated
  knock metric read 0 throughout.
- **HI DET does not choose a Fuel Comp table.** Each map slot holds one Fuel
  Comp table (+0x660). HI DET switches the timing trim (A at +0x020 to B at
  +0x120) and scales the fuel target's enrichment by the curve at 0xD247,
  which is 128 (×1.0) in all eight cells of this image.
- **Map 1 is the slot in use, shown by today's data.** Fuel Targets differs
  between slot 1 and slots 2–5 in ten cells. The last-column cell of the top
  rpm row is 159 (φ 1.242) in slot 1 and 164 (φ 1.281) in slots 2–5. The ECM
  commanded 1.242 at full pedal above 5,200 rpm. Fuel Comp is in the same
  2 KiB block the ECM copies to RAM, so it comes from slot 1 too.
  `mem:ECM_ACTIVE_MAP` was not in today's capture. It read 0 (slot 1) on
  196 of 196 reads on 2026-10-04.
- **The edit is in slot 1 only,** like every earlier tune. Slots 2–5 keep the
  20261004 Fuel Comp cells.
- **Timing while latched comes from trim B (High Det).** In slot 1 it equals
  trim A from 4,000 rpm up. It is 1–4 counts lower at 2,000–3,600 rpm, BFS
  columns 88–128. The k106 edits to trim A (rows 2,800–3,600 rpm, columns
  104–128, +1 to +2 counts) are not read while the latch is set.
- Loose end: `data/dids.yaml` (0x1134 evidence) says the rpm table 0xD247
  *replaces* the Target AFR cell under HI DET. `docs/ecu-firmware-analysis.md`
  says it *scales the excess over 128*. Today's data fits scaling: the table
  is all 128, and commanded φ was 1.16–1.24 with the latch set.

## Predictions (frozen before the flash)

Scored on the next warm drive with full-pedal pulls in 2nd or 3rd gear, same
sampler, same gates, bank 1, each rpm band needing ≥ 4 eligible reads.

| rpm band | Error predicted | Today |
|---|---|---|
| 3,000–4,000 (control) | +2.3 % (unchanged within ±2.5) | +2.0 % |
| 4,000–4,400 | −0.8 % ± 2.5 | −3.5 % |
| 4,400–4,800 | −0.5 % ± 2.5 | −4.2 % |
| 4,800–5,200 | −2.2 % ± 2.5 | −7.1 % |
| 5,200–5,600 | −1.9 % ± 2.5 | −6.8 % |

- **Mechanism check, independent of the A/F sensor:** at full pedal,
  BFS·rpm/MAF (DID 0x1208 with PID rpm and MAF, paired per read) falls by
  2.7 % at 4,000–4,400 rpm, 4.0 % at 4,400–4,800 and 4.9 % at 4,800–5,600,
  each ±1.5 points, against today's capture.
- **Commanded φ at full pedal is unchanged:** 1.164 at 3,600 rpm, 1.242 above
  5,200.
- **Idle and cruise trims do not move because of this flash.** They will move
  with temperature, as they did between 11-08 and today.
- **Falsifiers:**
  - The 4,800–5,600 error stays within ±1.5 points of today's. Then these
    cells are not the ones the ECM reads at WOT.
  - Any band reads leaner than +2.5 % of target. Then the cut is too large:
    flash 20261004 back.
- **Rail pressure:** today's pulls ran 47.7–52 psi. Note it per pull. A hot
  afternoon at 49 psi delivers about 1 % less than a cool morning.

## After the flash

Recorded as `ZS44A-20261110` in `tune_revisions.yaml` and
`afr_calibration.yaml` (`56cc348f`). The flash itself was not captured: the Pi
was unplugged from the OBD port for it. Its time is bounded between 18:16Z (no
tester traffic yet) and 23:45:36Z (engine running on the Pi's next frames).

## Scored: first drive after the flash (2026-11-10 23:53Z – 2026-11-11 00:23Z)

`_capture-tuning-2026-11-10T235310`, 30 minutes, about seven full-pedal runs to 5,000 rpm or more,
coolant 87–91 °C, intake air 19–23 °C, baro 82 kPa, rail 50–51 psi falling to
48.5–49.4 in the pulls. The morning baseline was at the same coolant and baro.
Same sampler and gates as the prediction; outputs in
`~/canary-data/analysis-scripts/2026-11-10-drive/fc-samples-pm/` (`score.py`
prints the table and reproduces the morning's figures from its own samples).

| rpm band | Before | Predicted | After (n) | Verdict |
|---|---|---|---|---|
| 3,000–4,000 (control) | +2.0 % | +2.3 ± 2.5 | no reads | not scored |
| 4,000–4,400 | −3.5 % | −0.8 ± 2.5 | no reads | not scored |
| 4,400–4,800 | −4.2 % | −0.5 ± 2.5 | +1.2 % (3) | inside, but under the 4 reads required |
| 4,800–5,200 | −7.1 % | −2.2 ± 2.5 | +0.6 % (4) | **missed by 0.3**: leaner than predicted |
| 5,200–5,600 | −6.8 % | −1.9 ± 2.5 | +0.4 % (6) | inside |

- **The rich error at the top is gone.** Measured AFR above 5,200 rpm is 11.9
  against 11.8 commanded; it was 11.0. One read at 5,812 rpm: −1.3 %.
- **It landed 2–3 points leaner than predicted.** The error moved 5.4 to 7.7
  points for a 3–5 % cut. The prediction assumed the morning's error was all
  table; 2–3 points of it were not. Coolant, baro and rail pressure match
  between the two captures, so the cause is not identified. It is the size of
  the 2.5 % WOT noise floor.
- **Mechanism check (BFS·rpm/MAF at full pedal):** fell 2.8 % at 4,400–4,800
  rpm (predicted 4.0 ± 1.5: inside), 2.9 % at 4,800–5,200 (predicted 4.9:
  **missed by 0.5**) and 3.8 % at 5,200–5,600 (predicted 4.9: inside). The cut
  is live; it reads about a point smaller on BFS than the cells say. BFS is
  filtered, and these are 3–6 reads a band.
- **Commanded φ unchanged:** 1.219 / 1.227 / 1.242 in the three bands, as
  before.
- **Control, from the repo's gated `delivery_error` (PID 0x44 as target):**
  2,500–3,000 rpm, rows the edit did not touch, +0.9 % (n 2) before and +1.0 %
  (n 8) after. The same digest gives −5.4 → +1.2 % at 4,500–5,000 (n 8) and
  −7.0 → −0.2 % above 5,000 (n 13).
- **Idle trims did not move:** closed-loop total trim at idle −2.3 / −1.6 %
  (bank 1 / 2), against −0.8 / −1.6 % that morning.
- **Falsifiers:** neither tripped. The top-end error moved by far more than
  1.5 points, and no band reads leaner than +2.5 % (largest single read
  +1.7 %).
- **Not covered:** full pedal between 3,000 and 4,400 rpm. The 4,000–4,400 row
  (cut 2.2–3.0 %) has no read after the flash. A pull held from about 3,000
  rpm in 3rd would fill it.

**Verdict: keep ZS44A-20261110.** WOT delivery is on target from 4,400 rpm up.
No further cut: there is no rich margin left beyond the target itself, and
the knock sensor is still in fallback.
