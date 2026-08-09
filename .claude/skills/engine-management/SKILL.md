---
name: engine-management
description: >-
  Engine management fundamentals plus the working method for reading engine
  datalogs and making calibration decisions. Use this whenever the task touches
  how a gasoline engine actually behaves or what a log is saying — fuel trims,
  lambda/AFR targets, volumetric efficiency and airflow, MAF vs speed-density,
  ignition advance, MBT and knock retard, boost control, torque-based ECUs and
  drive-by-wire, sensor and actuator failure signatures, or plain "is this
  reading normal?". Reach for it on any request to analyze or interpret a
  datalog, session, or capture; to diagnose a driveability, economy, or
  down-on-power complaint; to decide whether and how much to change a fuel or
  spark table; to plan what to log and at what rate; or to judge whether a log
  can support a conclusion at all. Use it too whenever someone is about to draw
  a conclusion from averaged log data or a single pull — the discipline here
  exists to stop confident-but-wrong diagnoses. For decoding raw CAN bytes or
  working out which PID/DID carries a signal, use ecu-reverse-engineering; for
  planning a multi-factor test matrix, use designed-experiments.
---

# Engine management: performance, logs, and tuning

Working knowledge of how a spark-ignition engine is controlled, and the method for
turning a datalog into a defensible conclusion.

**Scope:** gasoline/spark-ignition, naturally aspirated and forced induction, port
injection as the baseline. Make- and model-agnostic — the numbers here are physics and
convention, not one manufacturer's calibration. Direct injection and diesel are out of
scope; say so rather than extrapolating.

**Answer briefly.** Lead with the answer — the diagnosis, the number, the yes/no. Keep
walkthroughs compact. Pull detail from the reference files when the task needs it, not by
default. A correct three-line answer beats a correct three-page one.

## Three questions before you read any number

Almost every wrong conclusion in engine data skips one of these.

**1. What regime was the engine in?** A value is uninterpretable without it. Open vs
closed loop, decel fuel cut, power enrichment, warm-up, purge active, secondary air,
transient vs steady state, limiter active, post-reset relearn. Each has an expected,
non-faulty signature. "Trims went to zero under the pull" is *open loop*, not the fault
resolving.

**2. Is this number a measurement, a model output, or a control effort?** Calculated load,
commanded λ, estimated torque, and most "actual" values are the ECU's *belief*, computed
from other channels through a calibration — they inherit every error upstream. When a
modeled channel and a measured one disagree, that disagreement **is** the finding; do not
average them.

**3. What is it being compared against?** Against a spec? Against another channel at the
same instant? Against the same engine last month under different ambient conditions? Most
"findings" die here.

## Health lives in the effort, not the outcome

This is the single most useful idea in the file. Every control loop drives a variable to a
target. **The controlled variable tells you the controller is working. The control effort
tells you how hard it is working — and that is the health signal.** λ ≈ 1 proves the fuel
controller works; it says nothing about the engine. Idle rpm is perfect right up until
the moment authority runs out.

| Loop | Controlled variable | The effort to actually watch | Neutral | Saturated means | Flat/dead means |
|---|---|---|---|---|---|
| Fuel | λ / AFR | short + long fuel trim | 0% | out of adaptation authority — a real fault | loop not closed (open loop, dead sensor) |
| Idle | idle rpm | idle air duty / commanded plate angle | learned value | mechanical drift past what learning absorbs | control disabled |
| Spark | torque / knock margin | knock retard, per cylinder | 0° | at the retard limit | knock control disabled (often below a coolant threshold) — not "no knock" |
| Boost | manifold pressure | wastegate duty cycle | mid-range | out of authority: leak, gate, compressor limit | ECU deliberately limiting |
| Torque (DBW) | crank torque | requested vs actual at each stage | equal | a limiter is active — **silently, with no DTC** | — |

