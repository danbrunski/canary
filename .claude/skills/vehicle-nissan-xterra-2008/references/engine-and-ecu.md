# The VQ40DE and its ECM: specifications, fuel equation, tables, relearns

Read this for "is this reading normal?" on this truck, before advising on any
table edit, and after any flash, battery disconnect, or throttle-body change.
Factory figures come from the 2008 Xterra service manual on disk
(`xterra_fsm/ec.pdf`, the engine-control section). Cite its page when you use
one. Figures measured on this truck override the manual's typical values for
*this* engine, but a disagreement with the manual is worth reporting.

## Contents

- Engine and hardware
- Factory reference values (data monitor)
- The fuel equation as this ECM implements it
- ROM tables and what an edit moves
- Relearn procedures
- Common VQ40DE faults and how they look in a log

## Engine and hardware

| | | Source |
|---|---|---|
| Displacement, bore × stroke | 3,954 cc, 95.5 × 92.0 mm | EM-134 (2009 FSM) |
| Compression ratio | 9.7:1 | EM-134 |
| Compression pressure at 300 rpm | 1,275 kPa std, 981 kPa min, ≤ 98 kPa between cylinders | EM-134 |
| Firing order | **1-2-3-4-5-6** (online "1-4-2-5-3-6" is wrong) | EC-34 |
| Bank 1 | cylinders 1-3-5, **RH / passenger side**. The Bank 2 A/F connector is by the brake booster | EM-17, EC-25 |
| Rating | 261 hp @ 5,600 rpm, 281 lb-ft @ 4,000 rpm (sea level) | Nissan 2008 press kit |
| Upstream sensor | **A/F sensor 1**: planar, limiting-current, wide-range, heated ~800 °C, ahead of each manifold catalyst. Reported as λ on PIDs `0x24`/`0x28` (λ + voltage); `0x34`–`0x3B` are not supported | EC-141, EC-32, `pid-probe-2026-07-22.md` |
| Downstream sensor | narrowband heated O2 sensor 2, swings 0–0.3 ↔ 0.6–1.0 V | EC-431 |
| Fuel system | **returnless** ("N50 models do not have fuel return system"), ≈ 350 kPa (51 psi), regulator in the in-tank module. The pressure is fixed against atmosphere, not manifold (inferred from the in-tank location) | EC-494, EC-499 |
| Injectors | Bosch EV14 `0280158007` (Nissan 16600-7S000), ~296 cc/min at 3 bar (≈ 320 cc/min at 350 kPa), 11.1–14.5 Ω | EC-500; vendor data |
| Cam timing | continuously variable, oil-pressure driven, **intake cams only**. Base intake opening is 4° ATDC with the phaser off. Maximum phaser travel is not in the FSM | EC-43, EM-134 |
| Variable intake (VIAS) | one solenoid and one power-valve actuator. The solenoid is **ON (valve closed, long path) at 2,200–3,300 rpm warm**, OFF (open) below and above. Expect VE/torque steps near those two speeds; they are changeovers, not sensor faults | EC-44, EC-432 |
| Knock sensors | two (bank 1, bank 2), on the block in the V. Knock DTCs (P0327/28, P0332/33) do not light the MIL | EC-211/213 |
| EVAP purge | volume-control solenoid is **normally closed** (open with 12 V); vent valve normally open | EC-243, EC-74 |
| Throttle | electronic, coolant-heated body. Stock bore ~70 mm (medium confidence) | EM-25 |

Redline and fuel-cut rpm were not found in an authoritative source; UpRev can
change them anyway. Read them from the running ROM.

## Factory reference values (data monitor)

FSM EC-432 (SDS at EC-499). Conditions: engine warm, M/T in Neutral, A/C off,
no load.

