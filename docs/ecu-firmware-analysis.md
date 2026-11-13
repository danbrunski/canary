# ECM firmware analysis (2008 Xterra, calibration ZS44A)

Started 2026-10-04 at the operator's request: disassemble the ECM's program to
understand what it computes. This file records **addresses and
interpretations**, each with its confidence and evidence. It never records the
disassembly or decompiled code: the program stays outside Git, as ROM images
do.

## Setup (local only)

- **Image:** the 1 MiB flash read from the ECU on 2026-10-04 (sha256
  `a46baa24…be50`). It is byte-identical to the image in
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
| Full-throttle enrichment switch: when a flag is set, the load input becomes `0xFF`, which is the last BFS column | `0x055DB2` | Verified (code); matches the 2026-09-18 data finding | The flag is bit 0 of `0xFFFF2841`. It compares the ECM's **target throttle opening**, not the pedal, with a threshold; see "The enrichment switch tests target throttle" below. The name `mem:ECM_PEDAL_ENRICH_FLAGS` predates that. |

### Map slots (an UpRev tune)

| What | Where | Confidence | Evidence |
|---|---|---|---|
| The switchable tables run from a RAM copy of the loaded map slot: slot *n* (1–5) at `0x0E0000 + 0x800·(n−1)`, 2 KiB, copied to RAM `0xFFFFB040`. Slot 1 is loaded at start-up. | RAM `0xFFFFB040` | Verified | See "Map-slot tables" for what the copy holds. |
| Target AFR is read from the RAM copy, `0xFFFFB260` = `0xFFFFB040 + 0x220`. The calibration's own table at `0x00A169` is not referenced in an UpRev tune, so editing it changes nothing. | literal `0x055EC4` | Verified | The table-pointer literal. |
| The loaded map number is RAM byte `0xFFFFB008` (`mem:ECM_ACTIVE_MAP`) | — | Verified | See "Which map is loaded". |

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
| **44019 is UpRev's "Load Point Scaler".** It sits at `0x9416` in the calibration block, and a copy sits in each map slot at `+0x760`, just before K. | Verified (value at both addresses, and the copy read: both divides at `0x03CBE4`/`0x03CC00` load the word at `0x9416`; nothing references the slot copy's RAM `0xFFFFB7A0`, E05.2) | The bytes and the listing. Editing it moves every operating point along the Target AFR and timing load axes, which is the edit UpRev warns against doing by hand. |

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

### The enrichment switch tests target throttle, and first gear never reaches it

Read from the listing on 2026-11-11, after the ZS44A-20261111 drive showed
first-gear full-pedal pulls reading the load-indexed Fuel Targets cells and not
the last column (`data/analyses/2026-11-11-wot-target-lean-prediction.md`).

| What | Confidence | Evidence |
|---|---|---|
| The switch's input is the byte `0xFFFF3CDC` = min(255, `w2D28` × 20571 >> 16), i.e. `w2D28` × 0.3139. The flag sets when it reaches the threshold and clears 8 counts lower. | Verified (code) | `0x05535C` writes the byte; `0x055DC6`–`0x055DFA` compares it and adds 8 while the flag is set. |
| The threshold is the 16-entry curve `0xCEDB` read at rpm / 25 (`0xFFFF11CF`), also stored to `0xFFFF15F8`. It is **200 at every rpm** in ZS44A. | Verified (code and bytes) | Sixteen bytes of `0xC8`. |
| `w2D28` (`0xFFFF2D28`) is a throttle position in sensor counts above closed: the area-to-position curve (axis `0xDD94`, values `0xEE6A`, the pair the air model uses the other way round) applied to the driver-demand target area `0xFFFF1A64`, then `>> 2`. | Verified (code) | `0x07248C`. `0xFFFF1A64` is the area before the idle-air and purge terms are added (`0x0752C0`). |
| So the switch sets at a **target** opening of 638 counts and clears below 612. On PID `0x11`'s scale (counts × 100 / 321 of 255) that is 77.9 % and 74.7 %; a fully open throttle reads 88.6 % there (725 counts). | Derived from the three rows above | 200 / 0.3139 = 637.2. |
| The target area comes from a torque demand: `0x0756E0` caps the air-quantity word `0xFFFF1C36` at `0xFD50` (the curves `0xEB0C` × `0xEB2C`, both flat), converts it to area per swept volume through the curve pair `0xEEDA` / `0xEDCA` (or `0xEF1A` / `0xEE0A` from `0xFC00` up), and multiplies by rpm × 158 (cal `0x8D25`). | Verified (code) | `0x0756E0`–`0x075A04`. |
| **The `+0x260` slot map is the engine torque map**: torque as a function of rpm (rows, axis `0xCAE9`) and a normalised air quantity (columns, axis `0xE25E`, 0 to 65,535), 16 × 16 words biased by `0x8000`. `0x075A16` reads the RAM copy `0xFFFFB2A0` **backwards**: it interpolates the rpm row, then finds the air quantity at which that row gives the demanded torque `0xFFFF1C74`. The result is `0xFFFF1C36`. | Verified (code and bytes). The units are inferred: the last column peaks at 3,749 in row 80, which fits N·m × 10 and rpm ÷ 50 for an engine rated 381 N·m at 4,000 rpm. UpRev's editor calls it **Throttle A** ("X-Axis = Flow Potential; Y-Axis = Engine RPM", estimated torque; Tuning Guide Rev 8, p. 15). | Inverse read: `0x026D2C` with the map row as the axis and `0xE25E` as the values. The forward wrapper `0x026B14` gives the full-load torque (column `0xFFFF`) and the minimum torque (column 0) for the rpm, and the torque at a given air quantity (`0x0778E8`, `0x07BB3C`, `0x07BC58`). |
| `0xFFFF1C74` is the arbitrated torque demand: `0x07B57C` takes `0xFFFF1C76` and applies upper limits `0xFFFF3C36` (a torque-down request, `0x07A00C`), `0xFFFF37A2` (baro × rpm and intake-temperature table `0xB374`, all 1.0 here) and `0xFFFF3C4A` (the lowest of the rev-limit, overheat and speed-limiter words, `0x0726DC`), and the lower limit `0xFFFF473A` (the torque of the idle air, `0x07BB3C`). `0xFFFF3C6C` is `0x7FFF` outside cruise. | Verified (code) | `0x07B57C`, `0x0C254C`, `0x07BB3C`, `0x07BC58`. |
| `0xFFFF1C76` copies `0xFFFF2814`, and `0xFFFF2814` is written by `0x0726DC`: the driver demand `0xFFFF280E` (not below `0xFFFF3C80`), under the lowest of eight limit words (`0xFFFF3C60`, `3C62`, `3C64`, `3C66`, `3C6E`, `3C7E`, `3C86`, `3C8A`). The driver demand comes from `0xFFFF2812` through `0xFFFF2810` (`0x073068`: the lower of it and the rev-limit, overheat and speed-limiter words) and `0xFFFF280E` (`0x071A2E`). | Verified (code) | **`0x0726DC` was missing from the disassembly**: it is reached only through the task table entry at `0x021A20`, so no reference search found the store. Found by scanning the image for the store's opcode pattern, then disassembled read-only (2026-11-11). Other gaps in the listing may hide stores the same way. |

What limits first gear:

| What | Confidence | Evidence |
|---|---|---|
| The ECM estimates the transmission ratio as rpm ÷ vehicle speed, clamped to 748–4,696 (× 1/1024: 0.73–4.59, the span of this gearbox's 0.763–4.368), default 4,696 when stopped. Two copies: `0xFFFF27D2` (`0x0773D0`) and `0xFFFF1F72` (`0x076448`). | Verified (code); the scale is inferred from the clamp | The five-entry table `0xEBBC` (3,919 … 854) holds the automatic's ratios on the same scale. |
| **A torque cap applies while that ratio is at or above the cal word `0x9308` = 3,584 (3.50).** `0x073474` then sets the limit word `0xFFFF3C62` to the cal word `0x8EBC` = **3,138**, and otherwise to `0x8EBA` = `0x7FFF` (no limit). First gear is 4.368 and second is 2.518, so first is the only forward gear capped (the default ratio at a standstill is also above the threshold). | Verified (code and bytes) | The branch needs cal `0x8AD4` bit 7 and `0x8ADE` bit 7, both set (`0xA0`, `0x80`). A second term, the 8-entry curve `0xE634` on rpm, is `0xFFFF` throughout, which is no limit. The cap drops in at once and is released at 400 per tick (cal `0x9446`). The same function also applies the cap when the state byte `0xFFFF2208` is 1, 8 or 9 with bit 4 of `0xFFFF2858` clear, whatever the gear; that state is not identified, so the threshold is the edit that removes the gear trigger alone. Seven sibling limit words (`0x0735AE` onward) follow a state byte `0xFFFF2208` and were not read further. |
| The cap is a flat torque, so the throttle it allows changes with rpm: the torque map's rpm row decides how much air 3,138 needs. If the unit is N·m × 10 it is 314 N·m at the crank. | Derived from the two tables above | Full-load torque in the map (last column) is 3,065 at 1,600 rpm, 3,344 at 2,000, 3,749 at 4,000, 3,320 at 5,600 and 3,082 at 6,000. It crosses 3,138 at **1,705 rpm** rising and **5,906 rpm** falling, so the cap cannot bind outside that range. |
| **Measured: first-gear full-pedal throttle follows the cap.** Every local session on disk (first-gear frames from 2026-07-27 on), 0x23D pedal ≥ 250 with the throttle byte from the same frame, gear from rpm ÷ 0x284 wheel speed (8,755 first-gear frames, 23 sessions). | Measured; matches the prediction at the low end and in shape. The high end is not reached in the data. | The throttle byte is 255 on 94 % of frames at 1,600–1,800 rpm and on 30 % at 1,800–2,000, then never until 5,600 rpm. Band medians: 99–104 of 255 at 2,400–3,200 rpm, 115–117 at 3,400–3,800, **110–111 at 3,800–4,200**, 130 at 4,400–4,600, 175 at 5,000–5,200, 203 at 5,400–5,600, 229 at 5,600–5,800 (26 % of frames at 255). The dip at 4,000 rpm is the map's torque peak: the air the cap asks for falls from 0.908 of scale at 3,700 rpm to 0.871 at 4,100. Spread inside a band is a few counts (10th to 90th percentile 98–100 at 2,400–2,600). Second to fifth gear, 130,000 frames: median 255 and 25th percentile at least 250 in every band from 2,000 to 5,500 rpm. |
| The driver demand itself goes through a drive-torque stage that also uses the ratio: a target from the 16 × 16 map at flash `0xF71A` (the word `0xFFFF1248` on axis `0xFFE0`, 0–727, which fits accelerator counts; a vehicle-speed word `0xFFFF10E2` on axis `0x10000`; a second identical map at `0xF51A`) is divided by the estimated ratio (`0x0769B0`, `0x0776AA`) and capped at the full-load torque for the rpm to give `0xFFFF2812` (`0x0778E8`). **Corrected 2026-11-12: this stage does not run on ZS44A.** It needs `0x8ADE` bit 7 clear and `0x89A6` bit 5 set. With bit 7 set (this truck), pedal goes to torque through the torque map with no ratio (`0x0781F0`, `0x075F3C`); see "What else the output-shaft constant `0x91F8` moves". | Verified (code) for each step; not the cause; gated off here | Its numbers were not reconciled with the truck (the speed word's unit is not established). The cap above accounts for first gear without it. |

How the ratio estimate is formed, and what a regear does to it (2026-11-11):

| What | Confidence | Evidence |
|---|---|---|
| The ratio is engine rpm ÷ output-shaft rpm. Engine rpm is `0xFFFF1174` (`w120A` × 50 / 16, filtered). Output-shaft rpm is `0xFFFF117E` (filtered into `0xFFFF1180`), computed from the ECM's vehicle speed `0xFFFF10C2` (0.1 km/h per count, inferred) as speed × 65,536 ÷ the **cal word `0x91F8` = 25,491**, 25.71 shaft rpm per km/h. (Corrected 2026-11-12: the shift is 5, not 6, and the speed unit 0.1 km/h, not 0.05; the physical result is unchanged.) | Verified (code) | `0x076128`, `0x0761C4` (speed × `0x1C71C`, then `>> 5`, × 18, ÷ the word), `0x0773D0` (rpm × 1,024 ÷ output rpm). |
| That one word is tire circumference ÷ final drive. With the speed word at 0.05 km/h per count it gives 25.71 output-shaft rpm per km/h, which is 0.648 m of travel per shaft revolution: the stock 3.692 axle with a 2.39 m tire. | Inferred: the speed word's unit is taken from the result being a gearbox ratio | The same word turns output rpm back into the 0.1 km/h speed word `0xFFFF116E` (`0x0762B4`), so the ECM's speed does not depend on it. Two more readers, `0x03B126` and `0x093478`, were not traced. |
| **The 4.08 regear makes every gear read about 10–12 % short.** The speed comes from the wheel-speed sensors, so tire size cancels and the axle change shows through in full: 4.08 / 3.692 = 1.105. From the measured speed error instead (PID `0x0D` is 0.92 × GPS, 2.559 m tire) the factor is 1.123. | Derived; not read live | Expected `0xFFFF27D2`: first 4.83–4.91 (clamped to 4.59), second 2.78–2.83, fifth 1.105–1.123 where a stock truck reads 1.000. Second stays under the 3.50 threshold, so the regear does not move the first-gear cap to another gear. |
| Scaling the word by 3.692 / 4.08 (to about 23,070) would put the estimate back on the gearbox ratios. A steady fifth-gear read of `0xFFFF27D2` gives the exact factor: new word = 25,491 × 1,024 ÷ the reading. | Proposed, not done | A main-calibration edit, its own flash. What else the two untraced readers do with it is open. |

So "full pedal reads the last Fuel Targets column" holds wherever the ECM opens
the throttle fully, which is second gear and up. In first gear the torque cap
holds the target opening under 78 % from about 1,800 rpm to about 5,600 rpm, the
flag stays clear, and the lookup uses the BFS-indexed cell.

The cap and its ratio threshold are two calibration words (`0x8EBC`, `0x9308`)
in the main calibration, outside the map slots. Raising either would open the
throttle fully in first gear. Both are editable entries in `rom_map.yaml`
(`first_gear_torque_cap`, `first_gear_ratio_threshold`, with the axle word
`output_shaft_speed_constant`), each with a `raw_range` because the ECM loads
them signed or divides by them. UpRev's Tuning Guide (Rev 8) lists no setting
for any of the three. Its "Target Drive Force" tables (pro-tuner only) are the
drive-torque maps below, and its "ETC Control" setting switches the whole
torque path off (linear throttle, no cruise). That setting is not cal `0x8ADE`
bit 7, as first guessed here: that bit is set on this truck and the cruise and
pedal-to-torque-map code both run (corrected 2026-11-12, "What else the
output-shaft constant `0x91F8` moves"; the bit reads as "manual gearbox",
inferred). The threshold was moved in ZS44A-20261111-2 (flashed
2026-11-12T01:02:20Z), and the first drive agrees with this reading: in first
gear at pedal byte 220-250 and 3,000-4,200 rpm the throttle byte read 218-225
on all 47 frames, where six earlier captures never exceeded 120 on 316 frames
(`data/analyses/2026-11-11-first-gear-cap-off-prediction.md`, "Result"). Why
Nissan caps first gear is not read from the code (driveline or traction
protection are the usual reasons).

Not checked on the truck: the limit word itself. One RAM window during a
first-gear pull would show it: `0xFFFF27D2` (ratio), `0xFFFF3C62` (expect 3,138),
`0xFFFF2814`, `0xFFFF1C74`, `0xFFFF1C36`, `0xFFFF2D28`, `0xFFFF3CDC` and the flag
byte `0xFFFF2841`. The check script is
`~/canary-data/analysis-scripts/2026-11-11/first_gear_cap_check.py`.

### What else the output-shaft constant `0x91F8` moves

Read from the image on 2026-11-12, to decide whether scaling the word by
3.692 / 4.08 (about 0.905, to about 23,070) is safe. Readers were found by a scan
of the raw image that resolves PC-relative literals, base-register offsets and
GBR, so code in listing gaps is covered, and each hit was read in the listing or
a read-only decompile. This traces the two readers the table above left open.
"Dead" means a calibration bit switches the branch off in ZS44A. Four bits decide
most of them: `0x8ADE` bit 7 (set), `0x86DB` bit 7 (clear), `0x89A6` bit 5 (clear)
and `0x8ACB` bit 6 (clear). `0x8ADE` bit 7 set switches off everything that reads
the transmission control unit's messages, so it is taken to mean "manual gearbox"
(inferred). It is not UpRev's "ETC Control" off, as guessed above: with it set,
the cruise code and the pedal-to-torque-map path both run on this truck.

| What | Confidence | Evidence |
|---|---|---|
| Three functions read the word: `0x0761C4`, `0x0762B4` and `0x03B126`. A fourth, `0x093478`, multiplies by it. No other literal or base-relative form of `0x91F8` is in the image. | Verified (code) | Pool words `0x0762EC`, `0x03B2A8`, `0x0936A0`. |
| Correction to the table above: `0x0761C4` shifts right 5, not 6, so output-shaft rpm `0xFFFF117E` = speed `0xFFFF10C2` × 65,536 ÷ the word, and the speed word is 0.1 km/h per count. The physical result (25.71 shaft rpm per km/h) is unchanged. `0xFFFF117E` is filtered into `0xFFFF1180` and `0xFFFF2304`. | Verified (code); the unit is inferred, and consistent with `0x093A42` comparing `0xFFFF116E` and `0xFFFF117A` directly | Five `shar` at `0x0761E8`. `0x0DA914` writes `0xFFFF3CFC` = min(speed, `0x4800`) × `0x1C71C`. |
| **Vehicle speed does not depend on the word.** `0xFFFF116E` = `0xFFFF2304` × the word >> 16 cancels it (to rounding). `0x03B126` forms the filtered speed `0xFFFF117A` before dividing by the word. The drive-force map's speed `0xFFFF10E2` and the speed limiter's `0xFFFF10EE` are snapshot copies of those two (block `0xFFFF1124` to `0xFFFF1098`, `0x020F92`). | Verified (code) | `0x077B20` (limit word `0xFFFF1C8A`) compares `0xFFFF10EE` with the map-slot value at RAM `0xFFFFB050`. |
| `0x03B126` also makes a second output-shaft rpm, `0xFFFF22A0` = `0xFFFF117A` × 65,535 ÷ the word, filtered into `0xFFFF118C`. The ratio estimators divide by the lower of the two output-shaft speeds when the two speed words disagree by 2.5 km/h (bit 0 of `0xFFFF4859`, `0x093A42`). Both scale with the word alike. | Verified (code) | `0x0773D0`, `0x076448`. |
| The ratio estimate has three copies, all rpm × 1,024 ÷ output-shaft rpm, clamped 748–4,696: `0xFFFF27D2` (`0x0773D0`; copy `0xFFFF197A`, snapshot `0xFFFF2746`), `0xFFFF1F72` (`0x076448`) and `0xFFFF19A2` (`0x07719A`, which runs only on an automatic). Scaling the word by 0.905 scales all of them by 0.905. | Verified (code) | `0x0773D0` takes the manual branch when `0x8ADE` bit 7 or `0x86DB` bit 7 is set. |

Consumers, and what scaling the word by 0.905 does to each:

| Consumer | Effect of 0.905 | Confidence |
|---|---|---|
| First-gear cap `0x073474` (ratio ≥ `0x9308`). | None while `0x9308` is 32,767 (ZS44A-20261111-2). With the stock 3,584 it would still catch only first gear, which reads 4.37 instead of the clamped 4.59. | Verified (code); ratios derived |
| **Pedal to torque request, every gear.** With `0x8ADE` bit 7 set, `0x0781F0` takes the driver torque `0xFFFF1C68` from `0x075F3C`, which goes from pedal target area through the torque map and never uses the ratio. The Target Drive Force maps `0xF71A`/`0xF51A` (`0x077938`) and the divisions by the ratio (`0x0776AA`, `0x076EE4`, `0x0767D4`, `0x0769B0`) run only with that bit clear and `0x89A6` bit 5 set. Correction to the drive-torque row above: that stage does not run on this calibration. | None | Verified (code): gates at `0x0781F0`, `0x077060`, `0x0768B8` |
| **Cruise torque**, `0x0DAA62`: `0xFFFF475A` = the cruise drive-force command `0xFFFF3B4A` (written by `0x0DAFAC`) × `0x4211` (cal `0x934E`) × 2,048 ÷ the ratio latched at engagement (`0xFFFF2BBA`), >> 15. `0x077610` and `0x076CEC` raise the torque demand to it while bit 7 of `0xFFFF478B` is set. | Torque per unit of drive force +10.5 %. The stock word already absorbs the regear: the ratio then reads overall reduction ÷ 3.692, and on a stock truck it reads the gearbox ratio, so `0x934E` carries the stock axle (inferred). So today's conversion is right for the 4.08 axle. Changing the word alone makes cruise about 10 % more aggressive; scaling `0x934E` by the same 0.905 keeps it as it is. The loop closes on speed either way. | Structure verified (code). That this is cruise, and the axle inside `0x934E`, are inferred |
| **Cruise ratio check**, `0x093478`. When cruise engages (bit 7 of `0xFFFF4793`, set by `0x0DD0CA`) it latches the ratio snapshot into `0xFFFF2CA2`. It then predicts speed as rpm × the word ÷ the latched ratio, and compares the prediction with the filtered speed `0xFFFF470C`. A difference over 5 km/h above 32 km/h sets bit 2 of `0xFFFF4798`, and so does any of four received-message bytes (`0xFFFF4443`, `43E8`, `42C6`, `4142`) reaching 20. `0x0DD0CA` uses the bit to drop cruise (inferred: the clutch or gear-change cancel). | None: the latched ratio carries 1 ÷ the word, so the word cancels. In first gear the prediction stops being 5–7 % off, because the clamp no longer applies. | Verified (code). Readers of the bit are all in the cruise code (`0x0DD0CA`, and `0x0DB520`/`0x0DDD4A` via snapshot `0xFFFF4790`). No DTC reads it. |
| Expected-rpm limit, `0x094F54`: `0xFFFF16B4` = `0xFFFF197A` × `0xFFFF10F2` ÷ 1,024 + 600. It feeds the torque-ratio curve `0xEF6A` (`0x077554`, giving `0xFFFF1C50`). | None: the product cancels the word. `0xFFFF1C50`'s live readers are the cap siblings (limits 32,767) and the road-load estimate below. | Verified (code) |
| Road-load estimate, `0x0788F0`: drive force from `0xFFFF1AFA` at ratio `0xFFFF197A`, less rolling resistance and inertia, into `0xFFFF1994`. | None: its only reader, `0x078B1C`, multiplies by curve `0xEC9A`, which is all zero. | Verified (code and bytes) |
| Output-shaft deceleration `0xFFFF1146` (`0x03ADC0`, snapshot `0xFFFF10BA`). | +10.5 %. `0x03B688` acts only at 0xFFFF (cal `0x9306`). `0x0642A4` adds 2 instead of 4 to an rpm threshold in 25-rpm steps (`0xFFFF14C2`, giving bit 4 of `0xFFFF2857`) when deceleration × 3.83 is under 6, so its switch point moves 10 %. Negligible. | Verified (code); what the threshold is for is not read |
| `0x04F3A4`: output rpm × 3.83 against rpm, giving bit 4 of `0xFFFF2844`. | None: cal `0x8806` = 0xFF holds the bit set. | Verified (code) |
| Diagnostics. | None. Nothing compares speed with rpm through the word except the cruise check above. The other check in `0x093478` (`0xFFFF4710` against `0xFFFF116E`) is off (`0xFF42` bit 3 clear) and does not use the word. | Verified (code) |
| Dead in ZS44A. Behind `0x86DB` bit 7: `0x047AD0`, `0x0667F0`, `0x07113E`, `0x073068`, `0x07A718`. Behind `0x89A7` bit 5: `0x07777E`. Behind `0x8ACB` bit 6: `0x0DAA62`'s ratio branch. Behind `0x8ADE` bit 7 set: the automatic's received output-shaft rpm (`0x0CC434`, `0x0CC6EC`, `0x0CEA3E`, `0x0CEAAA`). Driven by the automatic's gear bytes `0xFFFF108E`/`108F`, which are zero here: `0x06547C`, `0x066656` and `0x07A19E`'s ratio-table terms. | None | Verified (code) |

So on this truck the word changes the first-gear cap's gear test and the cruise
torque conversion, and nothing else of consequence. Speed, the speed limiter,
pedal response, idle and the full-load torque path do not see it. No rev-matching
code reads the ratio. If the word is scaled, scaling `0x934E` by the same factor
keeps cruise as it is (inferred). Not checked on the truck: during cruise,
`0xFFFF2CA2` (the latched ratio), `0xFFFF3B4A` and `0xFFFF475A`, which would
confirm the cruise reading. With cruise off, `0xFFFF2CA2` holds 4,696.

**The `0xFFFF2208` trigger cannot fire on this calibration** (verified, code). Its
only writers are `0x0CC1E4`, which stores the high nibble of received byte
`0xFFFF41B2` (the low three bits become the automatic's current gear
`0xFFFF1090`), and `0x0CE994`, which zeroes it when that message is lost. Both
return at once with `0x8ADE` bit 7 set. Start-up zeroes RAM from `0xFFFF1000` to
`0xFFFF2AC2` (`0x028E30`, called from the application start at `0x029000`), so the
byte stays 0. Bit 4 of `0xFFFF2858` has one writer, `0x04FE6C`, which clears it
whenever `0x8ADE` bit 7 is set. On an automatic it is set above 1,800 rpm when
`0xFFFF1BAA` ≥ 24,000 and cleared below 1,500. So 2208 is a code from the
transmission control unit (inferred from the gear in the same byte). Codes 1, 8
and 9 give the 3,138 cap. Codes 9, 10 and 14 drive the
sibling limit words `0xFFFF3C60`, `3C64` and `3C8A` (`0x073716`, `0x0735AE`,
`0x073834`), whose calibration values (`0x8EBE`–`0x8EC8`) are all 32,767. What the
codes mean (range or shift-mode states) is inferred, not read.

The same function has a third trigger that the row above missed. When bit 3 of
`0xFFFF4856` is set, the cap follows bit 4 of `0xFFFF2858` alone. That bit is
always clear here, so the cap would apply in every gear. `0x063D7C` sets bit 3
when bit 7 of `0xFFFF4785` is set and any of `0xFFFF2B46`, `2B47`, `2B4E`, `2B4F`,
`2B78`, `2B79` or `2B7A` reaches 11. These are snapshots of per-message bytes kept
by the CAN receive layer (`0x0CBBFE`); on this calibration only `0xFFFF41B1` and
`0xFFFF41BB` feed them. Inferred: a received-message timeout, so a CAN fault gives a
limp-mode torque cap in all gears. Its snapshot (bit 3 of `0xFFFF484E`) also
suspends the cruise ratio check (verified, code). It is not set in normal driving
(inferred from measurement): second to fifth gear at full pedal read throttle 255,
which a 3,138 cap would prevent between 1,705 and 5,906 rpm. Which messages
`0xFFFF41B1` and `0xFFFF41BB` track is not identified. A live read of
`0xFFFF4856`, `0xFFFF2B46`, `0xFFFF2B47`, `0xFFFF2208` (expect 0) and
`0xFFFF2858` would confirm all of this.

### Diagnostic service handlers

| What | Where | Confidence |
|---|---|---|
| Service dispatcher. It keeps the request's service byte at RAM `0xFFFF8003` and the response byte (service + 0x40) at `0xFFFF810A`; transmit is at `0x02FB7C` and negative responses at `0x034F34`. | `0x030714` | Verified |
| Mode 01 handler. Its support mask at `0xC85C` equals the 41 PIDs the 2026-07-22 probe found. Handlers exist for PID `0x0B` (MAP) and `0x43` (absolute load), but the mask leaves them off, so they never answer. | `0x0D3AFC` | Verified |
| Mode 22 DID → RAM pointer tables, separate for K-line and CAN but **pointing at the same variables** for every DID they share (K-line the live copy, CAN the per-cycle snapshot; reversed for 0x120D–0x1210), except 0x1247, 0x1248 and 0x1315 (DID audit, 2026-10-05). An earlier reading, "the same DID number can point at different variables on each transport", was wrong. K-line adds 0x14xx and 0x16xx (99 K-line-only DIDs). | K-line `0x022738`, `0x022938`, `0x022AB4`; CAN `0x024988`, `0x024B84`, `0x024D00` | Verified |

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

The `data/dids.yaml` entries below came from UpRev's **K-line** stream labels. The 2026-10-04 explanation was that the transports have separate tables with different meanings. The full audit of both tables on 2026-10-05 showed that is wrong: K-line 0x1102 is the speed byte too, and K-line 0x1125 is LTFT B1 too. The labels were UpRev's, not the ECM's.

| DID | Catalog says | It actually is | Confidence |
|---|---|---|---|
| `0x1102` | `ECM_LONG_FUEL_TRIM_B1`, 2 bytes, % | **Vehicle speed**: the byte at `0xFFFF11A8`, which PID 0x0D also reads. One byte on CAN, km/h = raw × 2. | Verified: the firmware pointer, plus data. On `_capture-rom-trace-2026-09-21T142926`, DID 0x1102 tracks broadcast wheel speed with correlation 0.9997 over 202 pairs, and its per-sample ratio is steady (median 32.1, p10–p90 31.6–33.6). |
| `0x1125` | `ECM_LONG_FUEL_TRIM_B2` | **LTFT B1** (`0xFFFF26DE`, the PID 0x07 word) | Verified (firmware pointer equals the PID handler's) |
| `0x1126` | (not catalogued) | **LTFT B2** (`0xFFFF26E0`) | Verified (firmware) |
| `0x1123` / `0x1124` | (not catalogued) | **STFT B1 / B2** (`0xFFFF26B4` / `B6`) | Verified (firmware) |

Any earlier trim analysis that read DID `0x1102` as learned trim was reading road speed.

Applied to `data/dids.yaml` on 2026-10-04 (roadmap D03.36): `ECM_VEHICLE_SPEED_COARSE`, `ECM_SHORT_FUEL_TRIM_B1/B2`, `ECM_LONG_FUEL_TRIM_B1/B2`. The 0x1102 decode is pinned against wheel speed in the ground-truth suite. Evidence entries now carry `transport`, and the catalog refuses an entry whose only support is K-line: K-line support has meant a third-party tool's labels.

#### Mode 22 DID audit (2026-10-05)

The firmware session checked all 172 CAN DIDs against both pointer tables and against the 2026-09-17 did_log sweeps (`~/ecu-re/out/did_audit.yaml`). Applied to `data/dids.yaml` on 2026-10-07 (roadmap D03.40):

| DID | Catalog said | It is | Confidence |
|---|---|---|---|
| `0x1107` | A/F heater duty, ×0.5 % | **Final ignition timing**, byte `0xFFFF11FF` (the PID 0x0E and DID 0x110A byte), 110 − raw °BTDC. Renamed `ECM_IGNITION_TIMING_1107`. The heater duties are `0x114E`/`0x114F`, 0.4 % per count (warm idle 48.8 %, the NT510's 48 %). | Verified (pointer; same-pass equality with 0x110A rises to 93 % at 7.5 ms skew) |
| `0x123F` | intake air temperature | **Monitor run counter**, u16 `0xFFFF5268`, increment-only. Renamed `ECM_MONITOR_RUN_COUNTER_123F`. IAT is `0x1106` (`0xFFFF2CB8`, °C + 50; PID 0x0F = that byte − 10). | Verified |
| `0x1117` | ×100/256 % | ×**100/255** %: the PID 0x04 byte, sent unchanged. | Verified |
| `0x1134` | rich-side target candidate | **Target-AFR-map enrichment request**, high byte of Q14 `0xFFFF26C0`, φ = raw/64; 64 when the cell is below ~0x82; an rpm table replaces the map under HI DET. | Verified |

Confirmed exact: `0x1123`–`0x1126` (high byte of a 0x6400 = 1.0 word, 1 % per count, truncated), `0x1209` (the PID 0x10 word), `0x1219` (1 rpm per count, 3.125 rpm steps), `0x122A` (the ASCD speed). Not settled by code: volts per count of `0x120D`/`0x120F`/`0x1210` (5/1024 assumes a 5.000 V reference; the ECM's own PID 0x24 voltage uses 5.0024 mV per count). Added to the catalog: battery voltage, fuel tank temperature (inferred), EVAP pressure ×2, baro, rear O2 ×2, relative TPS, heater duty ×2, MAF sensor volts, first flagged DTC, pedal sensor 2, cruise set speed and deviation, battery current count. `0x1249`/`0x124A` are the RAM words `mem:ECM_AF_LEARNED_OFFSET_B1/B2` already catalogues. `0x120B` reads P0328 while driving, which reopens the knock verdict below (D03.40).

### Map-slot tables, confirmed from the code that reads the RAM copy

| Slot offset (RAM) | Table | Confidence | Evidence |
|---|---|---|---|
| `+0x020` (`0xFFFFB060`) and `+0x120` (`0xFFFFB160`) | **Ignition timing trims**, two 16 × 16 tables. Their axes are rpm (`0xCC93`, raw × 50 = 400–6,000 rpm) × BFS-X counts (`0xCC83`, 8–128), and cells are read masked with `& 0x7F`. (Corrected 2026-10-04: an earlier version swapped the two axes. canary-60 found it from the wrapper's register use, and the axis contents agree.) **The HI DET latch chooses between them:** `FUN_0004e6d8` loads `0xFFFFB160` (trim B, `+0x120`) and switches to `0xFFFFB060` (trim A, `+0x020`) only when `0xFFFF283F` bit 6 is clear (`tst #0x40` at `0x04E6FE`). (Corrected 2026-10-05, canary-6e's listing trace, checked here. An earlier version said `0xFFFF2842`; its bit 2 gates the knock controller's trim-difference branch at `0x04A512` instead.) HI DET latches on accumulated knock or a knock-sensor fault (see "Knock monitoring"), so trim B is the High Det table. A coolant condition (cal `0x8CCC`/`0x8CCD` = 110/106 raw) follows in the same routine. | Verified location and selection; that B is used after knock is verified, its tuning intent inferred | The readers of both tables, and the flag gating. The `+0x09C` cells k106 changed are **inside** the `+0x020` table (cell `0x7C`), not a separate table. |
| `+0x220` (`0xFFFFB260`) | Target AFR, 8 × 8 | Verified | As above. |
| `+0x260` (`0xFFFFB2A0`) | **Engine torque map**: torque on rpm (`0xCAE9`) × normalised air quantity (`0xE25E`), 16 × 16 words biased by `0x8000`. The most-read switchable map, with at least 21 readers. | Verified (code and bytes, 2026-11-11); units inferred | "The enrichment switch tests target throttle" above: the throttle path reads it backwards to turn a torque demand into air. |
| `+0x660` (`0xFFFFB6A0`) | **Fuel Compensation**, 16 × 16 (`+0x660–0x75F`). Axes: **rpm = `0xCACD`** (raw × 50 = 400–6,400) and **load ("Volumetric Flow Ratio") = `0xCB29`**, compared bytewise with `0xFFFF118A`. **UpRev's editor labels them the other way round.** | Verified from code (2026-10-04) | Call site `0x02DC34–0x02DC46`: r4 = table `0xFFFFB6A0`, r5 = `0xCACD`, r6 = `0xCB29`, r7 = engine-speed word `0xFFFF1210` (the PID 0x0C rpm word), stack byte = `0xFFFF118A`. Wrapper `0x026A24` searches r5's axis with r7 through `0x027194` (axis byte `shll2`, compared with the word: the rpm search), and r6's axis with the byte through `0x027114` (a direct byte compare: the load search), then interpolates through `0x026F40`. It is the Target AFR wrapper `0x0269AC` instruction for instruction, apart from the table size, and that mapping is verified on live data (W05.1). Stock `0xCB29` runs 16…255, the full range of a load byte (12,750 "rpm" in UpRev's display). **The k105/k106 "rpm axis 800–12,750 → 800–7,200" edit compressed the load axis to 16–144**: 25 % of the 2026-10-04 drive's samples (up to 218) read the last column. **ZS44A-20261004 restored the axis to 16…255** and slot-1 cells to base (flashed 2026-10-04 23:02Z; read back byte-exact). |
| `+0x760` | Load Point Scaler copy (44019) | Verified (bytes) | See above. |
| `+0x762` (`0xFFFFB7A2`) | **K**, big-endian 16-bit, multiplied into the air-charge accumulator: Tp = K · Qa / Ne. | Verified | The read at `0x03C6E0` inside `0x03C4D0`. It holds 28,715 in all five slots, and the base value 27,089 is in the ratio 1.0600. |

The four switchable maps and their axis pairs:
- `{0xCC83, 0xCC93}` timing trims (`0xCC93` rpm, `0xCC83` BFS-X);
- `{0xC894, 0xCAA5}` Target AFR;
- `{0xCACD, 0xCB29}` Fuel Comp (probably `0xCACD` rpm, `0xCB29` VFR; see above);
- `{0xCAE9, 0xE25E}` the `+0x260` engine torque map.

### UpRev's Fuel Comp display, checked against memory

The operator pasted UpRev's Map 1 Fuel Comp grid for the corrected
`ZS44A-Tuned-20261004.OsirisROM`. It equals the image's table at
`0xE0660` **row-major, 256/256**; transposed, only 60/256 match.

So UpRev's rows are the ECM's rpm rows and its columns are the load
columns, and the cell values are where the code reads them. What UpRev gets
wrong is which breakpoint list belongs to which axis:
- the row axis, named "RPM", shows `0xCB29` × 50 (800–12,750);
- the column axis, named "VFR %", shows `0xCACD` / 256 (3.13–50.00 %).

The true values follow by swapping the two lists: row *i*'s rpm = column
*i*'s "VFR %" × 128, and column *j*'s load = row *j*'s "RPM" ÷ 128 (as VFR %).

### Which map is loaded

| What | Confidence | Evidence |
|---|---|---|
| The map is switched by a **discrete input** on the vehicle, not by a DID or a diagnostic command. | Verified | — |
| `0xFFFFB008` holds the committed active map, 1–5, and is safe to read. It reads **0 after a cold boot**, meaning "slot 1, loaded by default, not yet re-selected". | Verified (code and live) | The 2026-10-04 drive, below. |

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
| `0xFFFFB008` reads 0 after a cold boot (slot 1 loaded by default) and changes only on a map switch | 0 on all 196 reads. | **Confirmed** (no switch event occurred) |
| Pedal-switch flag: bit 0 of `0xFFFF2841` sets near full pedal and forces Target AFR's last load column | Set on 2 samples, at broadcast pedal 4.285 V and 4.582 V (full scale 4.58 V). Never set at or below 3.795 V, the highest pedal seen while it was clear. While set, the commanded φ RAM word read **exactly 1.1640625** (`0x95/128`, the last-column value) while the X trace sat mid-table (~100 counts). | **Confirmed live.** The threshold lies between 3.80 and 4.29 V, which fits the 2026-09-18 data figure of ≥ ~227/255. |
| Timing-trim select flag at `0xFFFF2842` | Constant 4 (bit 2) all drive | **Superseded 2026-10-05:** `0xFFFF2842` does not select the trim table. `0xFFFF283F` bit 6 (HI DET) does. Read `283F` live to know which trim table is running before any timing-trim edit (N01.2). |
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

### BFS formation, traced (E05.2)

Listing of the a46baa24… image, 2026-10-05. Node by node, with widths and
branches, in `data/nissan-xterra-2008/ecu_application_model.yaml` (model 4);
the arithmetic is `src/decode/air_charge.py`.

| What | Confidence | Evidence |
|---|---|---|
| BFS is made by two tasks in different dispatch groups of the tick dispatcher `0x020A3C` (not the background cycle `0x020EF2`). Group `0x10` (table `0x02189C`, 67 tasks) runs `0x03C3A4` (the load byte `0xFFFF118A`, entry 38) and then `0x03CC3C` (entry 40: the fill time constant and coefficient, then the Fuel Comp lookup into `0xFFFF225A`). Group `0x20` (table `0x021874`, 10 tasks) runs `0x03BB40` (MAF to the airflow long `0xFFFF2100`, entry 4) and then `0x03C62C` (entry 5). After group `0x20` the dispatcher copies `0xFFFF20A4..20B3` to `0xFFFF20B4..20C3` (`0x020BFC`), which is how the filter sees last run's value. | Verified (code) | Task-table words at `0x021888`, `0x021934`, `0x02193C`; dispatcher `0x020BD2`-`0x020BFE` and `0x020CC4`-`0x020CD6`. |
| **Periods.** The ATU compare ISR (vector 120, `0x02040C`) adds 320 to its compare register each tick (`0x020442`-`0x02044A`), at φ′/32 (TCR `0xFFFFF4AC` = 5; PSCR = 1, so φ′ = Pφ/2). It sets `0xFFFF84B9` bits: `0x80` every tick, `0x40` every 2, `0x20` every 4 (BFS group), `0x10` and `0x04` every 10. It then runs the groups (`0x0208D6` → `0x020A3C`). The background loop `0x0290D0` waits for bit `0x04`, then runs the main cycle `0x020EF2` (task table `0x0219D0`, the fuel task among them). At Pφ 20 MHz a tick is 1.024 ms: BFS every 4.096 ms; Fuel Comp, the fill coefficient and the main cycle every 10.24 ms. With the default weight (819/32768 per run) the filter's time constant is about 164 ms, which fits the ~0.15 s BFS lag behind MAF in `data/dids.yaml`. | Counts and bits verified (code); Pφ inferred (SCI1 BRR gives 10,417 / 9,615 baud at 20 MHz; the code holds no clock constant) | `0x02040C`-`0x0204A6`, `0x0208D6`, `0x0290D0`-`0x0290E2`, TCR store `0x027A2A`. |
| **Tp** (`0xFFFF2110`, a long): on the running branch, `N = max(0x40, w1204)` (`w233C` while `0xFFFF283B` bit 6, `b2C91 << 3` while `0xFFFF4786` bit 6). For airflow > 0 the code forms `K × airflow × 226 / 256` as a 24.8 number from two 16 × 16 products in byte halves, every step a wrapping 32-bit `mul.l`. It divides the integer part by `3 × N` (`0x021D6C`). If that quotient is below `0x727`, the remainder and the fraction byte make a second divide (`0x021FCC`, `0x021D6C`). The result is capped at `0x726F9`, then `Tp = q × 0x23CB >> 16`. The cap makes Tp at most `0xFFFF`. Below the wrap this is `min(0x726F9, ⌊⌊K·Qa·226/256⌋ / (3N)⌋) × 0x23CB >> 16`. | Verified (code) | `0x03C646`-`0x03C7AA`; literals `0x726F9` (`0x03C888`) and `0x23CB` (`0x03C78C`) are `rom_map.yaml` program constants. K is read from RAM `0xFFFFB7A2` (slot `+0x762`) at `0x03C6E6`. |
| `w233C = (w233C + 255·w1204) >> 8`, plus one when it stalls below, so it equals `w1204` once rpm holds; `w1207 = min(255, w1204 >> 4)`. | Verified (code) | `0x03907A`, `0x039048`. |
| **Fuel Comp product** `w105C = min(0xFFFF, Tp × FC >> 7)`, unconditional while cal `0x8AE1` bit 5 selects the table. FC is the byte `0x03CC3C` last wrote. | Verified (code) | `0x03C7AC`-`0x03C7D2`; lookup call `0x03D0F4`. |
| **Fill coefficient**: `T` = 8 × 8 byte map `0x9BD9` while `0xFFFF2899` bit 7 (airflow falling) else `0x9B99`, on rpm `0xC87C` (× 50 rpm) and the load byte `0xC884`. Then `w1164 = min(0x7FFF, (T × cal 0x8D26 (58) × rpm word × 55) >> 16)`. The new-value weight `a = w1164` while `0xFFFF283C` bit 3, else cal `0x9066` (819); `b = max(0, 0x8000 − a)`. | Verified (code), on cal `0x8D28` bit 4 set | `0x03CF52`-`0x03D06C`. The decompiler folded the calibration reads at `0x8D26` and `0x9066` into the constants `0x3A` and `0x333`; the listing shows them as reads. |
| **Filter** `w20A6`: `y = (4·w105C·a + w20B6·b) >> 15`, with saturations and a +1 when `y` stalls below `min(0xFFFF, 4·w105C)`. Its unique fixed point is `min(0xFFFF, 4·w105C)`, and `0x02D5F4` seeds it there at reset. While `0xFFFF289C` bit 5 is set, a subtracting form is used. No writer of that bit exists in the listing (all store forms and the block-copy helpers scanned), and RAM is cleared at reset, so it reads 0 unless some computed-pointer write sets it. | Verified (code) | `0x03C7D4`-`0x03C8B6`; reset seeding `0x02DC1A`-`0x02DCF6`. |
| **Transient** `w105A = clamp_s16(((w20A6 − w20B6) × 160) >> 6)` (arithmetic shift, cal `0x8653`). It applies only with `0xFFFF283C` bits 3 and 2 set, the near-full-throttle flag `0xFFFF289B` bit 7 clear, and both fault holds at 0. **Blend**: `w20A4 = (w20A6·w + w20B4·(256 − w)) >> 8` with `w = max(0x1A, min(255, b1207 × 255 >> 5))`, while bit 3 is set, bit 2 is clear and near-full-throttle is set; otherwise `w20A6`. | Verified (code) | `0x03C8B8`-`0x03CA5A`. |
| **BFS** `0xFFFF103A = min(max(0, w20A4 + w105A), D8B3[w1207] << 8)`; a MAF fault switches to table `0x95E5`. Then `0xFFFF2899` bit 7 = (`4·w105C < w20A6`). X divides this BFS by the word at `0x9416`. | Verified (code) | `0x03CA5C`-`0x03CBE2`, `0x03CBE4`-`0x03CC22`. |
| **Airflow** `0xFFFF2100 = max(0, L20AC + w20E2 + w273E)`. `w20E2` is 0 **by calibration**: `0x03C2EC` writes `(b216A − 0x7B) × cal 0x874D >> 5` or `b6424 × cal 0x874D >> 5`, and skips the write while cal `0x86C8` bit 5 and cal `0x8ADE` bit 0 are both set; cal `0x874D` = 0 and `0x86C8` bit 5 is clear (calibration conditions). `w20A8` low-passes the MAF curve `0xE27E` output with gain cal `0x86D9` (26/256, nudged when it stalls). Off the transient fill model, `L20AC = ((L20BC × (256 − g)) >> 4 + (w20A8 × g) >> 4) >> 4` with `g` = cal `0x8A68` (128): it can stall one count below a rising input. On it, a lead (gain cal `0x82C2`, 45/8), a pulsation term (cal `0x82BA`–`0x82BC`) and a carried residue, exact at a fixed point. `w273E` is the cycle-start copy of `w27CA`, which the EVAP purge control writes at `0x06E2E8` as `(w1046 − w8DC4) × w38AA / 0x19000`; `w1046` is the snapshot of `w1056` (purge vapour flow, inferred). The hot-wire term (`0x03BBF2`/`0x03BEFC`) decays to 0 after start. PID 0x10 and DID 0x1209 both report `min(0xFFFF, w1056 × cal 0x83E9 (149) >> 8)`, `w1056` = `((w1054 + w273E) + (2^n − 1)·w20BA) >> n` with n = cal `0x89B3` = 1 (`0x022248` is a variable left shift), nudged when it stalls: a two-point average that settles exactly. `w273E` and `w1056` share the airflow-word unit, 149/25,600 g/s per count (the SAE PID 0x10 scale with cal `0x83E9`). At a fixed point `0xFFFF2100 = w1056`, or one count below it. | Structure verified (code; the hot-wire term from the decompile text); the steady-state equality is checked on drives (below) | `0x03BB40`-`0x03C2EA`, `0x06E2A0`-`0x06E2E8`, `0x03D2B6`, PID handler `0x0D41B0`. |

**Checked on drives (steady state only).** BFS was predicted from the reported MAF word (DID 0x1209), the rpm word (PID 0x0C), K and the Fuel Comp band of that rpm. The prediction was compared with DID 0x1208 per sample. Seven ZS44A-20260920 captures supplied 14,843 BFS reads; 458 passed gates frozen before scoring: MAF ± 2 % and rpm ± 1 % over ± 1 s, MAF paired within 0.1 s. Every other read is counted by reason. Over those 458:

- observed / predicted: median 0.9991, p10 0.9898, p90 1.0097, with 98.5 % inside the exact quantisation interval;
- coverage: 725–3,588 rpm, 3–62 g/s, BFS 3,344–33,732.

Controls on the same samples miss badly: K read from the neighbouring word `+0x760` gives 0.65; the multiplier from cal `0x89D3` gives 1.53; the UpRev base K (27,089) gives 1.06; the report taken as the airflow word gives 1.71.

The check does **not** resolve errors under about 1 %. Fuel Comp held at 128 still gives 1.0014, and a one-word rpm shift gives 1.0128. So it establishes the K·Qa/N structure and constants, not Fuel Comp's contribution. `tests/test_air_charge_private_rom.py` runs it.

**Branch flags.** `0xFFFF283B` bit 6 has one writer, `0x03B994` (store `0x03BB02`). It rises when all of these hold: `0xFFFF283E` bit 6; the fill-model state `0xFFFF4088` is 2; coolant is at or above cal `0x872A`; `0xFFFF2834` bit 4; there is no MAF fault; and `|4·w105C − BFS|` is within cal `0x8A7B` `<< 8`. It holds while `0xFFFF2834` bit 4 is set. `0xFFFF2C91` is written only by `0x03F048`. It is the median of the last three `min(255, 0xA2C2 / w2648)` (signed divide `0x021C48`; `w2648` a crank-segment period, inferred), or `0xFFFF1206` while `0xFFFF28B1` bit 5. Its use × 8 as a 1.5625-rpm word makes it 12.5 rpm per count. `0xFFFF4786` has two bits here:

- **Bit 7** (start input) is set by `0x0398DE` from I/O port word `0xFFFFF75E` bit 4, and set or cleared at `0x0394D6` from state `0xFFFF407C`.
- **Bit 6** (start-phase latch, name inferred) is written at `0x039CF0` by `0x039904`: `and 0xBF`, then `or 0x40` when r7 ≠ 0. r7 starts as the old bit 6 (`0x03991C`-`0x039928`). It is set when `0xFFFF28A5` bit 5 is set (start conditions: bit 7 with `0xFFFF2C91` < 0x18). It is cleared when `0xFFFF28A5` bit 6 is set (bit 7 clear, `0xFFFF1206` > 0x4F, or the battery-voltage and coolant-timer terms).

**Not modelled:**
- the airflow pipeline's state (decoded, but no capture carries it);
- the inputs of `w273E`;
- the cranking Tp (`0xEEBA`) and the MAF-fault table;
- the K and Fuel Comp RAM copies, which are assumed equal to the slot (blocker `air_charge_ram_copy_unverified`).

**Absolute units.** At a steady state BFS ≈ 44.18 × K × (FC/128) × MAF[g/s] / rpm. That is 0.736 × K × FC/128 per gram of air per revolution, or about 21,144 BFS counts per g/rev at K 28,715 and FC 128. The constant comes from the code's 226/256, 0x23CB/65536, the divisor 3, 1.5625 rpm per `w1204` count and the airflow-word unit. It depends on the MAF curve being right. The fill byte and the throttle area stay relative: the code never multiplies them by a physical constant.

**Not settled from the listing:**
- whether `0xFFFF289C` bit 5 is ever written: a static scan cannot rule out a computed-pointer store; a live read can;
- Pφ: inferred from baud rates, not stated in the code;
- the per-drive state of the airflow pipeline and the purge controller's learned limit behind `w273E`: needs live reads.

So dynamic BFS stays conditional: application model output `base_fuel_schedule_word`, blockers `air_charge_ram_copy_unverified`, `airflow_unmodelled`, `fill_byte_not_computed`, `fill_dynamics_reference_only`. The drive check assumes no sample was cranking, MAF-faulted or in the subtracting filter form: no channel reads those flags, so each scored sample is counted under `regions_assumed`.

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

### Knock monitoring (observation only)

Decompiler pass, 2026-10-05. The goal was to *observe* the ECM's
knock-protection results so that CANary can watch them. Calibration internals
and any way of changing knock control were out of scope and are not recorded
here.

**Update 2026-10-05, later: knock sensor 1 is flagged on every logged drive since 2026-08-05.**
The DID audit found that Mode 22 DID `0x120B` (CAN handler `0xD178E`) returns
the DTC word of the first diagnostic item whose status bit 0 (`0x657E + 3i`)
is set. Reproduced from the raw logs:
- `0000` at key-on, engine off (2026-08-04 baseline);
- `0328` (**P0328, knock sensor 1 circuit high**, item `0xAB`) in all 13
  engine-running sweeps on 2026-08-05;
- `0328` on 2,678 of 2,683 reads on 2026-09-17. The five `0000` reads are at
  or just after start.

Sources: `data/analyses/did-drive-sweeps-20260805/`,
`~/canary-data/did_log/did_log-2026-09-17-*.jsonl`. That bit is exactly what
`0xFFFF479D` bit 7 ORs, and bit 7 forces the fixed `0xFB00` retard target.
So **knock control is very probably in its fixed-retard fallback whenever the
engine runs**. Generic Mode 03/07 never show it, which is why no scan tool did.

**Update 2026-10-09: the fault predates the 2026-08-01 unplug test, and the
ECM reports it to a Nissan code read.**
- The scan tool's self-diagnosis read at ECM entry, `17 FF 00`, was answered
  `57 01 03 28 C0`: one DTC, **P0328**, status `C0`.
  - 2026-07-27 22:46Z, engine idling at 750 rpm (session
    `2026-07-27T223930`, laptop copy under `~/canary-data/sessions/`).
  - 2026-09-24 16:56Z, engine running (Pi session `2026-09-24T165319`). That
    session's note had recorded only `57 01`.
  - `C0` matches item `0xAB`'s byte 1 in the 2026-10-08 read (bit 7, failing
    now). The two were not read at the same instant.
- Every UpRev log that carries the knock columns reads KNOCK STRENGTH = 1280
  and HI DET = 1, from the first (2026-07-26 22:53Z, engine running) to
  2026-09-24. That is the fallback signature the 2026-10-08 read confirmed at
  `0xFFFF13FE` (`0xFB00`, −5.00°) and `0xFFFF283F` (bit 6).
- **So the 08-01 test did not cause the fault, and its result did not show
  knock sensor 1 intact.** Unplugging a circuit that is already open changes
  nothing. For "unplugging set a code, reconnecting cleared it" to hold, one
  of these must be true:
  - the connector unplugged that day was sensor 2's;
  - sensor 1's open is intermittent, and re-mating the connector briefly
    restored it. The circuit carries a few µA, too little to break through a
    film on a fretted terminal.

  Every live sample so far shows sensor 1 failing. No CANary session exists
  for 2026-08-01.
- **Why no one saw a code:**
  - generic Mode 03/07 can never list it (below);
  - the item never reaches "confirmed";
  - its status reads zero at key-on, and the circuit check runs only with the
    engine running, so a key-on engine-off read of any kind is empty;
  - the scan tool did receive it at ECM entry, but did not visibly report it.

The 2026-10-05 list "still to reconcile" is settled:
- **The 2026-08-01 retard event.** It is in the 20.8-minute UpRev log of
  2026-07-31 at 17:35 local, written up on 08-01. Logged timing falls from
  19° to 9° while load rises from 76 % to 94 % at about 2,190 rpm. That is
  the timing map following load, not evidence of knock retard. The log has
  no knock columns, and P0328 was being reported before and after it.
- **The 08-01 unplug test as the cause.** Refuted, above.
- **Early flag versus confirmed code.** The 2026-10-08 read: pending, never
  confirmed.

**Confirm live** (reads only): key-on engine-off, then idle. Read `0xFFFF479D`
bit 7, `0xFFFF677D`–`677F` (item `0xAB`), the KS1 level `0xFFFF2CEF` (normal
50–199) and `0xFFFF13FE` under light load. *Done 2026-10-08 (D03.40): the
fallback is engaged, KS1 is pegged at `0xFF`, and KS2 reads `0x7F`/`0x80`.*

**Next** (bank 1, in the V):
- Measure knock sensor 1 from the sensor side, terminal 1 to the block. The
  FSM gives 532–588 kΩ. In range means the open is in the connector or
  harness; open means the sensor or its mounting.
- Wiggle the connector and harness at idle while watching
  `mem:ECM_KS1_LEVEL`. A drop from 255 toward 127 locates the open.

The earlier verdicts follow as the record of how this was reached.

**Verdict (revised 2026-10-05, after a follow-up pass; superseded by the
2026-10-08 live read, see above): knock control is active in this image.** A first reading said "fault fallback". It is retracted;
the reasons are below.

- **The 2026-08-01 drive's single retard event** (2,200 rpm, 89 % load,
  timing 11° → 8° → recovering) can only come through the *active* branch. A
  dip that recovers cannot happen with the fixed fallback engaged (verified
  from code plus the drive log). The sensors were confirmed intact the same
  day: unplugging one set the code, and reconnecting it cleared it.
  *(Both halves refuted 2026-10-09: the dip is logged timing with no knock
  column, and P0328 was already being reported on 2026-07-27. See the update
  above.)*
- **The fallback has one code origin.** `0xFFFF13FE` takes `0xFB00` only in
  the branch where bit 7 of `0xFFFF479D` (the knock-sensor fault flag) is set.
  The learned words never take that value (verified).
- **HI DET is not a fault indicator.** The `0xFFFF283F` bit-6 latch is set by
  *either* that fault flag *or* the accumulated-knock metric `0xFFFF1400`
  crossing a threshold. The second is normal protection selecting the High
  Det timing table (verified).
- **UpRev's constant knock columns are not solid evidence either way.** The
  binding of UpRev's stream offsets to `0xFFFF13FE`/`283F`/`28AA` is
  inferred: the sniffer missed the frames where UpRev defines its stream.
- **Knock-sensor faults are never reported by generic OBD (verified).**
  - The Mode 03 and Mode 07 builders list an item only if bit `0x80` is set
    in its config word (table at `0x10FBC`).
  - The four knock-sensor items (`0xAA`–`0xAD`, P0327/P0328/P0332/P0333) have
    config `0x0020`, with that bit clear. Every other item checked has it set.
  - So a generic code read can never show a knock-sensor fault, which is why
    none has appeared. A Nissan self-diagnosis read (service 0x17) does: it
    returned P0328 on 2026-07-27 and 2026-09-24 (see the update above).
  - **Generic DTC polling therefore cannot monitor knock-sensor health.** The
    RAM flags below can.
- **Item status bytes** (three per item, from `0xFFFF657C + 3i`):
  - `657D` bit 7 is "failing now" (live);
  - `657E` bit 2 is the Mode 03 "confirmed" state and bit 3 the Mode 07
    "pending" state;
  - `657E` bit 0 is the early flag that `0xFFFF479D` bit 7 ORs, and it can be
    set before a code is pending.
  The full `17 FF 00` reply is `57 01 03 28 C0`: a DTC count, then the DTC
  and one status byte per code. Its `C0` matches item `0xAB`'s byte 1
  (`677E`) as read on 2026-10-08, on different days. An earlier version of
  this line had only `57 01`.
- **Settle it live, with reads only:**
  - at key-on and warm idle: `0xFFFF479D` bit 7 (0 = normal), the item status
    bytes `0xFFFF677A`–`6785`, and circuit levels `0xFFFF2CEF`/`2CF0` (band
    50–199);
  - during one light pull: `0xFFFF28AE` bit 5 (knock loop running, should be
    1); `0xFFFF1254`/`26D2` (0 at rest, dipping only on an event); and
    `0xFFFF1400` with `0xFFFF283F` bit 6.
  If `283F` bit 6 is set while `479D` bit 7 is clear, HI DET is accumulated
  knock, not a fault.

**Why the knock-sensor codes never show in a code read (decompiler pass,
2026-10-10; verified from code unless marked inferred):**
- **The calibration hides them on purpose.** Each item's config word is
  reached through a pointer table at `0x10FBC`. The table and the words both
  sit in the calibration region, so a tune can edit them. The four knock items
  (`0xF37E/0xF37C/0xF382/0xF380`) read `0x0020` in ZS44A-20260920, -20261004
  and the UpRev base.
  - Bit `0x80` gates Mode 01 PID 01's count, Mode 03, Mode 07 (K-line and CAN),
    the MIL request (byte 1 bit 5 → lamp flag `0xFFFF649C`), the
    pending-at-confirm bit and freeze-frame priority. The knock items lack it,
    so they can never light the MIL.
  - Bit `0x20` only clears "failing now" at key-on.
  - 45 items lack `0x80`; inferred to be the no-MIL, dealer-tool-only set,
    including P0550, U1000/1001, P1550–1554 and P0456.
  - The item table starts at `0x102CC`: the code is the low 16 bits of the
    u32 at record + 4.
- **Status bytes** (three per item from `0xFFFF657C`; item `0xAB` = `677D–677F`):
  - **byte 0:** a warm-up aging counter;
  - **byte 1:** b7 failing now, b6 a latched copy, b5 MIL request, b4 newly
    failing, b2 passed this cycle;
  - **byte 2:**
    - b0 failed at last result: the bit `0xFFFF479D` bit 7 ORs, which holds
      the fallback;
    - b1 failed this drive cycle;
    - b2 listed by Mode 03, computed as b4 ∧ byte 0 < 40;
    - b3 pending; b4 stored; b5 confirmed-active; b6 first-trip.
- **Maturing takes two trips** (`FUN_0007f57a`). A first failure gives byte 2 =
  `0x4B`, which is what 2026-10-08 read. A failure on the next key cycle
  confirms it as `0x3F`, which is what the 2026-10-10 tuning capture read. **P0328 is
  confirmed.** Confirmation changes neither the MIL nor the fallback.
  Readiness comes from `0xFFFF651A`, not item status. Mode 0A doesn't exist.
  Mode 06 doesn't read item status. A knock item *can* take and lock the Mode
  02 freeze frame (`0xFFFF6476`, no `0x80` check), so Mode 02 PID 02 may show
  P0328.
- **What does report them:**
  - CAN service `0x17` (`FUN_000d1908`; the first reply byte is the count, so
    the 09-24 `57 01` was one DTC, and `57 01 03 28 C0` is expected now);
  - K-line `0xA3` (`FUN_00033ab0`);
  - DID `0x120B`.
  None of them checks `0x80`. There's no `0x18` or UDS `0x19` handler.
- **Clearing:**
  - With the circuit back in band, only "failing now" clears. b0 stays, so
    **the fallback holds for the rest of the key cycle**. The next key-on
    releases it.
  - Pending and first-trip clear after 1 good trip; confirmed after 3; the
    `0x17`/`0xA3`/Mode 03 listing after 40 warm-ups. "Stored" stays until a
    clear.
  - Generic Mode 04 (K-line and CAN), KWP `0x14 FF00` (CAN) and K-line `0x14`
    all reach one clear-all (`FUN_00083252`), which erases them. A fault that's
    still present re-sets as first-trip `0x4B` after about one pass (inferred).
- **⚠ Reading DID `0x120B` is not side-effect-free.** When it returns a code,
  the CAN handler clears byte 2 bit 0 on every item (`FUN_0002f748`, near
  `0xD17F4`). That drops the knock fallback for about one pass (inferred) and
  touches every item's last-result flag. Polling it is a volatile write to ECU
  diagnostic state, so no profile should poll it. Read the status bytes
  (`0xFFFF677A`–`6785`) or service `0x17` instead.

**Where the observation values live.** All are RAM, reset at engine stop, and
none is exposed by any Mode 01 PID, Mode 22 DID (both pointer tables
searched) or Mode 06 MID.

- **Sensors:** two, odd firing index → sensor 1/bank 1, even → sensor 2/bank 2
  (`0xFFFF11FC` is the 1–6 firing counter; that it equals the cylinder number
  is inferred). Circuit levels are at `0xFFFF2CEF`/`2CF0`, with an in-range
  window of 50–199.
- **Detection per firing window:**
  - `0xFFFF249B`: the cylinder just processed;
  - `0xFFFF24A8`: per-cylinder knocked bits;
  - `0xFFFF2550 + 4(c−1)`: s32 intensity, unitless;
  - `0xFFFF249C + 2(c−1)`: the threshold in force;
  - `0xFFFF28AA` bit 2: the last window knocked;
  - `0xFFFF249A`: event count, cleared every 10.24 ms.
- **Retard (global, not per cylinder):**
  - `0xFFFF1254`: s16, 1/256°, ≤ 0;
  - `0xFFFF26D2`: whole degrees;
  - `0xFFFF26E6`: limit in force;
  - `0xFFFF13FE`: target.
  - Learned retard `0xFFFF124E`/`1250`/`1252` resets when the engine stops or
    coolant is below 60 °C.
- **Status:** `0xFFFF28AE` bit 5 = knock control active, `0xFFFF283F` bit 6 =
  High Det, `0xFFFF479D` bit 7 = knock-sensor DTC stored. Item status bytes
  are at `0xFFFF677A`–`6785`.
- **Path to final timing (verified):** the applied retard combines into base
  timing at `0xFFFF2338` and reaches the logged timing word through
  `0xFFFF1346` → `0xFFFF232E` (rate-limited), where
  `ECM_IGNITION_TIMING_RAM` = 110 − `0xFFFF232E`. So final timing minus base
  timing shows the retard live.

**Verification, reads only (no ECU writes):**
1. **At key-on (engine off) and at idle**, read `0xFFFF479D`, `0xFFFF13FE`,
   `0xFFFF677A`–`6785` and `0xFFFF2CEF`–`2CF0`, and repeat the scan tool's
   `17 FF 00`. Bit 7 set with `0xFB00` confirms the fallback. The status bytes
   name the item (sensor 1 or 2, low or high circuit), and the circuit level
   shows whether it is in range now.
2. **Simultaneous with UpRev** (CANary passive on the ECM session): the three
   addresses should match UpRev's Knock Strength, HI DET and Knock Event.

**Open:**
- which item is stored, and whether it is a sensor, a harness, or a stale code;
- whether the status survives a battery disconnect;
- whether `0xFFFF11FC` order equals cylinder number.

Proposed memory windows (`0xFFFF232E`/12, `0xFFFF124E`/8, `0xFFFF13FE`/2,
`0xFFFF249A`/15, `0xFFFF2550`/24, the flag bytes, and a ≤ 1 Hz fault window)
are in `~/ecu-re/NOTES.md`, session 5. They go into the catalog through the
`data/dids.yaml` owner, with known-answer checks.

### A/F sensor and λ chain: one sensor per bank, corrected for altitude only in PID 0x24

Decompiler pass, 2026-10-05, checked on logged data. It answers whether the
"+2–4 % lean at WOT" (2026-09-21..23) rested on three independent
instruments. It did not: there is one A/F sensor per bank.

- **Voltage words (verified).** DIDs `0x1225`/`0x1226` → `0xFFFF2C02`/`2C04`
  on both the CAN table `0x24B84` and the K-line table `0x22938`.
  - Bank 1 = AN8 − the 10-sample mean of AN11 (clamped 440–460) + 440,
    unfiltered and unscaled. Bank 2 uses AN20/AN23.
  - The banks are assigned from the code's own fault items (P0131/P0132 bank
    1, P0151/P0152 bank 2).
- **PID 0x24/0x28 λ (verified).**
  - A,B = `(0xFFFF46C2 << 14) / 3675`, so λ = word/7350, with the word =
    AFR × 500. `46C2` is last cycle's copy of `0xFFFF4722`.
  - `0xFFFF4722` is computed at `0x3FAD0` from the DID `0x1225` word, in
    order:
    1. double it;
    2. apply a **barometric gain** about stoich: `x' = 880 + (x − 880)·128/k`,
       with k from table `0xD00F` on raw baro `0xFFFF4C30`. k = 128 at
       99.5 kPa, 112 at 80 kPa, 94 at 63 kPa, so it is always active at this
       truck's altitudes;
    3. look up a 16-point AFR table (values `0xE644`, breakpoints `0xF12A`);
    4. add a learned downstream-O2 offset `0xFFFF4CA4` (±1 AFR; "rear trim"
       is inferred);
    5. add a small rpm × load offset table `0xACF4` (≤ +0.07 AFR).
  - No temperature or heater correction is applied.
  - PID 0x24's voltage bytes C,D are raw AN8 × 0.0050024 V, not the DID
    word: they read about 0.02–0.05 V higher.
  - PID 0x28 is the same for bank 2 (`0xFFFF4724`, `46C4`).
  - The 2.0 seen in fuel cut is the PID field saturating (word ≥ 14,700), not
    an ECM clamp.
- **The fuel loop controls this same value (verified).** The STFT controller
  `0x5E368` measures `0xFFFF4722`/`4724` against target `0xFFFF46B2`
  (default 7350 = 14.7). So PID 0x24 agreeing with stoich in closed loop
  proves nothing about the sensor's accuracy.
- **UpRev's AFR and CANary's voltage curve are the same uncorrected
  calculation.**
  - `afr_calibration.yaml`'s curve is the ECM's `0xE644` table *without* the
    barometric gain (within 0.26 % over 10–13 AFR, 0.83 % at worst). UpRev's
    lookup also skips the gain and both offsets.
  - On same-reply pairs, a single gain makes the table reproduce PID λ within
    0.3 % (λ 0.70–1.30). The fitted gain follows altitude as the code
    predicts:

    | Session | Baro | Fitted gain | Code predicts |
    |---|---|---|---|
    | 09-17 | ~76 kPa | 1.175 | 1.174–1.195 |
    | 09-21 | 80–81 kPa | 1.120 | 1.130–1.139 |
    | 10-04, both banks | 81–83 kPa | 1.105 | 1.109–1.130 |

  - At about 81 kPa the uncorrected path reads, relative to PID 0x24:
    +1.5 % leaner at λ 0.85–0.90, +2.5 % at 0.80–0.85, and +3.9 % at
    0.70–0.80. This explains the 09-17 "voltage path +3.1 to +3.7 % leaner
    at 12.1–12.3 AFR" observation.
- **What this means for the WOT-lean finding.**
  - Two of its three "instruments" are one uncorrected calculation. It is
    biased lean at altitude by about the size of the finding.
  - PID 0x24, the only corrected one, gave +2.9 % on 09-21, but −1.1 to
    +0.7 % on the five held-out drives.
  - So the lean is not established. An independent check needs an external
    wideband with its own controller, logged with baro and pressure-corrected
    per its maker. The AEM gauge measures fuel pressure, not λ: it can test
    delivery sag, but not λ.
- **Catalog consequences** (for the owners of those files):
  - the voltage-path routes in `lambda_delivery.yaml` are derived from the
    same sensor as PID 0x24/0x28, and are biased unless the gain is applied;
  - `afr_calibration.yaml`'s curve provenance is "ECM table `0xE644`, gain 1";
  - candidate memory channels: `0xFFFF4722`/`4724` (λ × 7350), `46B2`/`46B4`
    (closed-loop target), `4CA4`/`4CA6` (learned rear offset), and
    `0xFFFF36BB` (k, 128 = none).

### Injector pulse chain, dead time and the BFS ceiling

Decompiler pass, 2026-10-05, checked on six tuning captures: the per-sample
ratio of (DID − Ts) to the predicted pulse has median 1.006, p10–p90
0.961–1.030, over 5,039 steady samples; at BFS above 28k it is 1.007, n = 594.
46 fuel-cut reads equal the predicted Ts within ±2 counts.

- **Order (verified).** The task table `0x0219D0` that the main loop
  `0x020EF2` walks (134 entries, `full.asm` `0x2129C`–`0x212A6`) runs, as these
  entries:
  1. φ (`0x575EC`), entry 37;
  2. dead time Ts (`0x57144`), entry 41;
  3. the fuel-pressure correction (`0x53B8C`), entry 42;
  4. transients (`0x527CC`), entry 44;
  5. the per-cylinder stages.

  `0x21A30`, quoted here earlier as a separate table, is the address of entry
  24. The loop's *nominal* period is 10.24 ms, but a late cycle is not caught
  up, so the real period is a measurement ("Scheduler and task tables", below).
  Crank-segment code loads the ATU-II channel-8 one-shot counters (`0xFFFFF640 + 2c`).
- **Formula (verified; u16 words, u32 intermediates):**

      base_b = (BFS 0x1038 × φ 0x26BE) >> 17                 # BFS·φ/8, φ in Q14
      G_b    = max(246, ((base_b·α_b/0x6400 + wallfilm 0x26DC) × w1800) >> 14)
      k_b    = STFT 0x26B4/6 + LTFT 0x26DE/E0 + w2E8C − 0xC800   # trims ADD; 0x6400 = 1.0
      Ti_b   = (G_b·k_b >> 16) + Ts                          # bank pulse 0x1B9A/1B9C
      x_c    = (Ti_b + 0x141A[c] − 0x152E[c]) × D605[c] >> 7 # D605 = 128 for all six
      P_c    = max(short-pulse(x_c), min pulse 0x2770)       # short-pulse cal 0x9120–0x9126

  - α is the EVAP purge factor (`0x4698`/`469A`). `w2E8C` is a
    diagnostic perturbation, neutral in normal driving.
- **EVAP purge factor α (structure verified 2026-10-27, `EVAP_purge_control`
  `0x6A52C`; headless read-only decompile with the divide helpers).**
  - **Reset:** `α_b = 0x6400` (1.0) whenever `w9134 = 0` (purge inactive this
    tick or the last) or `b8E48 ≥ 2`.
  - **Filter:** otherwise α is `w9050 = (g·α* + α_prev·(0x8000 − g)) >> 15`,
    with `g = w10EC·5/2` (an airflow-like word, inferred). It is clamped to
    [`0x3200`, `0x6400`], so it never cuts below 0.5; `w3B3E` sets a further
    floor.
  - **Target:** `α* = (0x8000 − v_b·200/D)·25/32`, which maps 0x8000 to
    0x6400, with `v_b = min(0x8000, b3D4E·(T_b + 250)·2/125)`.
    - T_b = `0x46B2`/`46B4` is the per-bank A/F target ×500 (7350 = 14.7).
    - D = `(F >> 9) + 200`, with F the purge-flow word (chosen from
      `38AC`/`8FD6`/`8FDE`/`1816`). When `47F0` bit 2 is set, D comes from
      `w2BB4` instead.
  - **Vapour quantity:** `b3D4E = min(255, (w900C·f(F)) >> 20)`. `w900C` is
    the 8×8 u16 map `0xBF74` (axes `0xDF02`/`0xDEC2`), indexed by `w900E`
    (vacuum-like, from `w8E28`) and `w9010 = 0x1388000 / (5·((w9002 >> 8) +
    0x91))`. `w9002` follows `w8E22` while purging and `w2BD0` otherwise:
    the learned vapour concentration (inferred).
  - **Copies:** α at `0x9064`/`9066` and `0x8E00`/`8E02`.
  - **Checks:** the constants 0x19 with >>5, 200, 125, 250, the shifts and
    the 0x8000 literal at `0x6E514` are line-checked in the listing. The
    0x3200 floor and the gain come from the decompile only. Maps `0xBF74`
    and `0xBEF4` and their axes are byte-identical in 20260920 and 20261004.
  - **Data** (four tuning captures 10-26/27, pulse-chain ratio with α held
    at 1, n = 1,659): 1.006 with purge off, back within 2 s of purge ending.
    With purge on it falls 1.01 / 0.98 / 0.96 / 0.93 / 0.909 at 0–4 / 4–8 /
    8–16 / 16–32 / > 32 s after DID 0x1110 turned on. A term proportional to
    duty at that instant would have dropped at once, and a slow release would
    have left purge-off reads below 1; neither is seen. The settled level
    differs by drive (0.904–0.944).
  - **Not separated:** the α filter from slow learning of `w9002`.
  - **Unverified:** α live (never polled); which D path the truck takes; the
    meanings of F, `w2BB4`, `w10EC` and `w9002`; the tick rate; whether
    `w9134 ≠ 0` matches DID 0x1110 > 0.
  - Units: 10 µs per count. Inferred, but the timer clock and UpRev's ×0.01 ms
    scale agree.
- **Dead time is linear, not a table (verified).**
  `Ts = max(0, 47 + ((175 − Vb)·92) >> 7)`, with `0x8C41` = 47, `0x84F2` = 92,
  and Vb = PID 0x42 / 0.08 V. That is 0.47 ms at 14.0 V plus 0.090 ms per volt
  below it.
- **Fuel-pressure correction (verified structure).** `w1800 = b1772 × 10601
  (0x9254) >> 8`, where `b1772` is a linearised `256·√(350/(350 + vacuum))`.
  - 175 at `0x8A89` is read as 350 kPa (inferred).
  - Vacuum is *estimated* from baro and the load and rpm curves `D627`/`D637`.
    The estimate never falls below about 0.2·baro.
  - Removing this term makes the data ratio drift from 0.97 to 0.91 with
    vacuum.
- **Coolant and after-start enrichment live inside φ** (the `0x26F6` terms and
  the cold state), not as separate pulse factors. Start pulses and the min pulse
  fade out after start; the steady min pulse is 50 µs.
- **DIDs `0x1206`/`0x1207` (verified)** = `0xFFFF2710`/`2712`, the
  cycle-start copies of `279C[0]`/`279C[1]`. That is the per-cylinder pulse for
  injector index 0 (bank 1) and 1 (bank 2); that these are cylinders 1/2 is
  inferred.
  - The value **includes dead time**, trims, transients and the pressure
    correction.
  - It is taken **before** the short-pulse correction and the min-pulse floor.
  - Unit 10 µs. In fuel cut both read Ts.
- **Short-pulse linearisation (verified, F01.4).** `INJ_short_pulse_linearize`
  `0x05B8B0` maps the per-cylinder pulse x (`0xFFFF279C[c]`, the DID 0x1206
  word) to the output y (`0xFFFF27A8[c]`):

      a = min(0xFFFF, (x·w9120 + 100·w9124) >> 14)
      b = min(0xFFFF, (x·w9122 + 100·w9126) >> 14)
      y = max(x, a, b)

  - Calibration 16358, 15846, 74, 959 (u16 at `0x9120`–`0x9126`), identical
    in every ZS44A image read (the ecuread, Tuned 20260920, 1004, 1110, 1111,
    1111-2, and ZS44A-Tuned). In `rom_map.yaml` as `short_pulse_gain_a/b` and
    `short_pulse_offset_a/b`; `src.analysis.injector_pulse.short_pulse_linearize`
    implements it.
  - Term b lifts pulses below about 1.78 ms (+5 counts, 50 µs, at x = 0; +2 at
    1.0 ms); term a reaches x only below about 2.84 ms, by under one count. So
    y − x is at most 50 µs and is zero above 1.78 ms.
  - Because DID 0x1206 is x, the pulse-chain known answer is unaffected by
    this stage. Whether the plant fuels on y is the plant model's choice
    (`short_pulse_undecoded` in the simulator still refuses short pulses).
  - Live check: one window reading `0xFFFF279C` and `0xFFFF27A8` (both six
    u16s) at hot idle, where x is shortest.
- **PID `0x44` is not φ in general (verified).** `PID 0x44 = 0x26BE ×
  0x46AC / 3675`, and `46AC` copies the A/F feedback target `46B2`. So it
  equals φ only while that target is 7350 (14.7). 2,437 idle reads of 0.871
  have pulses matching φ = 1.0. Use `mem:ECM_TARGET_EQUIV_RATIO` for φ.
- **K and injector size (verified).**
  `Tp = min(0x726F9, K·Qa·226/256 ÷ (3·N)) × 0x23CB >> 16`, about
  0.0105·K·Qa/N (N = rpm/1.5625), and BFS ≈ 4·Tp·FC/128.
  - K moves BFS one-for-one, and with it the Target AFR X byte, the timing-trim
    load axis, the pressure model's load input, the wall film, and every BFS
    threshold.
  - **A separate injector-flow scaler, `0x9254` = 10601**, multiplies every
    pulse after BFS (main, per-cylinder, async, start and min) without moving
    any load axis.
  - Injector-specific constants: Ts (`0x8C41`/`0x84F2`), short-pulse
    (`0x9120`–`0x9126`), rail reference `0x8A89`, and the start/min curves.
    All are identical in the UpRev base and this tune; only K differs.
- **BFS ceiling (verified, data-checked).** `D8B3` is 255 at all 16 rpm points
  in base and tune. The cap is 65,280 counts (32.0 ms at ÷2040), effectively
  the 16-bit limit.
  - WOT at about 81 kPa peaks at 37,562 (18.4 ms) at 4,000 rpm, 0.48–0.58 of
    the cap across 1,500–5,500 rpm.
  - +50 % air at home altitude stays under it (about 56k). At sea level with
    today's K it would clip (about 70k).
  - **The first limit is the load axes:** Target AFR and timing saturate at
    BFS 44,019 (the Load Point Scaler), only 17 % above today's WOT peak.
- **Fuel cut (verified).**
  - Rev limiter (`0x4F768`, from map-slot words `0xFFFFB040`–`46`): cuts at
    6,350 rpm and resumes below 6,150. With the unidentified flag
    `0xFFFF2841` bit 1 set, it is 6,200 / 5,800.
  - Stationary cut: speed 0, ≥ 3,600 rpm and BFS ≥ 36,864, after a delay.
  - The BFS decel-cut curves are all zero.
  - Limp cut at 2,800 rpm on diagnostic item `0x30`.
  - No BFS- or airflow-based cut while moving.
- **The cut mask and the decel cut (F01.5, 2026-11-12).**
  - **One writer.** `0xFFFF26B8` is written only by `FUELCUT_mask_26B8_task`
    (`0x04F240`), main-cycle entry 29. The fuel task is entry 37, so the mask
    is always this cycle's when the command is formed.
  - **The mask** is `0x3F` (all six cylinders) if any of about fifteen
    condition routines returns true, ORed with a second decel group and the
    item-`0x30` limp cut, then `& 0x3F`. A test mode (`0xFFFF47FE` bit 3)
    substitutes `0xFFFF2F78`.
  - **Decel cut** (`0x050130`; decompile text, with branches on this image's
    calibration pruned by the decompiler). It needs the idle flag
    `0xFFFF2833` bit 4 and `0xFFFF2835` bit 4. It then cuts while rpm
    exceeds curve `0xD379` on the rescaled coolant byte, or, on the path
    taken when `0xFFFF282B` bit 6 (and `0xFFFF282F` bit 6) is set, while
    speed `0xFFFF111C` > 3 and rpm exceeds curve `0xD3AD`. A fault flag
    (`FaultGroup_flags` bit 3) adds 600 rpm, and another
    (`FaultGroup_flags_snap` bit 0) sets an 1,800 rpm floor. Each cut sets
    `0xFFFF47AB` bit 6. There is no hysteresis in this routine: it resumes
    when rpm falls to the curve or the throttle opens.

    | Coolant °C | −40 to 0 | 10 | 20 | 30 | 40 | 50 | 60 | 70 | 80–100 | 110 |
    |---|---|---|---|---|---|---|---|---|---|---|
    | `0xD379`, rpm | 3,000 | 2,800 | 2,700 | 2,600 | 2,500 | 2,400 | 2,200 | 2,000 | 1,800 | 1,800 |
    | `0xD3AD`, rpm | 3,000 | 2,800 | 2,700 | 2,600 | 2,500 | 2,400 | 2,200 | 2,000 | 1,400 | 1,600 |

    The curves are identical on all seven images.
  - **Data** (ten tuning captures 2026-10-26 to 11-12, 20,633 command reads
    against Mode 01 rpm and throttle within 0.3 s, coolant 33–96 °C). The
    1,884 cut reads (the `_SENTINEL` channel) span 1,312–5,350 rpm (5 %:
    1,838). With throttle ≤ 6.0 %, no read below 1,300 rpm is in cut, and
    66 % of reads at or above 2,000 rpm are. That is consistent with the
    curves, but not with the throttle flag alone: `0xFFFF2835` bit 4 (not
    identified) or a delay gates the rest.
  - **Dead here:** the zone cut `0x050000` (rpm tables `0xD389`/`0xD391`
    all zero, and `0xFFFF287C` bit 1, its zone flag, is only ever cleared),
    `0x04F6F2` (returns 0), and the BFS decel curves.
  - **Other cut routines, traced from decompile text:**
    - Coolant ≥ 140 °C (°C + 50 byte ≥ 190, with hysteresis) cuts off idle
      above 2,200 rpm (`0x04F994`).
    - Stationary, off idle, an accumulator over rpm curve `0xD1CF` reaching
      1,200 cuts above 1,750 rpm and resumes below 1,500 (`0x04F808`).
    - `0xFFFF2823` bit 7 cuts at ≥ 2,400 rpm (`0x04FC26`).
    - `0xFFFF47CB` bit 3 cuts above 2,800 rpm, with 300 rpm hysteresis
      (`0x04FFB4`).
    - `0xFFFF47EE` bit 3 with `0xFFFF545C` > 4 cuts above 2,500 rpm and holds
      to 2,000 (`0x050EA4`).
    - `0xFFFF478D` bit 7 cuts (`0x04FFF4`).
    - `0x050D58` combines a speed-word limit (`0xFFFF1044` ≥ 0x3852) and the
      throttle failsafe.
  - **Not decoded:** the re-entry group `0x05059C`/`0x05099E`/`0x050906`
    (`0xFFFF15AE`/`15AF`, `0xFFFF3C80`–`3C84`), the meanings of the fault and
    diagnostic flags above, and `0xFFFF2841` bit 1.

### Closed-loop enable and trim authority (F01.2, 2026-11-12)

Decompile text of `0x05E368`, `0x05CCD0`, `0x05C9F4` and their helpers, with
the clamp constants and the PID 0x03 mapping line-checked in the listing. The
calibration values are identical in the ecuread and all six tuned images.
Both routines run in the main cycle (task `0x0219D0` entries 56 and 57).

- **Controller** `0x05E368`. Measured A/F `0xFFFF4722`/`4724` (×500, clamped
  1,500–20,000) against target `0xFFFF46B2`/`46B4`. Output `0xFFFF2D08`/`2D0A`
  is clamped to [cal `0x8F64`, cal `0x8F62`] = [19200, 32000], i.e. 0.75–1.25
  with 0x6400 = 1.0. The integrator stops while last cycle's output
  (`0xFFFF2D30`/`2D32`) sits at a clamp and the error pushes further. The
  target is 7350 (14.7) unless one of three special modes in `0x060E6A`
  selects another; it does **not** follow the enrichment command.
- **Arbitration** `0x05CCD0`, per bank b, first match wins:
  1. `0xFFFF2F8E` bit b: the forced value `0xFFFF2E86[b]` (written by
     `0x09DA10`, a diagnostic routine).
  2. `0xFFFF2D24` bit b: `0xFFFF17BE[b]` (written by `0x0599C8`).
  3. Ready latch `0xFFFF26B9` bit b clear: `0x6400`.
  4. Post-transition hold `0xFFFF88BC[b]` (`0x05CB70`): `0x6400`. It starts
     when the multiplier `0xFFFF26C0` returns to `0x4000`, the correction
     `0xFFFF1736` returns to 0, or `0xFFFF15B3` returns to 0. It ends when the
     measured A/F is within ±150 (±0.3 AFR) of the target, or after
     20 × curve `0x237D2` cycles (axis `0x237CA` on `0xFFFF11D8 >> 1`; values
     20, 10, 5, 3, 3, 2, 2, 1, code constants): about 4 s at the lowest axis
     point, 0.2 s at the highest.
  5. Global sensor hold `0xFFFF8960` (`0x05D94E`, sentinel readings of the
     coolant and speed bytes): `0x6400`.
  6. Open-loop condition `0xFFFF8963[b]` (`0x05DD04`): `0x6400`. Any of: base
     pulse at its floor (`0xFFFF2886` bit 4), `0xFFFF3057` bit b (a test mode
     `0xFFFF99D0` bit 1, or `0xFFFF47FE` bit 5 with `0xFFFF286E` bit 4, or
     `0xFFFF2880` bit 7 with `0xFFFF286E` bit 3), any cylinder in fuel cut,
     `0xFFFF479E` bit 5 (written by `EVAP_purge_control` among others),
     `0xFFFF2D0C`, `0xFFFF47BE` bit 0 or `0xFFFF47D3` bit 7, or the throttle
     failsafe `0xFFFF28BB` bit 6 with BFS above `0xFFFF1BEA`.
  7. **Enrichment gate** `0xFFFF8961[b]` (`0x05DB04`): `0x6400` while the
     multiplier `0xFFFF26C0` ≠ `0x4000`, or `0xFFFF1736` ≠ 0, or
     `0xFFFF3054` bit b, or a sentinel timer runs.
  8. Backup controller `0xFFFF8965[b]` (`0x05DDDA`): `0xFFFF46A8[b]`.
  9. Otherwise the controller output.

  The choice is clamped again to [cal `0x8F66`, cal `0x8F68`] (the same
  19200/32000) and `0x05C9F4` copies it to `0xFFFF26B4`/`26B6`. With
  `0xFFFF99D0` bit 1 set it writes `0xFFFF9931 << 8` instead (a test mode).
- **Ready latch** `0xFFFF26B9` bit b. It sets once `0xFFFF8958[b]` has held
  200 cycles (`0xFFFF1797 > 19`, about 2 s), or when `0xFFFF39B6[b]` > 807.
  It clears at once when the conditions fail. `0xFFFF8958[b]` (`0x05D78E`)
  requires all three of:
  - **Time since start.** `0xFFFF3C94` counts main cycles from the falling
    edge of `0xFFFF4785` bit 6 and must exceed 100 × curve `0xD07F` (axis
    `0xDB5F` on `0xFFFF1076`). The curve is 30 below 40 counts, 8 from 50 to
    110, and 4 from 120. Reading `0xFFFF1076` as start coolant in °C + 50,
    that is about 31 s below −10 °C, 8 s from 0 to 60 °C, and 4 s above
    70 °C.
  - **Coolant.** `0xFFFF1079` (°C + 50) ≥ 70 (20 °C), or ≥ 60 (10 °C) when
    `0xFFFF1076` ≥ 45, or > 69 once a second timer allows.
  - **A/F sensor ready**, `0xFFFF10D6` bit b.
- **Mode 01 PID 0x03** reads `0xFFFF4741`/`4742`, set by `0x083058`:
  - 8 if `0xFFFF11C9` or `0xFFFF26BC` (backup controller) bit b;
  - 1 if the ready latch is clear;
  - 4 if neither the controller (`0xFFFF26BA` bit b) nor the alternate value
    (`0xFFFF2D24` bit b) drives the trim;
  - otherwise 2.

  So `FUEL_STATUS` is the firmware's own verdict on every gate above, and
  CANary already logs it.
- **Data check** (ten tuning captures 2026-10-26 to 11-12, 17,061
  `FUEL_STATUS` reads paired with Mode 01 STFT and `mem:ECM_TARGET_EQUIV_RATIO`
  within 0.4 s):
  - With commanded φ above 1.02, 519 of 536 reads report status 4 and 13
    report 1 (cold). STFT reads exactly 0 % on 99.1 % of them. Enrichment
    **resets** the trim to 1.0; it does not freeze it.
  - At φ 1.0, 922 reads report 4: decel cut, the post-enrichment hold, or the
    pulse floor. They were not separated.
- **What UpRev's rule gets right and wrong.** "Closed loop only where the cell
  is exactly 14.7" holds for rich cells: a gated lookup of 130 or more
  (cal `0x8B39`; 129 while releasing) moves the multiplier off `0x4000` and
  the trim resets. It is wrong for lean cells and cells of 128–129. The gate
  writes φ 1.0 for those, the multiplier stays at `0x4000`, and the loop runs
  at 14.7 there, so a lean cell is ignored, not open-loop.
- **Trim authority:** each bank's STFT is limited to ±25 %. The
  controller's gains and step size are not recorded.
- **Open:** the meanings of `0xFFFF3054`, `0xFFFF3057`'s sources,
  `0xFFFF2D0C`, the `0xFFFF47xx` monitor bits, `0xFFFF1736` and the sentinel
  timer.

### Cold and warm-up fuelling, first pass (F01.3, 2026-11-12)

- **No Cold Fuel Comp table on this ROM.** UpRev's guide says the Cold table
  exists "depending on the ROM being used". Here, the only callers of the
  Fuel Comp wrapper `0x026A24` are `Fuel_state_init` (`0x02DC44`, once at
  reset) and the per-cycle lookup (`0x03D0F4`). Both pass the slot table
  `0xFFFFB6A0` with axes `0xCACD`/`0xCB29`. No other code loads those axis
  addresses or the table base, and an opcode scan finds no other access to
  `0xFFFFB6A0`–`0xFFFFB79F`. The alternate BFS `w1282`, selected by
  `0xFFFF2838` bit 6, is BFS plus a transient term (`INJ_transient_cyl_task`,
  `0x0528FC`), not a cold table. Below 70 °C this engine fuels through the φ
  corrections.
- **`L175C`, the warm-up enrichment** (`0x0537AE`). Its regime is set by
  F01.2's flags:
  - While the closed-loop ready latch `0xFFFF26B9` is clear (and not
    `0xFFFF2867` bit 6), it is (`w1762` + `w1726`) × `w14DA` >> 14.
  - `w14DA` falls by 49 per call once both banks' loop conditions
    (`0xFFFF1796`) hold, so the enrichment fades out as closed loop arrives.
  - `w1762` = `w1760` − (`b173F` × (`w1760` − curve `0xC774`[rescaled
    coolant] << 8)) >> 8: a blend toward a coolant curve, with
    `b173F` = (`b17FD` × 166 + `b17FE` × …) >> 8. Inputs are not identified.
  - With the latch set, a second formula applies, in `w1765`, `b173F`,
    `w1802`, `b1BB7` × `w1820` and `w3716`; it is not decoded further.
- **The curve term `w27DC`** (`0x055966`) applies only during F01.2's
  post-transition STFT hold: `0xFFFF135F` bits 0 and 1, the multiplier at
  `0x4000`, the ready latch set, and `0xFFFF2A90` clear. Its input
  max(`0xFFFF2D10`, `0xFFFF2D14`) is a per-bank state that `0x05F970` (main
  entry 53) computes from the measured A/F's excess over 14.7, or from the
  command when a sensor is not in use. It goes through a two-stage
  estimator (`0x05FB6A`, `0x0605EA`; states `0xFFFF2D38`/`2D44`). `0x05BC58`
  (main entry 58) presets it to `0x1820` once. That this is a catalyst
  oxygen-storage estimate, enriching after a lean excursion such as a fuel
  cut, is inferred.
- **Multiplier hold `0xFFFF2947`.** A 10.24 ms byte timer (F01.1), loaded
  with cal `0x879E` = 2 by `0x05687E` while `0xFFFF2BA9` < `0x20`. So it
  forces the multiplier to `0x4000` for about 20 ms. `0xFFFF2BA9` is still
  unidentified.
- **Not done:** the start and minimum pulse curves, the wall-film term's
  purpose, `0xFFFF1114`/`0xFFFF10C6` (their stores are indexed, and the
  opcode scan does not resolve them), and the vehicle check, a cold-start
  capture that reproduces `mem:ECM_TARGET_EQUIV_RATIO`.

### The MAF curve and the MAF-fault fallback (F01.8, 2026-11-12)

**The curve: `rom_map.yaml` `maf_transfer_curve`, editable.**

- **Lookup.** `0x03BB4E` calls `0x026C44` with `r4` = `0xE27E` and `r5` =
  `0xFFFF2118` << 4, and stores the result to `0xFFFF1054`.
  - `0x026C44` returns entry 63 for an input of 0x3F00 or more.
  - Below that it interpolates at index input >> 8 with fraction input & 0xFF
    (`0x027244`, 16-bit cells).
  - So this is a 64-entry curve on a uniform axis: entry *i* sits at
    `0xFFFF2118` = 16 *i*.
- **Axis.** `0xFFFF2118` is the MAF A/D word `0xFFFF102C` (DID `0x1204`)
  less the after-start hot-wire term, which is 0 once warm.
  - At the nominal 5/1024 V per count, entries are 0.078 V apart, covering
    0–4.92 V; above that the curve is flat.
  - The A/D reference is not read from code, so the volts are nominal. The
    counts are exact.
- **Cells.** The airflow word (149/25,600 g/s per count). They read 0 at
  entry 0, 204 (1.2 g/s) at entries 1–6, then rise monotonically to 62,215
  (362 g/s) at entry 63.
- **Same on every image.** No UpRev tune has changed it.
- **Checked against the truck.** The 2026-09-17 DID sweeps
  (`~/canary-data/did_log`) read DID `0x1204` about 5 ms before DID
  `0x1209`. On 278 steady pairs, the per-sample ratio of `0x1209` to this
  curve at `0x1204`:

  | Airflow | Median | p10–p90 |
  |---|---|---|
  | Below 8 g/s | 1.0000 | 0.993–1.005 |
  | 16–200 g/s | 1.007 | — |

  The higher band reads about 0.7 % high. The purge vapour term `0xFFFF273E`
  adds to the reported word, which fits that offset.
- **What it means for the build.** This is the lever for the cone intake
  and a new MAF housing. Only the product K × curve sets the base fuel
  schedule, and the curve also moves every load axis (as K does). The
  simulator does not route a curve edit through the airflow word yet
  (E09.8). Until then, the classifier names the lever and blocks on
  `airflow_qa`.

**The MAF-fault fallback.** When diagnostic item `0x114` faults (MAF
circuit; `0xFFFF2824` bit 7), `0x03CAE6`–`0x03CB46` replaces BFS with a
table:

- BFS = `0x95E5`[(rpm band − 1) × 4 + throttle band] << 8.
- **Rpm band** comes from `0xFFFF1206` (rpm / 12.5, `0x038FEA`). It is 1, 2
  or 3 below 800, 1,600 and 2,400 rpm.
- **At 2,400 rpm and above, BFS is 0, so fuel is cut.** The FSM states this
  fail-safe (EC section, MAF circuit: "Engine speed will not rise more than
  2,400 rpm due to the fuel cut").
- **Throttle band** comes from `0xFFFF107F`, the second input E09 had left
  unidentified. It is relative throttle position, the byte PID `0x45` and
  DID `0x111E` report, and `0x0704DE` writes it. The band is 0–3 at 7, 15
  and 22 counts (2.7, 5.9 and 8.6 %).
  - Its value is the secondary throttle reading above the closed reference
    `0xFFFF2D58`, × 0x505B / 65,536, capped at 255.
  - It is 18 under a throttle-sensor fault (`FaultGroup_flags` bit 2).
- **Table** (raw bytes, identical on every image):

  | rpm band | < 2.7 % | 2.7–5.9 % | 5.9–8.6 % | ≥ 8.6 % |
  |---|---|---|---|---|
  | < 800 | 54 | 60 | 76 | 97 |
  | 800–1,600 | 50 | 52 | 64 | 90 |
  | 1,600–2,400 | 48 | 49 | 56 | 84 |

  Above 8.6 % throttle the table stops rising. So a truck with a failed MAF
  makes little more than light-throttle power, up to the 2,400 rpm cut.

### Idle air: the throttle-area correction and its learning (F01.10, 2026-11-12)

What the ECM learns as "idle air" is not an idle-speed controller. It is a
throttle-area correction. The loop matches the area the throttle model
reads from the throttle position to the area the measured air implies, at
steady points. All routines below run from main entry 78 (`0x0545E8`) in
the order `0x054606`, `0x0549BE`, `0x054C24`, `0x054D6E`.

- **The two areas.** The air model (`0x03C4D0`) computes both:
  - `0xFFFF2C30`: the throttle model's area, curve `0xEE6A` at the
    throttle position, less the correction below.
  - `0xFFFF2CC4`: the area the measured air needs. It starts from
    `0xFFFF46FE` (the cycle copy of `0xFFFF475E` =
    (`0xFFFF3B78` × BFS) >> 5, written by `0x054958`), passes through the
    curve pair `0xEEDA`/`0xEDCA` (or `0xEF1A`/`0xEE0A` from 0xFC00), and is
    multiplied by the rpm word and by 158 (cal `0x8D25`).

  The cycle copy carries them to `0xFFFF2B48` and `0xFFFF2BDC`.
- **Integrator** `0xFFFF3CB2` (`0x0549BE`).
  - While `0xFFFF480B` bit 4 is set and |`2B48` − `2BDC`| > 2, it steps by
    (`2B48` − `2BDC`) × 5 / 256. The gain is 10 / 256 during Idle Air Volume
    Learning.
  - It is clamped to −80..1,184, so it converges where the two areas agree.
  - With the bit clear, two decays apply:
    - a one-shot −254 (floor 127) on a closed-throttle condition set;
    - −10 per 20 calls (floor 20) while `0xFFFF1274` = `0xFFFF1934`.
- **Learned term** `0xFFFF5EA6` (`0x054D6E`).
  - It steps ±1 toward the integrator's sign when the integrator is at
    least 2 or at most −2.
  - It is clamped to −1,216..1,024.
  - It steps once per arming of `0xFFFF47D9` bit 0, and not while
    `0xFFFF4785` bit 7 is set.
  - On each step the integrator is saved to `0xFFFF5EA0`, with its
    complement at `0xFFFF52CE`.
- **Correction** `0xFFFF6560` = min(1,185, integrator + learned)
  (`0x054E3E`, `0x054E72`). It goes to two places:
  - The electronic throttle adds it to its target area (`0x070736`), so
    the throttle opens to supply the extra air.
  - The cycle copy carries it to `0xFFFF63FC`, which the air model
    subtracts (`0x03C5AA`).
- **What survives a key cycle.**
  - `0xFFFF5EA0` sits in the battery-backed RAM block, so it survives a key
    cycle on its own. `0x05455C` reads EEPROM records 0x71/0x72 into it
    only when that block is re-initialised or a RAM complement pair breaks,
    and only if the two records are complements. Otherwise it uses 0 (F01.11
    below; corrected 2026-11-12 from "at start-up").
  - The power-up init `0x0591B8` (reached from `0x028E14`, inferred to be
    the start-up path) sets the integrator from `0xFFFF5EA0` and zeroes the
    learned term.
  - So the integrator is what persists. The learned term restarts each
    power-up.
- **Feedback enable** `0xFFFF480B` bit 4 (`0x054606`). All of these must
  hold:
  - no fault on the MAF item `0x114` or three other items, whose ids the
    decompiler lost;
  - coolant 70–100 °C (bytes 120 and 150, hysteresis 3);
  - flags: `0xFFFF4785` bit 7 and `0xFFFF479E` bit 1 set, `0xFFFF6955`
    bit 7 set, `0xFFFF2838` bit 7 clear, `0xFFFF47B4` bit 4 clear and
    `0xFFFF4782` bit 3 clear;
  - `0xFFFF27D0` < 0x255;
  - `0xFFFF283E` bit 6 set;
  - steady over the last four cycles: BFS within 126, `0xFFFF11D2`
    within 40, and `0xFFFF2B08` within 2;
  - either IAVL is active or `0xFFFF1274` ≠ `0xFFFF1934`;
  - all of the above held for a 25-call timer (`0xFFFF4952`).
- **Idle Air Volume Learning** (`0x054C24`, the FSM's work-support
  procedure), requested by `0xFFFF47DF` bit 2.
  1. While feedback is enabled, it zeroes the learned term and runs the
     integrator at double gain.
  2. After 700 calls it sets `0xFFFF47D9` bit 1 (done) and `0xFFFF47CA`
     bit 0, and clears the request.
  3. The result is the integrator, which is then saved as above.
- **After the throttle-body swap.** A larger bore passes more air at the
  same position. The integrator absorbs that as a negative area
  correction, down to −80 (plus −1,216 learned). That is what IAVL should
  re-learn after the swap.
- **RAM catalog:** `mem:ECM_IDLE_AIR_INTEGRATOR`, `_STORED`, `_LEARNED`
  and `_CORRECTION` (hypothesized, in no profile).
- **Not decoded:**
  - the idle *speed* controller: the target rpm, its coolant, A/C and
    load steps, and the torque or air demand it sets;
  - idle spark feedback;
  - the four flags named above;
  - the three lost fault item ids.

### What a flash or a battery disconnect clears (F01.11, 2026-11-12)

The ECM keeps learned state in two places: a battery-backed RAM block, and
an external serial EEPROM. **An UpRev flash clears the same things a
battery disconnect does**, because the ECM's own download entry wipes the
RAM block. What survives either one is what the ECM restores from the
EEPROM. Everything below is verified from code unless marked inferred. A
read across a flash has not been made yet (F01.11 checklist).

**The battery-backed block** is `0xFFFF4C10`–`0xFFFF6AC7` (7,864 bytes).
Three words above it guard it:

- `0xFFFF6ACA` = `0xAA55` is the signature, written when the block is
  initialised.
- `0xFFFF6ACC` = `0x6997` is the clean-shutdown marker. `0x027578` writes
  it at the end of the after-run (`0x028BF8`, `0x0AC960`). The power-up
  check zeroes it.
- `0xFFFF6AC8` is an 8-bit sum of every byte of the block plus the two
  words above, written with the marker.

**The power-up check** is `0x0275EC`, the first call of `0x028C9C`:

- If `0xFFFFF75E` bit 4 is clear, nothing is checked or cleared. That is an
  input port bit, inferred to be the ignition sense: the after-run wait
  `0x0AC960` calls the restart trap while it is set.
- If the signature and the marker are both present, the checksum must
  match.
- If the signature is present but the marker is not, the program stopped
  without its after-run. Three plausibility tests must then all pass:
  1. every cell of both learned trim maps is within 0x5A00–0x6E00
     (`0x02766E`);
  2. `0xFFFF52DA`, the A/D gain word that `0x0683A8` applies (0x8000 =
     1.0), is within 0x7900–0x8700, unless `0xFFFF4858` bit 3 is set
     (`0x068358`);
  3. seven RAM complement pairs are intact (`0x027798`): `4C72`/`625C`,
     `4C74`/`625E`, `6172`/`614A`, `5EA0`/`52CE`, `618C`/`6256`,
     `618E`/`6258` and `52C8`/`52B2`.
- Anything else re-initialises the block: a missing signature, a checksum
  mismatch or a failed test. `0x0276D6` then:
  1. zeroes the whole block;
  2. runs the defaults below;
  3. writes the signature;
  4. sets `0xFFFF6958` bit 7, "re-initialised this power-up". A validated
     start clears that bit.
- `0x028E30` repeats test 1 after the volatile RAM is cleared, and
  re-initialises on a failure.
- `0x0276D6` has one more caller. Request 0x61 of the Nissan service 0xA0
  (`0x0314E8` queues it, `0x0D986A` runs it) re-initialises the block and
  then spins until a reset.

**Why a flash clears it.** UpRev opens its download with K-line `34 80`.
That reaches `0x035784`, which zeroes `0xFFFF84A0`–`0xFFFFA33F` (where the
loader is uploaded, 32 bytes per `36` block) **and `0xFFFF1000`–`0xFFFF6AC7`**.
That second range takes in the whole battery-backed block, but not the three
guard words.

When the loader's `67` restarts the program, the signature is still there
and the marker is not. So the plausibility tests decide, and test 1 fails on
the zeroed trim maps. The block is re-initialised exactly as after a battery
disconnect.

The `34 83` form skips the wipe. It needs further conditions (`0x035230`),
and UpRev does not use it.

**What is reset.** `0x02D1F4` sets the defaults:

- **Learned fuel trim**, both banks, back to 1.0. `0x059680` sets the maps
  `0xFFFF5460` and `0xFFFF54E0` to `0x6400` and zeroes their per-cell
  counters at `0xFFFF52F5` and `0xFFFF5335`.
  - Each map is 8 × 8: load (`0xFFFF1038` against `0xD290`) by rpm/50
    against `0xD372`. The clamp is 0x5A00–0x6E00, ±10 %.
  - The lookup is `0x057954` and the learning step is `0x057CF8`. The
    lookup puts the current cell in `0xFFFF26E2`/`26E4`.
  - A bank's whole map also resets to 1.0 when one of its cells leaves the
    clamp, when diagnostic item 0x114 (MAF) is failing, or on the one-shot
    `0xFFFF47B2` bit 7 (`0x0578E8`). That one-shot comes from request byte
    `0xFFFF99E4` bit 5, which K-line service `31` routine 6 sets
    (`0x034D98`, table `0x022F06`; the CAN side is `0x0D2E48`, table
    `0x0252FE`). That is inferred to be Consult's self-learning clear.
    UpRev does not send it.
  - The FSM's generic-tool substitute for that clear (EC-179: unplug the
    MAF until P0102 sets, then erase it) works through the MAF-item branch
    above. That agreement is a known answer from outside this analysis.
- **Every trouble code, freeze frame and readiness state.** The item status
  table `0xFFFF657C` is inside the block. UpRev's `14 FF 00` after the key
  cycle clears them as well.
- Everything else in the block that is not on the restore list below.

**What survives.** The defaults then restore these values from the EEPROM.
Each restore is skipped, leaving the default shown, while diagnostic item
0x72 is failing.

| RAM | EEPROM records | What it is | Default |
|---|---|---|---|
| `0xFFFF4C72` / `4C74` | 0 / 1, complements 0x78 / 0x79 (`0x0D90CC`) | Throttle sensor A / B closed positions (the `w2CC8` row above): the FSM's throttle valve closed position learning | 0x78 |
| `0xFFFF618C` / `618E` | 3 / 4, complements 0x7B / 0x7C (`0x06FC5C`) | Accelerator pedal released positions (inferred: `0xFFFF28A2` bit 7, pedal released, is set while the pedal reading `0xFFFF2C26` is below `618E` + 14 counts, `0x06FD16`) | 200 |
| `0xFFFF6172` | 2, complement 0x7A | Unidentified. `0x03F84C` subtracts it × 64 from `0xFFFF3FC8` | 0xE6 |
| `0xFFFF5EA0` | 0x71 / 0x72, a complement pair (`0x05455C`) | The idle-air integrator: the result of Idle Air Volume Learning (F01.10) | 0 |
| `0xFFFF52C8` | 0x75 / 0x4F, a complement pair (`0x04497C`) | Unidentified | — |
| `0xFFFF6548` | 0x73 / 0x4E, a complement pair (`0x0544A4`) | Unidentified | 0 |
| `0xFFFF6568`–`6578` | 0xE0–0xE8 (`0x035E96`) | 17 bytes, inferred to be the VIN | — |

A periodic task, `0x027724` (task table `0x0218C4`), also tests the seven
RAM complement pairs. If one breaks, it reloads the first four rows from the
EEPROM and sets `0xFFFF6958` bit 7.

**The EEPROM.**

- It is an SPI part on SCI2 in clocked-synchronous mode (`0xFFFFF013`–`F015`),
  chip select `0xFFFFF73E` bit 4.
- `0x067DA0` reads one word per record: command 0x03 and a 16-bit address
  of record × 2. Record ids run to at least 0xF9.
- **Writing.** The after-run save `0x0AD1BE` writes 29 records in turn from
  the table at `0x023E9E` (ids), `0x023EE2` (width) and `0x023EFF` (source
  pointer): records 0–4 and their complements, 5, 7, 0x71, 0x72, 0x75,
  0x76, 0x4F, 0xE0–0xE8, 0xF3, 0xF4 and 0xF5.
  - It starts on `0xFFFF478E` bit 7 with `0xFFFF4785` bit 7 clear. That is
    inferred to be ignition off, during the after-run.
  - `0xFFFF6952` bit 5 is held while it runs, and is set after a
    re-initialisation until the next complete save.
  - A failed write sets item 0x71; a complete save clears it.
- **Checking.** `0x0ACDB8` re-reads records 0–4 against complements
  0x78–0x7C and sets item 0x72 on a mismatch. Items 0x70–0x74 all report
  P0605.
- **The restored values are those of the last completed after-run save**,
  not those of the moment before the flash. Learning since the last key-off
  is lost.
- **UpRev's `A0 84 D8 F0 0D`** (`docs/skunkworks/uprev-flash-procedure.md`)
  is service 0xA0 request 0x84. That queues an EEPROM word write
  (`0x0D986A`: `0xFFFF33BD` = record, `0xFFFF33D0` = data). By the
  handler's byte positions it writes **record 0xD8 = 0xF00D** (inferred).
  Neither the ECM's program nor UpRev's in-ROM code (`0x0DE1A4`–`0x0DE428`)
  reads record 0xD8. It is a marker for UpRev's own use. The neighbouring
  requests 0x81–0x98 are the same service's EEPROM and memory accessors,
  not decoded further.

**What this means for relearning.**

- After a flash, only the learned fuel trim and the trouble-code state
  start over.
- The throttle closed positions, the pedal released positions and the
  idle-air integrator come back from the EEPROM, as saved at the last
  key-off. So none of the FSM's three relearns is needed because of a
  flash.
- The exceptions are an EEPROM fault (item 0x71 or 0x72, with the defaults
  in the table above) and changed hardware. **After the 75 mm throttle-body
  swap, the stored closed positions and integrator belong to the old throttle
  and will be restored as such.** Run throttle valve closed position learning
  and Idle Air Volume Learning then (FSM EC).
- A battery disconnect behaves the same way, provided the after-run save
  finished first: key off, and wait for the ECM relay to drop.

**Read across the next flash** (`query_memory`, KOEO, before and after; the
F01.11 acceptance):

- `0xFFFF6ACA`, `6ACC` and `6958`: after the flash, bit 7 of `6958` must
  be set if the block was re-initialised.
- Several cells of each trim map, in `0xFFFF5460`–`54DF` and
  `0xFFFF54E0`–`555F`.
- `0xFFFF4C72`/`4C74`, `618C`/`618E` and `5EA0`: these must come back
  unchanged.
- A read before the flash's key cycle and one after it separates the
  re-initialisation from the next validated start.

### Barometric pressure: when it updates (F01.12, 2026-11-12)

**Baro is read continuously** from the EVAP control system pressure sensor
and tracks a climb with no lag visible at PID 0x33's 1 kPa resolution. It
freezes only while a hold flag is set. That flag's writers are the EVAP
purge code and four diagnostic state machines, inferred to be the EVAP leak
tests. Verified from code unless marked inferred.

**The chain** runs in the 10.24 ms group (`0x03DD8C`, task table
`0x02189C`):

1. **Sensor.** A/D input AN3, slot 7 of the external multiplexer, goes to
   `0xFFFF1FFC`. Cal `0x88F0` bit 7 selects the EVAP pressure sensor for
   that slot.
2. **Conditioning.** `0x03DA08` filters it, and the result × 64 is
   `0xFFFF11A2`.
3. **Fast filter.** `0x03DA8C` runs `0xFFFF6272` += (`11A2` − `6272`) / 16
   each call, a time constant of about 0.16 s. The filter is frozen while
   the hold flag `0xFFFF4853` bit 0 is set.
4. **Learned baro** `0xFFFF4C60`:
   - It copies `6272` each call while the two are within 0xA00 counts
     (2.3 kPa) and the hold has been clear for 100 calls (1.0 s).
   - A larger gap closes by 0x500 counts (1.1 kPa) per 102.4 ms event
     (`0xFFFF84C0` bit 6), about 11 kPa/s.
   - `0xFFFF4C30` is its per-cycle snapshot. `0xFFFF4466` is its high byte,
     or 0xFF while item 0xCD, 0xCE or 0x2C (P0452, P0453, P0448) or two ids
     the decompiler lost are failing.
5. **Units.** kPa = (raw × 58/128 + 27,724) / 512, which is 0.000885 kPa
   per count, from cal `0x89AA`/`0x92D4`. PID 0x33 is that value truncated
   to whole kPa (`0x053B8C`), or 101.3 kPa (`0xCA99`) on a sensor fault.

**Persistence.** `6272` and `4C60` are both inside the battery-backed block
(F01.11), so the learned baro carries across key cycles. After a flash or a
battery disconnect they restart at `0xDA80`, which is 103.6 kPa. At home
that is 23 kPa high, and the slew corrects it in about 2 s of key-on.

**The hold**, `0xFFFF4853` bit 0, has six writers:

- `0x06A288` sets it from `0xFFFF4858` bit 2 OR `0xFFFF47B3` bit 1.
  `EVAP_purge_control` writes `4858` bit 2 (from `0xFFFF91F0`). `47B3`
  bit 1 is written by `EVAP_purge_control` and `0x0BDCF2`.
- Four autocoded diagnostic state machines also write it: `0x0B0A3C`,
  `0x0B3ED8`, `0x0B5260` and `0x0BA320`.
- That these are the EVAP leak tests, during which the vent valve is closed
  and the sensor reads purge-line vacuum rather than the atmosphere, is
  inferred. It is consistent with PID 0x32 (baro minus the EVAP sensor)
  reading 0 outside leak tests (2026-10-27).

**A second word, `0xFFFF1188`**, is a separately filtered copy of `11A2`.
It takes `0xDA80` while `0xFFFF6958` bit 4 is set. It follows `11A2`
only while `0xFFFF1115` > 0x51 and `11A2` is within 0x1600–0xF100,
and otherwise holds. Its consumers have not been followed.

**Checked against GNSS altitude.** Each PID 0x33 reading was paired with
the same-second GNSS altitude converted through the standard atmosphere:

| Drive | Pairs | GNSS altitude | PID 0x33 | PID / standard atmosphere |
|---|---|---|---|---|
| 2026-08-06T162657 | 197 | 1,849–2,816 m | 72–82 kPa | median 1.007, residual s.d. 0.58 kPa |
| 2026-09-18, 17 sessions | 1,180 | 1,688–3,882 m | 63–83 kPa | median 1.0065, residual s.d. 0.83 kPa, worst 2.2 kPa |

- The ratio drifts from 0.998 below 2,100 m to 1.018 near 3,000 m. That is
  the real atmosphere against the standard one, which assumes a fixed
  temperature lapse, plus a day's weather. It is not the ECM.
- A stale or lagging baro would read high while climbing. The residual has
  the opposite sign: −0.36 kPa at 0.5–2 m/s of climb, +0.04 kPa
  descending. So any lag is below what 1 kPa steps can show, and smaller
  than the GNSS receiver's own altitude smoothing.

**Answer.** Baro follows a climb from 82 to 63 kPa. Nothing in the drives
shows a stale value, and the code holds it only for EVAP leak tests
(inferred). The λ gain, the injector pressure model and the torque limit
that read `4C30` see the current altitude.

### Throttle area units: not settleable at the desk (F01.9, 2026-11-12)

**Why the desk can't settle them.**

- The area curve (axis `0xDD94`, values `0xEE6A`) and the factor 158
  (cal `0x8D25`) are relative units.
- Nothing in the code multiplies either by a physical constant (see the
  table above and "Absolute units").
- Every image on hand is one program, ZS44A, in one calibration lineage. So
  no second calibration with a different throttle body exists to scale
  against.
- The reading of 158 as 3,954 cc / 25 stays speculation.

**The swap does not need the absolute unit.** Both users of the curve map a
sensor reading to an area in the ECM's own unit:

- the air model's fill byte, through `0xFFFF2C32`;
- the electronic throttle's target position, through `0xFFFF2D28`.

A larger throttle body passes more air at the same sensor reading. Both
paths are therefore wrong by the ratio of the new effective area to the old
at each reading, whatever the unit. That ratio is what an edit to `0xEE6A`
has to apply, and it can be measured before and after the swap.

**The measurement that would pin the unit** (on the truck, no second image
needed). In choked flow (manifold pressure below about 0.53 × baro: idle
and closed-throttle coasting), mass flow is
Cd·A × P0 × 0.0404 / √T0 (SI). Read these in one window:

- `0xFFFF2C32` (area) and `0xFFFF63FC` (the idle-air subtrahend);
- MAF (DID `0x1209`), baro (PID `0x33`) and IAT.

That gives Cd·A in mm² per area count at several openings. It yields the
effective area (Cd included), which is the quantity the ECM's model stands
for.

### Ignition timing chain: the remaining terms (F01.6, 2026-11-12)

E09.7 decoded the chain's spine: `0xFFFF27C6`, `0xFFFF279A`, the trim term,
the base sum, the combine, the rate limit and the final byte. It left about a
dozen words as arguments with no known writer. With every task table seeded
(F01.1), all of them now have one, except the knock controller's branch,
which stays observation only (F01.7). Calibration values below are identical
on all seven images. One count is one degree at the final byte: the chain
from `0xFFFF1A58` to `0xFFFF232E` only adds, and `0xFFFF11FF` =
110 − `0xFFFF232E` matches PID `0x0E`.

**Where they run.**

- The base-timing task `0x04DE4C` (main entry 74) calls, in order:
  - `0x04B8C8` → `6418`
  - `0x04D8FC` → `27C4`
  - `0x04D2B8` → `27B6`
  - `0x04C9D8` → `174A`
  - `0x04DA02` → `1D3E`
  - the base sum `0x04CB9E` → `1A58`
- The per-segment chain `0x04E6AE` calls the slew `0x04E5B0`, then the
  combine `0x04DF38`, then the rate limit `0x04E0E0`.
- After the task loop, the cycle copies move the block `0xFFFF2792`–`281D` to
  `0xFFFF2706`–`2791`, and move `0xFFFF6418` (0x164 bytes) to `0xFFFF62B4`.
  So the combine reads `27B6` as `272A`, `27C4` as `2738`, `2798` as `270C`,
  `27BE` as `2732` and `27DC` as `2750`.

**Intake temperature retard, `0xFFFF11CB`.**

- `0x04D8FC` computes `0xFFFF27C4` =
  min(cal `0x8B05` = 7, max(0, `0xFFFF2BD0` − cal `0x8B02` = 80) ×
  `0xFFFF1766` / 640).
  - `0xFFFF2BD0` is the cycle copy of the intake temperature byte, IAT + 50
    (offset inferred).
  - `0xFFFF1766` is cal `0x87BD` = 70 when the term is enabled and rpm is at
    least 800 (cal `0x88C8` = 16 on rpm/50). Otherwise it is cal `0x87BC` = 0.
- **Enable.** All of the following hold:
  - IAT ≥ 46 °C: `0xFFFF4806` bit 1, set at byte ≥ 96 and cleared below 94.
  - Diagnostic item `0x11A` is not faulted.
  - (`0xFFFF287A` bit 7 and `0xFFFF2887` bit 2) or `0xFFFF2876` bit 4. These
    flags are not identified.
- **Size.** About 0.11° per °C above 30 °C, truncated, so the retard is 1° at
  46 °C, rises with IAT, and reaches 7° at 94 °C.
- **Path to the trim term.**
  1. The cycle copy moves `27C4` to `0xFFFF2738`.
  2. `0x04E5B0` slews `0xFFFF1203` toward it by at most 5 per segment, so it
     lags by about one segment.
  3. The 0x38-byte snapshot copies `1203` to `11CB`.
  4. The trim term is `0xFFFF2793` = max(0, trim − `11CB`). Trim cells are
     43–68 counts on this calibration, so the floor never binds and the full
     retard comes off.
- **Off idle only.** On the idle path (`0xFFFF2833` bit 4) the base sum uses
  `0xFFFF27C3` and no trim term. So the retard reaches the spark off idle
  only, for example when pulling away after heat-soaked idling, where this
  truck's IAT reads high.
- **Data.** The only read of `11CB` (2026-10-04 RAM study, 180 reads) was 0
  throughout, with IAT 27–42 °C, below the enable. That is consistent but
  proves nothing, since the value never changed.
- **After the supercharger build.** If the kit moves the ECM's IAT input
  post-intercooler, this is the only path from charge temperature to spark:
  at most 7° for a charge above 94 °C, and none below 46 °C.

**Constant on this calibration:**

- `0xFFFF1D3E`, the cam-phase error term from `0x04DA02`, is neutral at
  0x40 (cal `0x8D60`/`0x8D61`).
  - It departs from 0x40 only at coolant ≥ 120 °C, or when a cam-target cell
    holds the 0x80 sentinel. Then it becomes
    `0xFFFF1ED4` + `0xFFFF1DFB` − 0x40.
  - The two cam-error curves on that path are flat 0x40.
  - Curve `0xDA8F` takes off up to 8° for a large cam error above a load
    floor (curve `0xDAFB` on rpm).
  - Any of diagnostic items `0x137`–`0x13E` (VTC) forces 0x40.
  - The base sum's `+ 0x1D3E − 0x80` is therefore −0x40 in practice.
- `0xFFFF1344`, a lean-target advance written by the base sum itself, needs
  `0xFFFF47A6` bit 6 and the smaller bank target AFR ≥ 14.7 (word ≥ 0x1CB6).
  Its value is coolant curve `0xCCB7` × map `0xAEF4` × `0xFFFF1B7C` /
  500,000, capped at 255. Map `0xAEF4` is all zero on every image, so it is 0.
- `0xFFFF2334`, the combine's subtrahend, is curve `0xE584` (axis `0xE0A2`)
  at `0xFFFF2750` (the copy of `0xFFFF27DC`, F01.3's storage-estimator
  term), × cal `0x89E2`/`0x89E3` (100) / 100. The curve is all zero, so it
  is 0.

**Transient and torque-management retards** (zero in steady running):

- `0xFFFF272A` (from `0x04D2B8` via `27B6`) is a retard after a closed-throttle
  coast, floored at −20.
  - **Arms when all of these hold:**
    - coolant 70–109 °C;
    - rpm 550–2,950;
    - off idle, with `0xFFFF11C5` < 20;
    - `0xFFFF2860` bit 3;
    - `0xFFFF2855` bit 0, which the decel flag `0xFFFF2835` bit 4 sets
      through a 10-step timer.
  - **Size.** It starts at 9–12 counts from tables `0xD8E3`–`0xD8F3`, indexed
    on `0xFFFF1075` − `0xFFFF1111`. The table variant depends on whether the
    fuel cut mask was `0x3F`. It ramps out over 30 steps (cal `0x87AD`).
  - An rpm-rate damping term exists, but its gain curves `0xD0D7`/`0xD0DF`
    are zero, so only the ramp acts.
- `0xFFFF1215` (`0x0637C2`) is not knock. It is min(127, max(`2798`,
  `27BE`) >> 8). A variant adds `0xFFFF2372` when `0xFFFF28B3` bit 4 is set.
  It is 0 while `0xFFFF484B` & 0x60.
  - `0xFFFF2798` (`0x064854`): a torque-reduction retard. It inverts a
    timing-to-torque efficiency curve (`0xC784` against `0xCB01`) to find the
    retard that delivers the reduction `0xFFFF1A92` asks for. Who asks is
    F01.13.
  - `0xFFFF27BE` (`0x063960`): a state machine around decel fuel cut, with
    states in `0xFFFF1415`.
    - It holds 17° (0x1100 / 256) while the idle flag is set and the mask is
      `0x3F`.
    - It ramps back in at 0x36/256 per call during re-entry (warm only).
    - It decays by `0xFFFF11DC` per call afterwards.
    - When the throttle opens from a decel, it holds the difference between
      the off-idle and idle base timing, so spark does not jump.
- **Scan-tool terms** (zero unless a tool sets them):
  - `0xFFFF62B4`, the copy of `0xFFFF6418` (`0x04B8C8`), is a ±2° timing
    adjustment. Its source is one of:
    - the IOControl buffer `0xFFFF99E8` while `0xFFFF4853` bit 5 is set;
    - an analog-stepped value from `0xFFFF10EA` while bit 6 is set;
    - otherwise the stored `0xFFFF2E0C`. That is non-volatile record 0x74,
      saved through the queue at `0x069220` and read back at init by
      `0x069884`.

    The rate limit adds it only when warm (coolant byte ≥ 110), below about
    6,200 rpm, and with `0xFFFF104E` < 252.
  - `0xFFFF9932` is the service-0x30 (InputOutputControl) buffer's timing
    retard, applied while `0xFFFF99D0` bit 2 is set and clamped to −10..0.
    `0x031A64` answers 0x30 with NRC 0x12/0x22. Active tests require
    stationary and warm (`0x02F2DA`).

**Still open:**

- the knock controller's `0x04A8B6` branch (F01.7);
- the enable flags `0xFFFF287A`.7, `2887`.2, `2876`.4 and `4806` (IAT,
  above);
- `0xFFFF2860`.3;
- `0xFFFF484B` & 0x60;
- who writes `0xFFFF1A92` (F01.13);
- the meaning of `0xFFFF1075` − `0xFFFF1111`;
- the inside of `0xFFFF27C6` (`26E8`, written by `0x055386` from coolant,
  rpm and the warm-up terms; `175C`/`1736`);
- the `279A` ramps.

**To check on the truck (one 0x23 window set):**

1. At hot idle, then pulling away: `11CB`, `1203` and IAT. Expect 0 below
   46 °C and the formula above it.
2. In steady cruise: `1D3E` = 0x40, `1344` = 0 and `1215` = 0.
3. `62B4`, to confirm no stored adjustment.

### Scheduler and task tables (F01.1, 2026-11-12)

The application's vector base is VBR = `0x20004` (vector v at
`0x20004 + 4v`); the bootloader's table at `0x0` is a different one.

- **Tick.** Vector 120 (`0x2040C`) adds 320 to GR3A each tick at φ′/32:
  1.024 ms at the inferred Pφ 20 MHz. It sets bits in `0xFFFF84B9`:
  - `0x80` every tick, and `0x02` if `0x80` was still set (an overrun flag);
  - `0x40` every 2 ticks, `0x20` every 4, `0x14` every 10;
  - `0x08` while the dispatcher runs (set `0x20A82`, cleared `0x20EA0`).
- **Groups.** The dispatcher `0x020A3C` walks these task tables (the block
  `0x02180C`–`0x021C0C`). All 254 entries resolve to functions since the
  seeding below.

  | Table | Entries | Runs |
  |---|---|---|
  | `0x021818` | 9 | every tick (1.024 ms) |
  | `0x02183C` | 14 | every 2 ticks |
  | `0x021874` | 10 | every 4 ticks (the BFS group) |
  | `0x02189C` | 67 | every 10 ticks |
  | `0x0219A8` | 7 | after a crank segment (`0xFFFF84BA` bit 7) |
  | `0x0219C4` | 3 | every 3rd crank segment (`0xFFFF84BA` bit 6) |
  | `0x0219D0` | 134 | the main cycle `0x020EF2` |
  | `0x021BE8` | 10 | the crank-segment dispatcher `0x02092E` itself |

  `0x02092E` sets `84BA` bit 7 on every segment and bit 6 on every third
  (`0x20962`/`0x20972`). The dispatcher clears each after running its group
  (`0x20C6A`, `0x20E7C`). The background state jump table `0x022668` is
  indexed by `0xFFFF84A8`.
- **Main cycle.** The background loop `0x0290D0` waits for `84B9` bit `0x04`,
  then calls `0x020EF2`, which clears the bit at its start (`0x20F24`). A
  cycle that overruns is not caught up: the period is 10.24 ms nominal and
  longer under load, so it must be measured, never assumed.
- **The time base and its event bytes.** The tick ISR keeps a 32-bit tick
  counter `0xFFFF84B0` (+1 per tick, `0x0204F0`) and a cascade of dividers.
  Each divider ORs one event bit into every per-consumer event byte (`84BC`,
  `84BD`–`84C2`, `84C4`):

  | Bit | Period | Divider | Set at |
  |---|---|---|---|
  | `0x80` | 10 ticks, 10.24 ms | `0xFFFF9EA4` | `0x02071A`–`0x02075C` |
  | `0x40` | 100 ticks, 102.4 ms | `0xFFFF9EA8` | `0x020810`–`0x020846` |
  | `0x20` | 1,000 ticks, 1.024 s | `0xFFFF9EAA` | `0x02084E`–`0x020880` |
  | `0x10` | 10,000 ticks, 10.24 s | `0xFFFF9EAC` | `0x020890`–`0x0208C2` |

  Each consumer clears its own byte after reading it, by copying `0xFFFF84BB`
  into it. `84BB` is never written after the RAM test, so it reads 0. That is
  why F01.1's search for its writer found none. The dispatcher clears `84BD`
  (1 ms group), `84BE` (2 ms), `84BF` (4 ms, `0x20BF6`), `84C0` (10 ms,
  `0x20CEA`), `84C1` (`84BA`.7 group) and `84C2` (`84BA`.6 group) after each
  group runs. The main cycle starts with `84C3 ← 84C4` and then `84C4 ← 84BB`
  (`0x20F26`–`0x20F36`).
  - **So `84C3` holds the events since the previous main-cycle start.** Its
    readers test bit 7 (77 sites), bit 6 (48), bit 5 (13) and bit 4 (1).
  - **Every enrichment timer steps on bit 7**: once per main cycle, nominally
    10.24 ms. When a cycle overruns past the next boundary, two events
    coalesce into one step, so the timers run slow under load rather than
    catching up. The VTC start delay is 30 steps, about 0.31 s.
  - **Byte timers.** The same ISR also decrements arrays of byte and word
    timers, each at one of three rates. Every tick: `0xFFFF2A71`,
    `0xFFFF2ABC`, `0xFFFF4A99`, `0xFFFF4BAA`. Every 10.24 ms:
    `0xFFFF28FD`–`0xFFFF29A7`, so `0xFFFF2947`, the multiplier's hold
    counter (loaded with cal `0x879E` = 2 by `0x05687E`), lasts 20 ms. Every
    102.4 ms: `0xFFFF28BC`, `0xFFFF2A96`, `0xFFFF485C`, `0xFFFF4B7E`.
  - The 1.024 ms tick rests on the inferred Pφ 20 MHz. Two host-timestamped
    reads of `0xFFFF84B0` measure it, and settle `enrichment_tick_rate`
    together with the run-time words below.
  - (Earlier on 2026-11-12, before the ISR was read, this section said `84C3`
    held `84BB` from one cycle earlier and that `84BB`'s writer was missing.
    Both were wrong; the emulation simply never wrote it.)
- **Run-time words.** The dispatcher times each group with the free-running
  ATU counter `0xFFFFF430`: `0xFFFF2380` (background/main), `0xFFFF204C`,
  `0xFFFF2060`, `0xFFFF20E8` (4 ms group), `0xFFFF224C`, `0xFFFF2250`,
  `0xFFFF2414`-indexed (segment group), `0xFFFF26B0`; peak-holds at
  `0xFFFF3DE0`, `0xFFFF3EE4`, `0xFFFF4044`, `0xFFFF4424`, `0xFFFF4428`,
  `0xFFFF4608`, `0xFFFF465C`. The counter's clock is not decoded (channel 0's
  set-up, `0x027916`, writes `0xFFFFF42A` = 2 and `F42C`/`F42E` = 1, and the
  register meanings were not checked against the manual), so read
  `0xFFFFF430` twice a few seconds apart with host timestamps to get it.
  Together these give the real main-cycle period and the CPU headroom.
