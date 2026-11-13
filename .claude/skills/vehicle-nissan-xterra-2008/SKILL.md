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
  `engine-management` (generic engine behavior). Those two stay
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
   learned fuel trim (below) and all trouble-code state, exactly as a battery
   disconnect does: the ECM's download entry wipes its battery-backed RAM.
   The throttle closed positions, the pedal released positions and the
   idle-air learning are restored from the ECM's serial EEPROM, as saved at
   the last key-off. A flash therefore needs none of the FSM relearns (firmware,
   2026-11-12, F01.11; a read across a flash is owed). New throttle hardware
   still does, because the old values are restored
   (`references/engine-and-ecu.md`).
2. **Hardware.** The build arrives in parts. **Fitted:** the Aeromotive 340
   pump assembly, between 2026-10-10 and 2026-10-26 (a window; nothing records
   hardware): warm-idle rail pressure 50.79 → 53.78 psi on the same instrument,
   about +2.5–3 % injector flow. **Still to come:** Z1 Level 2 (Harrop
   HTV1900), Bosch 700 cc injectors, 75 mm throttle body, cone intake. Those
   invalidate every NA-learned quantity: the MAF transfer function, K and dead
   time, idle and throttle learning, behavior surfaces, the trained detectors.
   Place a session against the pump as well as the flash. `labelled_events.yaml`
   has no entry for the pump, so nothing stops a fuel comparison from pooling
   across it (whether it is a `corpus_boundary` is the operator's call); record
   the rest of the build as an `intervention` with `corpus_boundary: true`.
   What the build will look like in this truck's data (blower pressure ratio,
   the ~10 % boosted fuel deficit, IAT, 91 octane against Z1's 93-octane
   figures) and the fuel-pressure channel: `references/engine-and-ecu.md`. Plan
   and open questions: `docs/roadmap/supercharger-build.md`.
3. **Altitude.** Home is **79–82 kPa** (Colorado Springs, ~2070 m), but drives
   reach **63–73 kPa** (2,900–3,900 m). Read `BAROMETRIC_PRESSURE` (PID `0x33`)
   per session. Never assume sea level, and never assume 80 kPa either. PID
   `0x33` tracks altitude continuously within its 1 kPa steps. It reads the
   EVAP pressure sensor and holds only during EVAP leak tests (firmware and
   GNSS check, F01.12). After a flash it reads 103.6 kPa for about 2 s. The
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
| Speed channels' ratio to GPS, tire circumference | `dyno.yaml`; `profiles.yaml` (repo root) |
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
- **UpRev's AFR is not an independent instrument, and neither are three
  channels from one sensor.** PID `0x24`, the voltage DIDs `0x1225`/`0x1226` and
  UpRev's AFR are one sensor per bank. UpRev's skips the ECM's barometric gain
  (1.5–4.5 % lean at altitude); CANary's route applies it since `e023c740`
  (2026-10-07). Short-term trim controls on that word, so closed-loop agreement
  with target proves nothing about the sensor, and an independent λ needs an
  external wideband (none on this truck). Detail: `references/engine-and-ecu.md`.
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
- **Long-term fuel trim resets on every flash and every battery disconnect,
  and relearns only when a purge gate opens.** Exactly 0.00 % on both banks is
  the neutral value (raw `0x80` on the PID `0x07` scale), not a decode bug or a
  fault: the gate has not opened since the last reset, which is a driving
  pattern. Total trim **adds**, `1 + STFT + LTFT` (`derived:TOTAL_TRIM_B1/B2` is
  the sum, not the product); split trim by purge (DID `0x1110`). Over Mode 22 on
  CAN, learned trim is DID `0x1125` (B1) and `0x1126` (B2), short-term trim
  `0x1123`/`0x1124`, the same RAM words as PIDs `0x06`–`0x09`. The gate, its
  timing and the learned cells: `references/engine-and-ecu.md` → "Learned fuel
  trim".
- **DID `0x1102` is vehicle speed, not learned trim.** The catalog named it
  `ECM_LONG_FUEL_TRIM_B1` from UpRev's *K-line* stream labels. It is the speed
  byte (one byte, km/h = raw × 2) on both transports: the firmware's K-line
  and CAN DID tables point at the same variables except 0x1247, 0x1248 and
  0x1315 (audit 2026-10-05). It tracks wheel speed with correlation 0.9997.
  The catalog's `0x1125` was bank-swapped the same way. **Never take a DID's
  meaning from a third-party tool's label.** See
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
- **Rich Target AFR cells open the loop and reset STFT to 0 %; lean cells do
  not open it** (firmware, F01.2, 2026-11-12). The loop is held open whenever
  the enrichment multiplier is off 1.0, which a cell does at lookup ≥ 130
  (AFR ≤ ~14.5). STFT is then forced to exactly 1.0, not frozen at its last
  value. A lean cell, or 128–129, is read as 14.7 and stays closed loop, so
  UpRev's "anything but 14.7 is open loop" is wrong on the lean side. Mode 01
  `FUEL_STATUS` is the firmware's own report of every gate (4 = open by
  condition). Details: `docs/ecu-firmware-analysis.md`, "Closed-loop enable and
  trim authority".