| Item | Idle | Other point |
|---|---|---|
| Idle speed | **625 ± 50 rpm** (electrical loads off, wheels straight) | |
| IGN TIMING | 13–18° BTDC (SDS: 15 ± 5°) | 25–45° BTDC at 2,000 rpm |
| MASS AIRFLOW | 2.0–6.0 g/s (MAF 0.9–1.2 V) | 7.0–20.0 g/s at 2,500 rpm |
| CAL/LD VALUE | 5–35 % | 5–35 % at 2,500 rpm |
| INJ PULSE-B1/B2 | 2.0–3.0 ms | 1.9–2.9 ms at 2,000 rpm |
| INT/V TIM (B1/B2) | −5 to 5 °CA | ≈ 0–30 °CA at 2,000 rpm |
| INT/V SOL (B1/B2) | 0–2 % | ≈ 0–50 % at 2,000 rpm |
| PURG VOL C/V | 0 % | |
| A/F SEN1 (B1/B2) | fluctuates around **≈ 2.2 V** warm at 2,000 rpm (the "1.5 V at stoich" quoted online belongs to other Nissan sensors) | EC-431 |
| Fuel pressure | ≈ 350 kPa at idle | |

These are sea-level, stock-calibration figures:

- **The FSM's "SP" check (B/FUEL SCHDL, A/F ALPHA, MAS A/F SE) is defined only
  at barometric pressure 98.3–104.3 kPa**, 20–30 °C, > 5,000 km (EC-79). It
  cannot be met at this truck's 63–82 kPa. Airflow and BFS read low at altitude
  on a healthy engine, so never call a fault from a reading below the SP band.
- The ranges assume the stock calibration. UpRev K, MAF and timing-trim edits
  move BFS, calculated load, and ignition timing off them by design.
- **No-load fuel cut:** in Neutral above 1,800 rpm the ECM cuts fuel "after some
  time" and restores it at 1,500 rpm (EC-486). A parked rev held above 1,800 rpm
  therefore contains fuel cut. Keep stationary rev sweeps short, or expect λ to
  peg lean.
- **MAF circuit fault fail-safe** (P0102/P0103): the ECM cuts fuel to hold the
  engine at or below 2,400 rpm (EC-463).
- **CONSULT has no knock item** (EC-72–75). Knock is visible only through
  UpRev's Knock Strength / High Det Flag (below).

## The fuel equation as this ECM implements it

Nissan (JECS/Hitachi) ECMs build injection from a *base fuel schedule*: Tp, shown
as "B/FUEL SCHDL" in CONSULT and "BFS" in UpRev, in ms. Nissan's own patents give
the form:

```
Tp = K × Qa / Ne                                   (airflow per rev × injector constant K)
Ti = (Tp + transient corr.) × target φ × (α + αm − 1) + Ts
```

- `α` is the closed-loop feedback and `αm` the learned correction; 100 % is
  neutral. CONSULT's `A/F ALPHA-B1/B2` includes the learned part.
- `Ts` is the battery-voltage (injector latency) term. UpRev models latency
  as a 14 V offset plus a slope.
- UpRev: "MAF × K = BFS", "the theoretical pulse width to hold λ = 1. This is
  NOT the actual injector pulse width." The FSM: "prior to any learned on board
  correction".
- K is a unitless ROM integer (UpRev: never below 10,000). The MAF table is
  a percentage on a fixed voltage axis. **Only the product K × MAF matters.**
  UpRev notes that K × 1.2 with the MAF table × 0.8 runs identically, so
  compare the product between two tunes, never K alone.

Sources: UpRev *Nissan Tuning Guide* (pp. 10–14, 26–27); Nissan patents
US4967713 and US5615660; FSM EC "B/FUEL SCHDL", "A/F ALPHA". What that means on
this truck:

- **BFS is the load axis for almost everything.** UpRev indexes Fuel Targets,
  Ignition Timing Trim, Cam Phasing, Estimated Torque and Crank Drag (misfire)
  on BFS × rpm. Calculated load (`CAL/LD VALUE`, PID `0x04`) is BFS divided by a
  per-rpm table, so it moves with K too. The exception is **Fuel
  Compensation**, which is indexed on Volumetric Flow Ratio (the throttle's
  flow potential) × rpm.
- The Target AFR table is 8 rpm rows × 8 BFS columns, row-major. Its BFS axis
  runs raw `0x34`–`0x80` (8.76–21.57 ms), rpm breakpoints are raw × 50, and
  `AFR = 14.7 × 128 / raw` (`docs/uprev-rom-table-verification.md`,
  `rom_viewer.yaml`).
- **Closed loop is a property of the target cell.** UpRev: closed loop is
  active only where the Target AFR cell is exactly 14.7, and "anything BUT
  14.7 the engine will operate in open loop". Editing a cruise cell off 14.7
  silently opens the loop there, so trims freeze and the A/F error in that cell
  becomes the MAF/K/fuel-comp error directly. Fuel Comp has a separate *Cold*
  table used below 70 °C coolant.
