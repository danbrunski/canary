# LTFT learning on the first `ltft-gate` capture (2026-11-08)

Desk decode of `_capture-ltft-gate-2026-11-08T180840` (18:08:40–18:20:48Z;
calibration ZS44A-20261004). Nothing here touched the Pi, the catalog or a
profile. The script is
`~/canary-data/analysis-scripts/2026-11-08-ltft/ltft_learning.py <session_dir>`
(tables: `tables.py <session_dir>`). CSVs are in `out/<session>/` beside it.
`_capture-ltft-gate-2026-11-08T173437` holds no ECM answers (the OBD plug was
out). `_capture-ltft-gate-2026-11-08T182054` was not on the laptop when this
was written. Run the same script on it once it is pulled.

## Summary

- **The learner has run, and the maps are in use.** At the first read
  (18:08:41.6Z) each bank already held 20 learned cells (B1 756 visits, B2 821).
  On 10-27 at 17:52Z every count read 0. During this capture it updated 3 cells
  per bank 40 times (20 per bank), all between 18:15:21 and 18:16:39Z, while
  creeping at ~3–4 km/h with 28B9.6 set. Nothing moved after the restart,
  because the accumulator restarts at key-on and 28B9.6 was clear.
- **Known answer passes.** PID LTFT A = floor((cell + w62AC − 0x6400)/200),
  with w62AC neutral:
  - 36/36 on steady reads with a settled cell and a BFS read ≤ 0.5 s away;
  - 129/136 on steady reads at any separation;
  - in 328/338 informative Mode 01 batches, the cell implied by both banks'
    trims together is among the cells the operating point indexed.

  Every miss sits in a transient.
- **"LTFT 0.00 at idle" is one unlearned cell.** Warm idle indexes r0c1 (rpm
  < 1000, BFS 5,376–10,240). It has never been visited. The idle path that
  could learn it was closed on every pre-key-off idle read: by purge flow for
  B1 (w38AA = 10240 ≥ 40) and by the 30-update cap for B2 (1436 = 31).
- **0x1518 cannot be seen at any 0x23 rate.** Its third term is the learner
  tick 2881.1, which `FUN_00057c54` sets just before the gate call and clears
  just after it. 2881.1 read set on 0 of 629 reads, and 1518 on 0 of 671. That
  includes 7 reads where 179C, 138A and 151A were all set. 151A *is*
  observable: it read set on 11 reads. The visit counts are the only
  observable proof of learning.
- **28B9.6 does not need the 770 threshold.** It read set on 73 reads at
  w5224>>15 = 754–769. The code recomputes it every call as
  `(w5224>>15 ≥ 0x302) OR ((w5224>>15) − w1584 > w14A0)`, and the second route
  held it.
- **Predictions, as written:** G1 unresolved, G2 fail, G3 fail, G4 fail
  (section 6).
- **Fuelling: the learned correction is cell-shaped, not uniform.** At
  1,800–3,400 rpm it runs from −4.6..−6.3 % at light cruise (≈ 10–25 g/s) to
  −0.3..−3.4 % at ≈ 40–70 g/s. At ≈ 69–89 g/s and 3,000 rpm it reaches
  +1.5..+2.6 % on B2, against −0.6..−0.7 % on B1. Both banks show the same
  rise with load. The overall level (visit-weighted −3.1 % B1, −2.9 % B2) sits
  inside N02.7's between-drive span. N02.7's "uniform within a drive" does not
  hold on the ECM's own per-cell estimate (section 7).

## 1. Data, decode and read rates

- **Windows.** The profile reads the visit counts (0xFFFF52F5–5374) as three
  windows of 57, 57 and 14 bytes, and the maps (0xFFFF5460–555F) as windows of
  57, 57, 57, 57 and 28 bytes. Every row of a window carries the whole window
  in `payload`, starting at its lowest member address. Re-decoding all 17,826
  gate and window anchor values from their payloads reproduced
  `value_number` with 0 mismatches. Two map words straddle windows (B1 cell 21
  at 0x550A, B2 cell 28 at 0x5498). For those, the decoder joins the latest
  byte of each window.
- **Layout** (`FUN_00057954`, `FUN_00057cf8`, `FUN_00059680`).
  - Map words are big-endian u16 and 0x6400 = 1.0, so 256 counts = 1 %, clamped
    0x5A00..0x6E00.
  - Bank index 0 is B1 (map 0xFFFF54E0, counts 0xFFFF5335). Bank index 1 is
    B2 (map 0xFFFF5460, counts 0xFFFF52F5).
  - Cell = row × 8 + column.
