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
| A list of axis addresses for UpRev's switchable tables | `0x0DE460` | Inferred | `0xCC83, 0xCC93, 0xC894, 0xCAA5, 0xCACD, 0xCB29, 0xE25E, 0xCAE9`. These include the Target AFR axes and both Fuel Comp axes (see "Map-slot tables"). |

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
| `0xFFFF103E = min(255, BFS × 128 / 44019)`, where BFS is the word at `0xFFFF103A` (the variable DID `0x1208` reports; it includes the Fuel Comp multiplier, see "how BFS is formed" below). One X count is 343.9 BFS counts, which is **0.1685 ms**. | Verified | The code, plus a capture: X equals floor(BFS × 128 / 44019) on 1,564 of 1,564 same-read rows, while control divisors fail. It matches P02.14's fitted 0.1695 ms per count. |
| **44019 is UpRev's "Load Point Scaler".** It sits at `0x9416` in the calibration block, and a copy sits in each map slot at `+0x760`, just before K. | Verified (value at both addresses); which copy the code reads is inferred to be `0x9416` | The bytes. Editing it moves every operating point along the Target AFR and timing load axes, which is the edit UpRev warns against doing by hand. |

Exact arithmetic (E02.4; read from the listing, implemented once in
`src/decode/ecm_lookup.py`): each search scans from the top breakpoint down for
the largest index with axis ≤ value. An equal value, a value past the top and
a value below the first breakpoint all give a fraction of 0. Otherwise the
fraction is `((value − axis[i]) << 16) / (axis[i+1] − axis[i])`, an unsigned
truncating divide. `0x026F10` computes `a + ((b − a) × frac >> 16)`, or
`a − ((a − b) × frac >> 16)` when b < a, so it truncates toward `a` in both
directions. `0x026F40` interpolates along the columns of row i and of row
i + 1, then between the two rows.

### Target AFR lookup to final command

Read from the listing for E02.4 on 2026-10-04. The twin profile
(`data/nissan-xterra-2008/target_phi_twin.yaml`) declares each item.

| What | Confidence | Evidence |
|---|---|---|
| After the lookup, while bit 6 of `0xFFFF283F` is set, the result's excess over 128 is scaled by the 8-entry curve at `0xD247` (÷ 128). That curve is all 128 in ZS44A-20260920, so the scaling is the identity there. | Verified (code and bytes); the curve's input `0xFFFF11CF` is not identified | `0x055E26`–`0x055E5A`. |
| The result reaches `0xFFFF174B` only when it is ≥ 130 (cal `0x8B39`), or ≥ 129 while bit 3 of `0xFFFF2886` is already set (release cal `0x8B3D` = 129). Otherwise `0x80` (φ 1.0) is written. A lookup of 129 is therefore ambiguous unless that bit is captured. | Verified (code) | `0x055E5C`–`0x055EEC`. |
| The commanded word `0xFFFF26BE` is written by `FUN_00055B5E`. It starts from min(0xFFFF, `w26F6` + `w27DC`) × `w26C0` >> 14. On most paths it then caps the word at `0xFE00` (cal `0x8B3B`) and limits the step against `w26CC`. It writes 0 while `0xFFFF26B8` = `0x3F` (fuel cut). | Verified (code); the meaning of the three words is unknown | `0x055B5E`–`0x055DA4`. |
| `w26C0` is the enrichment multiplier, and `0xFFFF174B` reaches the command through it. `FUN_00055DB2` writes it at `0x0568B4`, choosing by the state byte `0xFFFF14C4` among three values. One is `0x4000` (φ 1.0); in state 1, for example, or when a 0x8B3D-based test on `w26CE` fails. Another is a ramp against the previous `w26CE`. The third is `0xFFFF174B << 7`, the gated lookup, which also caps the word. | Verified (code); the meaning of each state value is unknown | Reads of `0xFFFF174B` at `0x0566F8`, `0x056810` and `0x05682A`, all in `FUN_00055DB2`. The other readers, `0x04CAF4` and `0x066028`, were not followed. |
| `w26F6` = clamp(`0x4000` + the long at `0xFFFF175C` + `w26E8` + `w1736`), and `w27DC` is a curve term clamped to 0…3,768. Both are additive corrections around 1.0. | Location verified (code); the terms are not identified | `0x05591C`–`0x055958` and `0x0559FA`. |

