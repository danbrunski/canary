# Fuel control, lambda, and trim diagnosis

## Lambda, AFR, phi — get the convention right first

- **λ = actual AFR ÷ stoichiometric AFR.** λ = 1 is stoichiometric whatever the fuel.
  λ < 1 rich, λ > 1 lean.
- **φ (phi, equivalence ratio) = 1/λ.** φ > 1 is rich. Telemetry routinely mixes these,
  and a channel labelled "AFR" may be λ, φ, or AFR — check that the idle value lands
  where it should before trusting a single number.

> **The naming trap.** SAE J1979 calls PID 0x44 "Commanded Equivalence Ratio" but instructs
> you to *multiply* stoichiometric AFR by it — so **SAE's "equivalence ratio" is λ, the
> reciprocal of the textbook φ.** The same applies to PIDs 0x24–0x2B and 0x34–0x3B.
> Implementations vary, and a single ECM has been observed reporting one of these as true λ
> and another as true φ **on the same vehicle**. Resolve it empirically rather than from the
> name: at steady closed-loop cruise the value must be ≈1.00, then force a rich WOT event —
> **λ goes down** (~0.85), **φ goes up** (~1.18). One pull settles it permanently.
>
> Also: on a vehicle with only a narrowband sensor, PID 0x44 reports **1.0 throughout closed
> loop by definition**, showing the real command only in open loop. A flat 1.000 there is
> correct behavior, not a stuck channel.
- Stoichiometric AFR **by mass** (not volume): gasoline ≈ 14.7:1, E10 ≈ 14.1:1,
  E85 ≈ 9.8:1, methanol ≈ 6.4:1. Because the *ratio* moves with fuel, **λ is the
  portable quantity** — quote λ when comparing across fuels or vehicles.
- Ethanol content also lowers energy density (E10 ≈ −3%), so fuel-trim and pulse-width
  baselines shift seasonally and regionally with blend. That is a real confounder in
  any long-horizon trim comparison.

### The bands that matter

| Band | λ | Note |
|---|---|---|
| Rich, usable | 0.85–0.95 | WOT power/charge-cooling territory |
| Best drivability | 0.9–1.1 | the engine runs fine anywhere in here |
| **Catalyst window** | **0.99–1.00** | ~10× tighter than the drivable range |
| Lean, usable | 1.05–1.30 | economy/lean-cruise territory |
| Lean misfire limit | > 1.30 | too much air to support consistent combustion |

**An engine can run perfectly smoothly and emit badly.** Never infer catalyst health
from how the engine feels or from λ sitting near 1 on average.

### The three optima, which do not coincide

| Objective | λ | AFR (E0) |
|---|---|---|
| Best torque | 0.85–0.90 | 12.5–13.2 |
| Best brake-specific fuel consumption | 1.05–1.10 | 15.4–16.2 |
| Three-way catalyst conversion | 1.000 ± ~0.01 (time-averaged) | ≈14.7 |
| NA WOT production calibration | 0.85–0.92 | 12.5–13.5 |
| Boosted WOT, aggressive | 0.75–0.82 | 11.0–12.0 |

Best-power λ is genuinely engine-specific — it moves with charge motion, compression ratio,
chamber design, and residual fraction — and **the torque-vs-λ curve is flat near its peak**
(typically well under 2% torque across λ 0.85–0.92). That flatness is why it is nearly
always right to run the lean edge of the best-power band and spend what's left on knock
margin, rather than chasing the exact peak.

**Catalyst-window caveat.** The ±1% window is a *time-averaged* constraint, not an
instantaneous one. Engines deliberately **dither** rich/lean to exercise the washcoat's
oxygen storage, with studied amplitudes of λ = 1 ± 0.02 to ± 0.08 at sub-Hz frequencies. A
lambda channel oscillating a few percent around 1.0 at well under 1 Hz is the control
system working correctly — not an AFR fault.

The three-way catalyst's two reactions want *opposite* oxygen environments — reduction
(NOx) wants low O2, oxidation (HC, CO) wants high O2. That is why closed-loop control
**dithers across λ = 1** rather than sitting on it, alternately charging and discharging
the washcoat's oxygen storage. A perfectly flat λ = 1 would starve one reaction. It also
means a healthy narrowband upstream sensor **must** be seen switching; flatness is the
fault.

## Mixture demand by condition — what is *not* a fault

| Condition | Target | Why |
|---|---|---|
| Idle, part throttle (closed loop) | λ = 1 | catalyst + economy |
| WOT / power enrichment | rich | power, charge cooling, exhaust-temperature protection |
| Cold start / warm-up | enriched | fuel condenses on cold walls; the in-cylinder mixture is leaner than the commanded one |
| Tip-in (closed→open throttle) | transiently **lean** | air arrives before fuel; wall wetting |
| Tip-out (open→closed) | transiently **rich** | fuel film on the port walls keeps arriving |
| Decel fuel cut (DFCO) | injectors **off** | λ pegs fully lean, fuel flow zero, closed loop suspended |