- **Axes** (`FUN_00057954`; bytes read from the ECU's own 09-20 ROM read, which
  differs from 20261004 only at 0xCB2A–CB38 and 0xE0671–E075F):
  - Column: BFS w1038 (the snapshot of w103A, which DID 0x1208 reads) against
    cal 0xD290 = 21, 40, 64, 88, 112, 128, 144 × 256. The edges are 5,376,
    10,240, 16,384, 22,528, 28,672, 32,768 and 36,864 counts, with ±1,024
    hysteresis.
  - Row: b11CA = rpm/50 against cal 0xD372 = 20, 36, 52, 68, 88, 108, 128. The
    edges are 1,000, 1,800, 2,600, 3,400, 4,400, 5,400 and 6,400 rpm. Moving
    up needs one count (50 rpm) past an edge.
  - Today BFS × rpm / MAF = 1.249 M (n 460, IQR 1.242–1.258 M). The column
    edges therefore equal, in g/s: 9.5, 18.0, 28.9, 39.7, 50.5, 57.7 and 64.9
    at 2,200 rpm, and 12.9, 24.6, 39.3, 54.1, 68.9, 78.7 and 88.5 at
    3,000 rpm.
- **Learner** (`FUN_00057cf8`): cell += (avg STFT − 0x6400) × gain >> 8.
  - The gain is 26 (10.2 % of the error) on a cell's first visit. After that it
    is cal 0xD60B = 26, 26, 16, 8, smaller for larger errors.
  - The count gains +1 per update.
- **Writer** (`FUN_00057b04`): LTFT 26DE/26E0 = cell + w62AC − 0x6400.
  - w62AC is a second persistent term (0x5500..0x8100) and is not logged.
  - It moves only when cell + STFT passes the clamp (`FUN_00058144`).
  - The flash-reset routine `FUN_00059680` resets it with the maps.

Achieved rates come from consecutive acquisition timestamps. The one key-off
gap of 85–90 s is excluded.

| Channel | n | Median Δt s | p10–p90 s | Rate |
|---|---|---|---|---|
| 1518 / 151A / 179C window | 671 | 0.91–0.93 | 0.71–1.16 | ~1.1 Hz |
| Other gate bytes (2792, 28B9, 2834, 1686, 1435, 38AA, 5224 …) | 629 | 0.98–1.00 | 0.82–1.19 | ~1.0 Hz |
| Count and map windows | 320 each | 2.00 | 1.90–2.12 | 0.50 Hz |
| DID 0x1208 BFS, 0x1110 purge | 679 | 1.00 | 0.50–1.02 | 1.0 Hz |
| PID LTFT B1 / B2 | 1,162 / 1,163 | 0.51 | 0.24–0.99 | 2.0 Hz |
| PID RPM | 1,339 | 0.32 | 0.24–0.90 | 3.1 Hz |

## 2. Known answer: PID LTFT against the indexed cell

At each Mode 01 LTFT read (bank b, raw A):

1. Take the row from PID RPM in the same batch (≤ 0.2 s).
2. Take the column from the two DID 0x1208 reads that bracket it (≤ 1.5 s).
3. Take the cell word from the latest map read.
4. Predict A = floor(word/200), with w62AC = 0x6400.

**Strict** means a steady operating point and a settled cell:

- both BFS reads give the same column, outside the ±1,024 band;
- rpm is outside the 50 rpm band;
- no map read within ±2.6 s shows the cell changing.

| Test | B1 | B2 |
|---|---|---|
| Strict, BFS read ≤ 0.5 s away | **18/18** (14/14 non-neutral) | **18/18** (14/14) |
| Strict, any separation | 68/70 (57/59) | 61/66 (50/55) |
| round(word/200) instead, strict | 19/70 | 51/66 |
| w62AC − 0x6400 allowed by the 18 tight reads | 0..+16 counts (0..+0.06 %) | 0..+154 (0..+0.60 %) |

- **The conversion is floor.** B1 rules out rounding.
- **w62AC is neutral on both banks.** That confirms the scale: 1 % = 256
  counts, and A = 128 + (word − 0x6400)/200, which is 100/128 % per count.
- **The 7 strict misses fall in two transients:**
  - 18:19:10Z, a tip-out at 2,050–2,090 rpm;
  - 18:18:43–50Z, pulling away 14–21 s after the first post-restart read.

  In both, the DID BFS read is 0.62–0.94 s from the Mode 01 read, and both
  banks' A fit the neighbouring cell together (r2c1 and r1c2). That is
  pairing skew, not scale.
