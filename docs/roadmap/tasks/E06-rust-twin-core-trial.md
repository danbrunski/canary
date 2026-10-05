# E06 — evaluate a Rust execution core for the ECU twin

Recorded 2026-10-04 from the operator discussion. Follow
[E00](E00-second-brain-README.md) and the roadmap's
[language boundary](../ecu-second-brain.md#rust-core-option-and-python-boundary).
Rust is a candidate for the deterministic ECU computation core. Adoption
depends on parity, measured Pi benefit and deployment cost; this checklist
does not select Rust as the default or require a second-brain rewrite.

The first lookup may already fit the Python budget. Establish its pure
computation boundary and preserve the Python reference now; evaluate Rust
as connected arithmetic/state paths grow. Required E02–E05 functionality
can ship in Python while this trial remains open or deferred.

[E07](E07-engine-plant-simulator.md) extends the experiment to a coupled
engine physics model. Keep the ECU and plant kernels separate: ECU arithmetic
requires exact integer/state parity, while the continuous plant needs
declared numerical tolerances and solver-convergence checks. Include their
scheduler and boundary costs in the Pi benchmark; a lookup speedup alone
does not establish feasible coupled-simulation throughput.

## Boundary

| Candidate Rust core | Python integration |
|---|---|
| Fixed-width arithmetic, table search/interpolation, clamps and masks | ROM Library access, profile/manifest loading and identity resolution |
| Decoded branches, bounded ECU state and transitions | Acquisition-time pairing, channel decoding/adapters and session readers |
| Predicted intermediates/commands, state snapshots and computation refusal reasons | Overall eligibility, residual accounting, evidence, investigations, behavior-model tooling and APIs/dashboard |

Pass typed input tuples, resolved immutable tables/constants and explicit
state into the core. Return typed predictions, next state, branch/trace and
validity information. Keep firmware semantics in one computation contract;
Python supplies paired observations and outer applicability without copying
the ECU calculation. Vehicle constants remain profile/ROM data. The core
does no bus, disk or network I/O, and has no dependency on Python objects.

## Checklist

- [ ] **E06.1 — Define the language-independent computation contract.**
  Inventory the existing E02/E05 numerical kernels and establish explicit
  inputs, outputs, bounded state, initialization/reset and snapshot version.
  Declare integer widths/signedness, casts, shift/division/rounding behavior,
  overflow, saturation and invalid-input rules for each decoded operation.
  Preserve the current Python implementation as the executable reference
  and retain its firmware-derived known-answer vectors.
  **Accept:** the Python kernel runs from supplied values/tables with no
  session, daemon or storage dependencies; live and stored adapters use the
  same contract. Unresolved arithmetic/state remains a named blocker rather
  than becoming a language default. Existing numerical results are preserved.

- [ ] **E06.2 — Trial a small Rust library against the reference.**
  Implement one decoded path with explicit wrapping/saturating/checked
  operations where the firmware requires them; do not rely on differing
  debug/release overflow defaults. Start with the qualified lookup arithmetic
  and add an identified stateful path when its semantics are known. Run the
  same immutable known-answer vectors and generated edge cases against both
  implementations: axis edges, clamps, rounding, integer boundaries,
  branches, sentinels, reset and missing history. Keep prediction and target
  observations separate, including aliases.
  **Accept:** bit-exact integer outputs and matching state transitions,
  computation reasons and branch traces in debug and release builds. Python
  parity alone is not ECU validation: independent firmware/capture vectors
  and wrong-table/branch controls must still pass E02/E05's requirements.
  No tolerance is widened to accommodate the port.

- [ ] **E06.3 — Measure the full Python-to-Rust path on the Pi.**
  Trial a thin PyO3 binding with one call per complete tuple or bounded
  batch, instead of crossing languages for every arithmetic operation.
  Rust-only work can detach from the interpreter where beneficial. Preserve
  E04's bounded worker handoff: releasing the GIL does not make a synchronous
  call nonblocking or isolate crashes. Use owned immutable inputs and one
  explicit state owner. Measure conversion/copy cost, attach/detach overhead,
  computation and return handling separately and together.
  **Accept:** paired Python/Rust runs on the Pi report identical outputs,
  p50/p95/p99/max latency, incremental RSS, startup/import cost, replay
  throughput and missed ticks/collector loss under representative collection
  load. Include tiny calls and worst supported batches; record Pi/compiler/
  Python/PyO3 versions and build flags. Meet E04.3's first-slice limits
  (≤1 ms p99, ≤5 ms max per tick, ≤1 MiB table/state) where applicable,
  reporting extension/runtime overhead separately and total impact as well.
  Laptop microbenchmarks or Rust-only timings cannot close this item.

- [ ] **E06.4 — Record the adoption decision and deployment implications.**
  Compare the measured headroom, arithmetic clarity and replay benefit with
  maintenance, Python/native conversion and ARM64 packaging cost. Decide
  adopt, defer or reject with evidence. If adopted, pin the Rust/binding/build
  toolchain, produce reproducible artifacts for the supported Pi and dev
  environments, integrate parity checks into the existing gate, and select
  the backend explicitly. Record backend/build identity in twin provenance.
  Preserve the Python reference for verification; a fallback must be visible
  and meet the same resource/applicability contract, never activate silently
  inside the collector. Test installation and rollback on the Pi.
  **Accept:** the decision cites E06.2/E06.3 results. Adoption additionally
  has a reproducible installed artifact, named backend/version, parity gates
  and E04 collection-isolation evidence. A defer/reject decision names its
  measured reason and revisit trigger; conditional Rust rollout work is then
  explicitly inapplicable, and qualified Python operation continues.

## Sources and limits

The [Rust Reference](https://doc.rust-lang.org/reference/behavior-not-considered-unsafe.html)
documents explicit wrapping arithmetic and the distinction from implicit
overflow. The [PyO3 performance guide](https://pyo3.rs/main/performance.html)
describes conversion/boundary costs and detaching for Rust-only work.
Recheck the chosen versions when implementing the trial.

Neither language resolves missing ECU logic, observation timing or sparse
capture coverage. Rust does not by itself establish real-time scheduling,
firmware fidelity or a speedup for this workload. Those claims require the
same observation and Pi measurements as the Python path.
