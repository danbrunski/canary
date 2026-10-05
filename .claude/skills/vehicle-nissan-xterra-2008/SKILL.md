---
name: vehicle-nissan-xterra-2008
description: >-
  Vehicle-specific facts for the 2008 Nissan Xterra (VQ40DE, manual 6-speed),
  the development vehicle for this CANary install, profile slug
  `nissan-xterra-2008`. Use this whenever a task touches THIS truck's ECU,
  telemetry, logs, or tune: which PID is λ and which is φ, what a Mode 22 DID
  or RAM channel really measures, how the diagnostic session and K-line
  behave, what an UpRev flash resets, factory reference values and relearn
  procedures, the upcoming supercharger build, and the physical facts
  (MAF-only with no MAP sensor, 63–82 kPa ambient, no TCM, an IAT that reads
  engine-bay air at idle) that change how its data must be read. Load it
  alongside `ecu-reverse-engineering` (generic decode method) or
  `engine-management` (generic engine behaviour). Those two stay
  vehicle-agnostic on purpose, and this file is where the Xterra-isms live.
  Do NOT apply anything here to another vehicle.
---

# 2008 Nissan Xterra: vehicle-specific ECU facts

Profile slug `nissan-xterra-2008`. Engine VQ40DE, 4.0 L V6, port injection,
naturally aspirated (for now), drive-by-wire, mass-airflow metered.
**Manual 6-speed, so there is no TCM on the bus**: skip TCM steps in any
capture protocol.

**This file is per-vehicle. Another CANary user may be on a completely
different ECU.** Generic method belongs in `ecu-reverse-engineering` /
`engine-management`. If you learn something here that is true of ECUs in
general, put it there instead.

## Place every session on three boundaries before comparing anything

1. **Calibration.** The operator reflashes with UpRev often. Resolve each
   session's calibration from `tune_revisions.yaml` by flash time
   (`src.vehicle.tune_revisions.resolve_session_revision`). The calibration
   id does **not** change on an UpRev flash (it stays `1ZS44A00000`), so
   comparing ids cannot detect an unrecorded flash. Every flash resets the
   learned fuel trim (below). Whether it also clears throttle and idle learning
   is unknown; if idle is off after a flash, run the FSM relearns
   (`references/engine-and-ecu.md`).
2. **Hardware.** A supercharger build lands from late 2026-09: Z1 Level 2
   (Harrop HTV1900), Aeromotive 340 pump assembly, Bosch 700 cc injectors,
   75 mm throttle body, cone intake. Every NA-learned quantity is invalidated
   by it: the MAF transfer function, K and dead time, idle and throttle
   learning, behaviour surfaces, and the trained detectors. NA baselines, open
   questions and the first-start plan are in `docs/roadmap/supercharger-build.md`.
   The build should be recorded as an `intervention` with
   `corpus_boundary: true` in `labelled_events.yaml`. What it will look like in
   this truck's data (generic mechanics are in `engine-management` →
   `air-load-and-boost.md`):
   - The HTV1900 is a positive-displacement roots blower, so it holds pressure
     *ratio*. Expect about 21 % less gauge boost at home than a sea-level figure
     (7 psi → ~5.5 psi), and less again on mountain drives (~4.4 psi at 63 kPa).
     That is not belt slip.
   - The ECM measures no boost. With returnless fuel and no MAP input, injector
     ΔP falls by the boost pressure: a ~5–7 % WOT flow deficit that moves with
     baro and that the trims never see (WOT is open loop). Unless the fuel
     system gets a boost-referenced regulator, the TMAP and AEM data CANary
     collects can only reach the ECM as Fuel Targets (rpm × BFS) numbers. That
     table can't see baro or charge temperature. The ROM's commanded φ will also
     read richer than what is delivered, by design. See
     `supercharger-build.md` → "Boosted fuelling without a pressure input" and P5.
   - Z1's other kit (2022+ Frontier, VQ38, same blower) moves the MAF into a new
     tube and routes the ECU's IAT signal to a post-intercooler sensor through
     the MAF connector. If this kit does the same, `INTAKE_TEMP` stops being
     engine-bay air and becomes charge temperature. Check before reusing any IAT
     trap from this file.
   - Z1's figures on 93 octane: Level 1 309 whp / 307 wtq, Level 2 350 / 343,
     Level 3 443 / 441 (snippets of Z1's own pages; the Level 2 boost and pulley
     are still unpublished).
3. **Altitude.** Home is **79–82 kPa** (Colorado Springs, ~2070 m), but drives
   reach **63–73 kPa** (2,900–3,900 m). Read `BAROMETRIC_PRESSURE` (PID `0x33`)
   per session. Never assume sea level, and never assume 80 kPa either. The
   trained models fire out-of-distribution at 63–73 kPa, and that is not a
   fault.

## Canonical homes: check these before trusting anything below

Values drift, and the data files are the truth. This file holds facts that
have no structured home, and it **points at** the ones that do rather than
copying them. Paths are under `data/nissan-xterra-2008/` unless shown.

