# F01 — Engine functions the firmware has not told us yet

Written 2026-11-12, from the operator's question "is there anything else about
the firmware we can disassemble to fill knowledge gaps about engine functions?"

**Why this exists.** The decoded part of the ECM is the warm fuelling chain
(base fuel schedule, Fuel Comp, target φ, the injector pulse), the λ chain,
intake cam timing, EVAP purge and trim learning, the torque and throttle
demand path, the base timing term, knock observation and DTC reporting
([`../../ecu-firmware-analysis.md`](../../ecu-firmware-analysis.md)). Most of
the image is still unread. E07's coverage count for `ZS44A-20260920`, slot 1:
modelled code reads 1,185 calibration bytes, and 52,741 are unknown. Of the
153 one-dimensional curves, 118 have an inferred input and 35 have none.

This file lists the engine functions that a listing pass can still explain,
in the order worth doing them, and says what question each one answers.

**Where it came from.** Compiled from `docs/ecu-firmware-analysis.md`, the
`uncovered` list in `data/nissan-xterra-2008/ecu_application_model.yaml`,
`engine_sim_contract.yaml`, the E05–E09 checklists and `~/ecu-re/NOTES.md`. No
new listing pass was made for it. "Nothing recorded" below means neither the
firmware doc nor the notes mention the function. It does not mean someone
looked for the code and failed to find it.

Same conventions as the other task files: task-level truth is the checkboxes,
`[x]` only with the evidence named, `[~]` for partial with a note.

## Working rules

- **The listing stays out of Git.** Disassembly and decompiled code live in
  `~/ecu-re/`. The repo records addresses and interpretations only, in
  `docs/ecu-firmware-analysis.md`, with each claim marked verified (code),
  verified (data) or inferred.
- **One owner for the Ghidra project.** If another session has it open, send
  that session the asks. If none does, run `analyzeHeadless … -readOnly`.
  Never copy the project.
- **Check every constant against the listing.** Ghidra folds calibration
  bytes into constants. Read each one from the image, on every calibration
  the claim is made for.
- **"No store found" is not evidence until F01.1 is done.** `0x0726DC` was
  missing from the listing and was found only by an opcode scan.
- **A decode is a hypothesis until it reproduces a known answer.** Each item
  names the logged quantity or truck read that would confirm it. Compare with
  `src.analysis.compare.compare_channels`, on `acquired_at`, never on
  aggregates.
- **Results land in the structured homes**, not only in prose: a closed gap
  removes or narrows its `uncovered` entry in `ecu_application_model.yaml`, a
  new table goes in `rom_map.yaml`, a new channel in `data/dids.yaml`
  (`hypothesized` until a live read).
- **These are not `implementer` dispatches.** They need `~/ecu-re`, which is
  on one machine. The follow-on work in the repo (a manifest node, a catalog
  entry, a simulator term) goes out at the Deep tier.
- **Reads only.** Nothing here writes the ECM.

## Foundation

