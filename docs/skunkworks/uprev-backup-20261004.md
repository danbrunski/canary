# Reading the ECU's flash, and UpRev backup files

## The ECU's flash reads as plain bytes

2026-10-04, read-only. The ECM hands out its whole flash as plain bytes over
standard UDS on CAN:

- DiagnosticSessionControl `10 85` once on `0x7E0` (positive response
  `50 85`),
- then, per block, `10 FB` and ReadMemoryByAddress
  `23 [BE32 address] [BE16 length]`, up to 63 bytes per read, across
  `0x000000–0x0FFFFF`, with no SecurityAccess. The ECM answers on `0x7E8`.

1 MiB took about 624 s (~1.7 KB/s) at one cycle every 35 ms. No UpRev
tooling is involved. This is the recipe CANary's own ROM backup uses
(`data/platforms/nissan-xterra-n50/rom_backup.yaml`), and it matches the
≤63-byte `0x23` windows CANary already uses for RAM channels. A whole-flash
read is still a read, so it needs neither of the non-volatile-write
conditions in AGENTS.md.

The image read that day, of tune ZS44A-20260920, has sha256 `a46baa24…be50`
and CRC32 `95b993c5`. It is byte-identical to the image loaded from
`ZS44A-Tuned-20260920.OsirisROM`.

## UpRev backup `.bin` files

CANary does not load UpRev's ROM backup `.bin` files (1,048,592 bytes). The
ROM library and `src.analysis.rom_image.load_image` refuse one by name; use
the `.OsirisROM` of the same tune, or back the ECU up from CANary.

The factory ZS04C image survives only inside the 2026-07-22 and 2026-07-26
UpRev backups. The ECU no longer holds it, so CANary has no copy of the
factory image.
