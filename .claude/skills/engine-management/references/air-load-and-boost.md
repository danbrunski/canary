# Airflow, load, altitude, and boost

Air is measured; fuel is derived from it. That direction of causality is why an airflow
error always presents as a *fueling* symptom, and why the first question about any mixture
problem is "is the air measurement right?"

Contents: VE and speed-density · MAF versus speed-density (and MAF housings) · the four
meanings of "load" · barometric pressure and altitude (J1349) · **boost**: name the
compressor first (comparison table) · turbocharger boost control · pressure ratio ·
positive-displacement superchargers (roots, twin-screw) · centrifugal superchargers ·
anything boosted (load clamps, MAF placement, returnless fuel, sensor placement) · charge
temperature and intercoolers · derived channels to compute.

## Volumetric efficiency and speed-density

**VE** = actual charge mass inducted ÷ the mass that would fill the displaced volume at
intake-manifold conditions. It is the engine's breathing curve: it peaks where the intake
and exhaust are tuned to, falls off on either side, and steps or kinks where a runner flap
or cam phaser switches. A modern naturally-aspirated engine peaks in the region of 85–100%
(higher with well-tuned intake resonance); a boosted engine referenced to *manifold*
conditions is in the same range, because the boost is already in the density term.

Four-stroke, SI units:

```
ρ_intake [kg/m³] = MAP[Pa] / (287 · T_intake[K])          (R_air = 287 J/(kg·K))
ṁ_air    [kg/s]  = ρ_intake · VE · V_disp[m³] · rpm / 120
air per intake event [kg] = ṁ_air / (rpm · n_cyl / 120)
```

In the units a log actually gives you:

```
ṁ_air [g/s] = 0.029031 · VE · V_disp[L] · rpm · MAP[kPa] / IAT[K]
```

Rearranged to *measure* VE from a log — this is the useful direction:

```
VE = ṁ_air_measured[g/s] · IAT[K] / (0.029031 · V_disp[L] · rpm · MAP[kPa])
```

Sanity: a VE curve that exceeds ~1.1 on a naturally-aspirated engine, or that is wildly
lumpy, is usually telling you the *airflow* or *temperature* channel is wrong, not that the
engine breathes strangely.

**VE is only defined at a given cam phase.** On a variable-cam engine, intake phasing
changes both the trapped charge and the internal residual (overlap back-flows exhaust into
the intake). More residual means less fresh charge and a slower burn that wants more
advance. A VE or trim discontinuity confined to certain rpm bands can be a phaser that
isn't reaching its command. Log commanded vs measured cam before blaming the air model.

**The temperature you use matters.** In-manifold air temperature lags badly during
transients and heat-soaks at idle, so a VE computed with a manifold sensor during a
transient carries that error. For steady-state work it is fine; for transient work, model
charge temperature or restrict the analysis to steady points.

## MAF versus speed-density

| | MAF | Speed-density (MAP + IAT + VE) |
|---|---|---|
| Measures | air **mass** directly | infers mass from pressure, temperature, and a VE model |
| Density correction | inherent — altitude and temperature are already in it | must be applied explicitly |
| Main error mode | element contamination (under-reads, **no code**), reverse-flow double counting at pulsation | VE model wrong (cam phase, wear, modification, temperature error) |
| Affected by intake mods | yes — tube diameter and flow profile change calibration | no, but VE changes |

Most modern ECUs run both and use each to rationality-check the other. So should you:
**compute a speed-density estimate and compare it to the MAF at the same instants.** Two
independent estimates disagreeing is evidence; either trace alone always looks plausible.

**A MAF is calibrated to its housing, not just its element.** The element sees only a
fraction of the flow, and the conversion to total mass flow is a calibration of that
element *in that duct*. The same element can read 4.94 V in a 50 mm venturi and 3.29 V in
an 82 mm one at the same 370 kg/h. Changing the tube diameter, the filter, or the upstream
flow profile (a cone intake, a new housing, a relocated sensor) invalidates the MAF curve,
and it must be rescaled. The curve also has a ceiling. Past the top of the element's range
the output plateaus while airflow keeps rising, so the ECU under-reads air and the engine
runs lean exactly at peak power. A MAF voltage or frequency pinned at its maximum at WOT is
that ceiling, not the engine's airflow.

Signatures worth memorizing:

- **Contaminated MAF** — under-reads, so the ECU under-fuels, so trims go positive; the
  bias grows with airflow and is worst where closed-loop authority is least.
- **Pulsation / reverse flow** — a direction-blind element counts reversed air twice and
  over-reads, giving **negative** trims confined to specific rpm/load cells (worst at low
  rpm and wide throttle on large-displacement, few-cylinder engines).
- **MAF replaced by a substitute** (throttle-based) after a fault — the load channel becomes
  a smooth function of throttle and rpm with no airflow texture at all.

## "Load" is several different quantities

Mixing these silently is a common source of nonsense. Establish which one a channel is
before comparing anything.

**Calculated load — PID 0x04 (`LOAD_PCT`).** SAE J1979 defines it as current airflow
divided by peak airflow at WOT at standard conditions, corrected by `(BARO/29.92)` and
`√(298/(ambient°C+273))`. Because it divides altitude and temperature out, the standard
states it "reaches 1.0 at WOT **at any altitude, temperature or rpm**." It means *fraction
of the torque available today*, correlates with vacuum, and is often what schedules power
enrichment. Transport scaling `A × 100/255`, 0–100%.

**Absolute load — PID 0x43 (`LOAD_ABS`).** Air mass per intake stroke ÷
`1.184 g/L × cylinder displacement` — where 1.184 g/L is simply air density at STP.
**Not baro-corrected**, which gives the useful identity:

```
LOAD_ABS ≈ VE · (MAP[kPa] / 101.3) · (298 / IAT[K])
```

i.e. "VE × density ratio versus STP". Runs 0 to ~0.95 naturally aspirated, 0 to ~4
boosted; correlates with indicated and brake torque; often what schedules spark and EGR.
It is a **two-byte** PID (`(A·256+B) × 100/255`) — decoding it like the one-byte 0x04 is a
frequent bug.

**mg/stroke or relative charge** — cylinder air mass per event, the internal currency of
most OEM ECUs. The honest quantity: prefer it when available.

**MAP** — a pressure, not a mass, until corrected for temperature. Most aftermarket
standalones index their tables directly on MAP; most OEM ECUs do not.

**Throttle position** — a plate angle, and on a torque-structured ECU an *output*, not
driver demand.

| Definition | Baro-corrected | NA at WOT | Boosted at WOT | Reads |
|---|---|---|---|---|
| `LOAD_PCT` (0x04) | **yes** | ~100% at any altitude | ~100% | fraction of *available* torque |
| `LOAD_ABS` (0x43) | **no** | ~0.75–0.95, altitude-dependent | up to ~4.0 | air mass/stroke vs STP |
| mg/stroke | n/a (absolute) | — | 2–4× | absolute air mass |
| MAP | no | ≈ baro − intake loss | boost + baro | manifold pressure |

Practical rules:

1. Never plot `LOAD_PCT` and `LOAD_ABS` on one axis and call it "load."
2. A load figure quoted in a tune, table, or forum post is meaningless until you know
   *which* of these it is **and** the altitude it was taken at.
3. `LOAD_PCT` pinned at 100% is not evidence of full airflow — it saturates by design. Use
   `LOAD_ABS` or mg/stroke to see actual air.
4. For any airflow question, convert everything to **g/s or mg/stroke**.
5. **Never bin on calculated load when the thing you changed affects fueling or airflow** —
   it is derived from that math, so you would be conditioning on the thing that moved. Bin
   on measured airflow and rpm, or on the actual table axis.

## Barometric pressure and altitude

Air density scales as P/T. **80 kPa ambient** is ~1,950 m in the standard atmosphere and
**63 kPa** is ~3,830 m. The pressure ratios to sea level are 0.790 and 0.622. At equal
intake temperature those are also the density ratios; real air up high is usually cooler,
which claws a little back (standard-atmosphere density ratios 0.83 and 0.68). Use the
*measured* baro and intake temperature, never an altitude lookup.

| Quantity (NA, same intake temp) | Sea level | 80 kPa ambient | 63 kPa ambient |
|---|---|---|---|
| WOT MAP | ~95–100 kPa | **~75–79 kPa** | **~59–62 kPa** |
| Peak airflow / torque / power | 1.00 | **~0.79** (−21%) | **~0.62** (−38%) |
| **VE (manifold-referenced)** | 0.95 | **~0.95 — essentially unchanged** | ~0.95 |
| `LOAD_ABS` at WOT | ~0.93 | **~0.73** | ~0.58 |
| `LOAD_PCT` at WOT | ~100% | **~100%** | ~100% |
| Idle manifold *gauge* vacuum | spec value (e.g. 20 inHg / 68 kPa) | **falls by ~21 kPa (6.3 inHg)**: ~14 inHg / 46 kPa | **falls by ~38 kPa (11.3 inHg)**: ~9 inHg / 29 kPa |