- [ ] **F01.1 Seed every task table, and decode the scheduler's periods.**
  Functions reached only through task tables are not all in the listing
  (`0x0726DC`, pointer at `0x021A20`, was one). The tick that every enrichment
  timer steps on (`0xFFFF84C3` bit 7) and the period of the `0x020EF2` cycle
  are undecoded (`uncovered: enrichment_tick_rate`), and the peripheral clock
  is inferred (20 MHz).
  - *Answers:* whether a missing writer is really missing, for every later
    item; the duration in seconds of every enrichment ramp and hold.
  - **Accept:** every pointer in the task tables at `0x0219D0`–`0x021A30…`
    resolves to a function in the saved project; the listing is regenerated;
    the periods are stated with the timer registers that set them; one
    `0xFFFF84B9`-paced counter is timed against acquisition time on the truck
    (the 1.024 ms tick).
  - [~] 2026-11-12, desk part done; truck reads owed. 24 functions seeded
    (2,532 → 2,556, backup `~/ecu-re/backup/XterraECM-20261112-pre-F01.1.tar.gz`),
    listing regenerated; all 254 entries of the eight task tables
    (`0x021818`–`0x021BE8`) resolve to functions, including the three that did
    not (`0x0726DC`, `0x05152C`, `0x051F48`). Group periods, the main-cycle
    trigger and the time base are in the firmware doc, "Scheduler and task
    tables". The ISR's divider cascade sets `84C3` bit 7 every 10.24 ms, bit 6
    every 102.4 ms, bit 5 every 1.024 s and bit 4 every 10.24 s. Each
    enrichment step is one main cycle that saw a 10.24 ms event (nominal
    10.24 ms; steps coalesce on overrun). `0xFFFF84BB` is the constant-zero
    clear source, not an event source. The byte-timer arrays tick at 1.024 ms,
    10.24 ms or 102.4 ms. `enrichment_tick_rate` is narrowed to Pφ and
    overrun. Owed on the truck: two host-timestamped reads of the tick counter
    `0xFFFF84B0` (the acceptance's paced counter) and of `0xFFFFF430` (the
    run-time words' clock, not decoded), plus the run-time words.
    Side finding: `~/ecu-re` SeedCode.java's prologue mask `0xF0FF` can never
    match `mov.l Rn,@-r15` (needs `0xFF0F`), and it skipped GBR-saving
    prologues.

## Fuel control

- [ ] **F01.2 Closed-loop enable, authority and the rear-sensor trim.** What
  starts, freezes and resets the STFT controller `0x05E368` is untraced
  (`uncovered: closed_loop_enable`), and so is the writer that holds
  `0xFFFF26B4`/`0xFFFF26B6` at `0x6400`. The trim clamp is undecoded
  (`closed_loop_trim_authority_undecoded`). The downstream-O2 correction of
  the front sensor is traced as a chain but named by inference.
  - *Answers:* which samples carry feedback; why the simulator marks every
    closed-loop cell `unsupported`; whether UpRev's "closed loop only where
    the cell is exactly 14.7" holds (the gate already traced writes φ 1.0 for
    every lookup below cal `0x8B39`, lean cells included, so it is not that
    gate).
  - **Accept:** the enable conditions and the clamp are listed with their
    calibration addresses; a same-window read of `0xFFFF26B4`, `0xFFFF4722`
    and `0xFFFF46B2` at warm cruise in a 14.7 cell and in an enriching cell
    agrees; E09's `closed_loop_trim_authority_undecoded` is lifted.
  - [~] 2026-11-12: enable, holds, reset and clamp traced; the truck read and
    the simulator follow-on are owed. Controller `0x05E368` → `0xFFFF2D08`,
    clamp cal `0x8F64`/`0x8F62` (0.75–1.25, anti-windup at the clamp);
    arbitration `0x05CCD0` → `0xFFFF26B4` via `0x05C9F4`, clamp cal
    `0x8F66`/`0x8F68` (same values, identical on all seven images). Ready
    latch `0xFFFF26B9`: time since start beyond curve `0xD07F`, coolant warm,
    A/F sensor ready, held 200 cycles. The enrichment gate (`0x05DB04`) forces
    STFT to 1.0 whenever `0xFFFF26C0` ≠ `0x4000`, so the loop runs in lean
    cells and resets in rich ones. Mode 01 PID 0x03 is the firmware's own
    report of these gates (`0x083058`). Data: ten captures, 519/536 enriching
    reads report status 4 and STFT reads exactly 0 % on 99.1 %. Written up in
    the firmware doc ("Closed-loop enable and trim authority") and the model
    (`closed_loop_feedback_stft`; `closed_loop_enable` narrowed to the
    unidentified hold flags). The "rear-sensor trim" part is not done. Not
    lifted: `closed_loop_trim_authority_undecoded`. The clamp is decoded, but
    the reason lifts only when every closed-loop override is
    vehicle-qualified (E09.10), so applying the ±25 % clamp in `paired_steady`
    is a Deep follow-on.

