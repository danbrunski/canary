# E07 — engine physics model coupled to the ROM-backed ECU twin

Requested 2026-10-04: couple the ECU twin to an engine simulator, validate it
against logged telemetry, and simulate tuning changes by loading complete
ROM images. Follow [E00](E00-second-brain-README.md), [E05](E05-ecu-application-twin.md)
for ECU semantics and [E06](E06-rust-twin-core-trial.md) for the Rust trial.
This document plans the simulator; it does not claim that a plant model or
validated tuning predictor already exists.

## Two models with an explicit interface

The **ECU twin** models the controller: sensor values/state plus an exact ROM
produce injector commands, ignition timing, throttle/cam commands and internal
quantities. The **engine plant** models the physical response: actuator
commands, environment, hardware parameters and physical state produce air
and fuel flow, mixture, pressures, torque and sensor observations. The two
exchange timestamped values through a simulation scheduler. Target AFR is a
controller objective; the plant must calculate delivered mixture from air
and fuel rather than setting measured AFR equal to the target.

Start with a mean-value model (behavior averaged over engine cycles), using
mass balance and bounded physical state plus empirically identified
parameters. Add cylinder-resolved combustion only for a demonstrated need.
The ROM does not supply all plant parameters: injector flow/dead time,
pressure behavior, breathing, friction and spark-to-torque response need
hardware facts or measurements. Engine geometry and other vehicle facts
remain profile data. Keep ECU integer arithmetic distinct from the plant's
continuous equations and numerical solver.

Loading the whole ROM means W08 accepts the complete image, verifies its
identity and resolves all currently modelled code paths/tables/constants.
It does not make undecoded bytes executable. A coverage manifest lists
consumed, inactive-for-this-scenario and unknown/unmodelled regions. A change
whose dependencies reach unknown logic cannot receive a qualified prediction
for the affected outputs. Full SH7058 instruction/peripheral emulation is a
separate possible implementation, not an assumed capability of this plant.