- **Seeding.** Twenty-four functions were added to the project (2,532 →
  2,556), among them the three task targets that were missing: `0x0726DC`
  (main entry 20), `0x05152C` (main entry 82) and `0x051F48` (`0x0219C4`
  entry 2). Code-like bytes outside any function fell from 8.7 KB in 35 gaps
  to about 2 KB. Eight prologue-less regions remain (`0x4F6F8`, `0x4AC0E`,
  `0x4B014`, `0x3B8DA`, `0x2EE92`, `0x26452`, `0xCF968`, `0x3D738`, about
  0.9 KB) with no branch, call or pointer to them: dead code, or reached only
  by a computed jump. A "no store found" search now covers every task-table
  routine.

## Open

- **Catalog:** the Mode 22 corrections and the 2026-10-05 audit are applied
  (D03.36, D03.40). Open: the measurements D03.40 lists for the unresolved
  DIDs.
- **Active map:** `mem:ECM_ACTIVE_MAP` is catalogued and read 0 all drive
  (2026-10-04). Still to see it change: the selector stepping when the map
  switch is used, which is the operator's call. Also to check: the live
  Target AFR copy at `0xFFFFB260` equals that slot's flash table.
- **ROM map data:** move the confirmed slot tables (timing trims, Fuel
  Comp, K, Load Point Scaler) into `rom_map.yaml`. (`rom_trace`'s BFS-axis
  refusal is lifted: W05.1, `rom_viewer.yaml`.)