- [ ] **F01.3 Cold start, after-start and warm-up fuelling.** UpRev documents
  a Cold Fuel Comp table used below 70 °C; the traced Fuel Comp lookup
  (`0x03CC3C`) has no coolant branch (`uncovered: cold_fuel_comp_table`). The
  inputs of the corrections `0xFFFF26F6` and `0xFFFF27DC` (`0xFFFF1114`,
  `0xFFFF10C6`, `0xFFFF2D10`/`2D14`, `w1762`/`w1726`/`w14DA`) and their gating
  flags are unidentified (`correction_inputs`, `enrichment_flags`). The hold
  counter `0xFFFF2947` has no decrement found. The wall-film term is located
  but its purpose is inferred. The cranking Tp curve (`0xEEBA`) is traced; the
  start and minimum pulse curves are only located.
  - *Answers:* what the engine is commanded below 70 °C, where every fuel
    plant fit is refused today (warm only); what the first start on 700 cc
    injectors needs changed besides the flow scaler.
  - **Accept:** the cold table is in `rom_map.yaml` with its reader; each
    correction input has a name and a source; a cold-start capture reproduces
    `mem:ECM_TARGET_EQUIV_RATIO` from coolant and time since start.
  - [~] 2026-11-12, first desk pass. **This ROM has no Cold Fuel Comp
    table**: both calls of the Fuel Comp wrapper read the one slot table, and
    nothing else touches its axes or RAM copy. UpRev's guide says the cold
    table is ROM-dependent. The acceptance's first clause becomes "shown
    absent", and `uncovered: cold_fuel_comp_table` is removed (the evidence is
    on node `fuel_comp_lookup`).
    - Warm-up enrichment `L175C` runs while F01.2's ready latch is clear and
      fades out (`w14DA` −49 per call) once the loop conditions hold.
    - `w27DC`'s input `0xFFFF2D10`/`2D14` is a per-bank estimator state from
      the A/F excess over 14.7, applied only in the post-transition STFT
      hold; a catalyst oxygen-storage estimate, inferred.
    - The hold counter `0xFFFF2947` is a 10.24 ms byte timer, about 20 ms.
    - Firmware doc: "Cold and warm-up fuelling, first pass".
    - Not done: `0xFFFF1114`/`10C6`, the start and min pulse curves, the wall
      film's purpose, `0xFFFF2BA9`, and the cold-start capture.

- [ ] **F01.4 The short-pulse correction.** Cal `0x9120`–`0x9126`, applied
  after DID `0x1206` is taken, undecoded (`short_pulse_undecoded`).
  - *Answers:* idle and light-load delivery with 700 cc injectors, where the
    pulse is shortest and this term is largest.
  - **Accept:** the formula is in the firmware doc and `rom_map.yaml`, and a
    same-window read of `0xFFFF279C` (x) and `0xFFFF27A8` (y) at hot idle
    matches it. (Amended 2026-11-12: DID `0x1206` is x, taken before this
    stage, so the pulse-chain known answer cannot include it.)
  - [~] 2026-11-12, decoded; truck read owed. `INJ_short_pulse_linearize`
    `0x05B8B0`: `y = max(x, a, b)`, a/b = `min(0xFFFF, (x·gain + 100·offset)
    >> 14)`, cal 16358/15846/74/959, identical in every ZS44A image. At most
    +50 µs and zero above 1.78 ms. On 700 cc injectors more of idle and
    light load falls below 1.78 ms, so it acts on more cells; whether the cal
    should change with them is a supercharger-build question (the values
    describe the injector, not the engine). In the firmware doc, `rom_map.yaml`
    (`short_pulse_gain_a/b`, `short_pulse_offset_a/b`) and
    `src.analysis.injector_pulse.short_pulse_linearize` with tests. Follow-on
    (Deep): the simulator still refuses short pulses as
    `short_pulse_undecoded` (`paired_scenarios.py`); applying y in the plant and
    renaming that reason is an E07 modelling change.

- [ ] **F01.5 Fuel cut.** The rev limiter (`0x04F768`) and the stationary
  high-load cut (`0x04F6A4`) are traced. Open (`uncovered: fuel_cut_paths`):
  every writer of `0xFFFF26B8` and where it sits in the cycle, the decel-cut
  trigger and its recovery (the BFS decel-cut curves are all zero, so it is
  not BFS-based), the item-`0x30` limp cut, and the flag `0xFFFF2841` bit 1
  that selects the lower rev limit.
  - *Answers:* predicting cut and re-entry instead of reading them; the lean
    spikes at closed-loop re-entry that the light-load trim cells carry
    (N01's noise floor).
  - **Accept:** entry and exit conditions listed with their thresholds, and
    checked against `0xFFFF26B8` on one logged drive.
  - [~] 2026-11-12: entry traced, exit partly. The only writer of
    `0xFFFF26B8` is `0x04F240`, main entry 29, before the fuel task (37).
    - **Decel cut** `0x050130`: idle flag `0xFFFF2833` bit 4 plus
      `0xFFFF2835` bit 4, and rpm above curve `0xD379` on coolant. Warm, that
      is 1,800 rpm, or 1,400 rpm on the moving path (curve `0xD3AD`); 3,000
      cold. No hysteresis. Identical on all seven images.
    - **Checked against the captures' fuel-cut sentinel** (ten drives, 1,884
      cut reads). No closed-throttle read below 1,300 rpm cuts, and 66 % at
      or above 2,000 rpm do. So `0xFFFF2835` bit 4, or a delay, still gates
      entry.
    - **Dead on this calibration:** the zone cut `0x050000` (tables zero, zone
      flag never set) and the BFS decel curves.
    - **Protection cuts listed:** overheat above 2,200 rpm, stationary
      high-rpm, and the diagnostic limps.
    - **Not done:** `0xFFFF2835` bit 4, the re-entry group
      `0x05059C`/`0x05099E`, the limp flags' meanings, `0xFFFF2841` bit 1,
      and the same-window truck check. Firmware doc: "The cut mask and the
      decel cut".

