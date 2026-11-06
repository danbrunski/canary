# What an `.OsirisROM` holds, for CANary

An `.OsirisROM` carries the ECU's 1 MiB flash image together with UpRev's own
data about the tune. CANary works on the image and keeps the rest: a
candidate written by `scripts/rom_targets.py apply` leaves everything in the
file outside the edited cells and the image's checksum words exactly as it
was. Loading, writing and the image checksums are in
`docs/uprev-rom-codec-decoded.md`.

## Known answers for the ROM map

Two edits the operator made and saved in UpRev pin table positions in the
image. They test the ROM map (`data/nissan-xterra-2008/rom_map.yaml`), not the
file format:

- **Map 1 Fuel Compensation**, row 7200 rpm, column 50.00 % Volumetric Flow
  Ratio, 103.13 → 102.34: raw 132 → 131 at image offset `0xE075F`, displayed
  as `100 × raw / 128`. Saved as `ZS44A-Tuned-Map1Change.OsirisROM` against
  `ZS44A-Tuned.OsirisROM`, and reverted afterwards. Besides the cell, only
  the two checksum words changed.
- **Map 1 Fuel Targets**, 5200 rpm / BFS 18.20 ms: image offset `0xE025C`,
  the cell the 2026-09-20 tune changed (`docs/skunkworks/uprev-flash-20260920.md`).

The full Fuel Targets table, matched against an independent transcription,
is in `docs/uprev-rom-table-verification.md`.
