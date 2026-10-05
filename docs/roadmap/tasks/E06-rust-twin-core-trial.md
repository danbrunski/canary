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

- [x] **E06.1 — Define the language-independent computation contract.**
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
  *Done 2026-10-05 (laptop).* Contract v1 is
  [`data/ecu_twin_contract.yaml`](../../../data/ecu_twin_contract.yaml),
  explained in [the contract page](../../ecu-twin-computation-contract.md):
  12 operations, each with widths, signedness, casts, shifts, truncating
  divisions, saturations and refusals; `uprev_state` with init, reset and
  snapshot version 1; an `inventory` of the other E02/E05 kernels, each with
  the reason it is not yet an operation; and 7 named blockers
  (`final_command_path`, `enrichment_gate_latch`, `ram_copy_identity`,
  `bfs_saturation`, `uprev_indicator_meaning`, `enrichment_tick_and_cycle`,
  `coolant_blend_divide`). The listing resolves the eighth,
  `interpolation_compare_signedness`: `0x026F10` uses `cmp/hs`, callers
  zero-extend with `extu.w`/`extu.b`, the axis searches use `cmp/hi`, and
  `0x027448` is `div0u`. Five hand vectors pin it with operands ≥ 0x8000;
  a signed reading misses all four interpolation pins. `tests/test_ecu_twin_contract.py` (13 tests)
  checks the schema, that every Python reference resolves and every Rust
  symbol exists, and that versions agree. Kernel extracted:
  `src/decode/target_phi_command.py` (`target_command`, `x_from_bfs`), which
  `target_phi.predict` and `x_from_bfs` now call. A fresh-interpreter test
  shows `ecm_lookup`, `target_phi_command` and `fuel_comp_load` import only
  each other and the stdlib: no session, daemon, storage, pandas or pydantic.
  A spy test shows `evaluate_session` and the live twin (`LiveTwin` →
  `TwinStream`) make the identical `target_command` call. The vectors are in
  `tests/fixtures/ecu_twin/{lookup,state}_vectors.json`, built by
  `tests/ecu_twin_vectors.py`; the test fails if a committed file differs
  from what the reference builds. Lookup: 4,160 vectors (96 hand/capture
  known answers with 129 pinned values, 8 wrong-table/decode/branch
  controls, 4,056 generated, 42 refusals). State: 1,242 vectors (12 hand,
  28 pinned values). Existing results were preserved:
  `test_ecm_lookup`, `test_target_phi`, `test_fuel_comp_load` and
  `test_twin_live` passed unchanged after the refactor (175 passed).
  Stricter refusals, each also a contract rule: `lookup_2d` refuses
  coordinates outside 0..0xFFFF; `target_command` refuses `flag_bit` > 31,
  load widths other than 8/16 and shifts > 16; `x_from_bfs` refuses shifts
  > 16; `fuel_comp_load.load_x` checks the purge word on the saturated
  branch too. Raw tuple inputs are never truncated: `target_command` refuses
  a fractional or out-of-word rpm/load and a flag outside 0..0xFFFFFFFF
  (vectors include rpm 236.5, flag −1 and flag 2**32), and `x_from_bfs`
  refuses a fractional or negative BFS and a fractional scaler.

- [x] **E06.2 — Trial a small Rust library against the reference.**
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
  *Done 2026-10-05 (laptop, rustc/cargo 1.93.1, x86_64).* `rust/` is a
  workspace. `ecu_twin_core` is `no_std` + `alloc` with
  `forbid(unsafe_code)`, no dependencies and no I/O. It implements all 12
  contract operations: the lookup family, `target_command` (branch, gate band
  reason, cell trace), `x_from_bfs`, the `0xFFFF118A` chain, and the UpRev
  selection state as the stateful path (`uprev_power_on`, `uprev_step`,
  `loaded_slot`; nullable state, named unknown-state reasons). Overflow
  handling is always explicit: widening `u32::from`, `min` saturation,
  `wrapping_add` for the selector byte, `truncate_u32` for the history, and
  `checked_*`/`try_from` on proven invariants. No profile override is used.
  `cargo test` and `cargo test --release` both pass 6/6. Each runs the
  4,160 lookup and 1,242 state vectors bit for bit (outputs, refusal kinds,
  reasons, cell traces, state transitions), checks that the 8 controls
  still miss, and adds Rust-side generated cases: every Q16 fraction against
  an independent i128 formula, the full u16 range on 5 axes, every rpm word
  0..1200 × every load byte on Map 1, and every input byte from 144 known
  UpRev states. Mutation check: rounding interpolation to nearest failed 645
  lookup vectors, and a saturating selector failed 11 state vectors.
  `ecu_twin_py` is a thin optional PyO3 0.27 binding (abi3-py312):
  `TargetPhiTable` and `LoadCurves` are validated once, with per-tuple calls
  and a bounded `word_batch` (≤ 4096) that detaches the interpreter.
  `tests/test_ecu_twin_core_rust.py` skips by name without it. With the
  wheel built into a scratch directory (never installed into `.venv`), it
  passed 7/7 against both a release and a debug wheel: all vectors through
  the binding, plus seeded differentials of 20,000 random table/tuple pairs,
  10,000 load chains and 20,000 random UpRev cycles. Prediction and
  observation stay separate: the kernels take only lookup inputs, and
  observed words appear only as pinned answers and `must_differ` controls.
  The state path's vectors and tests read E05.3's `ecu_state_models.py`
  (committed in `0beea7c`, MODEL_VERSION 1).

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