**The apparent-restriction failure mode:** comparing this engine's WOT `LOAD_ABS` (0.73) or
WOT MAP (78 kPa) against a sea-level reference (0.93, 98 kPa) manufactures a phantom ~20%
intake restriction — phantom cam wear, phantom throttle-body problem, weeks of chasing
nothing. Vacuum-gauge diagnostic specs are sea-level specs too, and they do *not* scale by
the pressure ratio. Idle needs a roughly fixed air mass, so idle **absolute** MAP stays
about where it was (~34 kPa for a 20 inHg sea-level idle) and gauge vacuum, which is
ambient minus MAP, falls by about the barometric difference. From 20 inHg that is ~30 %
lower at 80 kPa and ~55 % lower at 63 kPa (31 % and 57 % at exactly constant MAP; a little
less in practice, since pumping work falls too). The service rule of about 1 inHg per
1,000 ft says the same (it slightly overstates above ~10,000 ft). Quote idle vacuum in
inHg or kPa below ambient, never as a percent of the sea-level spec.

**How to normalize, in order of preference:**

1. **Compute VE.** It is intrinsically density-normalized and is the correct invariant to
   compare across days, altitudes, and vehicles.
2. **Compare `MAP ÷ measured baro`.** At WOT a healthy NA engine reaches roughly 0.93–0.99
   of baro near the torque peak. *That ratio*, not absolute MAP, is the restriction metric.
   PID 0x33 gives baro at 1 kPa/bit; if the ECU doesn't broadcast it, key-on/engine-off MAP
   is baro.
3. **Correct power to standard conditions** with SAE J1349 (Aug 2004):
   `CF = 1.176 × (99 / p_dry[kPa]) × √((T[°C]+273)/298) − 0.176`, reference 99 kPa dry
   air at 25 °C, `p_dry` being barometric minus vapour pressure. The older 1.18 / 0.18
   form differs by < 0.1 %. J1349 covers NA *and* pressure-charged engines. Its rule is
   that **a parameter the engine already compensates for is not corrected**: a boosted
   engine that holds an *absolute* boost target is not corrected for baro (§5.5.2).
   Correcting it would double-count, because the controller already restored the density.
   A mechanically driven supercharger with no absolute pressure control is corrected
   like an NA engine, because its manifold pressure falls with ambient. ISO 1585:2020
   says the same: mechanical superchargers take the ordinary ambient correction, and only
   turbo systems that compensate for ambient get a separate one. The standard's
   test envelope is 90–105 kPa and 15–35 °C, so correcting from 80 kPa or below is an
   extrapolation; say so.
4. **Never** compare raw g/s, raw mg/stroke, or `LOAD_ABS` across altitudes without
   dividing by the density ratio.

Second-order effects at altitude, worth knowing: exhaust backpressure falls (slightly
better scavenging, VE can nudge *up* ~1%), the knock threshold pressure falls so **knock
margin actually improves**, and a turbo needs more wastegate opening for the same manifold
pressure.

Two more notes on the sensors themselves. Many modern ECUs carry no dedicated baro sensor
and infer it from MAP at key-on and at near-WOT moments — so a MAP bias becomes a baro bias
becomes an everything bias. And the free check: **engine off, MAP = baro**; on a boosted
engine the charge-air pressure sensor must agree too, and ECUs commonly set an
implausibility code on a disagreement of a couple hundred millibar.

## Boost: name the compressor before reading the trace

What makes the boost decides what "low boost" means, which channel is the control effort,
and how the engine behaves at altitude. The same boost trace means different things on a
turbo and on a belt-driven blower, so identify the type first.