| Fact | Lives in |
|---|---|
| Broadcast byte→signal bindings, scales, evidence | `data/bindings.yaml` |
| Quantities searched for and NOT found on the bus | `data/bindings.yaml` → `absent_signals` |
| Mode 22 DID catalogue, decode params, evidence | `data/dids.yaml` → `dids:` |
| RAM (service `0x23`) channels and read windows | `data/dids.yaml` → `memory:` |
| Session gating, keep-alive, exit, max read length | `data/dids.yaml` → `access` |
| Which PIDs the ECM answers | `get_obd_coverage` (live); `pid-probe-2026-07-22.md` |
| A/F sensor voltage → AFR, and the λ/φ proof | `afr_calibration.yaml` |
| Which commanded-λ source is valid in which regime | `lambda_delivery.yaml` |
| Flash history, which calibration was running | `tune_revisions.yaml` |
| Trim resets, altitude trips, other non-fault events | `labelled_events.yaml` |
| Run-to-run noise floor (by condition) | `noise_floor.yaml` |
| K-line addresses, wake-up, modules (ECM, ABS) | `kline.yaml` |
| ROM tables, axes, verified extraction | `rom_viewer.yaml`, `docs/uprev-rom-table-verification.md` |
| Flash image layout, map slots, candidate tables | `rom_map.yaml` (W08), `docs/uprev-rom-table-verification.md` |
| What the ECM's code computes (disassembly findings) | `docs/ecu-firmware-analysis.md` |
| Factory service manual (engine control = EC) | `xterra_fsm/ec.pdf` |
| Per-vehicle partitioning of all learned state | `src/vehicle/profiles.py`, keyed by VIN |

## The traps that have cost the most on this truck

- **PID `0x24` is λ and PID `0x44` is φ.** They are reciprocal conventions on
  one ECM, so neither can be inferred from the other. In closed loop `0x44`
  dithers 0.9796 / 1.0204, not a flat 1.000.
  And `0x44` is φ only while the ECM's A/F feedback target is 14.7 (firmware):
  it reads 0.871 at idle with φ = 1.0. Use `mem:ECM_TARGET_EQUIV_RATIO`.
- **UpRev's AFR is not an independent instrument.** It is the A/F voltage
  through the ECM's table *without* the ECM's barometric gain, so it reads lean
  at altitude. PID `0x24`/`0x28` is the corrected value.
- **No MAP sensor, and no boost, fuel pressure, or torque from the ECM.** PID
  `0x0B` is unsupported (the VQ40DE is MAF-metered), and so are `0x0A`, `0x22`,
  `0x23`, `0x59`, `0x43` and `0x61`–`0x63`. A "manifold pressure" label on any
  byte of this vehicle is wrong by construction; that mistake sat in
  `bindings.yaml` for three months.
- **Mode 01 and Mode 22 are mutually exclusive.** Mode 22 needs session `0xC0`,
  and inside it Mode 01 goes silent. `10 81` is the only fast exit. TesterPresent
  is rejected, and Mode 22 takes one DID per request. Details:
  `references/diagnostic-access.md`.
- **Several DIDs are not what they were first named.** The worst one is
  `0x1208` (`ECM_AIR_CHARGE`): it is air charge × K, the base fuel schedule
  (verified 2026-10-02; the ms channel is `derived:BASE_FUEL_SCHEDULE`), not
  injector pulse width, so never derive fuel mass or φ from it. Read a DID's
  `description` and evidence in `data/dids.yaml` before trusting its name.
- **Long-term fuel trim resets on every flash and every battery disconnect, and
  relearns slowly.** After the 2026-06-28 disconnect it read exactly 0.00 % on
  both banks until sometime between 09-10 and 09-18. The 09-20 flash reset it
  again, and it was still 0.00 on 09-23. That 0.00 is the neutral value (raw
  `0x80` on the PID `0x07` scale), not a decode bug or a fault. While it sits
  there, trim analysis is STFT-only. Once it is active, use total trim
  `(1+STFT)(1+LTFT)−1`. Over Mode 22 on CAN, learned trim is DID `0x1125`
  (B1) and `0x1126` (B2), and short-term trim is `0x1123`/`0x1124`. These are
  the same RAM words PIDs `0x06`–`0x09` read (firmware, 2026-10-04).
- **DID `0x1102` is vehicle speed on CAN, not learned trim.** The catalog
  named it `ECM_LONG_FUEL_TRIM_B1` from UpRev's *K-line* stream definition.
  The firmware has separate K-line and CAN DID tables, and on CAN `0x1102` is
  the speed byte (one byte, km/h = raw × 2). It tracks wheel speed with
  correlation 0.9997. The catalog's `0x1125` is bank-swapped as well. **Never
  carry a DID's meaning across transports.** See
  `docs/ecu-firmware-analysis.md` → "Mode 22 catalog corrections"; applied
  to `data/dids.yaml` on 2026-10-04 (0x1102 is `ECM_VEHICLE_SPEED_COARSE`;
  D03.36), and the catalog now refuses a DID backed only by K-line evidence.
