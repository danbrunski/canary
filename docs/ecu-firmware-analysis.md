# ECM firmware analysis (2008 Xterra, calibration ZS44A)

Started 2026-10-04 at the operator's request: disassemble the ECM's program to
understand what it computes. This file records **addresses and
interpretations**, each with its confidence and evidence. It never records the
disassembly or decompiled code: Nissan's program and UpRev's patch stay
outside Git, as decoded ROMs already do.

## Setup (local only)

- **Image:** the 1 MiB flash read from the ECU on 2026-10-04 (sha256
  `a46baa24…be50`). It is byte-identical to the decoded
  `ZS44A-Tuned-20260920.OsirisROM`. See `docs/skunkworks/uprev-backup-20261004.md`.
- **Tool:** Ghidra 12.1.4, processor `SuperH:BE:32:SH-2`, base address 0.
- **Assumed blocks:** RAM at `0xFFFF0000–0xFFFFBFFF` and I/O at
  `0xFFFFC000–` are added so references resolve. The exact MCU part is not
  established.
- **Entry points:** seeded from both vector tables, boot at `0x000000` and
  application at `0x020004`. Auto-analysis finds 2,139 functions.
- **Workspace:** the project, scripts and notes are under `~/ecu-re/` on the
  laptop: `ghidra/`, `scripts/`, `out/`, `NOTES.md`.

Confidence uses the same words as the ROM map. **Verified** means it is
read directly from the bytes or code, and agrees with an independent
observation. **Inferred** means strong evidence, not yet confirmed.

## Findings

### Target AFR lookup (Nissan's code)

| What | Where | Confidence | Evidence |
|---|---|---|---|
| The function that computes target AFR | `0x055DB2` | Verified | It calls the fuel-target lookup with the Target AFR table and both axis addresses (`0xCAA5` rpm, `0xC894` BFS) as constants. |
| The 2-D lookup used only for Target AFR | `0x0269AC` | Verified | It has one caller, `0x055DB2`. |
| Load-side input to the lookup | RAM byte `0xFFFF103E` | Verified | Passed straight into the lookup. This is CANary's `mem:ECM_FUEL_TARGET_MAP_X_TRACE`. |
| Rpm-side input to the lookup | RAM word `0xFFFF11D8` | Verified | Read as base `0xFFFF11C4` + `0x14`. This is CANary's `mem:ECM_ENGINE_SPEED_RAM`. |
| The inputs are compared with the axes' raw bytes, so the X trace is in BFS axis counts (≈0.1685 ms per count) | — | Inferred | The value is passed unscaled. P02.14 fitted UpRev's X trace at ≈0.1695 ms per count. Confirming means decoding the lookup's interpolation helpers (open below). |
| Pedal-switch enrichment: when a flag is set, the load input becomes `0xFF`, which is the last BFS column | `0x055DB2` | Verified (code); matches the 2026-09-18 data finding | The flag sets and clears with +8 counts of hysteresis on a value read from the 1-D table at `0xCEDB`. Its flag bit is bit 0 of `0xFFFF283F + 2`. |

### UpRev's map switching (UpRev's patch)

| What | Where | Confidence | Evidence |
|---|---|---|---|
| UpRev state block | RAM `0xFFFFB000` | Verified | Magic `0xAA5555AA` at `+4`, selector nibble at `+3`, current map number at `+8`, a history word at `+0xC`. |
| Slot copy routine: copies slot *n* (1–5) from `0x0E0000 + 0x800·(n−1)` to RAM `0xFFFFB040`, 2 KiB | `0x0DE3E4` | Verified | Literal values `0xFFFFB000`, `0x0E0000`, `0xFFFFB040` and stride `0x800`. |
| Start-up: if the magic is absent, initialise the block and copy slot 1 | `0x0DE1A4` | Verified | Same literals, plus a 0x200-word copy. It also writes `0x15`/`0x55`/`0x15` to 16-bit registers at `0xFFFFF73A`, whose meaning is unknown. |
| Nissan's Target AFR table pointer redirected to the RAM copy | literal `0x055EC4` = `0xFFFFB260` | Verified | `0xFFFFB040 + 0x220`. The calibration's own table at `0x00A169` is no longer referenced. |
| The active map number is readable as RAM byte `0xFFFFB008` | — | Inferred | It is the routine's own record of the selected slot. Not yet a CANary channel, so verify before adding one. |
| A list of axis addresses for UpRev's switchable tables | `0x0DE460` | Inferred | `0xCC83, 0xCC93, 0xC894, 0xCAA5, 0xCACD, 0xCB29, 0xE25E, 0xCAE9`. These include the Target AFR axes and the Fuel Comp rpm axis; the rest are not yet identified. |

### 1-D curves

| What | Where | Confidence | Evidence |
|---|---|---|---|
| 16-entry byte-curve lookup (returns entry 15 past the end) | `0x026BBC` | Verified | Decompiled. It has 71 call sites, each with a constant table address. |
| 8-entry byte-curve lookup (returns entry 7 past the end) | `0x026BA4` | Verified | Decompiled. It has 93 call sites. |
| About 120 distinct curves packed in the calibration block | `0x00CC1B–0x00D379` | Verified locations; names unknown | The call-site arguments. Each call shows which variable a curve is read with, and naming a curve means identifying that variable. |

## Open

- Decode the Target AFR lookup's interpolation helpers, to confirm the inputs
  are compared with raw axis bytes (W05.1's X axis).
- Find the Mode 01 and Mode 22 handlers. Their PID/DID → RAM tables name RAM
  variables by quantity, which are the anchors for naming every curve.
- Find the other table routines: Fuel Comp (16 rpm × N), timing trims, MAF
  scaling, cranking, and enumerate their call sites.
- Confirm the candidate K (`+0x762`) and timing cells (`+0x09C`) from the ROM map
  by finding the code that reads them in the RAM slot copy (`0xFFFFB040 +` offset).
