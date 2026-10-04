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
| The inputs are compared with the axes' raw bytes, so the X trace is in BFS axis counts (0.1685 ms per count) | — | Verified | See "Target AFR lookup, decoded" below. |
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

### Target AFR lookup, decoded (settles W05.1)

The lookup at `0x0269AC` calls three helpers:
- `0x027194`, the rpm-axis search;
- `0x027114`, the load-axis search;
- `0x026F40`, bilinear interpolation, with cell interpolation at `0x026F10` and division at `0x027448`.

| What | Confidence | Evidence |
|---|---|---|
| The load byte at `0xFFFF103E` is compared **directly** with the raw BFS axis bytes (`0x34…0x80` at `0xC894`), with no scale or offset. | Verified | The search at `0x027114`. |
| The rpm word at `0xFFFF11D8` counts 12.5 rpm per unit and is compared with `axis byte << 2`, so an axis byte × 4 × 12.5 = axis × 50 rpm. | Verified | The search at `0x027194`, which agrees with the rpm axis decode (raw × 50). |
| Interpolation is bilinear over 8 rpm rows × 8 BFS columns, row by row. Past either end it **clamps** to the first or last row and column. | Verified | `0x026F40`. The clamp is the edge behaviour P02.15 found from UpRev logs. |
| `0xFFFF103E = min(255, BFS × 128 / 44019)`, where BFS is the word at `0xFFFF103A` (the variable DID `0x1208` reports). One X count is 343.9 BFS counts, which is **0.1685 ms**. | Verified | The code, plus a capture: X equals floor(BFS × 128 / 44019) on 1,564 of 1,564 same-read rows, while control divisors fail. It matches P02.14's fitted 0.1695 ms per count. |
| **44019 is UpRev's "Load Point Scaler".** It sits at `0x9416` in the calibration block, and a copy sits in each map slot at `+0x760`, just before K. | Verified (value at both addresses); which copy the code reads is inferred to be `0x9416` | The bytes. Editing it moves every operating point along the Target AFR and timing load axes, which is the edit UpRev warns against doing by hand. |

### Diagnostic service handlers

| What | Where | Confidence |
|---|---|---|
| Service dispatcher. It keeps the request's service byte at RAM `0xFFFF8003` and the response byte (service + 0x40) at `0xFFFF810A`; transmit is at `0x02FB7C` and negative responses at `0x034F34`. | `0x030714` | Verified |
| Mode 01 handler. Its support mask at `0xC85C` equals the 41 PIDs the 2026-07-22 probe found. Handlers exist for PID `0x0B` (MAP) and `0x43` (absolute load), but the mask leaves them off, so they never answer. | `0x0D3AFC` | Verified |
| Mode 22 DID → RAM pointer tables, **separate for K-line and CAN**. The same DID number can point at different variables on each transport. | K-line `0x022738`, `0x022938`, `0x022AB4`; CAN `0x024988`, `0x024B84`, `0x024D00` | Verified |

RAM anchors, each tied to a Mode 01 PID or a CANary channel:

| RAM address | Quantity | Tied to |
|---|---|---|
| `0xFFFF4706` | coolant | PID 0x05 |
| `0xFFFF26B4` / `0xFFFF26B6` | STFT B1 / B2 | PID 0x06 / 0x08 |
| `0xFFFF26DE` / `0xFFFF26E0` | LTFT B1 / B2 | PID 0x07 / 0x09 |
| `0xFFFF1210` | rpm | PID 0x0C |
| `0xFFFF11FF` | timing, as 110 − raw | PID 0x0E |
| `0xFFFF11A8` | vehicle speed | PID 0x0D |
| `0xFFFF26BE` | commanded φ | PID 0x44; the same word as `mem:ECM_TARGET_EQUIV_RATIO`, which ties that memory channel to commanded φ |

The full list is in `~/ecu-re/out/ram_vars.yaml`.

### Mode 22 catalog corrections (CAN)

The `data/dids.yaml` entries below came from UpRev's **K-line** stream definition, applied to the CAN DIDs. The firmware shows the transports have separate tables.

