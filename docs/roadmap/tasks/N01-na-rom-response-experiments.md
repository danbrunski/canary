# N01 — How the NA engine responds to ROM changes

Written 2026-09-23, after the morning `tuning` drive
([drive log](../../../data/analyses/2026-09-23-drive-log.md)).

**Why this exists.** The supercharger goes in within weeks
([`../supercharger-build.md`](../supercharger-build.md)). The operator wants to
use the time before then to learn how this engine and ECM respond to ROM
edits, while the engine is still a known quantity. Phase 1 (NA1–NA8) records
what the NA engine *does*. This file records what it does *when the
calibration changes*. After the build, fuel and spark edits are the main
levers, and this is the cheapest time to learn their units and side effects.

**Who does what.** The operator makes every flash, in UpRev, with the key on,
the engine off and the truck parked (AGENTS.md). CANary never writes the ECM
here. Its job is to freeze predictions before the drive that follows a flash,
then measure the result against them.

Same conventions as the other task files: task-level truth is the checkboxes,
and `[x]` only with the measurement in hand and the session named.

## What the design rests on (measured 2026-09-23)

**Noise floor: the same ROM on two drives.** Warm, closed-loop STFT medians per
rpm × MAF cell, `_capture-tuning-2026-09-22T170459` against
`_capture-tuning-2026-09-23T142926`. Both are on `ZS44A-20260920` with LTFT at
0.00 %, and both sessions are on the laptop under
`~/canary-data/sessions/nissan-xterra-2008/`.

| region | cells with n ≥ 15 on both | difference between the drives |
|---|---|---|
| cruise, 2,000–3,000 rpm, 5–70 g/s | 12 | within ±1.56 points on both banks (two STFT quanta of 0.78 %) |
| idle, ~725 rpm, < 5 g/s | 1 | +6.25 bank 1, +3.12 bank 2 |

**Third same-ROM drive (2026-09-23 afternoon, `_capture-tuning-2026-09-23T164007`,
no flash recorded) — the ±1.6 floor holds only at 15–70 g/s.** The frozen
prediction was "every cruise cell within ±1.6 of the morning". It failed in the
light-load cells:

| cell (rpm, g/s) | bank 1 Δ | bank 2 Δ |
|---|---|---|
| 2,000, 5–10 | +4.30 | +5.08 |
| 2,000, 10–15 | +2.34 | +1.56 |
| 2,500, 5–10 | +3.52 | +2.73 |
| 2,500, 10–15 | +7.42 | +5.86 |
| 2,000–2,500, 15–45 (five cells) | −1.17 … +1.95 | −1.56 … +1.56 |
| 2,000–2,500, 45–70 | 0.00 / +1.17 | 0.00 / +2.34 |
| idle | +3.91 | +6.25 (idle read **positive** this drive) |

The mid-load cells stayed put, so the light-load moves are not an unrecorded
fuel-compensation edit, which would shift the whole block. Light-load cells
hold tip-outs and closed-loop re-entry after fuel cut, and their n was 24–34
against 43–86 in the morning. **So judge N01.1 on the 15–70 g/s cells only**,
where three drives agree within ±1.6 (one 2,000 rpm / 45–70 bank-2 cell at
+2.34). A ≥ 4 % edit is still 2.5× that floor. Cells below 15 g/s, and idle,
are reported but never scored.

**The pattern that repeats on both drives.** At 2,500 rpm and 10–30 g/s,
bank 1 STFT sits at −6 to −8 % and bank 2 at −4 to −5 %. Both shrink to about
0 at 45–70 g/s. Closed-loop delivery (λ·φ − 1 from same-instant pairs) is
within ±0.5 % in every 2,000–2,500 rpm cell from 15 to 70 g/s. The closed loop
is reaching its target; the negative trim is what it costs.

**A one-count edit in one cell is invisible.** The 2026-09-20 flash moved one
Fuel Targets cell (5,200 rpm / BFS 18.20) by one count, to 11.54 AFR. On the
2026-09-23 pull the ECM commanded φ 1.227–1.242 (11.98–11.84 AFR) at
5,050–5,360 rpm. The final target interpolates between neighbouring cells, and
BFS has never been logged above 17.80, so that cell contributes only part of
its weight. **Edit blocks of cells, by several counts.**

**What CANary cannot see.** Neither axis of these tables is logged: not BFS,
and not the Volumetric Flow Ratio that Fuel Compensation Main is indexed on.
Cam angle is visible only in UpRev's logger. So an edit is placed by making the
block wider than where the truck operates, not by knowing which cell it sits in.

## Rules for every flash here

- **One category of change per flash.** The 2026-08-03 flash changed K, the
  targets and spark at once, and none of the three could be separated.
- **Keep the running ROM file** so any result can be checked by flashing back.
- **Record each flash in `data/nissan-xterra-2008/tune_revisions.yaml`**
  (revision, flash time, ROM file, SHA-256, the exact edit) before the next
  drive. A session that spans a flash is excluded from comparison. Nothing
  catches a missing entry: every UpRev flash so far has kept calibration id
  `1ZS44A00000`, so `/status` `calibration_matches_registry` stays true
  whether or not the flash was recorded.
- **Freeze the predictions in the next-drive plan before the drive.** Never
  move them after seeing data.