- **Full-throttle enrichment is a switch on target throttle (≥ 78 % on PID
  `0x11`) into the Fuel Targets map's last load column, and on some revisions
  first gear never trips it.** Resolve the session's revision first. Through
  `ZS44A-20261111` a torque cap holds first-gear full pedal to ~40 % throttle
  (load-indexed cells); from `ZS44A-20261111-2` (flashed 2026-11-12T01:02Z, cal
  `0x9308` 3,584 → 32,767) first-gear full pedal is full throttle and reads the
  last column. Keep sorting full-pedal data by gear. Gear bands, the every-gear
  trigger (`0xFFFF4856` bit 3) and the `rom_map.yaml` entries:
  `references/engine-and-ecu.md`.
- **Several broadcast bytes are not what their binding names say** (firmware,
  2026-11-12). The names stay for compatibility, so read the notes in
  `data/bindings.yaml`:
  - `ECM_THROTTLE_SENSOR_V` (`0x23D` b2) is the ECM's *target* opening.
  - `ECM_ENGINE_STATE_A`/`_B` (`0x231` b0/b3) are signed torques,
    (byte + 50) × 3 N·m.
  - `0x233` b1–2 are commanded fuel flow (`ECM_FUEL_FLOW_233`), not airflow.
  - `0x551` b1 is the ECM's own fuel-used counter.

  The byte maps are in `docs/ecu-firmware-analysis.md`, "The CAN frames the
  ECM builds and reads".
- **The factory A/F sensors floor at 10.0 AFR** in UpRev logs, so samples there
  are censored. They are good to roughly ±5 % at WOT. Don't chase a smaller WOT
  error with them.
- **Warm-idle lean follows heat, so a lean idle trim is not yet a leak.** With
  purge off, total trim at idle was 0.0 / −1.6 % on a cool morning (IAT
  19–28 °C) and +7–10 / +4–8 % on a hot afternoon (IAT 41–65 °C); a fixed idle
  correction sized from the hot drive was withdrawn
  (`data/analyses/2026-11-08-tuning-proposals.md`, "Corrected 2026-11-10").
  Compare idle trims at matched IAT and rail pressure first.
- **Polled rows in a `tuning` capture carry the round's scheduled start as
  `timestamp`**; the wire time is `metadata_json.acquired_at` (RAM reads sit a
  median 164 ms after their stamp). Join polled rows to broadcast frames on it
  (`read_channels(…, acquired_time=True)`; `compare_channels` does). A hand join
  produced a withdrawn "stray read" on 2026-11-12
  (`data/analyses/2026-11-11-first-gear-cap-off-prediction.md`, "Clock").
- **A flash can change two things at once.** On 2026-08-03 the operator raised
  fuel compensation *and* the AFR target in one flash. An error-versus-target
  metric credited the target move to delivery. Get the edit list from the
  operator or the map diff; never infer a target change from binned data.

## Air, fuel and altitude numbers worth knowing

- WOT airflow at home peaks near **145–147 g/s at 5,500–5,750 rpm**, still
  rising from ~140 g/s at 5,000, so VE falls as 1/rpm. Against the measured
  baro (81–82 kPa), four stored pulls (2026-11-10..12, throttle ≥ 80 %) read VE
  ≈ 0.90 at 4,000–4,500 rpm, 0.86–0.87 at 5,000 and 0.80–0.83 at 5,500–5,750.
  **Approximate:** manifold pressure is taken as baro at WOT and ρ comes from the
  session's median baro and IAT. That is plausible for an NA engine here, and
  nothing in these data says it is healthy beyond that. Against an assumed
  101.3 kPa the same data computes to 65–73 % and looks like a restriction that
  isn't there. The sea-level equivalent of the peak is ~180 g/s. The pulls stop
  at ~5,750 rpm.
- **Fuel is standard pump gasoline, usually E10** (operator, 2026-11-12), and
  the blend varies. Fuel-mass arithmetic uses a stoich of about 14.1, with 14.7
  (E0) as the bound; the ECM's own AFR numbers stay λ × 14.7 whatever is in the
  tank. With the supercharger the truck moves to 91 octane.