- **Joint inversion.** Take the cells whose B1 *and* B2 words reproduce both
  A values of a Mode 01 batch. In the 338 batches where fewer than 40 cells fit,
  328 contain a cell that the operating point indexed within ±1 s. Of the 10
  misses:
  - 3 at 18:16:33–34Z fall inside 2–3-visit updates, whose intermediate values
    were never read;
  - 7 are post-restart reads (18:18:38–18:20:27Z, 1,100–1,500 rpm) while
    driving away. There, BFS moves between the 1 Hz DID reads, so the
    indexed set is a guess.
- **Idle.** Idle BFS sits inside the ±1,024 hysteresis band around the
  5,376 column edge (4,352–6,400), where the column the ECM uses depends on
  where it came from. Reads at rpm < 1000, with A for B1 and B2:
  - BFS 4,352–5,376: A = 128 on 72 and 75 reads, and r0c0's value
    (126/127) on 46 and 50;
  - BFS 5,376–6,400: 128 on 636 and 643, and r0c0's value on 38 and 38;
  - above 6,400 (column 1 for certain): 128 on 31 and 31.

  None fell below 4,352. The 3 reads above 6,400 that show r0c0's value are
  pairing skew. So warm idle almost always stays in column 1, r0c1.
- **The changes match the cells the ECM indexed.** All 62 cell changes (31
  count, 31 map) lie in a cell that some PID or DID rpm read in their bracket,
  crossed with a DID BFS read within 0.5 s, indexes. That is 58/62 with
  DID-only pairing; the 4 others are a 1,000 rpm edge crossing that only the
  3 Hz PID RPM caught.
- **The step sign matches STFT.** In 29 of the 29 brackets whose median bank
  STFT is non-zero, the learned step has the same sign as STFT.

## 3. The learned maps

All values are % correction, with visits in parentheses and `·` for neutral
and unvisited. Rows 5–7 (≥ 4,400 rpm) and column 7 (BFS ≥ 36,864) are neutral
and unvisited in both banks at both reads. Every non-neutral cell has visits,
and every visited cell has moved.

**First complete read, 18:08:41.6Z** (learned before the capture):

B1:

| rpm \ BFS | <5376 | 5376–10240 | 10240–16384 | 16384–22528 | 22528–28672 | 28672–32768 | 32768–36864 |
|---|---|---|---|---|---|---|---|
| <1000 | −0.94 (10) | · | · | · | · | · | · |
| 1000–1800 | · | −0.97 (4) | −1.50 (16) | −1.64 (9) | −1.71 (8) | · | · |
| 1800–2600 | −0.12 (2) | −6.32 (33) | −2.62 (67) | −4.78 (77) | −1.68 (63) | −2.62 (55) | +0.33 (4) |
| 2600–3400 | −1.36 (32) | −6.21 (43) | −4.70 (78) | −3.71 (153) | −1.98 (54) | −0.67 (34) | −0.58 (13) |
| 3400–4400 | · | · | · | · | +0.04 (1) | · | · |

B2:

| rpm \ BFS | <5376 | 5376–10240 | 10240–16384 | 16384–22528 | 22528–28672 | 28672–32768 | 32768–36864 |
|---|---|---|---|---|---|---|---|
| <1000 | −1.51 (31) | · | · | · | · | · | · |
| 1000–1800 | · | −0.14 (1) | −0.86 (10) | −3.00 (11) | −1.92 (8) | · | · |
| 1800–2600 | +0.54 (2) | −4.61 (61) | −5.15 (64) | −6.00 (78) | −3.41 (67) | −2.88 (55) | +0.18 (3) |
| 2600–3400 | −1.14 (35) | −5.30 (51) | −3.02 (84) | −2.87 (157) | −0.29 (54) | +2.61 (35) | +1.54 (13) |
| 3400–4400 | · | · | · | · | −0.09 (1) | · | · |

**Last read, 18:20:47.8Z.** Only three cells per bank differ from the first
read:

| Cell | B1 | B2 |
|---|---|---|
| r0c0 (<1000 rpm, BFS <5376) | −0.94 (10) → −0.35 (12) | −1.51 (31) → −1.00 (33) |
| r1c0 (1000–1800, <5376) | · (0) → +3.24 (17) | · (0) → +2.34 (15) |
| r1c1 (1000–1800, 5376–10240) | −0.97 (4) → −0.93 (5) | −0.14 (1) → +0.18 (4) |

At the end, each bank holds 21 learned cells: B1 776 visits, B2 841.