| | Turbo | Roots / twin-screw (positive displacement) | Centrifugal |
|---|---|---|---|
| Driven by | exhaust energy | belt, fixed ratio to crank | belt or gear, fixed ratio, stepped up to 50k–200k rpm |
| Boost vs rpm | spools, then held flat by the controller | near-flat from ~2,000 rpm, near-instant | rises about as **rpm²**, peaks at redline, little below ~2,000 |
| What sets boost | wastegate controller | pulley ratio | pulley ratio (and inlet temperature) |
| Control-effort signal | **wastegate duty** vs target | none. Watch airflow per rev, bypass state, PR vs rpm | none. Watch the rpm² constant (below), blow-off valve state |
| Altitude holds | gauge boost (spring-only gate) or absolute MAP (ECU control) at higher shaft speed | **pressure ratio**: gauge and absolute boost fall with baro | pressure ratio at a given rpm and inlet temperature |
| Compressor map applies | yes | no (volume pump) | yes: surge and choke like a turbo |

Worked case: a setup making 7 psi at sea level (149.6 kPa absolute, PR 1.48), driven at
80 kPa ambient. Roots, screw or centrifugal: ~118 kPa absolute, **5.5 psi**. Turbo with a
spring-only wastegate: ~128 kPa, 7 psi, PR 1.60. Turbo with ECU control on absolute MAP:
~150 kPa, 10.1 psi, **PR 1.87**, with more shaft speed and discharge heat.

Two things apply to every belt-driven blower. **There is no wastegate duty to read:** below
WOT the throttle is the only boost actuator. And **blower speed is a calculation:** blower
rpm = engine rpm × (crank pulley ⌀ ÷ blower pulley ⌀) × internal step-up (1 for positive
displacement) × (1 − slip). Check it against the unit's rated maximum at the rev limit.
The blower costs drive power, `P = ṁ · c_p · T₁ · (PR^0.286 − 1) / η`: roughly 5–6 % of
crank output at PR 1.5 and 9–13 % at PR 2.0. That power is part of why boosted torque
never scales 1:1 with manifold pressure.

## Turbocharger boost control

Wastegate position is metered by a **PWM duty cycle**, so the duty channel is the control
*effort* and the boost trace is the controlled variable. Read them together:

| Boost vs target | Duty | Diagnosis |
|---|---|---|
| under target | **saturated (near max)** | out of authority — boost leak, gate not sealing or spring wrong, cracked pipe, worn actuator, or the compressor is simply past its efficient flow |
| under target | **low/mid** | the ECU is **deliberately limiting** — altitude, charge temperature, knock, component protection, a torque limiter |
| over target then corrected | swinging | control tuning or a transient overshoot function |

Failure directions are deliberately safe: venting the actuator line typically *closes* the
gate, and every degraded pressure input (boost sensor, baro) leads to **reduced boost and
reduced power** rather than continuing at target. A boosted engine quietly down on power
with a pressure-sensor implausibility stored is the most common presentation of this whole
class.

## Pressure ratio is the quantity that governs the turbo

```
PR = manifold (or compressor outlet) absolute pressure ÷ ambient absolute pressure
```

**Compressor shaft speed follows pressure ratio, not gauge boost.** At altitude, holding a
constant gauge target requires a higher PR and overspeeds the compressor — which is exactly
why ECUs reduce boost with altitude. Any cross-elevation or cross-season comparison of boost
must be done in absolute pressure or PR.

A compressor map at a glance: PR on the vertical axis, corrected mass flow on the
horizontal, with a surge line on the left, a choke line on the right, and efficiency
islands between. Two practical readings — near surge (low flow, high PR) the compressor is
unstable; past choke (high flow) it stops making pressure and just makes heat, which is what
a high-rpm boost taper with saturated duty usually is.

## Positive-displacement superchargers (roots and twin-screw)

A belt-driven positive-displacement blower moves a nearly fixed volume per revolution, and
its speed is a fixed multiple of engine speed. **Boost is the engine resisting that flow**:
the blower delivers volume, and manifold pressure is whatever it takes to push that volume
through the engine. An engine-side change that improves cylinder filling (head, cam,
exhaust) therefore *lowers* boost for about the same airflow. Boost pressure alone is a poor
measure of how much air the engine got.

**Shape and response.** Leakage past the rotors is a large share of flow only at low rotor
speed, so boost comes in near idle and is roughly flat from about 2,000 engine rpm. Response
is near-instant: Eaton measured 90 % of boost in 0.5 s against about 5 s for a comparable
turbo. There is no spool, so a lean or rich tip-in on a PD engine is transient fuelling, not
lag.