| DID | Catalog says | It actually is | Confidence |
|---|---|---|---|
| `0x1102` | `ECM_LONG_FUEL_TRIM_B1`, 2 bytes, % | **Vehicle speed**: the byte at `0xFFFF11A8`, which PID 0x0D also reads. One byte on CAN, km/h = raw × 2. | Verified: the firmware pointer, plus data. On `_capture-rom-trace-2026-09-21T142926`, DID 0x1102 tracks broadcast wheel speed with correlation 0.9997 over 202 pairs, and its per-sample ratio is steady (median 32.1, p10–p90 31.6–33.6). |
| `0x1125` | `ECM_LONG_FUEL_TRIM_B2` | **LTFT B1** (`0xFFFF26DE`, the PID 0x07 word) | Verified (firmware pointer equals the PID handler's) |
| `0x1126` | (not catalogued) | **LTFT B2** (`0xFFFF26E0`) | Verified (firmware) |
| `0x1123` / `0x1124` | (not catalogued) | **STFT B1 / B2** (`0xFFFF26B4` / `B6`) | Verified (firmware) |

Any earlier trim analysis that read DID `0x1102` as learned trim was reading road speed.

Applied to `data/dids.yaml` on 2026-10-04 (roadmap D03.36): `ECM_VEHICLE_SPEED_COARSE`, `ECM_SHORT_FUEL_TRIM_B1/B2`, `ECM_LONG_FUEL_TRIM_B1/B2`. The 0x1102 decode is pinned against wheel speed in the ground-truth suite. Evidence entries now carry `transport`, and the catalog refuses an entry whose only support is K-line.

### Map-slot tables, confirmed from the code that reads the RAM copy

| Slot offset (RAM) | Table | Confidence | Evidence |
|---|---|---|---|
| `+0x020` (`0xFFFFB060`) and `+0x120` (`0xFFFFB160`) | **Ignition timing trims**, two 16 × 16 tables. Their axes are BFS-X counts (`0xCC93`) × rpm (`0xCC83`), and cells are read masked with `& 0x7F`. A flag bit at `0xFFFF2842` chooses between them. | Verified location; the Main / High Det roles are inferred | The readers of both tables, and the flag gating. The `+0x09C` cells k106 changed are **inside** the `+0x020` table (cell `0x7C`), not a separate table. |
| `+0x220` (`0xFFFFB260`) | Target AFR, 8 × 8 | Verified | As above. |
| `+0x260` (`0xFFFFB2A0`) | A 16-bit-valued map on the axes `0xCAE9` (rpm-like) and `0xE25E` (16-bit). It is the most-read switchable map, with at least 21 readers. | Location verified; identity unknown | Candidates: cam phasing, or electronic throttle / torque. |
| `+0x660` (`0xFFFFB6A0`) | **Fuel Compensation**, 16 × 16 (`+0x660–0x75F`). Its x-axis is `0xCACD` (Volumetric Flow Ratio) and its rpm axis is `0xCB29`. | Verified | Readers at `0x02D5F4` and `0x03C4D0`. The base is `+0x660`; the diff only showed changed cells from about `+0x670`. |
| `+0x760` | Load Point Scaler copy (44019) | Verified (bytes) | See above. |
| `+0x762` (`0xFFFFB7A2`) | **K**, big-endian 16-bit, multiplied into the air-charge accumulator: Tp = K · Qa / Ne. | Verified | The read at `0x03C6E0` inside `0x03C4D0`. It holds 28,715 in all five slots, and the base value 27,089 is in the ratio 1.0600. |

UpRev's axis list at `0x0DE460` resolves to these four switchable maps, as axis pairs:
- `{0xCC83, 0xCC93}` timing trims;
- `{0xC894, 0xCAA5}` Target AFR;
- `{0xCACD, 0xCB29}` Fuel Comp;
- `{0xCAE9, 0xE25E}` the `+0x260` map.

### UpRev's map selector

| What | Confidence | Evidence |
|---|---|---|
| The map is chosen by a **discrete input**, not by a DID or a K-line command. Bits 1 and 3 of RAM byte `0xFFFF2EE0` are debounced through `0xFFFFB002`. On a transition the code increments the selector at `0xFFFFB003` and runs the slot copy. | Verified | `0x0DE358`, which runs periodically. |
| `0xFFFFB008` holds the committed active map, 1–5, and is safe to read. It reads **0 after a cold boot**, meaning "slot 1, loaded by default, not yet re-selected". | Verified (code) | The slot copy clamps the selector to 1–5 before committing. |

### Curves and other maps

| What | Confidence |
|---|---|
| 153 distinct 1-D byte curves are read through three routines: `0x026BA4` (8 entries), `0x026BBC` (16) and `0x026BD4` (16). Inputs: 4 strongly identified, 118 inferred, 35 unknown. Naming the rest needs tracing each caller. | Locations verified |
| About 30 flash-resident 2-D maps are enumerated with their axes and callers. Their identities are not yet resolved. | Locations verified |

Local outputs: `~/ecu-re/out/ram_vars.yaml`, `tables_1d.yaml`, `tables_2d.yaml`, `lookup_trace.txt`.

### Corrections to earlier notes

- `0x006000–0x007FFF` is not entirely erased: 48 bytes at `0x006EFE–0x006F2D` are in use. `rom_map.yaml` records this.

## Live verification: the 2026-10-04 drive

The firmware-located RAM channels were added to `data/dids.yaml` (commit
`dd189c2`) and logged on a drive. Capture
`_capture-ram-study-2026-10-04T204557`, 20:45:56–21:12:49Z, profile
`ram-study` (default polling plus the memory channels, DID/memory window
every 5 s, about 190 samples of each). Pairing used acquisition time.

| Prediction from the code | What the drive showed | Verdict |
|---|---|---|
| UpRev magic `0xAA5555AA` at `0xFFFFB004` | 196 of 196 reads = 2,857,719,210 = `0xAA5555AA` | **Confirmed** |
| `0xFFFFB008` reads 0 after a cold boot (slot 1 loaded by default) and changes only when the selector commits | 0 on all 196 reads. Selector `0xFFFFB003` was also 0 throughout. | **Confirmed** (no switch event occurred) |
| The map switch watches bits 1 and 3 of `0xFFFF2EE0` | Bits 1 and 3 never set. Bit 4 (16) was set on 148 of 186 reads, mostly off idle (median 2,441 rpm). Bit 5 (32) was set on 21 of 186, at idle (median 788 rpm, pedal released). | Consistent. Bits 4 and 5 are other inputs, still unidentified. |
| Pedal-switch flag: bit 0 of `0xFFFF2841` sets near full pedal and forces Target AFR's last load column | Set on 2 samples, at broadcast pedal 4.285 V and 4.582 V (full scale 4.58 V). Never set at or below 3.795 V, the highest pedal seen while it was clear. While set, the commanded φ RAM word read **exactly 1.1640625** (`0x95/128`, the last-column value) while the X trace sat mid-table (~100 counts). | **Confirmed live.** The threshold lies between 3.80 and 4.29 V, which fits the 2026-09-18 data figure of ≥ ~227/255. |
| Timing-trim select flag at `0xFFFF2842` | Constant 4 (bit 2) all drive | Steady; which value selects which trim is still unconfirmed |
| `X = floor(BFS × 128 / 44019)` | 168 of 194 same-read pairs exact. The rest are within ±1–4 counts, **both signs**, at transients. | Formula **confirmed**. The misses fit the two words being updated at different points in the ECM's loop, not a wrong formula. (P02.14's 1,564 of 1,564 came from a capture with steadier conditions.) |
| Fuel Comp x input at `0xFFFF118A` is a load-like quantity | Tracks BFS (r = 0.98 against DID `0x1208`, 0.89 against the RAM word). It is not a fixed scaling: the per-sample ratio to BFS/256 spreads from 1.43 to 2.85, and a straight-line fit has a +28 intercept. | Load-like, as the "Volumetric Flow Ratio" axis name says. Its exact definition is still open. |

## Open

- **Catalog:** apply the Mode 22 corrections to `data/dids.yaml`, with the
  known answers.
- **Active map:** `mem:ECM_ACTIVE_MAP` is catalogued and read 0 all drive
  (2026-10-04). Still to see it change: the selector stepping when the map
  switch is used, which is the operator's call. Also to check: the live
  Target AFR copy at `0xFFFFB260` equals that slot's flash table.
- **ROM map data:** move the confirmed slot tables (timing trims, Fuel
  Comp, K, Load Point Scaler) into `rom_map.yaml`, and lift `rom_trace`'s
  BFS-axis refusal (W05.1).
- **Unidentified tables:** the `+0x260` map, the remaining 1-D curve
  inputs, and the flash-resident 2-D maps.
