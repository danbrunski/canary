# Torque structure, electronic throttle, and "down on power with no codes"

Applies to essentially every drive-by-wire ECU (roughly 2000+). If the engine has
a cable throttle, skip to the last section.

## The structural change

Older ECUs mapped pedal → throttle → fuel → spark as a chain of local setpoints.
A torque-structured ECU converts **every** request — internal or external — into a
**torque demand**, arbitrates the demands against each other, and only then decides
which actuators to move.

Demand sources: driver pedal, cruise control, idle speed control, transmission
(shift torque reduction), traction/stability control, A/C and alternator load,
catalyst heating, rev limiter, and every component-protection limit.

**Consequence: pedal ≠ throttle ≠ airflow ≠ torque.** Each link is a controlled,
arbitrated, *limitable* transform. Reading throttle position as "driver demand" is
a category error on these systems — steady pedal with a moving plate is normal
(load compensation, idle control, software dashpot, emissions hold-open), and a
wide-open pedal does not imply a wide-open plate.

## The two pathways (this is the load-bearing idea)

| Path | Actuators | Speed | Direction | Used for |
|---|---|---|---|---|
| **Charge-air path** | throttle angle, wastegate | slow (manifold filling, turbo inertia) | up and down | **long-term** demands; "primarily used for a required torque increase" |
| **Crank-synchronous path** | ignition timing, injection timing/duration | fast (within a cycle) | essentially **down only** | **short-term** demands; "usually having a torque reducing effect" |

Spark and fuel can't *add* meaningful torque — spark is already at or near optimum,
and advancing past MBT costs torque and invites knock. So anything that must happen
*now* (knock retard, traction control, shift torque cut, rev limit, driveline
anti-jerk, over-temperature protection) lands on spark/fuel; anything that is a
steady-state *level* (cruise, idle load compensation, boost target) lands on air.

The two coordinate: a fast retard is typically **backfilled** by an air-path move so
the retard can be washed out. In a log that reads as a transient spark deficit paired
with a slower throttle/boost correction — one event, not two.

## Reading it: the requested-vs-actual chain

On a torque-structured ECU the diagnostic unit is a **pair**, not a value. Walk the
chain in order and find the first link that loses ground:

1. pedal → **requested torque** (driver demand)
2. requested torque → **torque after arbitration** ← *the limiters live here*
3. limited torque → **requested throttle angle / requested boost**
4. requested throttle → **actual throttle** (actuator/mechanical health)
5. requested boost → **actual boost** (boost control health)
6. requested spark → **actual spark** (knock/torque-intervention withdrawal)

**Torque limiters cap output silently.** Nothing illuminates when arbitration chooses
less torque than the driver asked for — that is the system working as designed. "Feels
down on power, no codes" is the *normal presentation* of an active limiter: component
protection, boost limit at altitude, catalyst protection, gear-dependent torque limit,
clutch/driveline limit, over-temperature. You find it only by logging the
requested/actual torque pair, or the manufacturer's torque-limitation-reason channel.

**Full pedal is not full throttle, and the fuel target can follow the throttle.** A
gear-dependent torque limit (or any limiter that trims the throttle request) holds the plate
short of wide open at a pedal that would otherwise open it. Some ECUs select their
power-enrichment target on *target throttle*, not pedal, so the same full-pedal pull is
fueled by a different target column in a capped gear than in an uncapped one, and its timing
and airflow differ too. Sort full-pedal λ, timing and airflow **by gear** (rpm ÷ road speed
identifies the gear without a gear channel) and read them against **throttle**, not pedal.
Pooling gears pools two different calibrations. On a vehicle without the torque PIDs, pedal
versus throttle is the requested-versus-actual pair that is available, and a pedal-minus-
throttle gap at full pedal is the limiter showing itself. A limiter is a property of the
calibration revision, so resolve which calibration was in the ECU before comparing pulls.

**Tuning corollary:** changing fuel or spark maps without the torque model agreeing
produces arbitration fights — the ECU re-derives actual torque from its air model and
pulls throttle or boost back to hit the *modeled* target. Torque-model and limiter
tables are part of the calibration, not background.

**What generic OBD-II gives you.** Newer vehicles support a small torque set — driver's
demand engine torque (PID 0x61) and actual engine torque (0x62), both as a percentage of
the reference torque in 0x63, scaled `A − 125`; plus 0x64 for the percent-torque breakdown.
Where those exist, the demand-vs-actual pair alone answers most "down on power" questions.

Everything else — boost target, cam angle, per-cylinder retard, adaptation values, limiter
reason flags — lives in the manufacturer protocol (UDS DIDs / measuring blocks), not the
mandated subset. If someone is diagnosing a torque-structured engine on generic PIDs alone,
say so early and say which channel would settle it.

## Electronic throttle: redundancy, plausibility, limp

- Throttle drive = electric motor through reduction gears. **Two angle sensors with
  deliberately opposite characteristics** (one rises as the other falls) so a
  common-mode fault can't produce two agreeing wrong readings.
- **Two pedal sensors, linear to each other on different scales** — same shape,
  different gain/offset, so plausibility is a ratio test and a single-track failure is
  unambiguous. Kickdown is derived from exceeding the pedal sensor's normal voltage
  limit, not from a switch, and is broadcast to the transmission over CAN.
- An independent **monitoring level** inside the ECU recomputes permissible torque from
  the pedal and can force limp if the main path disagrees (the E-Gas three-level
  concept). Modern implementations use contactless Hall/inductive sensors with digital
  CRC-protected interfaces, but the principle is unchanged.
- **Failure behavior:** one pedal sensor lost → run on the redundant one; both lost →
  emergency mode. Throttle-drive failure → emergency mode, **no substitute function
  exists**. Many makes light a separate power-control lamp rather than the MIL.
- A **load-signal** fault (e.g. MAF) can light the throttle/power lamp, because the ECU
  uses load as the reference for plausibility-checking other inputs. A bad load signal
  cascades into apparently unrelated implausibility faults — chase the load signal
  first when several implausibility codes appear together.

## Adaptation is volatile — treat resets as change points

Idle is controlled by moving the throttle plate itself; the ECU learns the plate
position that produces target idle, and compensates for aging, wear, and small false-air
leaks by moving that learned value. There is no external idle adjustment.

**Battery disconnect, power interruption, or a reflash erases learned throttle and fuel
adaptations** (coding survives; adaptation does not). Post-event logs are not comparable
to pre-event logs until re-adaptation completes, and driveability is genuinely worse in
the interim without anything being broken. Before attributing a step change in trims,
idle, or timing to hardware, **look for a reset event**.

"Adaptation limits exceeded" is the canonical *"the mechanical thing has drifted past
what learning can absorb"* fault — dirty throttle bore, air leak, worn gears.

## Cable-throttle engines

Without a torque structure, pedal *is* throttle and the chain above collapses. What
survives: idle air control position is still a load-compensation effort (it rises with
A/C, alternator load, in-gear, and with throttle-body fouling or a developing leak),
spark is still the fast torque path, and every "requested vs actual" pair you can find
still beats any absolute value.