So the final word equals the lookup's word only under two conditions: the state
byte selects `0xFFFF174B`, and `w175C + w26E8 + w1736 + w27DC` = 0. Neither is
captured today. A 58-byte read from `0xFFFF26BE` covers the command word,
`w26C0` and `w26F6` in one window, which would settle both conditions per
sample. `w27DC` needs a second read.

So the lookup's output is not yet known to be the final command. On the
2026-10-04 drive, 149 of the 153 command reads were 16384 (φ 1.0), and the X
trace stayed at or below 100 counts. Most reads were below the first BFS column
(52), where the table itself reads 128. The drive therefore cannot tell
closed-loop holding from the table. Until that path is traced, the twin labels
its value a lookup prediction and issues no qualified residual
(`final_command_path_unverified`).

### Final-command path, traced (E05.1)

Read on 2026-10-04 from the listing and decompile text of the a46baa24… image
(`~/ecu-re/out`), for E02.4's desk trace. Every item is recorded node by node,
with widths, constants, branches and update order, in
`data/nissan-xterra-2008/ecu_application_model.yaml`. That file is the
source; this table is the summary.

| What | Confidence | Evidence |
|---|---|---|
| The fuel-command task `0x0575EC` runs, once per cycle: `0x05535C`, `0x055DB2`, [`0x055A04`], `0x0556EC`, `0x055536`, `0x053400`, `0x055386`, `0x05591C`, `0x055AB8`, `0x055966`, `0x055B5E`. The multiplier is written before every reader of it in the same cycle. | Verified (code) | Listing `0x0575EC`–`0x057668`. |
| The cycle-start copy `0x020EF2` makes `w26CC` the previous command and `w26CE` the previous `w26C0`. It also makes `0xFFFF1079` a copy of `0xFFFF1083` (the byte DID `0x1101` reports, coolant −50) and `0xFFFF11CA` a copy of `0xFFFF1202`, which is the rpm word `>> 2` (50 rpm per count). | Verified (code); coolant meaning inferred from the DID pointer table | Copy arguments at `0x020F14`–`0x020FCC`; `0x03907A`. |
| `0xFFFF14C5` is a requested state. It is 8 when the coolant byte is < 115, i.e. 65 °C (+3 hysteresis); 1 when the gated lookup is `0x80`; otherwise 2/4/6 below 3,100 rpm / below 3,400 rpm / above (200 rpm hysteresis), or 3/5/7 with the pedal flag set. `0xFFFF14C4` commits it through four tick-stepped timers (`0xFFFF1742`–`48`) and takes values 1–6. | Verified (code) | `0x055EF0`–`0x0566BC`. |
| `w26C0`: state 1 gives `0x4000`. States 4 and 5 (5 = cold) give the gated lookup `0xFFFF174B << 7` directly. States 2, 3 and 6 step from the previous value, up `+0x180` or down `−0x130` per tick (cal `0x8496`/`0x8495` × 8). Between 2,000 and 3,400 rpm, state 2 (and state 6 above `0x4500`) ramps down toward `0x4000`. Otherwise they start at `0x82 << 7` and ramp up, giving the lookup itself once `0xFFFF2867` bit 7 records that the ramp reached it. The result is always capped at the lookup, and forced to `0x4000` while `0xFFFF2947` ≠ 0 or `0xFFFF2BA9` < `0x20`. | Verified (code); `0xFFFF2947`'s decrement and `0xFFFF2BA9` not identified | `0x0566BE`–`0x0568B4`. |
| In states 2, 3 and 6, a previous multiplier below `0x4000` with bit 7 clear leaves `w26C0` from an uninitialised stack word, so the firmware's own value is indeterminate there. It is reachable only through the alternate writer below. | Verified (code) | The stack slot `r15+0xC` is written only at `0x056822`. |
| The alternate writer `0x055A04` (`w26C0` = clamp(`0xFFFF2BCE << 7`, `0x1000`..`0x6000`)) is not scheduled: its call needs cal `0x8AD3` bit 4 set. The floor `w1724` is 0 every cycle (cal `0x8D51` = 0). The extra cap at cal `0x93E2` is dead (cal `0x896E` bit 3 clear). All three conditions hold on every local ZS44A calibration. The model checks them on each image (`unmodelled_logic`). | Verified (code and bytes) | `0x057602`–`0x05762E`, `0x055AB8`–`0x055B4E`, `0x056892`. |
| `w27DC` is 0 unless `w26C0` is exactly `0x4000` and flags `0xFFFF135F`/`0xFFFF26B9` bits 0–1 are set and `0xFFFF2A90` is clear. So it is 0 whenever the multiplier is enriching. When it applies, it is the 16-bit curve `0xED3A` on axis `0xDFD2` at max(`0xFFFF2D10`, `0xFFFF2D14`), capped at 3,768. | Verified (code); the inputs are not identified | `FUN_00055966`. |
| In `w26F6`, three terms are added: `L175C` (signed, `0x053400`/`0x0537AE`), and `w26E8` and `w1736` (both non-negative, `0x055386` and `0x0556EC`). `L175C` and the second part of `w26E8` are zero under the same flag set: `0xFFFF26B9` bits, `0xFFFF2867` bit 6, or `0xFFFF1796` bits with `w26C0` > `0x4000`. `w1736` is gated by `0xFFFF2875` bit 6 and capped at `0x5E77 − w26C0`. | Verified (code); what each correction is for is not identified | Same functions. |
| `0x055B5E` applies the `0xFE00` cap and the ±`0x3FC0` per-cycle slew (against `w26CC`) only while the pedal flag is clear, and only under further conditions. So during pedal enrichment the word is just min(`0xFFFF`, `w26F6` + `w27DC`) × `w26C0` >> 14. After fuel cut (previous word 0), the guarded path limits the first word to `0x3FC0`. While `0xFFFF4785` bit 6 is set, the word is instead scaled by cal `0x838A` / 64 (64 on every local image: the identity) and skips the floor, cap and slew. | Verified (code) | `0x055B5E`–`0x055DA4`. |
| CAN DID `0x1134` reads `0xFFFF26C0`, so it is the multiplier's high byte, not the command's. That explains its catalog evidence: it matched the command on 5,000 reads with neutral corrections, and read 64 while PID `0x44` read 1.06 or 0.87. | Inferred: the pointer table is from code (`~/ecu-re/out/ram_vars.yaml`); not checked against a same-read `0xFFFF26C0` | `data/dids.yaml` 0x1134 evidence. |

