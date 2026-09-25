# Ignition timing, MBT, and knock

## The invariant: burn phasing, not spark angle

The quantity that matters is *where the burn lands*, not when the spark fires. Classic
MBT criteria (Heywood) put peak cylinder pressure at about 15–16° ATDC and half the charge
burned at about 10° ATDC. Modern practice targets **CA50 ≈ 8° ATDC**; the true optimum
moves between about 5° and 11° with heat transfer, burn duration, and compression ratio.
Advance is whatever achieves that phasing. A production ECU without cylinder pressure
sensing does not *control* to it. It runs maps that were calibrated to it.

**How advance scales with rpm.** A popular simplification, still printed in some Bosch
training material, says combustion takes a fixed ~2 ms, so advance ≈ 0.012 × rpm degrees.
That is wrong above low speed. Turbulence intensity scales with mean piston speed, so flame
speed rises with rpm and the burn takes a nearly constant *crank angle*:

- A typical fast-burn engine at medium speed and part load burns 0–10 % in ~15°, 0–50 % in
  ~25°, and 0–90 % in ~35° (MIT 2.61).
- Burn angle grows only about as **rpm^0.2–0.35**. Heywood's data show the 0–10 % and 0–90 %
  angles growing ×1.6 for a ×4 speed increase. Most of that growth is in the 0–10 %
  flame-development phase.

A usable sanity band, scaling from a known MBT point (SA_ref at N_ref):

```
SA_MBT(N) ≈ (SA_ref + CA50) × (N / N_ref)^k − CA50        k ≈ 0.2–0.35, CA50 ≈ 8° ATDC
```

| From 24° BTDC at 2000 rpm | fixed-2-ms model (wrong) | angle-scaling model |
|---|---|---|
| 4000 rpm | ~48° | ~29–33° |
| 6000 rpm | ~72° | ~32–39° |

So advance climbs steeply off idle and **flattens at high rpm**. Three things pull a real
map below that band: dilution (EGR, residuals, lean mixture) lengthens the burn, idle is
often deliberately retarded for torque reserve, and low-to-mid rpm at WOT is usually
knock-limited. A logged high-rpm timing curve that keeps rising linearly with rpm needs an
explanation. So does one that sits far above this band.

**MBT is a flat optimum.** About 5–7° of retard from MBT costs roughly 1 % of torque. That
is why production calibrations can sit a few degrees retarded for knock margin at almost
no cost. It is also why a 1–2° timing difference between two logs is rarely visible in a
torque or acceleration measurement.

## MBT and the knock ceiling

**MBT** = Minimum advance for Best Torque: the least advance that produces peak torque.
Advancing past MBT costs torque *and* moves toward knock, so nothing is gained.

Timing is therefore bounded on both sides:

- **Low load:** MBT-limited. The calibration can sit at or very near MBT.
- **High load:** **knock-limited.** Optimal timing sits just short of the knock boundary,
  which is why a good calibration is deliberately close to the edge at high load and why
  **transient knock retard there is the system working as intended**. *Sustained* retard
  is the anomaly.

Whether a given engine is knock-limited at all is a property of its calibration, not a
universal. An engine calibrated knock-free on regular fuel will show almost no retard,
and higher-octane fuel buys it nothing at stock timing. Establish which case you are in
before reading meaning into a timing number.

What moves the knock limit, all of which must be held constant to compare two logs:
**octane, intake/charge temperature, coolant temperature, boost/cylinder pressure, AFR
(enrichment cools the charge), EGR/dilution, and altitude.** Charge temperature is
usually the biggest mover in a datalog and the most often forgotten.

## Knock control in a log