**r0c0's visits were most likely all this key cycle's.** 1435/1436 are the
per-key-cycle idle-path update counters; both read 0 after the restart. They
read 10 and 31, exactly B1's and B2's r0c0 counts at the first read. They
did not change during the capture, so the r0c0 updates seen here came
through the off-idle path.

Where and when the other 746/790 visits were learned cannot be told. The
candidates are the unrecorded 17:26–18:08Z part of this key cycle (key-on
from RUN_TIME), or key cycles after 10-27 17:52Z that the Pi did not record.

## 4. Timeline of cell changes

Every count change came with a map change of the same cell in the same
bracket, and no map changed without a count. The brackets are consecutive
window reads, about 2 s apart.

- **"rpm" and "BFS"** are the ranges seen in the bracket, from PID and DID rpm
  and from DID BFS within 0.5 s.
- **The other columns** are the nearest read to the bracket's closing read.
  The largest separation is shown; the median is 0.19 s.
- **"179C"** lists the reads inside the bracket.
- **STFT is a single read**, the one nearest the bracket's close. It can
  carry the opposite sign to the step (e.g. 15:25–15:27). The sign test in
  section 2 uses the median of the bank's reads inside the bracket instead.
- **Constant over the whole window** (79 gate reads, 18:15:20–18:16:40Z):
  coolant 91–92 °C, 2792 = 1, and 26BA = 0x03 (both banks closed loop).

