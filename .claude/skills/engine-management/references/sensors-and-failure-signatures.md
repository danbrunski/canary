# Sensors, actuators, and what their failures look like in a log

The point of this file is not what each part is. It is **what a degraded part does to
the data** — because the failures that matter to performance almost never announce
themselves.

## The most dangerous thing in a log: substituted values

When an ECU disbelieves a sensor it does not blank the channel. It **feeds itself a
default**, and that default is perfectly smooth, perfectly plausible, and wrong. Learn to
recognize substitution on sight.

| Failed input | Typical substitute | What it looks like |
|---|---|---|
| Mass air flow | throttle position (+ rpm) becomes the load signal | load becomes a smooth function of TPS/rpm alone — no airflow noise, no response to filter restriction, no altitude sensitivity; load-vs-MAF collapses onto a curve |
| Throttle position | MAF + rpm | TPS is now derived, decoupled from pedal and from actual plate angle |
| Coolant temperature | a fixed value (e.g. 80 °C), or a **ruler-straight ramp** (e.g. 20 °C + 10 °C/min capped at 85 °C) | real coolant never rises in a straight line and never plateaus that cleanly, and it always shows a thermostat knee then a fan sawtooth. Absence of those features is the tell |
| Intake air temperature | a fixed value (e.g. 20 °C) | flat regardless of ambient, soak, or airflow |
| Cam position | **none** — falls back to batch injection and non-cylinder-selective, retarded knock control | power down, knock retard applied globally, injection phasing lost |
| Crank reference | **none** — keeps running on an internally computed signal, but **will not restart** | the classic "runs fine until you shut it off" fault: invisible in the running log |
| Oxygen sensor | **none** — closed loop stops, ECU reverts to base injection | trims freeze or read zero; λ wanders with load; the engine still runs well enough to hide it |
| O2 heater | none — sensor heated only by exhaust | abnormally long open-loop time after start; trims pinned at zero for minutes; may *drop out* of closed loop during long idles |
| Knock sensor | none — that sensor's cylinders are **retarded** | persistent retard with no knock event visible; power down, EGT up |
| Idle air control | drivers off, valve springs to a fixed position giving a normal **warm** idle | idles fine warm, wrong cold or loaded; duty channel dead |
| Purge valve (power lost) | valve stays **open** | uncommanded purge at all times — wandering, load-dependent trim disturbance, worst at idle |

**Rule: any channel sitting exactly on a round default is suspect until proven otherwise.**

## The diagnostic gap that defines everything else

| | Method | Catches |
|---|---|---|
| **Component monitor** | measures voltage at the pin: short to B+, short to ground, open | wiring and gross electrical failure only |
| **System monitor** | **command a change, verify the physical response in an independent sensor** | functional degradation, mechanical faults, flow blockage — things that are electrically perfect |

Everything that actually degrades an engine — a contaminated MAF, a lazy O2 sensor, a
clogged injector, a coked EGR passage, a sticking flap — lives on the right-hand side or
outside the table entirely.

> **"No DTCs" means "no broken wires."** It is not a clean bill of health.

The system-monitor recipe is also the right recipe for your own analysis: EGR commanded
open → look for the temperature rise; purge commanded open → look for the canister
pressure drop; secondary air on → look for the lean shift at the upstream sensor. **Pair a
command channel with a response channel and evaluate them at the same instants.** An
actuator that never moved and one that moved constantly can have identical means.

## Sensors

**Mass air flow (hot wire / hot film).** Measures air *mass*, so it is inherently
density-corrected — no altitude or temperature correction is needed for the load term.
Contamination is the failure that matters and it **sets no code**: the element under-reads,
the ECU trusts it completely, and you get a lean-biased trim that grows with airflow and
is largest at high load where closed-loop authority is least. Diagnose by **disagreement**
— MAF against a speed-density estimate, or against the pressure channel — never by the MAF
trace alone, which always looks reasonable. Also: a direction-blind element double-counts
reversed flow at low-rpm/high-load pulsation, giving negative trims confined to particular
cells.

**Manifold absolute pressure and barometric pressure.** These are the *same sensor* pointed
at different places. That yields the cheapest calibration check in existence: **with the
engine off, MAP must equal baro** (and any charge-air pressure sensor must agree with both,
typically within ~200 mbar or the ECU itself calls it implausible). Any offset at rest
propagates into every load, boost, and VE calculation. A cracked or disconnected vacuum
line makes MAP read ambient; a kinked one low-pass filters it, so MAP lags throttle on
tip-in.