**There is no control effort to read.** The pulley ratio *is* the boost setting. No
wastegate duty exists, so low boost is a hardware or ambient fact, not a controller running
out of authority. The diagnostic channel instead is **airflow per engine revolution** (g/rev
or mg/stroke, from the MAF). On a PD engine that is set by pulley ratio × blower volumetric
efficiency × inlet density:

| Airflow per rev (vs a baseline at the same baro) | MAP | WOT λ vs target | Where the fault is |
|---|---|---|---|
| low | low | on target | **blower side**: belt slip, bypass valve open or leaking, inlet restriction ahead of the blower, lower baro |
| normal | low | rich | **leak after the MAF**: metered air lost, so the fuel calculated for it arrives with less air |
| normal | high | — | **engine side**: exhaust or head restriction; the engine is swallowing less of what the blower delivers |

**The bypass valve.** It is a butterfly across the blower, held shut by a spring and opened by
manifold vacuum. At idle and cruise it is open, the rotors just recirculate air, and the
blower absorbs very little power (Eaton: under 0.5 kW at cruise). As vacuum falls it closes
and boost builds. Stuck open: airflow per rev at WOT is close to the NA baseline and boost is
low or absent. Stuck shut: hotter intake air and worse economy off-boost, and less throttle
for the same airflow at cruise. Leaking: between the two, with hotter discharge air because
hot air returns to the inlet.

**What limits it at high rpm:**

- **Blower speed.** Blower rpm = engine rpm × crank pulley diameter ÷ blower pulley diameter.
  Check it against the rotor pack's rating at redline; the Eaton TVS R-series is rated about
  18,000–24,000 rpm, depending on size.
- **Belt slip.** Smaller blower pulleys wrap less belt and slip more. The signature is boost
  that holds and then sags at high rpm, and **varies from pull to pull**, often worse wet or
  hot. In one test a better belt on the same pulley raised peak boost from 14.6 to 22.2 psi.
- **Inlet restriction ahead of the blower.** Whatever pressure is lost before the blower is
  multiplied by the pressure ratio after it.
- **Discharge temperature.** Efficiency falls at high pressure ratio and high speed.

**Efficiency and heat.** Modern twisted-rotor roots units (Eaton TVS and derivatives) claim
about 70–75 % or better thermal efficiency. Older straight-lobe roots units manage about
50–58 % at 15 psi. The discharge temperature follows the formula under "Charge
temperature". Unlike a turbo's transient heat, a PD blower's heat is a **steady-state load**
whenever it is making boost, which is why PD kits are usually intercooled, commonly
liquid-to-air.

**Twin-screw differs from roots in one way that matters: internal compression.** A roots
blower only carries air, and compression happens when that air meets manifold pressure at
the outlet. A screw compresses the air between its rotors before discharge. Screw
efficiency therefore *peaks* near its built-in volume ratio, while roots efficiency falls
steadily as pressure ratio rises. A Mazda patent puts the crossover near PR 1.8, above
which roots back-flow rises and its efficiency drops. Vendor bench data give a screw
roughly half the roots' temperature rise per psi (≈ 9 vs 15–18 °F/psi, pre-TVS roots).
Sanity-check any vendor charge temperature against `T₁ · (PR^0.286 − 1)`: some published
screw figures imply close to 100 % efficiency, which is impossible. The flip side is
bypass losses. Unloaded, a screw's internal compression costs *more* pumping work than a
roots, so a bypass stuck shut shows as more part-throttle heat and fuel use on a screw.

**Altitude: a PD blower holds pressure *ratio*, not pressure.** To first order it multiplies
whatever ambient pressure it is given: `MAP_abs ≈ PR × baro`. A kit making 7 psi at sea
level (PR 1.48) makes **~5.5 psi gauge / 118 kPa absolute at 80 kPa ambient**, and ~4.4 psi /
93 kPa at 63 kPa. That last figure is below sea-level atmosphere. The boosted engine
therefore loses the **same fraction** of airflow as an NA engine at altitude, while a turbo
can hold an absolute target until it runs out of compressor map or heat margin. Gauge boost
that falls with altitude on a PD blower is not a belt or bypass fault. Put baro in every
boosted comparison, and correct power like an NA engine (J1349, above).

**Starting points for a PD calibration on pump fuel** (rules of thumb, not physics):

- **Ignition:** about 1° removed from NA WOT timing per psi of boost as a first cut. Tuner
  practice ranges from 0.5 to 2° per psi, and some remove nothing for the first 2 psi.
  Verify with knock monitoring on a dyno or with repeated pulls.
