# Method: getting a trustworthy answer out of a datalog

Most wrong conclusions in engine data are not wrong physics. They are a correct
calculation performed on samples that did not mean what the analyst assumed.

## Before analysis: is this log capable of answering the question?

Ask this first, and say so plainly if the answer is no. It is a better contribution than
a confident answer built on unusable data.

- **Are the required channels present at all?** "The gates filtered everything" and "the
  inputs were never captured" are different answers demanding opposite responses. Some
  capture modes are mutually exclusive — a log configured for enhanced/manufacturer
  parameters may exclude the generic PIDs entirely, so a quantity can be **absent by
  construction**, not by chance.
- **Is the rate adequate for the question?** Roughly:

  | Question | Needed |
  |---|---|
  | Fuel economy, trip summary, slow drift | 1 Hz is fine |
  | Steady-state trims, VE at cruise | 1–5 Hz |
  | WOT pull, boost control, transient fueling | ≥ 10 Hz |
  | Knock retard events, per-cylinder work | as fast as the tool allows; events are milliseconds |

  A ~5 s WOT pull sampled at 1 Hz is five points spanning the entire rpm range. That is not
  a curve.
- **Were the channels sampled together or in rotation?** This is the single biggest hidden
  problem and it gets its own section below.
- **Was the engine in the regime the question is about?** See the gating checklist.

## Sampling rate is not the same as sample age

If channels are polled in rotation (most OBD tools, most loggers), no two channels share a
timestamp and every cross-channel analysis is implicitly an as-of join. That join is where
clean-looking analyses die:

- **Bound the join by sample age, explicitly, and per channel.** Temperatures move on a
  scale of minutes and tolerate 20 s; load, loop status, and purge move in fractions of a
  second and need a few seconds at most. A single uniform bound is either too loose for the
  fast channels or too tight for the slow ones. One real analysis kept **8 of 594** samples
  under a uniform bound and **110** under per-channel bounds.
- **Carry and report the joined sample's age.** A stale value looks exactly like a fresh
  one, and one stale reading re-paired four times looks like four independent measurements.
- **Report the drop count.** If freshness bounding discards two-thirds of the data, that is
  the headline, not a footnote — it means the conclusion rested on almost nothing.
- **Prefer a synchronously-sampled source when one exists.** A logger that puts every
  parameter in one frame needs no join and has no staleness. Results from such a source
  survive when join-based results don't.

## Gate on regime before interpreting any value

A number without its regime is uninterpretable. Establish these first, from channels or by
reconstruction (coolant temp, time since start, throttle rate, rpm):

- **Open vs closed loop** — trims freeze in open loop; λ there says nothing about control health.
- **Decel fuel cut** — no fuel at all, not "lean." Any average containing overrun is garbage.
  Note that a bare "open loop" flag covers *both* power enrichment and DFCO, and on a normal
  drive overrun dominates. Separate them.
- **Power enrichment / WOT** — rich by design.
- **Cold start, after-start, warm-up enrichment** — and whether closed loop has ever been entered.
- **Purge active, secondary air active** — both corrupt fueling data (see `fuel-and-lambda.md`).
- **Transient vs steady state** — gate on |dTPS/dt| ≈ 0 for any steady-state mixture claim.
- **Post-reset relearn** — a battery disconnect or reflash erases adaptation; logs across
  that boundary are not comparable.
- **Limiter or intervention active** — traction control, shift torque reduction, rev limit,
  component protection. Shift retard is not knock retard.

## Bin by rpm × load — and make sure the axis means what you think

Almost every ECU quantity is a function of speed and load plus corrections, so binning by
rpm × load is the correct first move. Two cautions:

- **Verify the control variable actually controls.** "Calculated load" often reads about
  the same at idle and at light cruise — two populations with completely different airflow
  landing in one bin. **For anything airflow- or temperature-related, control on measured
  airflow (MAF) and rpm**, and print the within-bin range of the control variable to prove
  the bin is actually narrow.
- **Never grid on a variable that moved because of the change you are studying.**
  Calculated load is derived from the fuel/airflow math; conditioning on it while fueling
  moves partly conditions on the thing that moved.

## Never compare two signals by their aggregates

This is the most expensive error available in this domain. Comparing per-window means, or
correlating two summary series, produces confident numbers from nothing:

- A byte that averages near mid-scale whatever the engine does will "match" anything else
  near mid-scale.
- Two datasets pooled across rpm bands with different coverage compare *coverage*, not the
  quantity of interest.
- A rate reported as count ÷ wall-clock reads low whenever the window opened early.

**Correlate same-instant values.** Refuse the comparison when there are too few genuinely
paired samples or when one side barely moves — those are exactly the cases where an
aggregate yields a confident number and same-instant values yield nothing. And **r² alone
is not a finding**: over one drive nearly every engine signal correlates with rpm. Check
whether the *scale* is stable across sub-windows, not just whether the fit is tight.

## Treat a wall-clock trend as a hypothesis, not a mechanism

"X rose through the drive" invites a thermal-accumulation story. Check first whether it
tracks **duty cycle** instead. A real example: intake air temperature correlated +0.87 with
time, which looked like heat soak; it was idle fraction. Before attributing a trend to time,
regress it against what the engine was actually doing.

## Reproduce a known answer before trusting a novel one

The strongest check in this domain is nearly free: **two independent instruments for the
same quantity must agree 1:1 once both are in engineering units.** A broadcast-derived
channel against the standard PID, a wideband against the ECU's own λ, MAF-derived airflow
against a speed-density estimate. That single comparison catches a wrong scale, offset,
unit, or endianness without anyone having to state the right answer in advance.

Its measured blind spot: slope is a ratio, so a +4% scale error shows up as only about
−2.9% slope error. Pin exact decode parameters separately rather than relying on the slope
check to catch small scale errors.

## Remember that many "measurements" are models

Calculated load, commanded λ, estimated torque, cylinder air mass, and the ECU's own
"actual" values are computed from other channels through a calibration. They are the ECU's
belief, not an observation, and they inherit every error upstream of them. When a modeled
channel and a measured one disagree, that disagreement *is* the finding — do not
average them.

## What a valid single pull needs

A WOT pull is a controlled experiment. It is comparable to another pull only if you hold
the confounders:

- same gear (loading rate changes everything about heat and transient fueling), full
  throttle held, no shift inside the window
- coolant at operating temperature and **stable**; charge/intake temperature recorded and
  comparable — this is usually the largest uncontrolled variable between two logs
- ambient temperature and barometric pressure recorded; a pull at a different elevation or
  20 °C warmer is a different experiment
- same fuel, same tank where possible
- adaptation settled (not immediately after a reset or reflash)
- **more than one pull.** One pull is an anecdote; the run-to-run spread is what tells you
  whether a difference is real.

If you are planning to vary more than one thing, or want the fewest runs that can separate
several factors, stop and use the `designed-experiments` skill — a designed matrix usually
answers the question in far fewer pulls than a sweep.

## Derived channels worth computing rather than reading

| Derived | From | Why |
|---|---|---|
| Volumetric efficiency | MAP, IAT, rpm, displacement, measured airflow | the actual breathing curve; makes airflow errors visible |
| Cylinder air mass per cycle (mg/stroke) | airflow ÷ (rpm × cylinders ÷ 2) | the true load axis, independent of ECU load conventions |
| Speed-density airflow estimate | MAP, IAT, VE model | cross-checks the MAF; disagreement is the finding |
| Fuel-flow-derived AFR | injector flow × duty vs airflow | independent of the O2 sensor |
| Pressure ratio | manifold absolute ÷ ambient | the boost quantity that governs compressor behavior |
| Intercooler efficiency | (T_compressor_out − T_post_cooler) ÷ (T_compressor_out − T_ambient) | separates heat soak from a boost fault |
| Trim vs load surface | trim binned on airflow | recovers the additive/multiplicative split |
| Spark advance vs rpm × load surface | timing, rpm, load | shows compensation activity against the base map |

## Stating a conclusion

Say which of these you have, and never let the reader guess:

1. **What the data shows** — with the sample count and the regime it was taken in.
2. **What you infer** — the mechanism, and its competing hypotheses.
3. **What would distinguish them** — the specific next measurement, ideally one that makes
   the hypotheses predict *opposite* results.
4. **What the data cannot answer** — and whether that is because the drive lacked the
   regime or because the channel was never captured.