So the enrichment command equals the lookup word when three things hold: the state is 4 or 5, or the ramp has finished; `w26F6` = `0x4000`; and `0xFFFF2947` is 0. `w27DC` is then necessarily 0. None of these is captured, so the path stays `final_command_path_unverified`.

The model's `equals_output_when` names each condition. The 58-byte read from `0xFFFF26BE` covers `w26C0`, `w26CC`, `w26CE` and `w26F6` in one read, which settles all three per sample.

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
| `+0x020` (`0xFFFFB060`) and `+0x120` (`0xFFFFB160`) | **Ignition timing trims**, two 16 × 16 tables. Their axes are rpm (`0xCC93`, raw × 50 = 400–6,000 rpm) × BFS-X counts (`0xCC83`, 8–128), and cells are read masked with `& 0x7F`. (Corrected 2026-10-04: an earlier version swapped the two axes. canary-60 found it from the wrapper's register use, and the axis contents agree.) A flag bit at `0xFFFF2842` chooses between them. | Verified location; the Main / High Det roles are inferred | The readers of both tables, and the flag gating. The `+0x09C` cells k106 changed are **inside** the `+0x020` table (cell `0x7C`), not a separate table. |
| `+0x220` (`0xFFFFB260`) | Target AFR, 8 × 8 | Verified | As above. |
| `+0x260` (`0xFFFFB2A0`) | A 16-bit-valued map on the axes `0xCAE9` (rpm-like) and `0xE25E` (16-bit). It is the most-read switchable map, with at least 21 readers. | Location verified; identity unknown | Candidates: cam phasing, or electronic throttle / torque. |
| `+0x660` (`0xFFFFB6A0`) | **Fuel Compensation**, 16 × 16 (`+0x660–0x75F`). Axes: **rpm = `0xCACD`** (raw × 50 = 400–6,400) and **load ("Volumetric Flow Ratio") = `0xCB29`**, compared bytewise with `0xFFFF118A`. **UpRev's editor labels them the other way round.** | Verified from code (2026-10-04) | Call site `0x02DC34–0x02DC46`: r4 = table `0xFFFFB6A0`, r5 = `0xCACD`, r6 = `0xCB29`, r7 = engine-speed word `0xFFFF1210` (the PID 0x0C rpm word), stack byte = `0xFFFF118A`. Wrapper `0x026A24` searches r5's axis with r7 through `0x027194` (axis byte `shll2`, compared with the word: the rpm search), and r6's axis with the byte through `0x027114` (a direct byte compare: the load search), then interpolates through `0x026F40`. It is the Target AFR wrapper `0x0269AC` instruction for instruction, apart from the table size, and that mapping is verified on live data (W05.1). Stock `0xCB29` runs 16…255, the full range of a load byte (12,750 "rpm" in UpRev's display). **The k105/k106 "rpm axis 800–12,750 → 800–7,200" edit compressed the load axis to 16–144**: 25 % of the 2026-10-04 drive's samples (up to 218) read the last column. **ZS44A-20261004 restored the axis to 16…255** and slot-1 cells to base (flashed 2026-10-04 23:02Z; read back byte-exact). |
| `+0x760` | Load Point Scaler copy (44019) | Verified (bytes) | See above. |
| `+0x762` (`0xFFFFB7A2`) | **K**, big-endian 16-bit, multiplied into the air-charge accumulator: Tp = K · Qa / Ne. | Verified | The read at `0x03C6E0` inside `0x03C4D0`. It holds 28,715 in all five slots, and the base value 27,089 is in the ratio 1.0600. |