- **λ:** NA full load 0.80–0.90, moderate boost 0.80–0.85, high boost 0.75–0.80.

## Centrifugal superchargers

A centrifugal blower is a turbo compressor driven from the crank instead of the exhaust.
Everything about compressor maps, surge and choke above applies to it. What changes is
the drive: impeller speed is locked to engine speed, so **boost rises roughly as the square
of rpm** and peaks at redline. One measured kit made 2.5 / 4.5 / 7 / 11 psi at 3 / 4 / 5 /
6k rpm, which an rpm² fit matches within ~10 %. It feels like a "linear turbo" with no
lag and no low-end boost.

- **The health constant.** Because the pressure ratio follows impeller tip speed²,
  `k = (PR^0.286 − 1) · T_in / rpm²` should stay constant through a pull and between
  pulls. The fuller form is PR = [1 + η·σ·U²/(c_p·T₀₁)]^(γ/(γ−1)), where U is tip speed.
  k falling at high rpm while rpm climbs faster is **belt slip**. Boost flattening while
  discharge temperature jumps is the compressor reaching **choke**.
- **Inlet temperature is in the equation; inlet pressure is not.** At a given impeller
  speed a centrifugal holds pressure ratio, so gauge boost falls with altitude as a PD
  blower's does. Cold air raises the pressure ratio, so a centrifugal makes more boost on
  a cold day. Compare pulls on PR corrected for inlet temperature, not raw psi.
- **Blow-off / bypass valve.** The impeller is still spinning when the throttle closes, so
  without a relief path the compressor surges: boost flutters on lift-off. A failed or
  missing valve shows exactly that. Vendors require one above ~6 psi.
- **Belt slip** happens mainly on deceleration, and on acceleration if the belt is loose.
  Ribbed belts start slipping around 15 psi and above, which is where cog drives are
  used.

## Anything boosted: metering, fuel pressure, and where the sensor sits

- **Load clamps.** Many ECUs cap their load value (Nissan's "TP limit", MAF voltage or
  frequency ceilings, table axis ends). A calibration that isn't opened up for boost
  silently clips load, and fuel then stops rising with airflow. A flat-topped load
  channel during a pull is a clamp, not the engine's airflow.

- **MAF before the compressor (draw-through).** The MAF measures air before the blower or
  turbo. Any bypass, recirculation, or blow-off valve must return air *downstream of the
  MAF*, or metered air is counted and then lost. The engine then runs rich, typically as a
  stumble on lift-off. A MAF trace that turns into a "seismograph" at part throttle points
  at the bypass or recirculation path. An intercooler's volume also puts a lag between what
  the MAF reads and what reaches the cylinder, so transient fuelling needs its own
  compensation. A larger MAF housing buys range at the cost of low-flow resolution; aim to
  use about 80 % of the element's range at peak flow. **A MAF after the compressor
  (blow-through)** sits under pressure. It can leak, collect oil and water, and read
  rich when heat-soaked, so it belongs after the intercooler, and a vented valve there is
  fine only upstream of it.
- **Returnless fuel under boost.** The injector differential is
  `rail − (MAP − baro)`. With a 350 kPa rail and 7 psi (48 kPa) of boost, ΔP falls to
  about 302 kPa and flow to √(302/350) = 0.929, **a 7 % deficit** that needs about +8 % pulse
  width. WOT is open loop, so the trims never see it. On a PD blower the deficit also moves
  with baro, because the boost does (≈ 5.6 % at 80 kPa ambient for the same pulley). There
  are three fixes:
  1. **At source:** a boost-referenced (1:1) regulator on a return conversion holds ΔP
     constant, and the problem disappears.
  2. **In the ECU:** an explicit correction from a MAP input.
  3. **In the fuel tables:** the only option on a MAF-only ECU with no pressure input.

  Option 3 works better than it sounds, but it has limits worth knowing before
  relying on it:
  - **The physics is known, so measure ΔP rather than discover the correction.** Log rail
    pressure and manifold pressure with added sensors, and compute the expected flow ratio
    `√(ΔP / ΔP_ref)` per sample. The table only has to hold that number.
  - **A load-indexed table can hold most of it.** At a given rpm, the ECU's load (air per
    rev) pins manifold *absolute* pressure for a given charge temperature and VE. So a
    fuel table on rpm × load can absorb the deficit exactly at one baro and one charge
    temperature. A table on throttle angle can't: the throttle axis saturates at WOT and
    cannot tell boost levels apart.
  - **Baro and charge temperature are what's left over.** In one load cell, ΔP moves 1:1
    with baro: a table tuned at 80 kPa is ~2.5 % lean at 63 kPa. Hotter charge raises
    manifold pressure for the same air mass, so heat soak also leans the mixture (about
    2 % for +30 °C of charge temperature near 7 psi). Both errors point lean at exactly the moments knock
    margin is smallest. Measure the within-cell spread of ΔP across real drives. That
    spread is the error no table can remove.
  - **The table target stops meaning delivered λ.** Pre-compensating means commanding
    richer than wanted: a 7 % deficit and a λ 0.80 goal means commanding λ ≈ 0.75. Any
    commanded-versus-measured comparison then shows a boost-dependent "lean of command"
    that is the ΔP term by design, not a MAF or injector error. Put the predicted ΔP
    term into the residual, or refuse boosted samples that lack the pressure channels.
