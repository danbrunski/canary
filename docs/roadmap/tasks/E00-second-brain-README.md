# E — second brain and deterministic ECM twin

Created 2026-10-04 from [ECU second brain](../ecu-second-brain.md), including
the [first twin slice](../ecu-second-brain.md#first-ecm-twin-slice-target-equivalence-ratio).
These checklists specify future implementation; creating them does not
implement the brain or add work to `OVERNIGHT-QUEUE.md`.

**Runtime decision (operator, 2026-10-04): the second brain runs on the Pi.**
The Pi performs live prediction, discrepancy reasoning, hypothesis updates,
measurement selection and candidate learning. It owns persistent memory and
performs evaluation, outcome recording and promotion after collection stops.
A phone displaying the Pi's dashboard is sufficient. A laptop is optional
for development, firmware research and accelerated analysis; normal operation
must not require it, a remote service, internet access or a language model.

**Twin objective (operator, 2026-10-04): reproduce decoded ECU application
logic on the Pi to predict and verify observations.** The twin grows from
E02's conditional target-φ calculation into E05's connected computations and
state transitions. Its coverage manifest names each reproduced code path,
observed dependency and unresolved behavior. The second brain uses those
predictions and residuals as evidence for investigations and learning.

## Checklists and order

| File | Deliverable | Dependencies | Hardware acceptance |
|---|---|---|---|
| [E01 — evidence and memory](archive/E01-evidence-and-memory.md) (done, archived) | Required records, evidence ids, intervention history and template cards | Existing state stores and card contracts | Persistence/restart demonstration in E04 |
| [E02 — target-φ twin](E02-target-phi-twin.md) | ROM-bound command prediction with qualified residuals | W08 image/table data; E01 for persisted evidence | RAM-table identity, missing captures, Pi budget in E04 |
| [E03 — investigations and learning](E03-investigations-and-learning.md) | Fuel-delivery investigation, hypothesis/measurement selection and isolated candidate learning | E01; existing B06 delivery analysis; qualified E02 output when available | Physical measurements and independent drives where missing |
| [E04 — Pi runtime and acceptance](E04-pi-runtime-and-acceptance.md) | Streaming integration, idle-time jobs, HTTP/MCP/dashboard and standalone demonstration | E01 first; E02/E03 integrated incrementally | Required Pi measurements and Pi-plus-phone demonstration |
| [E05 — ECU application twin](E05-ecu-application-twin.md) | Application dependency model, upstream fuelling, state transitions and later outputs | Firmware findings, W08 and E02; E04 for each deployed increment | Missing regimes, map switches and per-output verification |
| [E06 — Rust core trial](E06-rust-twin-core-trial.md) | Language boundary, Python/reference parity, native-core experiment and adoption decision | Existing E02/E05 kernels and known answers; optional trial | Complete Python-to-Rust Pi benchmark and packaging check if adopted |
| [E07 — engine plant simulator](E07-engine-plant-simulator.md) | Coupled ECU/engine model, whole-ROM effect coverage and logged-data validation of tuning scenarios | E05 controller, W08 images, B08 intervention evidence; E06 language trial | Plant parameter identification, independent intervention/torque evidence and Pi job benchmark |
| [E09 — one engine twin](E09-engine-twin-runner-and-levers.md) | One runner over the node graph serving both the live twin and the ROM editor; the capture that makes the twin scorable; the levers (timing, MAF curve, injector constants, closed loop) the editor cannot reach today | E05 manifest and kernels, E07 plant and paired rules, E08 workbench; the firmware listing for four items | The `twin` capture drive, post-flash scoring, the boosted plant on the new hardware |

Start E01's contracts and E02's fixture/ROM audit. Establish the E04 runtime
and persistence seams early, then integrate the twin and E03's first
investigation. The fuel-delivery evidence/memory loop can use existing
analyses before the twin qualifies. Later hypothesis selection and adaptive
learning do not block shipping a qualified command readout.

E05.1's application inventory can start alongside E02; its wider executable
paths follow the first qualified slice. Each added path repeats E04's Pi
validation instead of assuming the first slice's budget covers the expansion.

E06 establishes a pure computation boundary and preserves the Python
reference. A Rust execution core is a measured option for growing arithmetic
and state workloads; it does not block E02–E05 or move evidence, behavior
tooling and the dashboard out of Python. Adoption needs exact parity and a
Pi benchmark including boundary/conversion costs, not just a faster lookup.

E07 adds the physical engine model needed to predict a changed command's
effect. Begin with conditional steady fuel/lambda predictions under declared
rpm/airflow boundaries, then qualify dynamic air and torque/driveline models.
Loading a complete ROM preserves its identity and unknown bytes; only covered
code paths and independently validated plant responses support tuning claims.

Each numbered checkbox below these linked documents is task-level truth.
The parent roadmap's Part A is a summary: check a summary only when all its
linked tasks meet their own acceptance. Use `[~]` for partial completion,
name the remaining validation, and attach evidence before marking `[x]`.
Code finished without the required Pi measurement is partial. Hardware work
is blocked overnight under the queue's existing rules, not removed from
acceptance.

## Shared contracts

- **Collection never waits for the brain.** Live work consumes bounded new
  samples/state. Whole-session reads and heavier evaluation wait until
  collection is inactive; a parked vehicle may still be collecting.
- **Unevaluable is not normal.** Missing, stale, sentinel, unknown-context,
  uncovered and unvalidated results have named reasons and no qualified
  residual. A zero residual does not establish vehicle health.
- **Evidence is minted by tools.** All displayed numerical claims resolve to
  evaluator-produced records with conditions, versions, source/time and
  provenance. Historical prose stays narrative until re-evaluated.
- **Pair on acquisition time.** Use `compare_channels` for raw signal
  comparisons, its pairing primitives for the twin adapter, and per-sample
  ratios where meaningful. No aggregate-based identity claims. Matched
  operating-condition bins are for intervention evaluation, not a substitute
  for pairing raw channels.
- **Reuse vehicle-scoped state.** Profiles own identities, channel maps,
  gates and calibration definitions. Runtime records live under
  `/data/canary/` through `state_dir`; no runtime writer edits tracked files.
  Reuse `session_started_at`/`latest_session` and existing replay isolation.
- **The twin and brain do not write the ECU.** Diagnostics elsewhere retain
  the project policy. Any separate non-volatile write requires explicit
  permission for that specific operation and a fresh `ignition_only` verdict
  (key on, engine off, stationary); engine-running/unknown states refuse.
- **One implementation serves live and stored data.** Put numerical/state
  kernels at a layer usable by both, following `tests/test_layering.py`.
  A new package needs a layer; function-local imports do not evade the rule.
- **Validate the change at its owner.** Use targeted tests while iterating,
  the current AGENTS.md push gate for implementation, and dashboard checks
  for UI work. Do not duplicate already-completed implementation tasks.

## Existing ownership

| Existing work | E-series responsibility |
|---|---|
| [W08 ROM Library](archive/W08-rom-library.md), [W05 trace coverage](archive/W05-rom-maps-trace-coverage.md) | Consume exact images, table definitions and verified axis mappings; close a remaining prerequisite there with linked evidence. Do not build another ROM store. |
| [B06 delivery error](B06-lambda-delivery-error.md) | Consume its command-versus-measured residual and eligibility; the twin predicts the command itself. A twin residual cannot replace a delivery residual. |
| [B08 firmware-informed behavior](B08-firmware-informed-behavior.md) | Consume qualified models using actual Fuel Comp/timing context and the throttle-fill/MAF comparison. Keep ECU-computation, engine-response and historical residuals distinct; B08 owns candidate evaluation, E05 owns decoded calculations. |
| [B03 lifecycle](B03-behavior-lifecycle-and-refit.md), [online training](../online-training-strategy.md) | Reuse disposition, held-out validation, reference retirement and promotion. E03 adds the investigation/candidate connections. |
| [A05 plausibility](A05-plausibility-checks.md) | Consume qualified existing checks; E-series does not rebuild or declare them completed. |
| [LLM-assist S.5](../llm-assist-and-compute-roadmap.md#implementation-checklist--seams-only-sequencing-step-1) | E01 owns the shared resolvable-evidence implementation; update S.5 only when its own acceptance passes. |
| [N01 experiments](N01-na-rom-response-experiments.md), [supercharger plan](../supercharger-build.md) | E03 records predictions/outcomes and unresolved history; it does not initiate flashes or fabricate missing pre-build evidence. |
| [S03 state](archive/S03-state-out-of-checkout.md), [D03 open work](D03-open-work.md) | Reuse runtime paths, bounded analysis jobs and capture/restart rules. |

The optional language interface and distillation remain lower-priority work
in the parent roadmap's Part B and LLM-assist plan. Their host choices do not
move any required brain function off the Pi and do not gate E01–E05.