- **Unidentified tables:** the remaining 1-D curve inputs and the
  flash-resident 2-D maps. The `+0x260` map is the engine torque map
  (2026-11-11); `rom_map.yaml` names it but does not yet declare it as a table.
- **Throttle demand:** a live read of the first-gear limit word `0xFFFF3C62`
  and of the ratio word `0xFFFF27D2` in a steady fifth-gear cruise (expect
  1,158 ± 10 with the 4.08 axles, 1,024 stock;
  `data/analyses/2026-11-12-ratio-estimate-and-full-pedal-lean.md`). During
  cruise, `0xFFFF2CA2`, `0xFFFF3B4A` and `0xFFFF475A` (the cruise torque
  conversion). The fault-cap inputs `0xFFFF4856` bit 3, `0xFFFF2B46`/`2B47`,
  and which messages `0xFFFF41B1`/`41BB` track. (The readers of `0x91F8` and
  the state byte `0xFFFF2208` were traced on 2026-11-12, "What else the
  output-shaft constant `0x91F8` moves". The drive-torque stage whose speed
  word was owed here does not run on this calibration.)
- **Disassembly gaps:** seeded 2026-11-12 (F01.1): every task-table entry is
  now a function, and about 0.9 KB of unreferenced code remains ("Scheduler
  and task tables"). A "no store found" search is evidence for anything a
  task table reaches; it is still not evidence for a computed address.
- **Scheduler timing (vehicle):** two timed reads of the tick counter
  `0xFFFF84B0` (Pφ) and of `0xFFFFF430` (the run-time words' clock), and the
  run-time words, to give the real main-cycle period under load.
- **Short-pulse stage (vehicle):** `0xFFFF279C` against `0xFFFF27A8` at hot
  idle.