UpRev's axis list at `0x0DE460` resolves to these four switchable maps, as axis pairs:
- `{0xCC83, 0xCC93}` timing trims (`0xCC93` rpm, `0xCC83` BFS-X);
- `{0xC894, 0xCAA5}` Target AFR;
- `{0xCACD, 0xCB29}` Fuel Comp (probably `0xCACD` rpm, `0xCB29` VFR; see above);
- `{0xCAE9, 0xE25E}` the `+0x260` map.

### UpRev's Fuel Comp display, checked against memory

The operator pasted UpRev's Map 1 Fuel Comp grid for the corrected
`ZS44A-Tuned-20261004.OsirisROM`. It equals the decoded table at
`0xE0660` **row-major, 256/256**; transposed, only 60/256 match.

So UpRev's rows are the ECM's rpm rows and its columns are the load
columns, and the cell values are where the code reads them. What UpRev gets
wrong is which breakpoint list belongs to which axis:
- the row axis, named "RPM", shows `0xCB29` × 50 (800–12,750);
- the column axis, named "VFR %", shows `0xCACD` / 256 (3.13–50.00 %).

The true values follow by swapping the two lists: row *i*'s rpm = column
*i*'s "VFR %" × 128, and column *j*'s load = row *j*'s "RPM" ÷ 128 (as VFR %).

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
| Fuel Comp x input at `0xFFFF118A` is a load-like quantity | Tracks BFS (r = 0.98 against DID `0x1208`, 0.89 against the RAM word). It is not a fixed scaling: the per-sample ratio to BFS/256 spreads from 1.43 to 2.85, and a straight-line fit has a +28 intercept. | **Not air: a throttle-model (alpha-N) relative fill** (decoded below). Throttle area over rpm, through a saturating curve, which is why its ratio to BFS is not fixed. On steady points it tracks throttle/rpm (r = 0.92), not MAF/rpm (r = 0.36). Fuel Comp, indexed on it, is applied *inside* BFS. |