## Spark

- [ ] **F01.6 The rest of the final timing chain.** `0xFFFF27C6` is computed
  and `0xFFFF279A`'s inputs are known. Still open (`uncovered:
  timing_final_chain`): no node writes `0xFFFF11CB` (intake-temperature term,
  source `0xFFFF2BD0`), `0xFFFF27C4` or `0xFFFF1203`; the `0x04E5B0` slew; the
  gating flags of `0xFFFF1766`; inside `27C6`, `0xFFFF26E8` and the
  `175C`/`1736` corrections; and the unnamed words `174A`, `1D3E`, `1344`,
  `2334`, `272A`, `62B4`, `1215`, `9932`.
  - *Answers:* absolute ignition timing from the ROM (E05.4), not only the
    change a trim edit makes; how much timing the ECM pulls for intake
    temperature, which decides what a post-intercooler IAT signal does to
    spark after the build.
  - **Accept:** `ignition_chain_unmodelled` names nothing but the knock term;
    the chain reproduces DID `0x1107` at hot idle and steady cruise.
  - [~] 2026-11-12: every former argument now has a writer. Only the knock
    branch (F01.7) is left. Firmware doc: "Ignition timing chain: the
    remaining terms".
    - **Intake temperature** (`11CB`, `0x04D8FC`): 0 below IAT 46 °C,
      +0.11°/°C above 30 °C, capped at 7° from 94 °C, off idle only. It comes
      off the trim term.
    - **Zero or constant on this calibration:** `1D3E` = 0x40 (cam error
      term, neutral below 120 °C coolant); `1344` and `2334` = 0 (map
      `0xAEF4` and curve `0xE584` all zero).
    - **Transient:** `272A`, a post-coast retard (−20 floor).
    - **`1215` is not knock.** It is the larger of the torque-reduction retard
      `2798` and the decel fuel-cut retard `27BE`.
    - **Scan tool:** `62B4` is the ±2° stored adjustment (record 0x74), and
      `9932` the service-0x30 active-test retard.
    - **RAM catalog:** `1344`, `1215`, `11CB` and `1D3E` corrected.
    - **Not done:**
      - the twin nodes for these terms, so `ignition_chain_unmodelled` still
        lists the chain;
      - the `0x1107` comparison, which needs one truck window set (listed in
        the model's `next_evidence`);
      - `26E8`/`175C`/`1736` inside `27C6`, the `279A` ramps and the enable
        flags.

- [ ] **F01.7 Knock control's response (operator decision first).** The
  knock work so far is observation only by choice: sensors, levels, the fault
  path, HI DET and the fallback are decoded; the retard step per event, the
  recovery rate and the threshold tables are deliberately not recorded.
  - *Answers:* how much margin the ECM takes back on its own under boost, and
    how fast it returns it.
  - **Accept:** the operator says whether to open it. If yes: the response
    law is in the firmware doc and matches `0xFFFF1254` on a logged knock
    event. Fix KS1 first (P0328); the truck is in fallback and shows no
    normal knock control to check against.

## Air and throttle

- [x] **F01.8 The MAF curve and the MAF-fault fallback.** The MAF transfer
  curve `0xE27E` is not in `rom_map.yaml` (E09.8). The airflow pipeline is
  decoded but stateful. The fallback table `0x95E5` (item `0x114`) has one
  unidentified axis input (`b107F`).
  - *Answers:* the main lever for the cone intake and new MAF housing; what
    the engine runs on if the MAF signal is lost.
  - **Accept:** `0xE27E` is a declared, editable table with a verified axis;
    the fallback's second input is named.
  - [x] 2026-11-12. Firmware doc: "The MAF curve and the MAF-fault fallback".
    - **The curve.** `rom_map.yaml` `maf_transfer_curve` is editable and
      verified: 64 be16 cells, entry *i* at MAF A/D word 16 *i* (0.078 V
      nominal), cells in the airflow word (149/25,600 g/s).
      - The axis is implicit, from the lookup's step, so it is stated in the
        entry's evidence. The schema has no implicit-axis field.
      - Checked against the 2026-09-17 sweeps: DID `0x1209` ÷ curve(DID
        `0x1204`) has median 1.0000 below 8 g/s, and 1.007 above 16 g/s,
        where purge adds.
    - **The model.** `airflow_qa` cites the curve, so the classifier names the
      lever (blocked on `airflow_qa`, E09.8). The "unmapped byte" tests and
      the dashboard fixture moved to `0xE584`.
    - **The fallback.** Its second input is `0xFFFF107F`, the PID `0x45`
      relative throttle position. Its rpm bands end in a fuel cut at 2,400
      rpm, the FSM's MAF fail-safe.

- [x] **F01.9 Throttle area units.** The area curve (axis `0xDD94`, values
  `0xEE6A`) is used both ways: by the air model that makes the Fuel Comp load
  byte and by the torque-to-throttle path. Its absolute units and the
  displacement factor (158) are unconfirmed.
  - *Answers:* what the 75 mm throttle body changes. The same target area
    will pass more air than the ECM expects, in both paths.
  - **Accept:** the units are pinned from a second calibration or program
    (another VQ40 or VQ35 image with a different throttle body), or the item
    is closed as not settleable at the desk with the truck measurement named.
  - [x] 2026-11-12: closed as not settleable at the desk. Firmware doc:
    "Throttle area units: not settleable at the desk".
    - **Why.** Every image is one program and calibration lineage, and the
      code applies no physical constant to the area or to 158.
    - **The swap does not need the unit.** Both paths are wrong by the ratio
      of new to old effective area at each sensor reading, which is
      measurable.
    - **The measurement that would pin it:** a choked-flow window of
      `0xFFFF2C32`, `0xFFFF63FC`, MAF, baro and IAT at idle and on closed
      coasts. That gives Cd·A mm² per count.

- [ ] **F01.10 Idle speed control.** Only the integrator and the learned
  value appear (`w63FC = min(w3CB2 + w5EA6, 1185)`). Nothing recorded: the
  target idle speed and its coolant, A/C and electrical-load steps, the
  feedback law, idle spark feedback, and the learning conditions.
  - *Answers:* what "idle air volume learning" needs to complete after the
    throttle body swap, and what an idle that hunts afterward is doing.
  - **Accept:** target, feedback and learning conditions listed with
    addresses; the learned word is a catalogued RAM channel.
  - [~] 2026-11-12: the idle-air half is done. Firmware doc: "Idle air: the
    throttle-area correction and its learning".
    - **What `w63FC` is.** The integrator `0xFFFF3CB2` plus the learned
      `0xFFFF5EA6` form a throttle-area correction. It matches the throttle
      model's area to the area the measured air implies. It is not an rpm
      loop.
    - **Decoded:** the feedback law, its enable conditions (`0xFFFF480B`
      bit 4), the learning step, the IAVL procedure (700 calls at double
      gain), and non-volatile persistence (records 0x71/0x72). Only the
      integrator persists; the learned term restarts each power-up.
    - **Catalogued:** `mem:ECM_IDLE_AIR_*` (four channels, hypothesized).
    - **Not done:** the idle speed controller (target rpm and its steps) and
      idle spark feedback.

- [ ] **F01.11 What a flash or a battery disconnect clears.** LTFT is known
  to reset on both. Whether throttle closed-position and idle air learning
  survive is unknown (vehicle skill), and the loss condition of the inverted
  backup copy is not decoded (`engine_sim_contract.yaml`).
  - *Answers:* which relearn procedures a flash actually requires.
  - **Accept:** the backup-RAM validity check and its consumers are listed;
    one before/after read across an operator flash confirms it.

- [ ] **F01.12 Barometric pressure: when it updates.** The chain is traced
  (`0xFFFF4C30` from a slew-gated slow filter, PID `0x33` from it). That the
  source is the EVAP pressure sensor read while the canister is vented is
  inferred from the fault items that force the default.
  - *Answers:* whether baro tracks a climb from 82 to 63 kPa or holds a stale
    value. It feeds the λ gain, the injector pressure model and a torque
    limit.
  - **Accept:** the update gate is traced; PID `0x33` is compared with GNSS
    altitude over one mountain drive.

- [ ] **F01.13 Torque limits and who asks for them.** The arbitration is
  traced. Open: the torque-down request `0xFFFF3C36` (`0x07A00C`) and what
  sends it on a truck with no TCM, the baro × rpm and intake-temperature
  limit `0xFFFF37A2` (table `0xB374`, all 1.0 today), the all-gear limp cap's
  inputs (`0xFFFF4856` bit 3, `0xFFFF2B46`/`2B47`, and which received
  messages `0xFFFF41B1`/`41BB` track).
  - *Answers:* what can close the throttle under boost without a code; what
    VDC asks of the engine.
  - **Accept:** each limit has a source, and the limp cap names its CAN ids.

## Nothing recorded yet

- [ ] **F01.14 The CAN frames the ECM builds and reads.** Trace the transmit
  builders and receive handlers.
  - *Answers:* the exact byte, scale and offset of every ECM-originated
    broadcast signal, replacing statistical bindings in `data/bindings.yaml`
    with code evidence; which received frames the ECM acts on (F01.13).
  - **Accept:** a table of id → bytes → RAM word for every frame, and every
    ECM-originated entry in `bindings.yaml` either agrees or is corrected.

- [ ] **F01.15 Misfire monitor.** Only its DTC items and MIL blink routine
  are noted.
  - *Answers:* whether a per-cylinder crank-speed roughness word exists in
    RAM. It would be a new channel for the anomaly detectors and a
    per-cylinder check that fuel trims cannot give.
  - **Accept:** the roughness and counter words are located and read live at
    idle, or the item is closed with why they are not reachable.

- [ ] **F01.16 Outputs: fans, A/C cut, alternator, VIAS.** Nothing recorded.
  The FSM gives the VIAS valve as closed from 2,200 to 3,300 rpm.
  - *Answers:* expected-state rules for behaviour models (fan on by coolant,
    A/C cut at full throttle), and the real VIAS switch points, which put
    steps in VE and torque.
  - **Accept:** each output's condition and thresholds listed from the image.

- [ ] **F01.17 The unidentified curves and maps.** 35 curves with no input,
  118 inferred, and the flash-resident two-dimensional maps.
  - *Answers:* nothing by itself. It is the sweep that finds what the items
    above missed.
  - **Accept:** `~/ecu-re/out/tables_1d.yaml` has no `unknown` input, and
    each curve is assigned to a function in this file or marked unused.

## Not disassembly: decoded, waiting on a truck read

These are not listing work. More disassembly will not close them. Each is
tracked where it is named; listed here so they are not mistaken for F01 items.

- Purge α in the pulse chain: read `0xFFFF4698`–`469B` with the fuel window
  (E07.3, E09.11).
- Seven hypothesized timing-chain channels: `0xFFFF15DA`–`15DE`, `17C6`,
  `19CB`, `1B24`, `10EB` with `27C6` and `1A58` (E05.4 (c)).
- First-gear limit word `0xFFFF3C62`, the ratio word `0xFFFF27D2` in steady
  fifth, and the cruise conversion words (firmware doc, "Open").
- Airflow state through throttle steps and purge on/off
  (`uncovered: airflow_pipeline`).
- Air-charge branch state at key-on and crank (`air_charge_branch_state`).
- `mem:ECM_ACTIVE_MAP` stepping when the map switch is used.

## Order

F01.1 first: it makes every later "not found" trustworthy. Then by what is
coming:

- **Supercharger build:** F01.3, F01.4, F01.8, F01.9, F01.10, F01.11, then
  F01.6 and F01.13.
- **Simulator and twin:** F01.2, F01.5, F01.6.
- **New instruments for CANary:** F01.14, F01.15, F01.16.