- The Mode 22 MAF DID (`ECM_MAF_GPS`) agrees 1:1 with PID `0x10` (median ratio
  1.000 ± 0.007, 3–134 g/s) once gated to |dMAF/dt| ≤ 5 %/s. An apparent 5 %
  shortfall at high flow was acquisition skew.
- **Mode 01 does report the WOT target.** On a 5,762 rpm / 144 g/s pull, PID
  `0x44` read φ 1.195–1.242 while `0x24` measured within ~2 %. An earlier
  "unobtainable over Mode 01" conclusion came from never reaching real WOT.
- The ECM's final commanded φ is also in RAM (`mem:ECM_TARGET_EQUIV_RATIO`),
  which `lambda_delivery.yaml` prefers as the commanded source.
- Fuel pressure is `sensor:FUEL_PRESSURE`, the AEM 30-0301's *analog* output
  through a USB ADC (live since 2026-10-04), not the ECM; how to read it is in
  `references/engine-and-ecu.md` → "Fuel pressure". Boost and post-intercooler
  temperature will come from the kit's Bosch TMAP (`supercharger-build.md` →
  P2). The fuel system is returnless, so injector ΔP = rail − (MAP − baro)
  falls under boost; that derived channel is why the two sensors matter
  together.

- **The FSM's reference values are sea-level, stock-calibration figures.** Its
  spec check is defined only at 98.3–104.3 kPa barometric pressure, so a healthy
  engine here reads low on airflow and base fuel schedule. Warm idle is
  625 ± 50 rpm at 15 ± 5° BTDC. The VIAS power valve is closed from 2,200 to
  3,300 rpm. The firmware adds hysteresis: it opens near 3,550 rpm going up
  and closes near 3,350 rpm coming down. Expect VE and torque steps near those
  speeds; they are not a sensor fault.
- **Bank 1 is cylinders 1-3-5, on the right (passenger) side.** Firing order is
  1-2-3-4-5-6.

Engine specifications, factory reference values, the Nissan fuel equation as
this ECM implements it (base fuel schedule, K, which tables K moves), spark and
knock behavior, the relearn procedures, and known failure modes are in
`references/engine-and-ecu.md`.

## Physical and environmental facts

- **Driveline and speed: the truck is on 4.08 axles.** ECM vehicle speed (PID
  `0x0D`, DID `0x1102`) reads about **0.92 × GPS**, so
  distance, economy and acceleration from OBD speed are ~8 % low unless
  corrected. `dyno.yaml` holds the ratio per speed channel: 0.9203 for
  `obd:SPEED` (PID `0x0D`) and `WHEEL_SPEED_6F7_A` (the committee relation), and
  0.932 for `VEHICLE_SPEED_2D1` (`0x2D1`), which was measured separately (H07.3,
  0.9314–0.9348) and does **not** share 0.9203. Rolling circumference is **2.559 m
  since 2026-10-03**, 2.609 m before (H07.11): a boundary for anything derived
  from wheel speed. The ECM's gear-ratio estimate (`0xFFFF27D2`) reads 10–12 %
  high in every gear because cal `0x91F8` still holds the stock axle; leave
  that word alone. Sources: `docs/ecu-firmware-analysis.md` ("The 4.08 regear
  makes every gear read about 10–12 % short"), `dyno.yaml`, a comment in
  `src/preprocessing/segmenter.py`.
- **A/C cannot run with the blower off**, by design. The reachable states are
  both-off, blower-only, and blower+A/C, which is still enough to separate
  blower bits from A/C bits by nesting.
- **Manual transmission.** Clutch and gear are driver actions, not ECU states,
  and no transmission module requests a torque reduction at a shift. The
  firmware confirms it. The transmission torque requests and the CAN-fault
  limp cap do not run on this calibration. The only outside torque request is
  VDC's, on frame `0x29E`, and stored drives never show it active. Altitude and
  intake temperature don't derate torque either (`docs/ecu-firmware-analysis.md`,
  "Torque limits and who asks for them").

## Reference files

- `references/diagnostic-access.md`: Mode 01, Mode 22 and the session
  (exclusivity, exits, keep-alive, rates), RAM reads and the calibration id,
  the broadcast bus, K-line (addresses, wake-up, modules), and running beside
  the UpRev logger. Read it before sending anything to this ECM or planning a
  capture's request mix.
- `references/engine-and-ecu.md`: VQ40DE specifications and FSM reference
  values, the fuel equation (Tp/base fuel schedule, K, α) and ROM tables,
  cam timing and variable intake, learned fuel trim (when it learns, why it
  reads 0.00), fuel pressure (the pump and the AEM channel), the supercharger
  build as it will show in the data, relearn procedures, and common VQ40DE
  faults.
  Read it for "is this reading normal?", before advising on a table edit, and
  after any flash or throttle-body change.