Tip-in/tip-out excursions are transport physics, not sensor error. Judging steady-state
mixture requires gating on |dTPS/dt| ≈ 0 — or you are measuring the transient.

## Open loop is not a mode you can diagnose in

Open loop means the ECU is **not acting on** the oxygen sensor. Causes:

1. sensor below operating temperature after any start, hot or cold;
2. **deliberate** enrichment under hard acceleration — a perfectly valid signal is being
   ignored on purpose;
3. DFCO;
4. a failed/disconnected sensor.

In open loop **trims freeze** and λ says nothing about closed-loop health. "The trims go
to zero under a hard pull" is the system entering open loop, not the fault resolving.
Never compute trim statistics over open-loop windows — and note that a bare
"fuel status = open loop" filter captures DFCO too, which usually dominates a normal
drive. Separate power enrichment from overrun before pooling anything.

## Fuel trim: the structure

Two nested loops:

- **Short-term (STFT)** — the fast corrector, oscillating within the current window.
- **Long-term (LTFT) / adaptation** — the slowly-moving *position of that window*,
  stored across key cycles.

And two functional forms, usually learned in separate regions:

- **Additive** — applies at idle / low airflow. Modifies injector duration by a **fixed
  amount**, independent of base pulse width.
- **Multiplicative** — applies at part throttle. A **proportional** change to injector
  duration, dependent on base pulse width.

**The single most important consequence:** short-term trim is always re-centred inside
whatever window long-term adaptation has moved to. A fault can be **fully absorbed** —
λ sits at 1, STFT looks perfectly normal, and LTFT quietly holds a large offset.

> **λ ≈ 1 proves the controller works, not that the engine is healthy.** Health lives in
> the *magnitude of the correction*, read per load region. Diagnose on the effort, never
> on the controlled variable.

## The differential diagnosis — read the shape, not the number

Plot trim against load/airflow. The *shape* recovers the additive/multiplicative split
even when only one lumped trim number is logged.

| Trim shape vs load | Error class | Candidates |
|---|---|---|
| **Large at idle, shrinks as load rises** | fixed-*quantity* error (additive) | vacuum/intake leak, unmetered air, leaking purge valve, leaking injector, idle-air circuit leak |
| **Roughly constant in percent across load** | *scaling* error (multiplicative) | MAF calibration drift or contamination, fuel pressure wrong, injector flow wrong/wrong size, fuel blend change |
| **Both** | two faults, or a scaling error plus a leak | — |
| **One bank only** | bank-specific hardware | that bank's O2 sensor, injectors, exhaust leak, cam/valve issue |
| **Both banks equal** | shared source | common plenum leak, PCV, brake booster, purge, fuel supply, MAF |

The physics behind it: a fixed leak is a large fraction of a small idle airflow and a
negligible fraction of a large WOT airflow. A scaling error is the same percentage
everywhere.

### How big is too big

| Total (STFT+LTFT) | Reading |
|---|---|
| ±3–6% | **normal.** Trims are *not* zero on a healthy engine — injector tolerance, fuel volatility, and sensor aging all live here |
| ±5–10% | acceptable; note it and watch for a trend |
| ±10–20% | a real fault exists — diagnose it even with no DTC |
| beyond ~±20% | DTC territory |

Two cautions. **Exactly 0.0% on both banks after warm-up is a stronger fault signal than
±8%** — it usually means codes were just cleared, the loop is not closed, or the channel
isn't live. And **there is no standardized numeric threshold for P0171/P0174**: published
figures range from 12.5% to 35% because it is a manufacturer-calibrated value, evaluated on
a weighted average of long-term trim rather than an instantaneous peak. Don't quote a
constant as if it were universal.

**Pegged vs large-but-moving is a different finding.** A correction term stuck at its
authority limit means the controller ran out of range — categorically more serious than
a big term that is still oscillating. Correction ranges are typically **asymmetric**
(more rich authority than lean). Likewise a **flat, non-oscillating** correction means
the loop is not actually closed (dead or ignored sensor), which is not the same as a
correction of zero.

## Confounders that manufacture false trim findings

Check these *before* diagnosing hardware — each one moves trims with nothing broken:

- **EVAP purge** — unmetered fuel. Air is unchanged, delivered fuel rises, the sensor
  reads rich, trims swing **negative**. Purge-correlated trim excursions are the single
  most common false "rich" finding. Exclude purge-active windows or carry purge duty as
  a covariate. (The factory's own adaptation procedure disables purge before trusting
  learned values — copy that.)