- **`0x1208` (`ECM_AIR_CHARGE`) is BFS**, confirmed at raw × 0.00049 ms
  (405/405 rows against UpRev's display) and accepted by the operator as the
  verified BFS on 2026-10-02. It responds to air and to K, not to fuel. Its
  per-gram-of-air ratio stepped +7.5 % at the first Osiris flash (2026-07-26,
  K 1.07), in lock-step with UpRev's own BFS/air. The DID stays in raw counts
  (a deployed-model feature). `derived:BASE_FUEL_SCHEDULE` is the ms channel,
  and `derived:TARGET_AFR` is looked up on it. Idle and light cruise sit at
  ~2.7–4 ms, below the table's first BFS breakpoint (8.76 ms). **The ECM reads
  a BFS below 8.76 ms as the first column, and an rpm above 5,200 as the last
  row** (UpRev's own TARGET AFR, 2026-08-04 logs: 32 of 46 and 62 of 105 rows
  within 0.05 AFR; extrapolation matches 2 and 0). The lookup clamps there
  (`afr_calibration.yaml` `load_axis_out_of_range` / `rpm_axis_out_of_range`,
  P02.15). Rows below 1,200 rpm and above 21.57 ms stay empty, with no
  evidence either way, and so do rows in decel fuel cut (PID 0x44 reads exactly
  0; PID 0x03's 0x04 also covers power enrichment, so it cannot mark cut). Each
  row is looked up on the map of the revision flashed at its timestamp
  (P02.16). The first two UpRev flashes (K edits only) carry UpRev's base map,
  and the stock ZS04C map is not recorded.
  UpRev's "Fuel Target X Trace" reads ≈ 1.48 × BFS
  − 0.09 on six logs (2026-08-08..09-24), so it is not BFS ms. How it lands
  on the table axis is still unverified (`rom_viewer.yaml`).
- **Changing K (or the MAF table) walks every operating point across the BFS
  axis.** The same air now reads a different BFS, so each operating point lands
  in a different column of every BFS-indexed table. Raising K "to add fuel"
  also moves timing trim to higher-load cells, which usually means less advance,
  and can push a cruise point off a 14.7 cell into open loop. The 2026-08-03
  flash changed K (1.05 → 1.06), smoothed the AFR targets, and added spark at
  3000–4500 high load all at once. That is why its spark edit could not be
  measured (`tune_revisions.yaml`). Make one category of edit per flash, and
  compare logs across a K change on **g/s × rpm**, never on BFS.
- **Fix MAF error in the MAF table, not in Fuel Comp.** They sit on different
  axes, and tuner reports tie MAF error hidden in Fuel Comp to timing retard and
  idle bounce (medium confidence).
- **The WOT fuel error is airflow-dependent, not a flat supply offset.** WOT
  ran +2–4 % lean on three instruments while part-throttle trims ran rich. K
  scales fuel by one factor everywhere, so cutting it for light load would lean
  out WOT further. **Don't propose a K change for this**; the tables do the
  work, and K changes anyway with the 700 cc injectors
  (`docs/roadmap/tasks/N01-na-rom-response-experiments.md` → "Not now").
- **Mid-rpm WOT enrichment is a pedal switch** (≥ ~227/255 broadcast pedal) into
  the Target AFR table's last BFS column, overriding the column the BFS lookup
  would pick. `0x1134` holds that active rich target (raw/64 as φ). The code
  confirms it (`docs/ecu-firmware-analysis.md`): the target function at
  `0x055DB2` replaces the lookup's load input with `0xFF`, the last column,
  while a flag is set. The flag switches with +8 counts of hysteresis on a
  value from the 1-D table at `0xCEDB`. **Confirmed live on 2026-10-04:**
  - the flag (bit 0 of RAM `0xFFFF2841`, `mem:ECM_PEDAL_ENRICH_FLAGS`) set at
    broadcast pedal 4.29–4.58 V, and never at or below 3.80 V;
  - while it was set, commanded φ read exactly 1.1640625, the last-column
    value, even though BFS sat mid-table.
- **Fuel Compensation** is the second fuel table: 16 × 16 at slot `+0x660`.
  **UpRev's editor labels its axes the wrong way round**; this is verified from
  the code (2026-10-04, `docs/ecu-firmware-analysis.md`):
  - **rpm** is `0xCACD`, which UpRev calls "VFR": raw × 50 = 400–6,400 rpm.
  - **Load** is `0xCB29`, which UpRev calls "RPM": raw 16…255, compared with
    RAM byte `0xFFFF118A` (`mem:ECM_FUEL_COMP_X_INPUT`).

  The k105 "rpm axis 800–12,750 → 800–7,200" edit therefore compressed the
  **load** axis to 16–144. Load above 144 (25 % of a 2026-10-04 drive) now
  reads the last column. That flattens the base table's +5–8 % high-load,
  high-rpm enrichment to about +2 %, a plausible contributor to the WOT lean.
  Plan any Fuel Comp edit by the code's axes, not UpRev's labels.
  **Reading UpRev's Fuel Comp view (operator check, 2026-10-04):**
  - **The cells are right.** UpRev's 16 × 16 grid matched memory row for row,
    256/256, with rows = rpm and columns = load.
  - **The two axis lists are swapped.**
    - Real rpm of row *i* = the "VFR %" label of column *i* × 128: 3.13 %
      reads as 400 rpm, 50 % as 6,400 rpm.
    - Real load of column *j* = the "RPM" label of row *j* ÷ 128 (as VFR %):
      800 reads as 6.25 %, 12,750 as 99.6 %.
  - **Edits:** to change Fuel Comp's load breakpoints in UpRev, edit its "RPM"
    axis; to change rpm breakpoints, edit its "VFR" axis.
- The RAM map-trace channels (`mem:ECM_FUEL_TARGET_MAP_X_TRACE`,
  `mem:ECM_ENGINE_SPEED_RAM`) are the ECM's *own* coordinates into the Target
  AFR table. The disassembly shows they are exactly the two inputs the code
  passes to the lookup:
  - the load byte at `0xFFFF103E` is compared directly with the raw BFS axis
    bytes;
  - the rpm word at `0xFFFF11D8` counts 12.5 rpm per unit and is compared
    with axis byte × 4.
  Interpolation is bilinear, clamped at the edges.
  `X = min(255, BFS × 128 / 44019)`, where 44019 is UpRev's **Load Point
  Scaler** (at `0x9416`, with a copy in each slot at `+0x760`). One X count is
  0.1685 ms. Both axis mappings are code-verified (W05.1) and recorded in
  `rom_viewer.yaml`: the X byte's raw count (catalog decode undone) against
  the raw axis bytes, rpm against the rpm axis, clamped at the edges. The ROM
  Maps page and the cell-residency endpoint now place the ECM cell. While the
  pedal-switch enrichment flag is set the ECM reads the last load column but
  the X byte keeps its BFS value, so the ECM cell sits off the column read.

## Spark and knock on this ECM

- **There is no base timing table.** UpRev: final timing is
  "RPM → Engine Load → Mass Air Flow → Calculated MBT → Ignition Timing Trim →
  Knock Strength Feedback". MBT is computed by a formula. The Timing Trim
  tables (Main, High Det, Cold; High Octane on some ROMs) are unitless and
  **cannot be converted to degrees**. The only degree-valued outputs are
  `IGN TIMING` (PID `0x0E`) and `mem:ECM_IGNITION_TIMING_RAM`, so a timing
  change must be read from the log, not predicted from a trim edit.
- **Knock is visible only through UpRev.** CONSULT has no knock item. Pre-2009
  ROMs expose a loggable *Knock Strength* counter: it jumps negative on each
  event, recovers slowly, and its step size varies by ROM. After too many
  events the *High Det Flag* goes 0 → 1 and the ECM **latches onto the High Det
  trim table**. Log both through any pull that matters. A timing deficit that
  persists across drives may be the latch, not the map. On NA engines, knock
  learning re-advances a large pull over several runs. Whether this ROM also
  interpolates toward a high-octane map was not established.
- The FSM describes knock retard as "designed only for emergencies", i.e. it
  does not operate under normal driving. Treat any sustained Knock Strength
  activity as a finding.

## ROM tables and what an edit moves

- The calibration is UpRev "Osiris" (`1ZS44A`, stock was `1ZS04C`). CANary can
  decode and verify the container offline (`src.analysis.osiris`,
  `docs/uprev-rom-codec-decoded.md`) and build hash-bound Target AFR edits
  (`docs/offline-rom-targets.md`). Only the operator flashes.
- Table names and axes follow UpRev's *Nissan Tuning Guide*. The rom viewer
  cites Rev 8, pp. 9–10; the public Rev 2 PDF on uprev.com has the same
  structure. Not every ROM version has every table.
- **Five map slots exist, and the engine reads a RAM copy of one.** UpRev's
  patch copies the selected slot (2 KiB, at `0x0E0000 + 0x800·(n−1)`) into RAM
  at `0xFFFFB040`. Its state block is at `0xFFFFB000`: magic `0xAA5555AA` at
  `+4`, selector nibble at `+3`, current map number at `+8`. At start-up it
  loads slot 1. Nissan's Target AFR pointer now points at the RAM copy
  (`0xFFFFB260` = copy + `0x220`), and the calibration's own table at
  `0x00A169` is unused.
- **The map is selected by a switch input, not a command.** Bits 1 and 3 of
  RAM byte `0xFFFF2EE0` are debounced, and each transition steps the
  selector. The committed active map is at `0xFFFFB008`, as 1–5; it reads 0
  after a cold boot, meaning slot 1 is loaded by default. It is now the
  channel `mem:ECM_ACTIVE_MAP`, and it read 0 all through the 2026-10-04
  drive. The magic value `0xAA5555AA` at `0xFFFFB004` was confirmed on the
  same drive. A non-zero reading is the only sign the map was switched.
- **What's in a slot, confirmed from the code that reads the RAM copy:**

  | Offset | Table |
  |---|---|
  | `+0x020` and `+0x120` | two 16 × 16 **timing trims** (rpm `0xCC93` × BFS-X `0xCC83`, cells masked `& 0x7F`; a flag at `0xFFFF2842` picks one, likely Main vs High Det) |
  | `+0x220` | Target AFR, 8 × 8 |
  | `+0x260` | a heavily used 16-bit map whose identity is still unknown |
  | `+0x660` | **Fuel Comp**, 16 × 16 (rpm `0xCACD` × load `0xCB29`, the reverse of UpRev's labels, verified from code) |
  | `+0x760` | Load Point Scaler |
  | `+0x762` | **K**, big-endian 16-bit, multiplied into Tp = K · Qa / Ne |

  The k106 "added spark" cells (`+0x09C`) are inside the first timing trim.
- **Flash image layout** (`docs/uprev-rom-table-verification.md`, 2026-10-04
  section, and the "Xterra ECM Flash Map" artifact):
  - boot block `0x000000–0x007FFF`, with vectors at `0x0`;
  - calibration block `0x008000–0x01FFFF`: calibration ID at `0x008204`,
    where the checksum range starts; checksum words at `0x9598`/`0x95A0`;
  - application `0x020000–0x0DFFFF`, with vectors at `0x020004`;
  - UpRev's patch code near `0x0DE000`;
  - map slots from `0x0E0000`.
  About 120 1-D curves (8- and 16-entry) are packed at `0x00CC1B–0x00D379`
  and read through two shared routines. Their names come from the variables
  each call passes (`docs/ecu-firmware-analysis.md`).
- Every flash, even the single-byte 2026-09-20 edit, reset LTFT to 0.00 %.
- **The injector swap needs more than K.** For the 700 cc injectors, UpRev's
  procedure is: lower K (first cut K × old/new flow), enter the new latency
  line, and retune Cranking Enrichment Initial ("the main table when larger or
  smaller injectors are installed") and Injector Minimum. Then set the **Load
  Point Scaler** to the expected WOT BFS. Smaller pulse widths compress the
  BFS axis, and the scaler re-spreads it; don't hand-edit axis values. A larger
  MAF tube or cone intake goes the other way: raise K, then rework the MAF
  curve under load against a flat "AFR test" target table.
- UpRev-only log channels: Target AFR, Knock Strength, High Det Flag, Injector
  Duty Cycle. Nissan-standard channels include A/F correction B1/B2 and BFS.

## Relearn procedures

From FSM EC-19–22. The FSM lists these triggers:

| Learning | Required when | Manual procedure (no CONSULT) |
|---|---|---|
| Accelerator pedal released position | pedal sensor or ECM connector disconnected | Pedal fully up. Ignition ON ≥ 2 s → OFF ≥ 10 s → ON ≥ 2 s → OFF ≥ 10 s. |
| Throttle valve closed position | throttle actuator or ECM connector disconnected | Pedal up. Ignition ON → OFF ≥ 10 s, listening for the throttle valve to move. |
| Idle air volume | throttle actuator or ECM **replaced**, or idle speed/timing out of spec | See below. |
| Mixture ratio self-learning clear | after a fuel-system repair, when the learned value is wrong | CONSULT "SELF-LEARNING CONT → CLEAR". Generic-tool substitute (EC-179): unplug the MAF, idle ≥ 5 s, reconnect, confirm P0102 with Mode 03, erase with Mode 04. |

**Idle air volume learning.**

- Preconditions: battery > 12.9 V at idle, coolant 70–100 °C, Neutral, electrical
  loads off, wheels straight, stopped, and about 10 min of driving first when
  done without CONSULT.
- Steps:
  1. Do the two learnings above, then warm the engine.
  2. Ignition OFF ≥ 10 s.
  3. Ignition ON, pedal up, wait 3 s.
  4. Fully press and release the pedal 5× within 5 s.
  5. Wait 7 s, then hold the pedal down ~20 s until the MIL stops blinking and
     stays on.
  6. Release within 3 s.
  7. Start the engine and idle 20 s.
  8. Rev 2–3 times and confirm 625 ± 50 rpm and 15 ± 5° BTDC.

What the FSM doesn't cover:

- **A battery disconnect is not a listed trigger** for any of these. It clears
  diagnostic memory within 24 h (EC-60) and, on this truck, the learned fuel
  trim (`labelled_events.yaml`).
- **The FSM says nothing about reflashing.** An UpRev flash is observed to reset
  learned fuel trim. Whether it also clears throttle or idle learning is
  unknown. If idle is unstable or off-spec after a flash, run the three learnings
  above.
- **The 75 mm throttle body is an ECM-visible part swap.** Do all three
  learnings after fitting it. UpRev also says a larger throttle needs its
  "Throttle Opening Area / Duct Area Calculation" table recalibrated; the
  throttle's flow potential is Fuel Comp's x axis.

## Common VQ40DE faults and how they look in a log

- **P0171/P0174 (lean, both banks).** The FSM checks in this order (EC-181):
  1. Exhaust leak **ahead of the manifold catalyst**, i.e. upstream of the A/F
     sensor. It reads lean, so trims go rich with no intake fault.
  2. Intake leak after the MAF, including the PCV hose connection.
  3. A/F sensor circuit.
  4. Fuel pressure, pump, filter, regulator.
  5. MAF (against the 2.0–6.0 / 7.0–20.0 g/s reference, altitude permitting).
  6. Injectors.

  Cracked intake boots and PCV hoses are the usual forum finds. The trim-shape
  method in `engine-management` separates leak from MAF from fuel pressure.
- **P0101 (MAF range/performance)** sets on a high reading at light load or a
  low reading at heavy load (EC-106).
- **P2A00/P2A03 (A/F sensor 1 range/performance)** is a sustained lean or rich
  shift. The FSM's causes include fuel pressure, injectors, and intake leaks, not
  only the sensor (EC-386).
- **Secondary timing-chain whine** is covered by NTB09-128a (2005–2010 N50): a
  high-frequency buzz rising with rpm, fixed by replacing both secondary chains
  and tensioner shoes. The TSB names no DTC. Late-stage wear is linked to
  cam/crank correlation codes in forum reports (low confidence).
- **P0011/P0021 (intake cam timing)**: check the oil level first
  (NTB06-079a). The phaser is oil-pressure driven, so low oil shows as
  commanded-vs-measured cam tracking error at hot idle.
- **P0443 (purge flow while commanded closed)**: NTB08-059 covers the 2008
  Xterra, and the fix is an ECM reprogram. Whether the UpRev base ROM includes
  it is unknown.
- **Manifold catalysts** can crack or break up (P0420/P0430; forum-level).
- **Radiator ATF cross-contamination** affects automatics only. It is **not
  relevant** to this manual truck.