### Fuel Comp load input `0xFFFF118A`, and how BFS is formed

Two independent passes on 2026-10-04, reconciled:
- a listing-only trace of `out/full.asm` (canary-60), with a steady-point data check;
- a Ghidra decompiler pass on the same image.

They agree except where marked. Before decompiling, the project was repaired:
- the flash block had been marked writable, so literal pools were not being
  propagated, and 37,629 instructions sat outside any function;
- after the repair there are 2,532 functions, and the routine called
  `FUN_0003c4d0` is split at `0x3c62c` and `0x3cc3c`.

**MCU:** SH7058 family, high confidence. The A/D helper `0x2EDBC` uses ADCSR/ADCR at
`0xFFFFF818/38/58` with 10-bit results, and the flash is 1 MiB.

**The load byte.** `FUN_0003c3a4` is its only writer:

    X = w116A × (w1124/512 + w139E/2048)       (0x3c444, clamped to 0xFFFF; copy at 0xFFFF2136)
      = 2229 × (A + purge/4) / rpm             (rpm in rpm; 0xDC1D00 × 12.5 / (512 × 158) = 2229.0)
    0xFFFF118A = curve(X) >> 8                 (u16 tables 0xEAEC below X = 0x3C00, 0xEACC above)

| Term | What it is | Confidence |
|---|---|---|
| `w116A = 0xDC1D00 / clamp16(158 × rpm word)` | 1/rpm. The product saturates at about 5,185 rpm; above that X stops falling with rpm. 158 always multiplies an rpm word; 158 ≈ 3,954 cc / 25 is speculation, and `0xDC1D00` has no clean breakdown. | Verified (arithmetic); meaning open |
| `A = w2C32 = 0xDD94[4 × w2CC8] − w63FC`, floored at 0 | **Effective throttle opening area.** Table `0xDD94` has 32 rising u16 breakpoints at ROM `0xEE6A` (GBR `0xEDCA` + `0xA0`), input 4 × counts above closed (0–3,172, which is 0–97 %), output 112 → 48,060, convex. The electronic-throttle code (`0x7248c`, `0x706de`) runs the same pair in reverse to turn a target area into a target position. Relative units only. | Verified |
| `w2CC8` | **Throttle sensor A**: A/D channel 6 → `w2B0C` (DID `0x120F`) minus the learned closed position `w4C72` → `w2CCE`. Mode 01 PID `0x11` = `w2CCE × 100/321` (8.19 counts per %). Sensor B (channel 19, inverted, minus `w4C74` → `w2CD0`, DID `0x1210`, PID `0x47`) replaces it only when sensor A's fault items are set and B is healthy (`0xFFFF28B5` bits 7:6, set by `0x72528`). | Verified |
| `w63FC = min(w3CB2 + w5EA6, 1185)` | Idle-speed feedback integrator plus a non-volatile learned value (with an inverted backup copy): idle air learning, in area units. The air model subtracts it; the throttle target adds it. | Structure verified; name inferred |
| `w139E` | **EVAP purge**, from `0x6a52c`/`0x6e224`: `w139E = ((b − 1) × 144) >> 7`, where `b` is the purge byte at `0xFFFF272E`. That byte is the raw command + 1 while purging and 0 when off. Cal `0x82FE` = 144, so the gain is 1.125. PID `0x2E` reports `min(255, b × 1275/1000)`, in 0.5 % steps. `w139E` is added in X as purge/4 and subtracted from the throttle target area. | Verified (line-checked against the listing) |
| `w1124 = 782` (cal `0x90BE`) | Electronic-throttle limp-home area, used when bit 2 of `0xFFFF484E` is set. That bit is set by `0x8e58c` from the TPS1, TPS2 and correlation diagnostic items (`0xDF`–`0xE3`, plus unidentified item `0x11C`). Not a MAF fault. Those items most likely map to P0122/P0123/P0222/P0223/P2135, since the order matches the table at `0x10D40`. | Bit and items verified; DTC names inferred |

