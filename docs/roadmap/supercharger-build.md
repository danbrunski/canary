# Roadmap — supercharger build (Xterra)

Status: **planned** (operator, 2026-09-21). Install starts in the next few weeks.
Straight to `main`, no feature branches. Vehicle-specific: this is the Xterra's
build, and everything it learns about the Xterra belongs in the vehicle's data
files and skill, not shared code.

## The build

| Part | Replaces | Notes |
|---|---|---|
| Z1 Off-Road supercharger kit, **Level 2** | naturally aspirated intake | Harrop HTV1900 (1.9 L Eaton TVS roots), carbon inlet manifold, liquid intercooler with dual-pass radiator. Z1 quotes Level 2 at **350 rwhp / 343 rwtq on 93 octane** — +132 whp / +123 wtq over a stock VQ40. |
| Aeromotive 340 fuel pump assembly | in-tank pump **housing**, including the filter | Replaces the whole module, so the stock filter — and whatever pressure regulation lives in that housing — changes too. The supply pressure is a new unknown. |
| Bosch 700 cc injectors | stock injectors | Injector constant (K) and dead-time table both change. |
| 75 mm throttle body | stock throttle body | Throttle-to-airflow relation, idle air, and the throttle-map axes all change. |
| Cone intake | stock airbox | The MAF housing and inlet flow change, so the MAF transfer function changes. |