- **Sensor placement.** A boost sensor **after the intercooler and before the throttle**
  reads pre-throttle pressure, which is not manifold pressure. A large pre/post-throttle
  delta at high load is a throttle restriction or a torque-structure decision, not a boost
  fault. On a PD kit, find out whether the ECU's intake-air temperature still reads
  *ambient* air at the MAF or has been moved to post-intercooler charge air. They are
  different quantities, and the difference between them is the intercooler's work.
- **Speed-density above 1 bar** needs a MAP sensor whose range covers the boost (a 2.5- or
  3-bar sensor), and a VE model referenced to manifold conditions. Otherwise the load axis
  clips at atmospheric.

## Charge temperature

Compression heats the charge. Approximately:

```
T_out = T_in · (1 + (PR^0.286 − 1) / η_compressor)          (ideal-gas, γ = 1.4)
intercooler efficiency = (T_in − T_out) / (T_in − T_ambient)
```

Charge temperature is usually the **largest uncontrolled variable between two logs**, and it
moves the knock limit directly:

- A heat-soaked intercooler shows up as rising IAT and timing being pulled, while the **boost
  trace still looks perfectly healthy**. The boost channel alone will not find it.
- Air-to-air intercoolers lose effectiveness at idle and low road speed. Liquid-to-air
  units keep circulating and hold more heat, but soak across repeated pulls. An
  intercooler typically removes 35–40 °C and costs ~10 kPa of pressure drop.
- **Repeated-pull comparisons have a second confound: protection enrichment.** A
  catalyst-temperature model can command a richer mixture on a hot pull than on a cool
  one. OEM guidance warns this reads as a ~10 hp "difference" between parts, so log
  commanded λ on every pull.
- Liquid-to-air intercoolers soak on repeated pulls: the core absorbs heat faster than the
  heat exchanger rejects it, so the first pull is the strongest and the third or fourth is
  weaker. **Recovery time** between pulls is the useful metric. Log post-intercooler IAT,
  intercooler coolant temperature, ambient temperature, and the time between pulls. A
  post-intercooler IAT that ratchets up with every pull and never recovers at cruise is a
  failed pump or blocked circuit, not ordinary soak.
- Back-to-back pulls with different charge temperatures are not comparable, and neither are
  a spring log and a summer log.
- Boost targets should fall with rising intake temperature; that is a calibration doing its
  job, not a fault.

## What to compute rather than read

| Derived | Why |
|---|---|
| VE from MAP, IAT, rpm, measured airflow | the breathing curve; makes airflow errors visible |
| Speed-density airflow estimate vs MAF | two independent estimates; disagreement is the finding |
| Air mass per stroke (mg/stroke) | the honest load axis, free of ECU load conventions |
| Pressure ratio | the quantity the compressor actually responds to; on a PD blower, the quantity that stays constant across altitude |
| Airflow per engine revolution (g/rev) | on a belt-driven blower, the health signal that replaces wastegate duty |
| Injector ΔP = rail − (MAP − baro) | on a returnless system, the boosted fuel-flow deficit the trims never see |
| Intercooler efficiency | separates heat soak from a boost fault |
| Fuel-flow-derived AFR (injector flow × duty vs airflow) | a fueling estimate independent of the O2 sensor |