So `0xFFFF118A` is a **throttle-model ("alpha-N") relative fill**: throttle
effective area, minus learned idle air, plus purge area, over roughly
displacement × rpm, through a saturating curve. It reads 0–224 in relative
units, and has no MAF, barometric or temperature input. At full throttle it
reaches 224 only below about 1,660 rpm. The 0x3C00 split is a resolution
change, not a knee. UpRev's "Volumetric Flow Ratio" % (raw × 100/256) is a
display convention. CANary shows the axis as "Throttle-model fill (alpha-N)",
% of the 224 ceiling (raw × 100/224; `d7802d5`). From UpRev's editor, divide
the swapped "RPM" label by 112 to get that %, or by 50 to get raw counts.

**Where Fuel Comp acts, and how BFS is formed** (decompiler, verified; the
listing pass reached the same multiply):

    Tp     = K (0xFFFFB7A2) × Qa / rpm         or the cranking coolant curve 0xEEBA
    FC     = Fuel Comp[rpm, 0xFFFF118A] → 0xFFFF225A (128 = 1.0)     read at 0x3d0f4
    w105C  = Tp × FC / 128                     (0x3c7b4, unconditional; enabled by cal 0x8AE1 bit 5)
    w20A6  = (4·w105C·a + previous·(1 − a)) >> 15                    manifold-filling filter
    BFS    = 0xFFFF103A = min(w20A4 + transient w105A, D8B3[rpm])    DID 0x1208

- **Fuel Comp is inside BFS.** Editing it moves BFS (DID `0x1208`). It also
  moves the Target AFR X byte (`0xFFFF103E`) and the timing-trim BFS-X axis,
  exactly as K does. A Fuel Comp edit is not confined to fuel quantity.
- **BFS has an rpm-dependent ceiling**, table `D8B3`.
- **A MAF fault** (item `0x114`) switches BFS to table `0x95E5`.
- The `0xFFFF2CB6` path is the alternative behind cal `0x8AE1` bit 5. It is
  dead in this calibration. `0x2d5f4` only seeds these values at init.

**Other readers of `0xFFFF118A`:**
- the 8×8 manifold-filling time-constant tables `0x9B99`/`0x9BD9` (rpm × load byte);
- a transient flag (`|Δ over 5 cycles| ≥ 11` sets `0xFFFF283C` bit 2);
- a near-full-throttle flag (set at ≥ 202, cleared below 200) that gates the BFS blend;
- a learning loop at `0x39e40` stepping `0xFFFF4C58` by the sign of `BFS × 51/8192 − 0xFFFF118A` (purpose unknown);
- curve `0xCEB3`;
- EVAP purge scheduling, through a copy at `0xFFFF10FE`.

`0xFFFF2136`, `0xFFFF114A` and `0xFFFF2C30` have no code readers.

**Data check (listing pass):** the reimplemented chain reproduces the logged
byte at steady points of the ram-study capture. The median ratio is 1.000 at
idle (n = 38) and 1.027 off idle (n = 22), and wrong x-scalings and rpm
powers miss. Against BFS, `byte / (BFS × 51/8192)` is about 1.02 near full
load at 81 kPa and about 1.4 at idle and cruise.

**Catalog consequences** (owned by the data files):
- DID `0x1110` is commanded EVAP purge, the same byte as PID `0x2E`, not intake cam duty.
- DIDs `0x120F`/`0x1210` are 4.883 mV per count, so a 0.005 V scale is 2.4 % high.
- PID `0x11` → `0xFFFF2CCE` is verified, and PID `0x47` is sensor B.
- An earlier version of this section said cal byte `0x8ACF` inverts the fault
  test, and that `w1124` is MAF airflow. Both were wrong.

An example once quoted as contradicting the chain (byte 51 at 3,684 rpm
and "18 % throttle") came from ±2.5 s window means. At its exact instant
the throttle reads were 1.0 and 1.75 s away and rpm was falling. The chain
predicts 107–112 there, so it was a transient and is not evidence either
way. To test the whole chain on one instant, read
`0xFFFF2CC8, 2C32, 63FC, 139E, 116A, 2136, 118A, 225A, 105C, 103A` in one
memory window.