| Bracket (Z) | Bank | Cell | Visits | Value % | rpm | BFS | MAF g/s | STFT B1/B2 % | Purge % | 28B9 | 179C | 2834 | km/h | Max sep s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 15:21–15:23 | B2 | r1c1 | 1→2 | −0.14→+0.10 | 825–1175 | 5169–6559 | 3.8 | +0.8/+1.6 | 18.5 | 0x44 | 3,3 | 0x00 | 3.8 | 0.46 |
| 15:23–15:25 | B2 | r1c0 | 0→1 | 0.00→+0.35 | 1112–1238 | 4278–6559 | 5.2 | +2.3/+2.3 | 8.0 | 0x44 | 3,3 | 0x00 | 3.5 | 0.47 |
| 15:23–15:25 | B1 | r1c0 | 0→1 | 0.00→+0.19 | 1112–1238 | 4278–6559 | 5.2 | +2.3/+2.3 | 8.0 | 0x44 | 3,3 | 0x00 | 3.5 | 0.48 |
| 15:25–15:27 | B2 | r1c0 | 1→2 | +0.35→+0.57 | 1137–1250 | 5373–6113 | 3.9 | −5.5/−4.7 | 8.0 | 0x54 | 3,0 | 0x10 | 3.3 | 0.46 |
| 15:25–15:27 | B1 | r1c0 | 1→2 | +0.19→+0.31 | 1137–1250 | 5373–6113 | 3.9 | −5.5/−4.7 | 8.0 | 0x54 | 3,0 | 0x10 | 3.3 | 0.47 |
| 15:33–15:35 | B2 | r1c1 | 2→3 | +0.10→−0.01 | 838–1100 | 5382–5815 | 4.6 | +4.7/+1.6 | 12.0 | 0x44 | 3,3 | 0x00 | 2.6 | 0.46 |
| 15:33–15:35 | B1 | r1c1 | 4→5 | −0.97→−0.93 | 838–1100 | 5382–5815 | 4.6 | +4.7/+1.6 | 12.0 | 0x44 | 3,3 | 0x00 | 2.6 | 0.48 |
| 15:35–15:37 | B2 | r1c1 | 3→4 | −0.01→+0.18 | 825–1137 | 5643–6373 | 5.7 | 0.0/−2.3 | 7.5 | 0x44 | 3,3 | 0x00 | 3.6 | 0.48 |
| 15:37–15:39 | B2 | r1c0 | 2→3 | +0.57→+0.52 | 1184–1321 | 4632–6373 | 3.8 | +3.1/−0.8 | 7.0 | 0x54 | 3,0 | 0x10 | 3.4 | 0.48 |
| 15:37–15:39 | B1 | r1c0 | 2→3 | +0.31→+0.35 | 1184–1321 | 4427–5291 | 3.8 | +3.1/−0.8 | 7.0 | 0x54 | 3,0 | 0x10 | 3.3 | 0.49 |
| 15:45–15:47 | B2 | r1c0 | 3→4 | +0.52→+0.78 | 1050–1096 | 4568–4833 | 4.4 | +3.9/+3.1 | 6.0 | 0x44 | 3,3 | 0x00 | 3.3 | 0.48 |
| 15:45–15:47 | B1 | r1c0 | 3→4 | +0.35→+1.24 | 1050–1096 | 4568–4833 | 4.4 | +3.9/+3.1 | 6.0 | 0x44 | 3,3 | 0x00 | 3.3 | 0.50 |
| 15:47–15:49 | B2 | r1c0 | 4→7 | +0.78→+1.95 | 1109–1162 | 4568–5094 | 4.7 | +2.3/+3.1 | 7.5 | 0x44 | 3,3 | 0x00 | 3.6 | 0.46 |
| 15:47–15:49 | B1 | r1c0 | 4→7 | +1.24→+1.80 | 1109–1162 | 4568–5094 | 4.7 | +2.3/+3.1 | 7.5 | 0x44 | 3,3 | 0x00 | 3.7 | 0.47 |
| 15:49–15:51 | B2 | r1c0 | 7→8 | +1.95→+2.18 | 1084–1290 | 4236–5094 | 4.0 | +3.9/+4.7 | 20.0 | 0x54 | 0,0 | 0x10 | 3.0 | 0.41 |
| 15:49–15:51 | B1 | r1c0 | 7→8 | +1.80→+1.92 | 1084–1290 | 4236–5094 | 4.0 | +3.9/+4.7 | 20.0 | 0x54 | 0,0 | 0x10 | 3.0 | 0.38 |
| 15:51–15:53 | B2 | r0c0 | 31→32 | −1.51→−1.20 | 862–1134 | 4740–5337 | 3.9 | +2.3/+1.6 | 8.5 | 0x54 | 3,0 | 0x10 | 3.4 | 0.50 |
| 15:51–15:53 | B1 | r0c0 | 10→11 | −0.94→−0.79 | 862–1134 | 4740–5337 | 3.9 | +2.3/+1.6 | 21.5 | 0x54 | 3,0 | 0x10 | 3.4 | 0.49 |
| 15:57–15:59 | B2 | r0c0 | 32→33 | −1.20→−1.00 | 950–1125 | 5044–5353 | 4.7 | +5.5/+5.5 | 7.0 | 0x44 | 3,3 | 0x00 | 2.9 | 0.49 |
| 15:57–15:59 | B2 | r1c0 | 8→9 | +2.18→+2.54 | 950–1125 | 5044–5353 | 4.7 | +5.5/+5.5 | 7.0 | 0x44 | 3,3 | 0x00 | 2.9 | 0.49 |
| 15:57–15:59 | B1 | r0c0 | 11→12 | −0.79→−0.35 | 950–1125 | 5044–5353 | 4.7 | +5.5/+5.5 | 7.0 | 0x44 | 3,3 | 0x00 | 2.8 | 0.48 |
| 15:57–15:59 | B1 | r1c0 | 8→9 | +1.92→+2.29 | 950–1125 | 5044–5353 | 4.7 | +5.5/+5.5 | 7.0 | 0x44 | 3,3 | 0x00 | 2.8 | 0.48 |
| 15:59–16:01 | B2 | r1c0 | 9→10 | +2.54→+2.91 | 1012–1300 | 4080–5353 | 4.1 | +1.6/+3.9 | 15.0 | 0x54 | 3,0 | 0x10 | 3.5 | 0.49 |
| 15:59–16:01 | B1 | r1c0 | 9→10 | +2.29→+2.68 | 1012–1300 | 4080–5308 | 4.1 | +1.6/+3.9 | 29.5 | 0x54 | 3,0 | 0x10 | 3.4 | 0.47 |
| 16:31–16:33 | B1 | r1c0 | 10→11 | +2.68→+2.86 | 1134–1275 | 4836–6924 | 5.3 | +2.3/+4.7 | 7.5 | 0x44 | 1,3 | 0x00 | 3.9 | 0.49 |
| 16:33–16:35 | B2 | r1c0 | 10→12 | +2.91→+3.09 | 1300–1393 | 5036–5939 | 5.9 | −2.3/−4.7 | 8.5 | 0x44 | 3,3 | 0x00 | 4.2 | 0.48 |
| 16:33–16:35 | B1 | r1c0 | 11→14 | +2.86→+3.21 | 1300–1393 | 5036–5939 | 5.9 | −2.3/−4.7 | 8.5 | 0x44 | 3,3 | 0x00 | 4.2 | 0.46 |
| 16:35–16:37 | B2 | r1c0 | 12→14 | +3.09→+2.63 | 1350–1388 | 4914–5036 | 5.2 | +3.1/−3.9 | 8.5 | 0x44 | 3,3 | 0x00 | 4.0 | 0.47 |
| 16:35–16:37 | B1 | r1c0 | 14→15 | +3.21→+3.07 | 1350–1388 | 4914–5036 | 5.2 | +3.1/−3.9 | 8.5 | 0x44 | 3,3 | 0x00 | 4.0 | 0.49 |
| 16:37–16:39 | B2 | r1c0 | 14→15 | +2.63→+2.34 | 1275–1412 | 4074–4914 | 3.8 | +3.1/+1.6 | 6.5 | 0x54 | 3,0 | 0x10 | 3.5 | 0.47 |
| 16:37–16:39 | B1 | r1c0 | 15→17 | +3.07→+3.24 | 1275–1412 | 4074–4914 | 3.8 | +3.1/+1.6 | 6.5 | 0x54 | 3,0 | 0x10 | 3.5 | 0.48 |