**Coolant temperature.** ECT is a **gatekeeper**, not just a correction: knock control,
closed loop, purge, EGR, and idle adaptation are all typically gated on it. A skewed ECT
therefore silently disables half the control system, and the log signature is a *cluster*
of monitors "not running" plus odd trims — one temperature channel as the common cause. A
sensor reading cold holds warm-up enrichment forever; reading warm gives lean cold running
and premature closed loop. A lazy/decoupled sensor shows no thermostat knee and no fan
sawtooth.

**Intake air temperature.** IAT tracks **airflow, not weather** — it should fall when you
start moving and climb when you stop, because a manifold-mounted sensor heat-soaks at
idle. A trace that stays high while airflow is high is a genuine heat problem; a trace
that stays flat through both is dead or substituted. On a MAF system a skewed IAT is a
*spark* error that the fuel trims will never flag; on a speed-density system it is a
fueling term and shows up directly. Sensor placement (pre-throttle, post-intercooler,
in-manifold) makes these three different quantities — confirm placement before comparing
across vehicles or across a modification.

**Oxygen sensors.** See `fuel-and-lambda.md` for the control theory. The failure that
matters: a **lazy sensor sets no circuit code**. It still switches, so it passes range
checks, but the switch *rate* falls, the amplitude compresses toward mid-scale, and
transition time grows. Because the controller integrates against a delayed signal it
overshoots — **trim amplitude grows while its frequency falls**. Measure switch rate and
rail-to-rail travel; **never characterize an O2 sensor by its mean**, which is near
mid-scale whether the loop is healthy or oscillating wildly. Also note the pre-cat sensor
is often grounded through the exhaust threads, so a corroded thread or insulating
anti-seize shifts the whole signal and reads as a fueling error; post-cat sensors usually
get an ECU-supplied ground deliberately offset (~450 mV) so their near-flat trace can be
resolved — that resting voltage is bias, not a reading.

**Knock sensors.** Piezo elements read **relative to a tracked background noise level**, so
mounting torque is functionally critical — it sets the transfer function. Two failure
directions: a loose/over-torqued/isolated sensor gives **false knock** (retard that tracks
rpm or nothing, and does not correlate with load, IAT, coolant, or fuel), while a dead
sensor gives **no detection at all** until a "no signal" diagnostic fires. Real knock
tracks cylinder pressure and charge temperature. Retard/restore is deliberately asymmetric
— attack far faster than recovery — so **one genuine knock burst leaves a long retard
tail**; do not read the tail as ongoing knocking, and do not evaluate a tune from a window
that contains one. Many systems disable knock control below a coolant threshold, which
means **a cold-engine log shows zero knock activity by construction**.

**Crank and cam position.** Inductive pickups have amplitude proportional to speed, so a
marginal air gap fails **at cranking first** — a no-start that clears once running. The
transferable trap: **a cam signal that is out of phase with the crank signal can set an
open/short circuit DTC on two perfectly good sensors.** Never replace a cam or crank sensor
on a circuit code without confirming the phase relationship first — a stretched chain,
jumped tooth, or stuck phaser presents exactly this way, and the tell is that it sets under
hot/high-load conditions rather than randomly.

**Battery voltage.** Injector opening delay depends on supply voltage, so a wrong dead-time
table shifts mixture when the alternator loads up, the fans cycle, or the battery ages —
**most at short pulse widths**, i.e. idle and light cruise. Log system voltage alongside
trims; trims that step in sync with voltage, with no change in airflow, are a
characterization error, not a fuel fault.

## Actuators

**Injectors.** Delivery = (commanded pulse width − opening delay) × flow rate. The
*fractional* error from a wrong dead time is largest where pulse widths are shortest, so
**a tune that is perfect at WOT and drifts at idle is a dead-time problem, not a fuel-map
problem**. Diagnostics are electrical only, so a clogged or dribbling injector sets no
code and instead shows as that cylinder running lean: elevated per-cylinder knock retard,
misfire counts, and a positive trim on that bank alone. Injectors are also deliberately
switched off in normal operation — decel fuel cut and rev limiting — which is not a fault.

**Ignition coils.** Usually only the driver stage is diagnosed, not the coil, so **a weak
coil presents as misfire counts, not an ignition code**. On waste-spark systems know the
pairing: one plug always fires reversed and wears faster, and a fault in one cylinder can
raise demand voltage on its partner. If a cylinder shows misfire *and* zero injector pulse
width, that is the ECU cutting fuel to protect the catalyst — do not chase the fuel side.