Two failure directions, always distinguish them: **pegged** (ran out of range — serious)
versus **flat and not oscillating** (the loop isn't closed — different fault entirely). A
correction reading zero is not the same as a correction that is neutral.

## Triaging a log

1. **Can this log answer the question?** Channels present, rate adequate, the regime of
   interest actually driven. Say "these data can't answer that, here's what would" rather
   than producing an answer anyway — it is the more useful reply.
2. **Sanity-check the instruments before the engine.** With the engine off, MAP must equal
   barometric pressure. After a long soak, coolant ≈ intake ≈ ambient. Any channel sitting
   exactly on a round default (flat 20 °C IAT, flat 80 °C coolant, a ruler-straight
   temperature ramp) is a **substituted value**, not data.
3. **Classify the regime** for every sample; drop or separate the ones that can't answer
   the question.
4. **Bin on rpm × airflow.** Not on calculated load, which reads about the same at idle and
   light cruise and moves when fueling moves. Print the within-bin spread to prove the bin
   is narrow.
5. **Read the control efforts**, per region, before reading anything else.
6. **Form competing hypotheses and name the measurement that separates them.** One
   observation consistent with your hypothesis is not evidence; a measurement where the
   hypotheses predict *opposite* results is.

## Fuel: read the shape, not the number

The highest-yield diagnostic in the whole domain. Plot trim against load/airflow:

| Trim shape vs load | Error class | Candidates |
|---|---|---|
| Large at idle, **shrinks** as load rises | fixed-*quantity* (additive) | vacuum/intake leak, unmetered air, leaking purge valve, idle-air path |
| Roughly **constant in percent** across load | *scaling* (multiplicative) | MAF drift/contamination, fuel pressure, injector flow, fuel blend |
| Both | two faults, or a scaling error plus a leak | — |
| One bank only | bank-specific hardware | that bank's sensor, injectors, exhaust leak |
| Both banks equal | shared source | plenum leak, PCV, purge, fuel supply, MAF |

Why: a fixed leak is a large fraction of a small idle airflow and negligible at WOT; a
scaling error is the same percentage everywhere.

Two calibration points. **Trims are not zero on a healthy engine** — ±3–6% is ordinary
drift from injector tolerance, fuel volatility, and sensor aging, so *exactly* 0.0% on both
banks after warm-up is a stronger fault signal than ±8% (codes just cleared, loop not
closed, or the channel isn't live). And **there is no universal DTC threshold**: sources
quote anywhere from 12.5% to 35% for P0171/P0174 because it is a manufacturer-calibrated
value evaluated on a weighted average, not an instantaneous peak. Don't quote a constant.

Before diagnosing hardware, rule out the confounders that move trims with nothing broken:
**purge** (unmetered fuel → negative trims), **secondary air** (upstream sensor reads
artificially lean on cold start), **exhaust leak upstream of the sensor** (reads lean →
trims rich), **adaptation reset**, **MAF pulsation error** (negative trims in specific
cells only), **fuel blend**. Details and the sensor physics: `references/fuel-and-lambda.md`.

## Air, load, and altitude

Airflow is the load axis and fuel is derived from it, so **every mixture error is either an
air-measurement error or a fuel-delivery error.**

Speed-density, four-stroke:

```
ṁ_air [kg/s] = (MAP[Pa] / (287 · T_intake[K])) · VE · V_displacement[m³] · rpm / 120
air mass per intake event = ṁ_air / (rpm · n_cylinders / 120)
```

VE is the ratio of actual to theoretical charge — the engine's breathing curve. It peaks
where the intake and exhaust are tuned to and falls off either side; a *kink* in it usually
means a changeover (runner flap, cam phaser) rather than a sensor.

**"Load" is at least four different quantities**, and they are not interchangeable. Mixing
them is a common silent error:

- **Calculated load (PID 0x04)** — airflow ÷ peak available airflow, **normalized for baro
  and ambient temperature**. It reaches ~100% at WOT *at any altitude*, so it means
  "fraction of the torque available today," not "how much air." Saturating at 100% is not
  evidence of full airflow — it saturates by construction.
- **Absolute load (PID 0x43)** — air mass per intake stroke ÷ a standard-density full
  charge, and **not** baro-corrected. Approximately `VE × (MAP/101.3) × (298/IAT)`. A
  healthy NA engine reads ~0.93 at sea level and ~0.73 at 80 kPa ambient; boosted engines
  go to ~4. (Two-byte PID — decoding it like the one-byte 0x04 is a frequent bug.)
- **mg/stroke / relative charge** — the honest physical quantity. Prefer it.
- **MAP** — a pressure, not a mass, until corrected for temperature.

**Altitude changes everything and is routinely forgotten.** At ~80 kPa ambient (~2000 m)
the density ratio is 0.79: about 21% less air, 21% less power, WOT MAP around 78 kPa, and
absolute load falling from ~0.93 to ~0.73 on a perfectly healthy engine. Comparing any of
those to a sea-level reference produces a phantom ~20% "restriction." **VE is essentially
unchanged with altitude, which is exactly why VE and `MAP ÷ measured baro` are the metrics
to compare across days, sites, and vehicles** — never raw g/s, raw mg/stroke, or absolute
load. Full treatment: `references/air-load-and-boost.md`.

## Spark

Combustion takes roughly a fixed **time** (~2 ms), so advance rises nearly linearly with
rpm — about **0.012 × rpm degrees** of speed-driven advance. The control objective is
holding peak cylinder pressure at a fixed crank angle, not any particular spark angle.
Timing is **MBT-limited at low load and knock-limited at high load**, so transient retard at
high load is the system working at its intended edge; *sustained* retard is the anomaly.

Log **per-cylinder** retard — a mean hides one bad cylinder, and a persistent single-cylinder
offset localizes a mechanical or fueling problem. Separate three things that all move final
timing: the base map, feed-forward temperature compensation, and knock feedback. Timing being
down on a hot day with no knock events is the IAT compensation table doing its job, and shift
retard is not knock retard. More: `references/spark-and-knock.md`.

## Boost

Read the **duty cycle** alongside the pressure — they give opposite diagnoses from the same
boost trace. Target missed with duty **saturated** = out of authority (leak, gate not
sealing, compressor past its efficient flow). Target missed with duty **low** = the ECU is
deliberately limiting. Reason in **absolute pressure and pressure ratio**, not gauge psi:
pressure ratio sets compressor speed, which is why boost must be reduced at altitude and why
gauge-referenced targets overspeed a turbo up a mountain. Charge temperature is the usual
hidden variable — a heat-soaked intercooler pulls timing while the boost trace still looks
healthy. See `references/air-load-and-boost.md`.

## Torque-structured ECUs (any drive-by-wire engine)

Pedal ≠ throttle ≠ airflow ≠ torque; each link is arbitrated and limitable. **Torque limiters
cap output silently — "down on power, no codes" is their normal presentation.** The diagnostic
is the requested-vs-actual pair at each stage, not any absolute value. Air is the slow
bidirectional path; spark and fuel are the fast, essentially downward-only path, which is why
knock retard, traction control, and shift cuts all land on spark. See
`references/torque-structure.md`.

## Prescribing a change

You are allowed to say what to change and roughly by how much. Do it in this order:

1. **Rule out measurement error first.** A correction applied to a sensor problem is
   permanent damage to a good calibration.
2. **Establish that adaptation is neutral** in the region you're about to touch, or you are
   tuning against a controller that will quietly undo you.
3. **Size the correction from the ratio in engineering units**, per bin — e.g. measured λ
   1.075 against a target of 1.000 is a 7.5% airflow-or-fuel error in *that cell*. Never
   size a correction from a whole-pull average; the cells differ.
4. **Name which table.** A lean high-rpm region is a VE/airflow error *or* running out of
   fuel delivery (rail pressure falling, injector duty at its limit) — they produce an
   identical trace and need opposite fixes. Say which one the data supports and what would
   distinguish them if it doesn't.
5. **State the expected result and the falsifier**: "if this is right, the next log shows X;
   if instead you see Y, the hypothesis was wrong."
6. **Re-log and verify.** One pull is an anecdote — the run-to-run spread is what says
   whether a change did anything.

Stop short of writing numbers the data can't support. "The evidence supports about 6–8% in
these cells; here's the log that would pin it down" is a better answer than a confident
table.

## The traps

- **Never compare two signals by their aggregates.** Per-window means, or two summary
  series correlated against each other, manufacture confident numbers from nothing. Pair
  same-instant values, and refuse when there are too few pairs or one side barely moves.
- **r² alone is not a finding** — over one drive nearly everything correlates with rpm.
  Check whether the *scale* is stable across sub-windows.
- **Watch sample age, not just sample rate.** Rotation-polled channels never share a
  timestamp; bound the join per channel and report how much data the bound dropped.
- **A wall-clock trend is a hypothesis, not a mechanism.** Check it against duty cycle
  before telling a thermal story.
- **"No codes" means "no broken wires."** The failures that matter to performance are
  mostly electrically perfect.
- **Absence needs a reason.** "The filters removed everything," "the regime was never
  driven," and "the channel was never captured" are three different answers demanding
  opposite responses. Name which one.

## Reference files

- `references/fuel-and-lambda.md` — λ/AFR conventions and bands, closed-loop control, trim
  structure and differential diagnosis, confounders, O2 sensor physics, injector/fueling math.
- `references/air-load-and-boost.md` — VE and speed-density, MAF vs MAP, load definitions,
  altitude and barometric correction, boost control, pressure ratio, charge cooling.
- `references/spark-and-knock.md` — advance physics, MBT, the knock limit and what moves it,
  knock control in logs, dwell, misfire.
- `references/torque-structure.md` — torque-based ECUs, the two actuation paths, silent
  limiters, electronic throttle redundancy and adaptation.
- `references/sensors-and-failure-signatures.md` — what each sensor/actuator failure looks
  like in data, substitute/limp values, OBD-II semantics (DTCs, freeze frames, readiness).
- `references/log-analysis-method.md` — capture design, rates and joins, regime gating,
  binning, validation, what a valid pull needs, derived channels worth computing.
- `references/canary-workflow.md` — doing all of this with CANary's tools in this repo.

## Handoffs

- **Which byte, PID, or DID carries a quantity, or is a binding real?** →
  `ecu-reverse-engineering`. This skill assumes channels are already correctly identified
  and in engineering units.
- **Planning a test that varies several factors, or tempted by a full grid sweep?** →
  `designed-experiments`, then come back here to interpret.
- **Working on a specific vehicle?** → its vehicle skill, e.g.
  `vehicle-nissan-xterra-2008`. This skill is deliberately make- and
  model-agnostic; sensor fitment, altitude, and fuel/spark availability are
  per-vehicle and change what a number means.