All times are 18:MM:SS. Updates came at up to 3 per 2 s bracket, so the
learner tick is no slower than ~0.67 s while the condition holds. There were
no changes before 18:15:21Z (warm idle) and none after the restart.

## 5. The gate, read by read

Each 179C read is paired with the nearest read of every other input (median
0.12 s apart; coolant 0.34 s, purge DID 0.27 s). Rebuilding the learn
condition from the logged inputs agrees with the ECM's own 179C bit on 99.1 %
(B1) and 99.0 % (B2) of 671 reads. Three inputs cannot be observed in this
profile: the stability term 8828, |4CA4/4CA6| ≤ 250, and the idle-hold
counter 8820.

| Segment | n | rpm | km/h | 179C set B1/B2 | 151A ≠ 0 | 28B9.6 | 2792 < 2 | w38AA < 40 | Accum >>15 | Purge % |
|---|---|---|---|---|---|---|---|---|---|---|
| Pre-key-off, idle flag | 476 | 762 | 0.0 | 0.01/0.01 | 0 | 1.00 | 1.00 | 0.01 | 754–878 | 24 |
| Pre-key-off, off idle | 42 | 1,094 | 3.3 | 0.93/0.90 | 0.26 | 1.00 | 1.00 | 0.00 | 851–873 | 9 |
| Post-restart, idle flag | 32 | 1,206 | 3.3 | 0/0 | 0 | 0.00 | 1.00 | 1.00 | 0–4 | 0 |
| Post-restart, off idle | 121 | 1,775 | 6.1 | 0/0 | 0 | 0.00 | 1.00 | 0.54 | 0–4 | 0 |

- **The idle path was closed on every pre-key-off idle read.**
  - B1: w38AA < 40 was false on all 470 reads. It was the first false input
    on 419; on the other 51, a non-zero speed read came first. w38AA sat at
    10240, its maximum, at 24 % duty.
  - B2: the 30-update cap was false (1436 = 31, 1686 bit 1 set) on all 470,
    and w38AA as well.
  - The idle path's purge input (w38AA < 40) is taken from the 10-27 D03
    trace. The decompile lost the arguments of that compare.
- **The off-idle path was open pre-key-off and closed after the restart.**
  Pre-key-off: 28B9.6 set and 2792 = 1, with 179C = 3 on 38 of 42 reads.
  Post-restart: 28B9.6 was clear on 100 %, and w1726 (after-start enrichment)
  was non-zero on 16 % of those reads.
- **The drive log's "1518/151A/1686 = 0 on all 671 reads" is wrong on two
  points** (`2026-11-08-drive-log.md`; its 1518 is right):
  - 151A read non-zero on 11 reads, all in 18:15:23–18:16:38Z;
  - 1686 = 0x02 on 495 of 629 reads (pre-key-off).

  1686 and 1435/1436 read 0 after the restart. 138A (the "held ≥ 2 ticks"
  flags) read non-zero on 15 reads, and every non-zero 151A read falls inside
  them, as the code requires.
- **0x1518 = condition ∧ held ∧ tick.** The 7 reads with 179C = 138A = 151A
  = 3 (for example 18:15:26.3, 18:15:47.4–49.4 and 18:16:37.5–38.3Z) still read
  1518 = 0. Only the tick term was missing. The tick itself, 2881.1, never read
  set (0/629) although the learner ticked at least 40 times. Both are set and
  cleared inside one `FUN_00059044` call. A ~1 Hz 0x23 read is blind to them
  by construction, at any rate the daemon can reach.

## 6. Predictions, scored as written

The criteria are the frozen text in `2026-10-27-next-drive-deploy-plan.md`.