- **IAT reads engine-bay air at idle, not heat soak.** It is
  stagnation-dominated: above ~8 g/s it pins to ambient, and the high readings
  occur only at idle (corr with rpm −0.80). "IAT rose through the drive" was
  idle fraction. A hot-versus-cool comparison needs two sessions, not early
  versus late in one.
- **`ENGINE_LOAD` reads ~20 % at idle and at light cruise.** It cannot separate
  parked from moving. Bin on **MAF + rpm**.
- **K and the MAF table move timing too.** The ECM's load axis is the base
  fuel schedule (BFS = MAF × K), and Fuel Targets, Timing Trim and Cam Phasing
  are all indexed on it. A K or MAF edit therefore shifts every operating point
  to a different cell of those tables. Only the product K × MAF matters. Compare
  logs across such an edit on g/s × rpm, never on BFS, and make one category of
  edit per flash.
- **Closed loop exists only where the Target AFR cell is exactly 14.7.** Any
  other value in a cell opens the loop there, so its trims freeze.
- **Near-WOT enrichment at mid rpm is a pedal switch.** At 1,600–3,600 rpm the
  ECM goes rich when broadcast pedal reaches ≥ ~227/255 (~89 %) and uses the
  Fuel Targets map's **last load column**. No load threshold and no timer are
  involved (20 errors in 27,415 rows; evidence on DID `0x1134` in
  `data/dids.yaml`). To change mid-rpm WOT fuelling, edit that column.
- **The factory A/F sensors floor at 10.0 AFR** in UpRev logs, so samples there
  are censored. They are good to roughly ±5 % at WOT. Don't chase a smaller WOT
  error with them.
- **A flash can change two things at once.** On 2026-08-03 the operator raised
  fuel compensation *and* the AFR target in one flash. An error-versus-target
  metric credited the target move to delivery. Get the edit list from the
  operator or the map diff; never infer a target change from binned data.

## Air, fuel and altitude numbers worth knowing

- WOT airflow at home plateaus near **145 g/s from ~5,000–5,850 rpm**. Against
  the measured 80 kPa that is 85–93 % VE, a healthy NA curve. Against an assumed
  101.3 kPa the same data computes to 67–73 % and looks like a restriction that
  isn't there. The sea-level equivalent (~184 g/s) is consistent with the
  rating.
- The Mode 22 MAF DID (`ECM_MAF_GPS`) agrees 1:1 with PID `0x10` (median ratio
  1.000 ± 0.007, 3–134 g/s) once gated to |dMAF/dt| ≤ 5 %/s. An apparent 5 %
  shortfall at high flow was acquisition skew.
- **Mode 01 does report the WOT target.** On a 5,762 rpm / 144 g/s pull, PID
  `0x44` read φ 1.195–1.242 while `0x24` measured within ~2 %. An earlier
  "unobtainable over Mode 01" conclusion came from never reaching real WOT.
- The ECM's final commanded φ is also in RAM (`mem:ECM_TARGET_EQUIV_RATIO`),
  which `lambda_delivery.yaml` prefers as the commanded source.
- After the build, boost and post-intercooler temperature come from the kit's
  Bosch TMAP read by the CANary-HAT, and fuel pressure from an AEM CAN gauge
  (`sensor:FUEL_PRESSURE`), not from the ECM (`supercharger-build.md` → P2).
  The fuel system is returnless, so injector ΔP = rail − (MAP − baro) falls
  under boost. That derived channel is why the two sensors matter together.

- **The FSM's reference values are sea-level, stock-calibration figures.** Its
  spec check is defined only at 98.3–104.3 kPa barometric pressure, so a healthy
  engine here reads low on airflow and base fuel schedule. Warm idle is
  625 ± 50 rpm at 15 ± 5° BTDC. The VIAS power valve is closed from 2,200 to
  3,300 rpm, so expect VE and torque steps near those speeds; they are not a
  sensor fault.
- **Bank 1 is cylinders 1-3-5, on the right (passenger) side.** Firing order is
  1-2-3-4-5-6.

Engine specifications, factory reference values, the Nissan fuel equation as
this ECM implements it (base fuel schedule, K, which tables K moves), spark and
knock behaviour, the relearn procedures, and known failure modes are in
`references/engine-and-ecu.md`.

## Physical and environmental facts

- **A/C cannot run with the blower off**, by design. The reachable states are
  both-off, blower-only, and blower+A/C, which is still enough to separate
  blower bits from A/C bits by nesting.
- **Manual transmission.** Clutch and gear are driver actions, not ECU states,
  and no transmission module requests a torque reduction at a shift.

## Reference files

- `references/diagnostic-access.md`: Mode 01, Mode 22 and the session
  (exclusivity, exits, keep-alive, rates), RAM reads and the calibration id,
  the broadcast bus, K-line (addresses, wake-up, modules), and running beside
  the UpRev logger. Read it before sending anything to this ECM or planning a
  capture's request mix.
- `references/engine-and-ecu.md`: VQ40DE specifications and FSM reference
  values, the fuel equation (Tp/base fuel schedule, K, α) and ROM tables,
  cam timing and variable intake, relearn procedures, and common VQ40DE faults.
  Read it for "is this reading normal?", before advising on a table edit, and
  after any flash or throttle-body change.