Essentially every production controller has the same five stages: **detect** on one
cylinder in a crank-angle window, against a *tracked background-noise level* rather than a
fixed threshold; **retard** by a step; **hold**; **restore** by a rate-limited ramp; and
often **learn** a long-term octane adaptation that biases the base table (GM "octane
scalar", Ford "octane adjust ratio", VAG knock adaptation) so a bad tank isn't rediscovered
every trip.

Implementation details that shape what you see:

- **Restore is deliberately 10–100× slower than retard.** Bosch Motronic 2.5, for example,
  retards 3° per knock event, then advances in 0.75° steps only after ~2 s (20–120 cycles)
  without knock. That produces the characteristic **sawtooth: fast drop, slow climb**.
- **Latency is about one engine cycle.** A cylinder-selective controller retards the
  knocking cylinder's *next* firing.
- Some controllers use a *progressive* step, shrinking to a fraction of the base step on
  repeated events so a burst doesn't over-retard.
- **Many controllers fall back to a fixed, large global retard when the knock system
  itself is faulted** (Motronic 2.5: 10.5°). A flat, large, event-free retard can
  therefore mean a knock-sensor fault, not knock.

Exact step and restore constants are manufacturer-calibrated. Treat any specific figure
as one implementation, not a universal.

Reading the sawtooth:

| What you see | What it means |
|---|---|
| Isolated 1–2° events, fully restored within a second or two | Normal. Production engines knock-control continuously. |
| Sustained retard that never fully restores at a given load | The calibration is genuinely knock-limited there. Act on it. |
| Retard accumulating through a long pull, timing progressively falling | Heat soak — check the IAT trend *within* the pull. |
| Retard on **one cylinder**, repeatably | Cylinder-specific: injector flow, coolant flow, deposits, compression, plug, charge distribution. Or an artifact — check whether that cylinder is nearest the sensor. |
| Retard at light load or idle | Almost always **false knock**. Real knock needs cylinder pressure. |
| Retard at closed throttle, during shifts, or over bumps | False knock from driveline or road noise. |
| Retard appearing right after a mechanical change | New noise source in the knock band — re-baseline the noise floor. |
| **Never any retard, in any conditions** | Suspicious. Dead sensor, thresholds far too high, channel not really connected, or a very conservative tune. "Zero knock" is not automatically good news. |

Five tests that separate real from false knock: **load correlation** (real knock lives at
high cylinder pressure), **spark sensitivity** (pull 3–4° in that cell — real knock goes
away, false knock doesn't), **fuel sensitivity** (higher octane removes real knock only),
**temperature sensitivity** (real knock worsens with IAT and coolant), and
**repeatability** (real knock repeats in the same cell; scattered single hits are noise).

Further consequences for reading logs:

- **Log per-cylinder retard.** A mean or maximum hides one bad cylinder, and a persistent
  single-cylinder offset localizes a mechanical or fueling problem to that cylinder
  (injector flow, coolant distribution, deposits, a leaking valve).
- **Mounting matters.** Sensor torque sets the transfer function, so a loose or
  over-torqued sensor, or one isolated by a gasket, changes sensitivity — a common source
  of both false knock and missed knock.
- **Retard is a fast torque-reducing intervention** on the crank-synchronous path. On a
  boosted engine, sustained knock is answered more slowly on the air path — the ECU pulls
  boost. So "boost fell" and "timing was pulled" can be one event with one cause.
- **Feed-forward retard is not knock.** Most calibrations retard on intake-air and coolant
  temperature *before* any knock occurs. Timing being down on a hot day with no knock
  events logged is the IAT compensation table doing its job. Separate commanded-map
  timing, temperature compensation, and knock feedback before calling anything "knock
  retard" — mapped-versus-logged final timing at matched rpm × load is a *signal*, not
  proof, because several compensations move final timing.
- Some ECUs do not stream a knock channel at all and expose knock health only through
  DTCs. Absence of a knock channel is not absence of knock protection — check whether the
  channel exists before treating a flat reading as evidence.
- Because knock adaptation is persistent, a tank of poorer fuel shows as a
  slowly-developing advance deficit across the whole map rather than a burst of events —
  and it will survive into the next drive.
- Many systems **disable knock control below a coolant threshold**, so a cold-engine log
  shows zero knock activity by construction. Know every enable condition before
  interpreting an absence.

## Dwell

Dwell is coil **charge time** — how long the primary winding is energized. Longer dwell →
stronger field → higher secondary voltage → stronger spark. Because it is charge time it
is **battery-voltage dependent**, and every modern ECU carries a dwell-vs-voltage
correction. A logged dwell that moves with system voltage is normal. Excessive dwell is
coil and driver heat.

Older systems have no primary-circuit diagnosis at all, so ignition faults surface only
indirectly as **misfire**. Modern ECUs monitor coil current and set per-coil circuit
codes, and detect misfire from per-cylinder crank-speed irregularity.

## Misfire

Misfire dumps raw fuel into the exhaust. Sustained misfire overheats the catalyst
(sustained >900 °C sinters or melts the substrate), which is why a flashing MIL means
*actively damaging the catalyst right now* — a real-time indicator, not a latched one —
and why **a misfire code plus a catalyst-efficiency code is usually one causal chain,
not two faults**.

Misfire counts are the cheapest cross-check on any spark or fuel change: if a
calibration change raises misfire counts anywhere, it is not an improvement regardless of
what the torque numbers say.

## Plugs

Heat range is the defining characteristic: the electrode must run hot enough to burn off
carbon, cool enough not to become an ignition source. A **cold** plug transfers heat away
quickly; a **hot** plug transfers slowly. More specific output — more boost, more
advance, more load — needs a colder plug to avoid becoming a pre-ignition source; too
cold on a low-load-duty engine fouls. Heat transfer runs through the threads, so correct
torque into the head matters, and anti-seize on the threads compromises the path.