- **G1: unresolved.**
  - Population: n = 657 warm (coolant > 69 °C) closed-loop reads per bank.
    179C is set on 42 (B1) and 41 (B2), and 1518 is never set, so the
    premise's second branch holds literally.
  - No input is false on ≥ 95 % with every other input of its path true on
    ≥ 50 %. The only inputs false on ≥ 95 % are 28B8.3 and 28B6.1 (100 %
    each). Together they are the flags path, so neither has its other path
    input true.
  - The next highest are path-specific:
    - w38AA ≥ 40: 86.9 %;
    - B2's update cap: 78.4 %;
    - the off-idle selector (2834.4 clear): 75.5 %;
    - 28B9.6 clear: 21.6 %.
  - So by its own rule two or more inputs share the blame: purge flow (B1) or
    the cap (B2) at idle before the key-off, and 28B9.6 after the restart.
  - The premise is refuted in substance. The condition held on 6.4 % of warm
    closed-loop reads and the learner ran 40 times. "179C set, 1518 never" is
    the tick's invisibility, not a closed gate.
- **G2: fail.** 640 of 960 count-window reads are non-zero: every read of the
  two windows that cover the learned cells. At the first read, B1 held 756
  visits and B2 821, in 20 cells each.
- **G3: fail.**
  - Population: n = 163 warm off-idle reads (2834.4 clear).
  - 28B9.6 clear or 2792 ≥ 2 on 121 (74.2 % < 95 %), all of them
    post-restart.
  - Both open on 42 (all pre-key-off), with 1518 clear on 42/42, which is the
    fail clause.
  - **The fail clause's "1518 stays clear" cannot be observed.** The learner
    updated 40 times inside exactly those 42 reads. Read through the counts,
    the purge controller behaves as the gate it was named for: open with
    learning before the key-off, closed (28B9.6 clear, 121/121) with no count
    change after the restart.
- **G4: fail.**
  - **Clause a.** w5224 = 0 at the first post-restart read (18:18:29.70Z),
    which holds. It grew on 4 of 561 intervals in which every DID 0x1110 read
    was 0, which is the fail condition.
    - Those intervals were 18:19:47–18:20:48Z: +2, +10, +8 and +4 counts, under
      0.001 of a unit in total.
    - w38AA read 11–38 on three of the four. The accumulator integrates w38AA,
      which outlasts the commanded duty, so the criterion's choice of 0x1110 is
      what fails, not the integration.
  - **Clause b.** 28B9.6 was set on 73 of the 226 reads with w5224>>15 < 770.
    This fails. The 73 are every pre-restart read below 770
    (18:08:41–18:09:41Z, 754–769, 28B9 = 0x54). The other 153 are
    post-restart (0–4), with the bit clear.
  - **Clause c.** The counts were never 0 in this capture, so "stay 0 until it
    sets" fails. No count moved while 28B9.6 was clear (0 of 31 count
    changes), so the second fail condition does not occur.
  - **Rate:** 15.6 units/min at warm idle (24 % duty, w38AA 10240, 3.3 g/s),
    12.1–13.9 while creeping, and 0.9–3.4 after the restart (duty 0–9.5 %).
    10-27's figure was 12.3/min at cruise. The first read at or above 770
    came at 18:09:41.6Z and changed nothing, because the flag was already
    set.
  - **Firmware:** `FUN_0006a52c` sets 28B9.6 = `(w5224>>15 ≥ 0x302) OR
    ((w5224>>15) − w1584 > w14A0)` every call, with w14A0 from table 0xDE4A on
    w3930 (while 90E4) and w1584 a frozen copy of the accumulator. The second
    route held it. w1584, w14A0 and w3930 were not logged.

## 7. What the learned cells say about fuelling

Each cell is the ECM's own converged estimate of its fuel error there.
Negative means the ECM removes fuel. Learning runs only in closed loop with
28B9.6 set. At gain 26 (~10 % per update), a cell has absorbed ~96 % of a
step error after 30 updates, so cells with ≥ 30 visits are treated as
converged.

- **Count:** 21 cells per bank (rows 0–4, < 4,400 rpm; no cell at BFS
  ≥ 36,864). Nothing above 4,400 rpm or at the WOT end of BFS has been learned,
  so open-loop WOT gets no learned correction.
- **Converged cells:**
  - B1, 11 cells: mean −3.33 %, SD 1.95 %, range −6.32..−0.67 %;
  - B2, 12 cells: mean −2.75 %, SD 2.47 %, range −6.00..+2.61 %;
  - visit-weighted over all visited cells: −3.08 % (B1), −2.90 % (B2);
  - median of visited cells: −1.64 % and −1.14 %.
