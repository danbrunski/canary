# Prediction: restoring Fuel Comp to UpRev base (ZS44A-Tuned-20261004)

Recorded 2026-10-04, **before** the flash, so it can count as a prediction
(ECU second brain, capability 5).

## The intervention

`Tuned ROMs/ZS44A-Tuned-20261004.OsirisROM`:
- container sha256 `33d5d7a5e33ccddc4747821984973716e8902bd0d5c5f26f79c109cfbe26b858`;
- decoded image sha256 `5587f7e1e6dbd0fcb9ffe0c857cc0cafebbe11bf306636c0f3265b903254d253`.

Changes against ZS44A-20260920 (218 bytes):
- the Fuel Comp **load** axis `0xCB29` is restored from 16…144 to UpRev base
  16…255 (UpRev labels it "RPM", 800–12,750);
- slot 1's Fuel Comp cells `+0x660–0x75F` are restored to UpRev base;
- both checksum words.

Unchanged: K (28,715), the Load Point Scaler, Target AFR, timing trims and
slots 2–5. The axis roles are verified from code; see
`docs/ecu-firmware-analysis.md`, the `+0x660` row of "Map-slot tables".

## Predicted effect

Fuel Comp is a multiplier, 128 = 1.000. This compares the new file with
20260920 at the 2026-10-04 drive's operating points, with bilinear lookup on
the code's axes.

| Load byte `0xFFFF118A` | rpm | Predicted fuel change |
|---|---|---|
| < 64 (45 % of samples) | any | about −1 % (leaner) |
| 64–144 | any | −0.2 to −0.5 % |
| ≥ 144 | 4,000–5,600 | **+3 to +5.5 %** (richer). Base 1.047–1.078 vs tune ~1.016–1.023. |

What should be observable, if Fuel Comp is the cause:
- **WOT, open loop, load above 144:** measured λ moves richer by about the
  amounts above. The +2–4 % lean found 2026-09-21..23 shrinks with load
  above 144, and doesn't change below it.
- **Part throttle, closed loop:** the commanded fuel at the same rpm × load
  is about 1 % lower at low load, so STFT should move about +1 % toward
  adding fuel. That is the direction that reduces the reported rich trims.
- **Nothing else:** Target AFR, timing and K are untouched.

What would refute it:
- no change at high load beyond the noise floor for those conditions;
- a change that doesn't scale with load above 144;
- changes anywhere Fuel Comp didn't change.

Caveats:
- The flash resets LTFT. Compare STFT and λ, not total trim, until LTFT
  relearns.
- Compare matched rpm × load bins only, not drive averages.
- The load byte's exact meaning is still open; the band boundaries are in
  its raw counts.

## How it is checked

1. Raw CAN capture of the flash: only the calibration sector
   (`0x008000–0x01FFFF`) and the map-slot sector (`0x0E0000–0x0FFFFF`) are
   erased and written.
2. ECU read-back with the W08 backup: the image sha256 must equal
   `5587f7e1…d253`.
3. The next drives: WOT pulls across rpm plus steady cruise, logging the
   load byte, rpm, BFS, injector pulse, commanded φ, both A/F paths and STFT.
   Compare against the 2026-10-04 and 09-21..23 baselines.

## Addendum, after the flash (2026-10-04 evening)

Added after the flash and the read-back, and before any drive on
ZS44A-20261004. **Everything above is unchanged** and remains the
prediction of record. This section records a mechanism learned afterwards
that changes how the result must be read. Source: the decompiler and listing
passes in `docs/ecu-firmware-analysis.md` ("how BFS is formed").

- **Fuel Comp is applied inside BFS**, before the manifold filter:
  `BFS = min(filter(K·Qa/rpm × FC/128) + transient, D8B3[rpm])`. "Nothing
  else: Target AFR, timing and K are untouched" is true of the tables, not
  of their lookups. Where Fuel Comp changed, these move by the same factor:
  - BFS (DID `0x1208`);
  - the Target AFR X byte (`0xFFFF103E`, `floor(BFS·128/44019)`);
  - the timing-trim BFS-X axis.
  Commanded φ can therefore also change at high load, wherever the shifted X
  crosses a Target AFR breakpoint. At X ≥ the last breakpoint (BFS ≥
  21.57 ms) the column is clamped and it cannot.
- **Reading the refutation criteria:** a shift in BFS, the X trace or the
  timing cell, in the load-byte bands where Fuel Comp changed, is part of
  the predicted effect. It is not "a change anywhere Fuel Comp didn't
  change". Outside those bands the criterion stands.
- **The fuel-change table stands** for steady state: injected fuel scales
  with BFS, so by the Fuel Comp ratio. Add any commanded-φ step from the
  point above.
- **The load byte is now decoded:** a throttle-model relative fill (throttle
  area over rpm), not airflow. The bands (< 64, 64–144, ≥ 144) are in its raw
  counts as before. The full-throttle cases above 4,000 rpm sit at about
  207–217.
- **Behavior models:** the shipped air-charge surface models DID `0x1208`.
  It was fitted on 20260920, and BFS now includes a different Fuel Comp at
  high load. Refit it on 20261004 drives before trusting its high-load
  residuals.