- **Controls are part of the edit:** state which cells were deliberately left
  alone, and require them not to move.
- **Measure per cell, never pooled across cells.** Use `align_pairs` /
  `compare_channels` for any cross-channel comparison (AGENTS.md "Never
  compare two signals by their aggregates").

## N01.1 Fuel Compensation Main: −4 % over the cruise block

- [ ] **Before the flash:** during an ordinary cruise, use UpRev's map tracing to
  see which Fuel Compensation Main cells light up. Record the rows and columns.
- [ ] **The edit:** about −5 counts (128 = 100 %, so one count is 0.78 % and
  five are −3.9 %) over every cell of rows covering ~1,500–3,000 rpm and the
  columns that tracing showed, **plus one cell of margin on each side**. Leave
  the idle row and the full-throttle columns untouched: they are the controls.
  Record the exact cells and counts in `tune_revisions.yaml`.
- [ ] **The drive:** an ordinary drive on `tuning`, warm, with plenty of cruise
  and some idle. No pulls needed.
- [ ] **Predictions (freeze these in the plan before the drive):**
  - STFT in the edited cells **at 15–70 g/s** rises **+2.5 to +5.5 points** on
    both banks, against the medians of the three 2026-09-22/23 same-ROM drives.
    Cells below 15 g/s are too noisy between drives to score (see above). Expected: bank 1 about −7 → −3, bank 2
    about −4 → 0.
  - Closed-loop delivery stays within ±0.5 % of target in those cells.
  - Control cells (idle, ≥ 45 g/s if outside the block, full throttle) move by
    no more than ±1.6 points.
  - Timing at matched rpm × MAF may advance slightly, because a lower fuel
    compensation lowers BFS, and the timing table is indexed on BFS. Predicted
    < 1°; measure it, don't assume it.
  - **BFS moves with the edit.** *Rewritten 2026-10-03, before any
    prediction was frozen.* The earlier version asked whether 0x1208 was BFS.
    P02.14 answered that (`ca80026`): 0x1208 is the base fuel schedule
    (`derived:BASE_FUEL_SCHEDULE`, ms), verified against UpRev's own stream
    definition. The August shared-axis walk measured raising fuel
    compensation raising BFS, so at matched rpm × MAF in the edited cells,
    `derived:BASE_FUEL_SCHEDULE` falls by about the edit (−3.9 %). The
    control cells don't move. No fall means fuel compensation applies
    downstream of BFS on this program, which is worth knowing before
    N01.2's timing reading. BFS also indexes both the timing table and the
    target-AFR map's load axis, so a falling BFS can move a row to a
    neighbouring target cell. Score target-AFR cells on the BFS each row
    actually read, not the pre-edit one.
- [ ] **Outcome:** pass / fail / unresolved for each prediction, with n per cell.
  A pass says a fuel-table edit reaches delivered fuel 1:1 through closed loop.
  It cannot fix the bank 1 / bank 2 difference, which this table does not
  separate.

## N01.2 Ignition Timing Trim: a few counts of retard over the cruise block

- [ ] **Prerequisite:** N01.1 measured, or deliberately skipped. Never flash both
  at once.
- [ ] **The edit:** retard only (knock-safe), a few counts, over the same cruise
  block as N01.1 with the same margin and controls. Record it.
- [ ] **What it measures:** the table's unit. Nobody here knows what one count is
  in degrees. The ECM computes MBT from a formula and this table only trims it
  (UpRev guide pp. 9–14). RAM timing (`mem:ECM_IGNITION_TIMING_RAM`, 1 Hz) and
  PID 0x0E (4 Hz) both read the result, and they agree sample for sample
  (median Δ 0.0°, 77 % within 1°, n = 1,659, 2026-09-23).
- [ ] **Predictions (freeze before the drive):**
  - Timing in the edited cells drops by (counts × unit) at matched rpm × MAF.
    Whether it drops by a flat amount everywhere, or in proportion to the base
    timing, is what tells you whether the trim is additive.
  - The controls do not move. STFT does not move.
  - The fuel-economy cost of a few degrees near MBT is probably below what road
    load noise lets us see. Do not report it as a result.
- [ ] **Outcome:** degrees per count, additive or not, with n.

## Later candidates (not designed yet)

- **Cam discriminator at 3,200–4,000 rpm, high load.** Pull ~5° of intake cam
  advance and watch whether spark rises on its own over consecutive pulls. It
  needs repeatable full-throttle pulls (NA1) and UpRev logging for cam angle.
- **A full-throttle Fuel Targets block.** Only after NA1 has three steady pulls
  to compare against, and after fuel pressure (NA2) settles whether WOT is
  lean.

## Not now, and why

- **K.** It scales fuel by one factor everywhere, and the error changes with
  airflow. Cutting it to fix light load would lean out full throttle, which
  earlier drives put ~3 % lean. It also changes with the new injectors.
- **LTFT.** It reads 0.00 % since the 2026-09-20 flash. The operator's call
  (2026-09-23): it took weeks to relearn last time, so ignore it for now. While
  it stays at zero, STFT alone carries every fuel change, which makes N01.1
  cleaner. If LTFT starts moving mid-experiment, compare total trim
  ((1 + STFT)(1 + LTFT) − 1), not STFT.