- **Secondary air injection** during cold start — pumps ambient air into the exhaust, so
  the upstream sensor reads artificially **lean** while it runs. Fueling data from that
  window is uninterpretable.
- **Exhaust leak upstream of the sensor** — outside air is drawn in, the sensor reads
  lean, and trims drive **rich** with no intake fault at all.
- **Post-catalyst sensor bias** — on many ECUs the rear sensor trims LTFT to correct
  rear-sensor oxygen content. A rear-sensor failure can therefore set an *adaptation*
  fault naming the front sensor. A stored code names the symptom's location, not
  necessarily the fault's.
- **MAF pulsation / reverse-flow error** — a direction-blind hot-wire element measures
  reversed air twice and over-reports airflow, producing **negative trims confined to
  specific rpm/load cells** (worst at high load / low rpm on large-displacement,
  low-cylinder-count engines). That signature is a measurement artifact, not a fuel
  fault. Speed-density is immune to this but depends on its VE model instead.
- **Adaptation reset** — a battery disconnect, power interruption, or reflash erases
  learned values. Post-reset logs are not comparable to pre-reset logs, and driveability
  is genuinely worse until relearn completes. Look for the reset before blaming hardware
  for a step change.
- **A/C, alternator load, in-gear** — change idle-air demand and can shift idle trims.
- **Stale fuel and blend change** — a genuine **no-code** failure mode. Stale gasoline
  loses octane and varnishes injectors.

## Oxygen sensors: what you're actually reading

**Narrowband (switching, zirconia).** Output 100–1000 mV, and the ECU often applies a
stepped ground offset (~350 mV on some systems) — so a narrowband trace that looks
offset or compressed may be a *reference* problem, not a mixture problem. It only
resolves "rich or lean of stoichiometric"; the switching *rate* and amplitude are the
health metrics, not the absolute voltage. Loss of the sensor has **no substitute
function**: the ECU reverts to base injection time and runs open loop.

**Wideband (pumping cell + Nernst cell).** A **null-balance** measurement, not a voltage
read: the ECU drives a pump current to hold λ = 1 at the sensing cell, and the *pump
current required* is the measurement. Consequences that matter:

- The reported λ/AFR is **derived**, dependent on sensor temperature and on the pump
  circuit's calibration. It is not a raw signal.
- Sensor aging shifts the current-to-λ mapping, so widebands drift in **gain**, not just
  offset — which is exactly why an aftermarket wideband and the ECU's wideband can
  disagree by a scale factor rather than a constant.
- Widebands track much further lean, which lets the ECU stay in closed loop during
  deceleration fuel cutoff and control transients more accurately.

**Heater health matters more than it looks.** A failed heater means prolonged open loop,
elevated cold-start emissions, and unreliable trim data — a data-quality problem before
it is an emissions problem. Planar sensors reach operation in ~10 s versus far longer
for older thimble types.

## Injectors and fueling math

- Required fuel mass = cylinder air mass ÷ (target λ × stoichiometric AFR).
- Useful conversions: **duty % = PW[ms] × rpm / 1200** (four-stroke, one pulse per cycle);
  **lb/hr × 10.5 ≈ cc/min** for gasoline at SG 0.72.
- **Injector flow scales with √ΔP.** A 10% rail-pressure error is only about a 5% flow
  error — and it is a **pure multiplicative** error, so it lands in the multiplicative
  trim, never the additive one. Worked example: a rail running 51 psi where the calibration
  assumes 48 gives √(51/48) = **+3.1%** flow, which is exactly the size of residual that
  gets misread as a fueling-model error.
- Know which fuel system you have. A **manifold-referenced (returned)** regulator holds a
  constant differential across the injector, so mass flow is a clean function of pulse
  width. A **returnless** system holds constant absolute rail pressure, so the ECU must
  apply a MAP-based correction itself — and on those systems a MAP error corrupts fueling
  directly.
- **Dead time / latency** is the non-flowing part of each pulse (order 1 ms at ~3 bar and
  14 V, rising steeply as voltage falls). It matters most at small pulse widths — idle and
  low load — which is exactly where an uncorrected dead-time error masquerades as an
  additive trim, and why **a tune that is right at WOT but drifts at idle is a dead-time
  problem, not a fuel-map problem**.
- Injector resistance rises with temperature, so a cold bench measurement is not the
  in-service value.
- **Duty cycle** near its practical ceiling means the injector is running out of time, not
  necessarily out of flow — the resulting lean condition appears only at high rpm/load
  and looks exactly like a VE error. Rail pressure holding under load is the
  discriminator.