**Current firmware consequence:** Fuel Comp is applied inside BFS; BFS feeds
both Target AFR X and timing-trim X. The chain is therefore approximately
MAF/rpm × K → Fuel Comp → manifold-filling/transient terms and limits → BFS
→ target/timing lookups → final commands. Use the decoded exact operations
and order, not this explanatory shorthand. The throttle-model fill path also
includes learned idle-air correction, purge and throttle-sensor fallback.
See [the updated decode](../../ecu-firmware-analysis.md#fuel-comp-load-input-0xffff118a-and-how-bfs-is-formed).
A Fuel Comp edit cannot be simulated solely as a percentage change in fuel
while holding every downstream ECU quantity fixed.

## Simulation modes and first slice

| Mode | Supplied from logs/scenario | Predicted and checked | Claim allowed |
|---|---|---|---|
| ECU replay | Recorded sensors and declared internal-state checkpoints | ECU intermediates/commands | Reproduction of decoded controller logic |
| Plant replay | Recorded actual actuator inputs, environment and explicit boundary conditions | Physical outputs and sensor response | Plant fidelity under the observed command history |
| Conditional ROM comparison, first slice | Same prescribed rpm/airflow/thermal conditions and initial state for each ROM | Qualified ECU command chain, delivered fuel and steady lambda | Tuning effect conditional on those fixed operating conditions |
| Coupled rollout, later | Driver demand, environment, road/load and initial states | ECU commands and evolving engine/driveline/sensor state | Dynamic tuning effects within the validated envelope |

First qualify warm, steady open-loop fuel response in the existing naturally
aspirated configuration. Use recorded airflow and imposed rpm as explicit
boundary conditions; predict delivered fuel/lambda only when injector command,
flow/dead-time and pressure assumptions are supported. Unknown final injector
logic limits ROM comparison even when plant replay using measured injector
pulse is possible. Retain commanded φ as a separate ECU output/reference.
This slice does not predict new airflow, rpm, torque or acceleration.

Then add throttle/manifold filling, breathing and sensor dynamics, followed
by torque/friction/driveline and qualified spark/cam effects. Cold start,
wall wetting, purge transients, knock, exhaust temperature and boost need
their own state/parameters and validation before extending claims. The
supercharger is a hardware intervention with a new plant parameter set.

## Checklist

- [ ] **E07.1 — Specify controller/plant/scenario contracts.**
  Define typed units, clocks, solver step, ECU update schedule, actuator
  limits, sensor sampling/quantization/delay and state initialization/reset.
  Declare each quantity as measured input, prescribed boundary, inferred
  state or predicted output. Track ECU program/image/slot, plant version,
  hardware configuration, parameter evidence and scenario hash.
  **Accept:** all four modes have unambiguous ownership of every input/state;
  no circular same-step dependency is hidden. Predicted outputs cannot also
  enter as measured inputs except for an explicitly labelled state-estimation
  mode whose scores are separate from open-loop prediction.

- [ ] **E07.2 — Load complete ROMs with effect coverage.**
  Reuse W08 and E05's application manifest for all modelled fuel, timing,
  target-AFR, axes, constants and branch settings. Diff baseline/candidate
  images and trace changed bytes to affected computations and outputs.
  Preserve unknown bytes; do not infer their meaning or treat them as having
  no effect. Simulation edits create isolated candidate artifacts/provenance.
  **Accept:** identical ROMs and states produce identical results; a known
  fuel edit changes the correct path, including BFS/axis consequences.
  Changed unknown dependencies, unsupported program or unresolved active
  slot produce named partial/unevaluable outcomes. Images stay outside Git;
  no simulator path sends an ECU programming request.

- [ ] **E07.3 — Build and identify the first fuel-response plant.**
  Implement air/fuel mass accounting, injector command-to-fuel conversion
  including dead time and supported differential-pressure dependence, and
  the observation model for measured lambda. Reuse B06's acquisition and
  exhaust/sensor-delay evidence without fitting lag separately on test runs.
  Establish which parameters are known, identifiable from available logs or
  require new measurement. Do not absorb MAF bias into injector flow and
  call both independently measured. Freeze a baseline hardware parameter set
  before testing another ROM.
  **Accept:** conservation/unit checks and limiting cases pass; nonempty
  held-out steady regions meet predeclared lambda/fuel error bounds and
  report uncertainty and coverage. Missing pressure/injector evidence makes
  the corresponding effect unresolved. Target AFR is never copied into
  measured lambda to manufacture agreement.

- [ ] **E07.4 — Validate from telemetry and an actual intervention.**
  Inventory raw command, MAF/rpm, lambda, thermal/barometric, voltage and
  available pressure channels with acquisition clocks. Split physical drives
  and interventions before parameter fitting; reserve independent drives
  and post-change outcomes. Compare with `compare_channels` and per-sample
  diagnostics; report steady/transient misses, missing data and observation
  latency separately. Use B08.6's recorded October 4 Fuel Comp prediction
  and exact old/new images as the initial intervention case when coverage
  permits. Preserve its original prediction and account for LTFT relearning.
  **Accept:** fixed parameters reproduce the baseline and predict the
  direction/magnitude of an unseen intervention within declared uncertainty;
  fitting a separate plant to each ROM cannot count as this test. Absent
  evidence stays pending. A baseline fit alone cannot qualify tuning effects.

- [ ] **E07.5 — Add dynamic air, torque and vehicle response in stages.**
  Add manifold mass/energy balance and cylinder breathing, then spark/lambda
  efficiency, friction, rotational inertia and road/driveline load. Separate
  physical actuator response from ECU commands, including cam/throttle
  dynamics. Reuse H07's validated wheel-power/load measurements where
  available; do not calibrate torque against a fuel-derived torque estimate
  built from the same signals. Name required MAP/fuel-pressure measurements,
  controlled spark/cam variation and torque references before claiming
  parameter identifiability. Natural closed-loop logs alone may not excite
  the effects of changing those controls.
  **Accept:** each added subsystem has independent validation and bounded
  applicability; step-size refinement and mass/energy consistency establish
  numerical convergence. Spark edits cannot produce qualified power/knock
  predictions without relevant response data. Prescribed-rpm results are
  never presented as acceleration predictions.

- [ ] **E07.6 — Run paired tuning scenarios without leaking observations.**
  Simulate baseline/candidate ROMs with the same hardware parameters, driver
  scenario, environment and declared initial states. In coupled rollout,
  recompute all downstream state: do not feed recorded post-change BFS,
  trims, airflow or rpm back into predictions of those quantities. Declare
  initialization and reset of learned ECU state, including flash-related
  resets. Return traces/deltas for supported outputs, uncertainty, coverage,
  affected ROM cells and unresolved dependencies. Keep synthetic scenario
  outputs separate from observed evidence and baseline training data.
  **Accept:** a negative-control ROM reproduces the baseline; a known edit
  passes the held-out intervention test; unsupported cells/regimes cannot
  be ranked as a reliable improvement. Optimizing against the simulator is
  deferred until these effect predictions qualify and still requires later
  physical verification.

- [ ] **E07.7 — Integrate the simulator on the Pi through E06/E04.**
  Trial pure Rust controller and plant libraries behind a small scheduler
  interface, with Python owning datasets, fitting/evaluation, ROM loading
  and evidence/UI. ECU arithmetic parity remains bit-exact; continuous plant
  parity uses predeclared solver tolerances and convergence tests. Benchmark
  full coupled runs, not just one lookup. Prefer a simple typed interface;
  consider FMI export only if tool interoperability becomes useful.
  **Accept:** an isolated Pi job compares two ROMs and persists reproducible
  results visible on the phone. Scenario sweeps and fitting run through
  E04's bounded jobs only after collection stops; a lightweight live plant
  observer needs its own measured budget before enablement. The first twin's
  1 ms budget is not an assumed budget for a whole engine simulator. Missing
  resources defer jobs without blocking collection. Rust adoption still
  follows E06's evidence-backed decision.

## Measurement gaps and reuse

The [H07 driving dyno](H07-driving-dyno.md) owns road-load/power validation,
[B08](B08-firmware-informed-behavior.md) owns firmware-informed behavior and
the intervention comparison, and E05 owns decoded controller logic. Reuse
their measurements; none becomes completed merely because the simulator
uses it. Log acquisition rate bounds what fast dynamics can be identified.
Sparse RAM windows cannot establish cycle-by-cycle transient accuracy.

This architecture follows established separation of engine airflow and torque
submodels; structured torque models require spark/lambda efficiency and
friction information beyond controller maps. See the
[MathWorks SI engine model description](https://www.mathworks.com/help/autoblks/ug/spark-ignition-core-engine-breathing-and-torque-production-1.html).
It is an architectural reference, not a MATLAB dependency or evidence that
our vehicle model is calibrated. [FMI](https://fmi-standard.org/docs/3.0.2/)
is an optional future interchange interface for coupled models.