- **Shape (rows 2–3, 1,800–3,400 rpm):** the correction rises with load in
  both banks.

  | Column | g/s at 2,200–3,000 rpm | B1 | B2 |
  |---|---|---|---|
  | c1 | 9.5–25 | −6.32, −6.21 | −4.61, −5.30 |
  | c2 | 18–39 | −2.62, −4.70 | −5.15, −3.02 |
  | c3 | 29–54 | −4.78, −3.71 | −6.00, −2.87 |
  | c4 | 40–69 | −1.68, −1.98 | −3.41, −0.29 |
  | c5 | 50–79 | −2.62, −0.67 | −2.88, +2.61 |
  | c6 | 58–89 | +0.33 (4), −0.58 (13) | +0.18 (3), +1.54 (13) |

  - The banks agree in shape: r = 0.73 over the 11 shared converged cells.
    B2 − B1 has a median of +0.84 % and ranges from −2.5 to +3.3 %.
  - **It is not one global offset.** The spread across converged cells (SD
    2.0–2.5 %, range 5.7–8.6 %) is several times what a converged ~10 %-gain
    learner would wander from STFT jitter. That jitter estimate is ≲ 1 %:
    roughly 0.23 × the averaged-STFT SD. It is an estimate, not measured here.
- **Near idle**, the cells disagree with the cruise pattern:
  - r0c0: −0.35 % B1, −1.00 % B2;
  - r1c0 (< 6 g/s, creeping): +3.24 %, +2.34 %. Learned today, with purge
    duty 6–30 %.

  D03 (10-27, post-pump) found that while purging, the ECM cuts the pulse to
  0.898 at idle and 0.950 off idle. That is more than the vapour supplies, so
  STFT runs leaner with purge on (+2.7 / +3.5 %). These two cells most likely
  carry that purge-compensation error rather than a fuel-delivery one.
- **Compared with N02.7** (`2026-11-08-n02-7-fuel-comp-corpus.md`): sample
  median required factor 0.982 (−1.8 %), between-drive closed-loop offset
  −3.9..+1.6 %, error "nearly uniform across load" within a drive.
  - **The level agrees.** The learned map's visit-weighted −3 %, and its
    medians of −1.1..−1.6 %, sit inside N02.7's between-drive span, near its
    −1.8 %.
  - **The shape does not.** The ECM's per-cell estimate shows −5 to −6 % at
    light cruise, falling to −2..+2.6 % at 40–90 g/s. That is the direction the
    new pump's measured flow gain predicts: +4.7 % at 2–5 g/s down to +1.8 % at
    80–100 g/s (N02.7 §1). It is steeper, and offset more negative at light
    load.
  - **This is a qualitative comparison, not a same-instant one.** N02.7 bins
    on the Fuel Comp alpha-N input, not BFS, and its post-pump data is
    idle-heavy. The learned cells come from unrecorded driving, whose purge,
    fuel level and temperatures are unknown.
- **What it means:** the ECM now corrects the cruise cells itself, and the
  correction it has settled on is cell-shaped. A single global factor
  (0x9254 or K) would leave a ±3 % load-shaped residual for the trims to
  carry. Any ROM change made from these cells is an operator decision and an
  explicit per-flash permission, so nothing here proposes one.

## 8. Open questions

1. Where were the 746/790 pre-capture off-idle visits learned: the unrecorded
   17:26–18:08Z part of this key cycle, or earlier key cycles after 10-27?
   The 1435/1436 match says r0c0's visits were all this key cycle's.
2. What route 2 of 28B9.6 needs. Add w1584, w14A0, w3930 and 90E4 to
   `ltft-gate` (a profile change, not made here) and read when route 2 sets.
3. The warm-idle cell r0c1 has never learned. The idle path closes once idle
   purge flows (w38AA ≥ 40) and after 30 updates per key cycle. Whether any
   drive pattern teaches it is open.
4. `_capture-ltft-gate-2026-11-08T182054`. Re-run the script on it to see
   learning resume when 28B9.6 sets in a fresh key cycle, which is the
   accumulator fill from 0 that G4 is about.
5. The idle path's purge compare (w38AA < 40, cal 0x86D4) comes from the 10-27
   trace and was not re-derived here.

## Reproduce

```
cd /home/dan/code/canary
.venv/bin/python ~/canary-data/analysis-scripts/2026-11-08-ltft/ltft_learning.py \
    ~/canary-data/sessions/nissan-xterra-2008/<session>
.venv/bin/python ~/canary-data/analysis-scripts/2026-11-08-ltft/tables.py \
    ~/canary-data/sessions/nissan-xterra-2008/<session>
```

The script asserts the ROM axis and gain bytes, and it re-decodes every
window anchor from its payload. A mismatch would print.
