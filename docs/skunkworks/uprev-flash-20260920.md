# The 2026-09-20 flash (revision ZS44A-20260920)

The operator flashed `ZS44A-Tuned-20260920.OsirisROM` with UpRev on
2026-09-20. CANary records it as revision `ZS44A-20260920`, running from
2026-09-21T03:08:45.954035Z (`data/nissan-xterra-2008/tune_revisions.yaml`).
Sessions that started earlier keep the previous revision, `ZS44A-k106`, and
a session spanning the flash must be split before comparison.

## What changed

Against the previous tune (`ZS44A-Tuned.OsirisROM`) the image differs in
exactly three bytes:

| Image offset | Old | New | What |
|---|---|---|---|
| `0x0E025C` | `A2` | `A3` | Map 1 Fuel Targets, row 7 (5200 rpm), column 4 (BFS 18.20 ms): raw 162 → 163, AFR 11.61 → 11.54 |
| `0x009598` | `0C` | `0D` | High byte of the XOR checksum word (now `0D3BBAC0`) |
| `0x0095A0` | `F4` | `F5` | High byte of the sum checksum word (now `F538FCAA`) |

`AFR = 14.7 × 128 / raw`, so a larger raw count asks for a richer mixture.
The earlier Fuel Compensation test cell at `0xE075F` is unchanged.

| | sha256 |
|---|---|
| Flashed file `ZS44A-Tuned-20260920.OsirisROM` | `03d00051a59b86251ac0d5d9cf2ac9651682cfcba978edce81f07371f1c128a2` |
| Its image (1,048,576 bytes, CRC32 `95b993c5`) | `a46baa248f16db8d5ed9854913e614dd7d77b7579bb6c36bddb792741531be50` |
| Previous file `ZS44A-Tuned.OsirisROM` | `c5325776b08c3084edd49d8f68587dad4668e02b0e77c7686f7548fb6f7c8fa9` |

The flash read back from the ECU on 2026-10-04 is byte-identical to this
image (`uprev-backup-20261004.md`), so the ECU runs exactly what the file
holds. `scripts/rom_targets.py apply` reproduces the file byte for byte from
the previous one and the patch in
`data/nissan-xterra-2008/rom-targets-known-edit.json`
(`docs/offline-rom-targets.md`).

## Boundary

Flashing stays with UpRev. CANary has no flash path; bench read/write work is
under `ecu-rw-bench-research.md`, and any non-volatile write follows
AGENTS.md: explicit operator permission for that write, key on, engine off.