**Idle air control (or the DBW plate doing the same job).** *Watch the correction, not the
controlled variable.* Idle rpm stays perfect right up until authority runs out; the duty
cycle or commanded idle air tells you how hard the controller is working. A rising idle
duty over months = fouling valve or throttle deposits; a falling duty = a developing
vacuum leak. "Adaptation limit reached/exceeded" is the canonical code for *the mechanical
thing has drifted past what learning can absorb*. A sticking valve produces **hunting**
(a slow limit cycle with the duty sawtoothing against rpm), not a fixed error.

**This is the general principle, and it is the most useful idea in this file:**

> The correction is the early-warning signal. The controlled variable is a lagging
> indicator that only moves once the correction saturates. Applies equally to fuel trims,
> idle air, knock adaptation, throttle adaptation, and boost duty cycle.

**Purge valve.** Duty-cycle metered, and on many systems **de-energized = open**, so a
power loss gives uncommanded purge at all times. Purge is unmeasured fuel; see the
confounder list in `fuel-and-lambda.md`.

**Variable intake flaps / runner changeover.** Often **open-loop with no position
feedback** — "the ECU has no way of determining valve position." A stuck flap therefore
produces **no DTC** and shows only as a VE/torque/trim kink at the changeover rpm. Whenever
a signal discontinuity appears, check whether it coincides with a known changeover
threshold before blaming a sensor.

**Cam phasers.** With phasing, the cam sensor becomes a **feedback** channel, not just a
sync marker. Log **commanded vs measured** phase per bank; a phaser that cannot reach
command at hot idle / low oil pressure presents as a *tracking error*, not an out-of-range
value. And note that a VE map is only valid at a given cam phase — comparing VE across a
phase change compares two different engines.

## Fuel pressure (rail pressure)

Rail pressure is the discriminator between a fuel-delivery fault and an air-model error (a
lean high-rpm region that looks like a VE error is either), and many engines have no sensor
for it, so it is often an added gauge.

- **An added gauge's analog output may be regenerated in steps at the gauge's own update
  rate.** That aliases rail pulsation and limits usable bandwidth to half that rate. A
  output stepped at 100 Hz carries nothing above 50 Hz that is rail pressure. Know the gauge's
  rate before reading a waveform.
- **Average over at least half a second for a pressure.** An instantaneous sample is a
  pulsation phase, not the rail level.
- **Compare ratios on one instrument, not absolute readings across two.** An analog output
  and the gauge's own face (or a mechanical gauge) can differ by a fixed offset of a psi or
  two. A before/after change on the same sensor is meaningful where the absolute level may
  not be.
- **A brown-out during cranking reads as a pressure fault.** Supply voltage sagging under
  the starter can read as low or implausible rail pressure; check supply voltage before
  calling the pump or the sensor, and read pressure only after voltage has recovered.

## Reading DTCs, freeze frames, and readiness honestly

- **A diagnostic read can have a side effect.** A status read that clears flags (reading one
  manufacturer data item resets a per-DTC status bit, for example) is a write dressed as a
  read. Know what a read does before polling it, and keep such items out of sweeps and
  loggers.
- **OBD's threshold is emissions, not performance.** A large driveability defect can set no
  code; a trivial emissions issue can set one. A code is not a measure of severity.
- **A freeze frame is one sample.** Its value is that it names the *operating point* so you
  can go find that point in your own log. It cannot show a trend, a transient, or a rate.
  Treat it as an index into your data, not as evidence.
- **Readiness means "the test completed," never "the system is healthy."** A later fault
  does not clear readiness, but repairing and clearing the code does. A vehicle presented
  with "no codes" right after a clear carries **no information at all** — check readiness
  before believing an absence of faults.
- **Every monitor has enabling conditions.** "Not ready" means the conditions were never
  met. Generalize this to your own analysis: *an absent result is ambiguous between "the
  test ran and passed", "the test never ran", and "the inputs were never captured."*
  Record which analyses actually executed and under what conditions — a readiness byte for
  your own pipeline.
- **Many simultaneous unrelated open-circuit codes almost always mean one shared power or
  ground path, not many failed parts.** Intersect the affected components' supply and
  ground before working the codes one at a time. The same reasoning applies to a degraded
  sensor ground: it shifts *every* analog channel on it by a small, plausible, correlated
  amount that individually passes every rationality check.
- A stored code names the **symptom's location**, not necessarily the fault's — a
  post-catalyst sensor failure can set an adaptation code naming the pre-catalyst sensor.
- Some diagnostics only make sense under the conditions their enabling logic assumes; a
  scan taken key-on/engine-off will contain codes that are artifacts. Always record engine
  state alongside a fault snapshot.
