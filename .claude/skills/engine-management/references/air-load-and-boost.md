# Airflow, load, altitude, and boost

Air is measured; fuel is derived from it. That direction of causality is why an airflow
error always presents as a *fueling* symptom, and why the first question about any mixture
problem is "is the air measurement right?"

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

Air density scales as P/T. At **80 kPa ambient (~1950–2100 m)** the density ratio versus
sea level is **0.790**:

| Quantity | Sea level | 80 kPa ambient |
|---|---|---|
| WOT MAP (NA) | ~95–100 kPa | **~75–79 kPa** |
| Peak airflow / torque / power (NA) | 1.00 | **~0.79** (−21%) |
| **VE (manifold-referenced)** | 0.95 | **~0.95 — essentially unchanged** |
| `LOAD_ABS` at WOT | ~0.93 | **~0.73** |
| `LOAD_PCT` at WOT | ~100% | **~100%** |
| Idle manifold *gauge* vacuum | spec value | reads ~20% lower |

**The apparent-restriction failure mode:** comparing this engine's WOT `LOAD_ABS` (0.73) or
WOT MAP (78 kPa) against a sea-level reference (0.93, 98 kPa) manufactures a phantom ~20%
intake restriction — phantom cam wear, phantom throttle-body problem, weeks of chasing
nothing. Vacuum-gauge diagnostic specs are sea-level specs too.

**How to normalize, in order of preference:**

1. **Compute VE.** It is intrinsically density-normalized and is the correct invariant to
   compare across days, altitudes, and vehicles.
2. **Compare `MAP ÷ measured baro`.** At WOT a healthy NA engine reaches roughly 0.93–0.99
   of baro near the torque peak. *That ratio*, not absolute MAP, is the restriction metric.
   PID 0x33 gives baro at 1 kPa/bit; if the ECU doesn't broadcast it, key-on/engine-off MAP
   is baro.
3. **Correct power to standard conditions** with SAE J1349:
   `cf = 1.18 × (990 / p_dry[mbar]) × √((T[°C]+273)/298) − 0.18`, reference 990 mbar /
   25 °C, `p_dry` being barometric minus vapour pressure. Note J1349 is defined for
   naturally aspirated engines — applying it to a boosted engine already holding an
   absolute MAP target double-counts, because the turbo has restored the density.
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

## Boost control

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

Note also that a boost sensor placed **after the intercooler and before the throttle**
reads pre-throttle pressure, which is not manifold pressure. A large pre/post-throttle
delta at high load is a throttle restriction or a torque-structure decision, not a boost
fault.

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
| Pressure ratio | the quantity the compressor actually responds to |
| Intercooler efficiency | separates heat soak from a boost fault |
| Fuel-flow-derived AFR (injector flow × duty vs airflow) | a fueling estimate independent of the O2 sensor |