Sources: [Z1 Xterra kit](https://www.z1offroad.com/supercharger-kits/z1-off-road/05-15-nissan-xterra-supercharger-kit-by-z1-off-road-p-68066.html),
[Z1 Frontier kit](https://www.z1offroad.com/supercharger-kits/z1-off-road/05-19-nissan-frontier-supercharger-kit-by-z1-off-road-p-76800.html),
[Harrop HTV1900](https://www.harrop.com.au/htv1900-supercharger). Z1's pages refuse
automated fetches, so the Level 2 details below are open questions, not facts.

**Open questions for Z1 / the install** (answer them here, with the source):

- [ ] Boost at Level 2 (psi), pulley size, and what separates Level 1/2/3/4.
- [ ] Does Z1 supply a base UpRev tune for this combination, or only a
  recommendation? Its injector data (K, dead times) for the Bosch 700 cc.
- [ ] Where the MAF sits in the kit's intake path and its housing diameter —
  the MAF scaling depends on both.
- [ ] Where intake-air temperature is measured after the kit (pre- or
  post-intercooler). Every VE and density number depends on it.
- [ ] Fuel pressure the Aeromotive assembly regulates to, and whether it is
  referenced to manifold pressure.
- [ ] Bypass-valve behaviour at part throttle (what the ECM's MAF sees off-boost).

## What the build breaks in CANary

Nearly every assumption the current analysis rests on is a naturally-aspirated
one. Name them now, before data from the new engine arrives looking plausible:

- **No manifold or boost pressure from the ECM.** The VQ40DE is MAF-based; Mode 01
  PID 0x0B is unsupported (checked live 2026-09-21), and so is absolute load
  (0x43). Boost is *not observable* through the ECM. Without an added sensor,
  every boosted analysis is blind to the one quantity that defines it.
- **No fuel pressure from the ECM either.** PIDs 0x0A, 0x22, 0x23 and 0x59 are all
  unsupported. The open question from 2026-09-21 — is WOT lean the MAF curve or
  delivery sagging? — could only ever be answered by an external gauge, and
  after the build fuel pressure matters more, not less.
- **No knock stream.** Final ignition timing (`mem:ECM_IGNITION_TIMING_RAM`, PID
  0x0E) is the only knock proxy. Under boost, timing pulled by knock control and
  timing mapped low look the same unless the map is known — so the ROM that is
  running must be on hand for every boosted analysis.
- **VE above 1.** The VE/density math assumes manifold ≈ baro. Boosted, VE vs
  baro exceeds 1 by design; the honest quantity becomes air per cylinder-event
  (g/cyl), which the analysis already computes.
- **Calculated load (PID 0x04) saturates at 100 %** by construction, so it cannot
  separate 0 psi from full boost. Gates that use `load >= 85` as "WOT" still work
  as a gate, not as a measure.
- **Every learned "normal" is invalidated.** IF/LSTM/TCN models and behavior
  surfaces are trained on the NA engine; on the new engine they will fire
  everywhere. The corpus needs a hardware boundary as firm as a tune boundary.
  `tune_revisions.yaml` records calibrations; nothing records hardware.
- **RAM addresses.** They survive calibration flashes on the same program
  (2026-09-21). A new UpRev base tune for a supercharged combination may still
  be the same program — re-run `tools.verify_memory_channels` on first start
  rather than assume.
- **Injector-derived channels.** `ECM_INJECTOR_PULSE_1/2` (0x1206/0x1207) stay in
  ms, but ms → fuel mass changes with the 700 cc injectors. `ECM_AIR_CHARGE`
  (0x1208, air × K) changes scale with K.

## Phase 1 — Catalog the NA engine (now, before teardown)

The name of the game: a baseline complete enough that every post-build number
has an NA number to be compared against, on the same instruments. Every capture
uses the `tuning` profile, so the baseline and the new engine share a schema.

- [ ] **NA1 — Steady WOT gear pulls.** 3rd gear, 2,500 rpm → limiter, at least
  three on the same stretch of road, tagged at start and end. The 2026-09-21
  drives produced only 2–6 s tip-ins; a steady pull is what makes a fuelling,
  timing and airflow curve rather than a scatter. Also the only way to reach the
  5,200 rpm / BFS 18.20 cell edited in the 2026-09-20 flash.
- [ ] **NA2 — Fuel pressure through a pull.** The AEM CAN gauge (P2) on the
  stock system, logged through NA1 — which means P2's fuel-pressure half must
  land *before* teardown, not after. Settles the 2026-09-21 question — steady pressure
  says MAF curve, falling pressure says delivery — and gives the stock pump's
  figure to compare the Aeromotive against. The single most useful baseline
  number the ECM cannot give.
- [ ] **NA3 — Closed-loop trim map.** Warm, rpm × MAF, both banks, across idle,
  cruise at several speeds, and light load. **Wait for LTFT to relearn first**:
  it read exactly 0.00 % after the 2026-09-20 flash, so today's map is STFT
  alone.
- [ ] **NA4 — Idle baseline.** Warm idle MAF, throttle position, STFT, idle rpm
  stability; with electrical loads stepped (headlights, blower, A/C) — the
  stock-injector dead-time reference (`injector-deadtime-idle`, archived, has
  the protocol). The throttle body and injectors both change idle.
- [ ] **NA5 — Whole-vehicle performance.** 40→80 mph (or 2,500 rpm → limiter) in
  a fixed gear on flat road, GPS speed, both directions to cancel grade and
  wind. A before/after acceleration number is the result the build is for, and
  it does not depend on any ECU channel meaning the same thing afterwards.
- [ ] **NA6 — Heat.** IAT and coolant through a long climb or repeated pulls.
  The NA intake is the reference for the intercooler's job.
- [ ] **NA7 — Snapshots.** A full DID sweep and Mode 06 at key-on, engine-running
  and warm idle; readiness; the exact ROM file and hash that is running
  (`ZS44A-20260920`). The static configuration DIDs are the easiest place to see
  what the new tune changed.
- [ ] **NA8 — Write the baseline down.** One document with the NA curves (λ error,
  timing, VE, g/cyl by rpm band; trim map; idle; acceleration) and the sessions
  they came from — the thing every post-build comparison cites.

## Phase 2 — Prepare CANary for the new engine (desk, before first start)

- [ ] **P1 — Hardware boundary.** Record the build as a boundary the corpus
  respects: training discovery must not mix NA and boosted sessions, and
  analyses that compare across it must say so. Extend the revision registry
  (an entry type for hardware, or a separate `build_revisions`) rather than
  inventing a second mechanism. Models will need retraining on the new corpus;
  per AGENTS.md, fix the model rather than adding kill-switches for the
  interim.
- [ ] **P2 — Boost and fuel pressure sensors.** The ECM supplies neither.
  **Decided (operator, 2026-09-21):**
  - *Fuel pressure* — an **AEM fuel-pressure gauge with CAN output**, on a
    **second CAN channel** on the Pi (not the vehicle bus). CANary work: bring
    up the second interface (bitrate per the gauge; it is a separate bus, so the
    vehicle bus's MCP2515 limits and budget do not apply), decode AEM's CAN
    frame for pressure into a channel with its own source prefix, record it into
    the same session, and make it a selectable capture channel so it lands in
    `tuning` captures and same-instant pairing.
  - *Boost* — the **supercharger kit's MAP sensor**, read through an **ADC on the
    Pi**. CANary work: an ADC collector (sample rate, the sensor's
    voltage→kPa transfer from its datasheet, and a sanity check that it reads
    baro with the engine off — the NA-era check that MAP must equal baro at
    KOEO), recorded as a first-class channel.
  - Both must share the capture clock (monotonic receive time, like the OBD
    path), or boosted fuelling analysis stays blind to the quantities that
    define it. Keep transports symmetric: a second CAN channel is a CAN source
    like `can0`, not a special case.
- [ ] **P3 — Vehicle data.** New injector K and dead times into
  `afr_calibration.yaml` once known; update the vehicle skill's physical facts
  (injectors, pump, throttle body, intake, boost, and that absolute load
  remains unsupported).
- [ ] **P4 — Boosted capture profile.** The `tuning` profile plus whatever P2
  adds, at rates set for a boosted pull (a roots blower makes boost almost at
  once, so transients are faster).

## Phase 3 — First start and the safe tune

**Flashing is a non-volatile write to the ECU**, so AGENTS.md's conditions hold
without exception: the operator asks for that flash, in those words, at that
time; `vehicle_state` must read `ignition_only` (key on, engine off,
stationary). An agent may prepare and review a ROM; it does not flash. The flash
itself is done with UpRev.

What a *safe* first tune has to get right, to be reviewed against the ROM
before it is written:

- **Injector scaling and dead times** for the Bosch 700 cc. Wrong K is the
  single biggest first-start risk: the engine runs lean or rich in every cell.
- **MAF scaling** for the new intake/housing.
- **Rich, conservative targets under boost** and **timing reduced in the
  boost cells**, both relaxed only as logs prove margin. Source the numbers from
  Z1's base tune or the tuner, not from this document.
- **A rev/load ceiling for the first drives**, lifted in steps.

First-start monitoring, in order, each step gated on the one before:

- [ ] **F1 — KOEO.** Fuel pump primes; no DTCs; `ignition_only`. Re-verify the
  RAM memory channels (`tools.verify_memory_channels`).
- [ ] **F2 — Idle.** λ at stoich, STFT near 0 on both banks (a large STFT at idle
  says K or dead time is off — stop and correct before driving), stable idle,
  no leaks, IAT sane.
- [ ] **F3 — Light load.** Closed-loop trims across rpm × MAF; compare with NA3.
- [ ] **F4 — Progressive load.** Part-throttle, then short WOT, then pulls; λ
  against target and timing each step, fuel pressure (P2) holding.
- [ ] **F5 — Compare with the baseline.** NA8's curves against the new engine;
  acceleration (NA5) before and after.