**Open:**
- the absolute units of the area and of the byte;
- `0xDC1D00`;
- diagnostic item `0x11C`;
- the purpose of `0xFFFF4C58`.

### Intake cam timing (VTC): controller, duty scale and banks

Decompiler pass, 2026-10-04. The catalog side is D03.37 (landed 2026-10-05:
`ECM_INTAKE_CAM_ANGLE_B1`/`_B2`, `ECM_INTAKE_VTC_SOLENOID_DUTY_B1`/`_B2`).

- **The DID bytes are a snapshot.** Each cycle, `0x20EF2` copies `0x8C` bytes
  from `0xFFFF2792` to `0xFFFF2706`, so CAN DIDs `0x1138`/`0x1139`/`0x1135`/`0x1163`
  (`0xFFFF278C`–`278F`) mirror `0xFFFF2818`–`281B`. The K-line table reads
  `0xFFFF281A` and `0xFFFF2700` directly.
- **Controller, verified.** It is a PID per bank, run from task `0xC4D98`:
  - error = target `0xFFFF2700` − measured angle, in 0.5° counts;
  - P: Kp from an 8×8 rpm × coolant map (`0x9C59` bank 1, `0x9C99` bank 2),
    active when |e| > 4;
  - I: clamped ±128 and held in RAM, so nothing is learned across key cycles;
  - D: none (the Kd tables are all zero);
  - a ±12 kick when a settled cam drifts;
  - feed-forward holding duty from coolant tables `0xDA2F`/`0xDA37`.
  - Output (`0xC641A`): `duty = (hold + PID·128/68) × 150 / Vbatt byte`, capped
    at 255.
- **Duty scale, verified.** `0xC66C4` writes timer channel 7 compare
  registers (`0xFFFFF592` bank 1, `0xFFFFF590` bank 2) as `raw·33·39/80`,
  against a period of 4,125 counts. So **duty = raw × 0.39 %**: a warm hold
  of about 99 is about 39 %. PWM is about 303 Hz, assuming a 20 MHz
  peripheral clock.
- **Banks, verified from the DTC item table at `0x102D0`.**
  - Index 0 = Bank 1: P0011/P0075, cam input `0xFFFFF44E`, `0x1138` duty and
    `0x1135` angle.
  - Index 1 = Bank 2: P0021/P0081, `0xFFFFF44C`, `0x1139` and `0x1163`.
  - Whether Nissan's Bank 1 is physically the cylinder 1/3/5 side is inferred.
    To check it, unplug that side's solenoid at key-on engine-off; the circuit
    test should then set P0075, not P0081.
- **Angles, verified.** `raw × 0.5 − 64` °CA of advance from the learned
  retard stop (128 = the stop). `0xFFFF26FC` is a copy of the target. The
  warm target map `0xA579` tops out at +35°.
- **Duty forced to 0 when any of these hold:**
  - within 30 ticks of start;
  - a crank or cam sensor fault, or P0011/P0021 this trip;
  - the Vbatt byte outside 125–200;
  - coolant above 120 °C, or below 56 °C on a cold start;
  - rpm below 1,200 (off below 1,000);
  - the target at the stop.
  Above a 38° target it runs open loop at full duty for about 1 s.
- **Open:** the task rate (needed to state the I gain per second), the Vbatt
  byte scale, and a scope check of the PWM period.

## Open

- **Catalog:** apply the Mode 22 corrections to `data/dids.yaml`, with the
  known answers.
- **Active map:** `mem:ECM_ACTIVE_MAP` is catalogued and read 0 all drive
  (2026-10-04). Still to see it change: the selector stepping when the map
  switch is used, which is the operator's call. Also to check: the live
  Target AFR copy at `0xFFFFB260` equals that slot's flash table.
- **ROM map data:** move the confirmed slot tables (timing trims, Fuel
  Comp, K, Load Point Scaler) into `rom_map.yaml`. (`rom_trace`'s BFS-axis
  refusal is lifted: W05.1, `rom_viewer.yaml`.)
- **Unidentified tables:** the `+0x260` map, the remaining 1-D curve
  inputs, and the flash-resident 2-D maps.
