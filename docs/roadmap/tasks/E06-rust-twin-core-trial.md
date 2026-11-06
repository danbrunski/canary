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
  reason, cell trace), `x_from_bfs`, the `0xFFFF118A` chain, and the map-slot
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
  map-slot states. Mutation check: rounding interpolation to nearest failed 645
  lookup vectors, and a saturating selector failed 11 state vectors.
  `ecu_twin_py` is a thin optional PyO3 0.27 binding (abi3-py312):
  `TargetPhiTable` and `LoadCurves` are validated once, with per-tuple calls
  and a bounded `word_batch` (≤ 4096) that detaches the interpreter.
  `tests/test_ecu_twin_core_rust.py` skips by name without it. With the
  wheel built into a scratch directory (never installed into `.venv`), it
  passed 7/7 against both a release and a debug wheel: all vectors through
  the binding, plus seeded differentials of 20,000 random table/tuple pairs,
  10,000 load chains and 20,000 random map-slot cycles. Prediction and
  observation stay separate: the kernels take only lookup inputs, and
  observed words appear only as pinned answers and `must_differ` controls.
  The state path's vectors and tests read E05.3's `ecu_state_models.py`
  (committed in `0beea7c`, MODEL_VERSION 1).

- [~] **E06.3 — Measure the full Python-to-Rust path on the Pi.**
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
  - *Harness, 2026-10-08 (laptop; uncommitted at writing).* The binding
    gained five probes, so that each part of a crossing is timed alone:
    `bench_noop` (bare call), `bench_detach` (detach/reattach with no work),
    `TargetPhiTable.bench_convert` (extract and narrow a bounded batch, no
    compute), `bench_compute_ns` (Rust-only compute, timed inside Rust with the
    interpreter detached) and `bench_return_words` (return a list shaped like
    `word_batch`'s). `word_batch` now shares its conversion with the probes,
    and its behaviour is unchanged. `tools/bench_ecu_twin.py` runs the paired
    Python/Rust run and writes one JSON:
    - parity: all 4,160 + 1,242 vectors, the real lookup inputs of the
      October 4 capture, and seeded random tuples over the real ZS44A-20260920
      table. Each side's outputs are hashed with sha256 over canonical JSON,
      covering the full trace, `word` and `word_batch`.
    - latency: p50/p95/p99/max for one-tuple calls, batches of 1/16/256/4096,
      and the four parts.
    - throughput.
    - fresh-interpreter import time and incremental RSS.
    - the E04.3 verdict.

    `scripts/e06_pi_bench.sh` is the Pi run. It refuses the daemon's
    checkout as its working copy. It refuses any `vehicle_state` other than
    `parked`/`bench`/`engine_off`, and an active capture, before the run; it
    reads them again afterwards and exits 3, marking the run invalid, if the
    state left idle. It builds clean under `nice -n 19` and records
    rustc/cargo/maturin/PyO3/Python, flags, build seconds and wheel size. It
    unpacks the wheel into its own `bench-venv`, which sees the daemon venv
    read-only through a `.pth`, so it never installs into the daemon venv.
    `tests/test_bench_ecu_twin.py` has 4 tests that always run and 2 that skip
    by name without the extension. `tests/test_ecu_twin_core_rust.py` now uses
    the tool's `ext_run`, so the gate and the Pi run share one runner.
  - *Real tuples, measured:* the October 4 capture holds 563 real
    `(rpm, load, flag)` raw tuples, one per input read with the latest read of
    the other two: 291 distinct, 6 on the override branch, 1 in the gate band.
    The full stored session
    (`~/canary-data/.../_capture-ram-study-2026-10-04T204557`) gives the same
    563, with the same digest as the committed fixture. **The stored twin
    hands its kernel none of them** (a spy on `target_command` counted
    `twin_kernel_calls: 0`): all 174 candidates are unevaluable
    (`input_stale:load` 120, `sentinel` 21, `input_stale:rpm` 18,
    `map_identity_unbracketed` 15). E04.3's Pi replay measured `twin_eval`
    p95 0.536 ms on this capture. By E06.1's spy test (the live path makes the
    stored path's kernel calls), that time was all spent outside the kernel
    boundary Rust would replace. This is an inference, not measured in the
    daemon.
  - *Laptop dry run, 2026-10-08* (x86_64 WSL2, Python 3.12.3,
    rustc/cargo 1.93.1, maturin 1.3.2, PyO3 0.27.2, release, no RUSTFLAGS,
    abi3-py312). This does not close the item. The script ran end to end
    against a stub `/status` (`bench`):
    - Build: clean in 28.3 s; wheel 305,010 B; `.so` 747,936 B.
    - Parity: identical. Lookup 4,160 and state 1,242 vectors with 0
      mismatches (sha256 `8f0145d1…`, `f2c0c111…` on both sides); real 563
      (`d575ec11…`); random 20,000 (`52ac1d1c…`).

    Latency in µs (p50 / p99 / max):

    | Call | p50 | p99 | max |
    |---|---|---|---|
    | Python `target_command` | 13.8 | 50.7 | 8,034 |
    | Python `predict` | 18.1 | 105.8 | 1,693 |
    | Rust `word` | 0.77 | 2.18 | 129 |
    | Rust `command` (dict trace) | 3.5 | 26.5 | 258 |
    | `uprev_step`, Python | 6.9 | 55.6 | 220 |
    | `uprev_step`, Rust | 1.6 | 6.2 | 218 |
    | `word_batch(4096)` total | 703 | 2,224 | 2,292 |
    | `word_batch(4096)`: convert | 285 | – | – |
    | `word_batch(4096)`: detach | 0.2 | – | – |
    | `word_batch(4096)`: compute | 270 | – | – |
    | `word_batch(4096)`: return | 157 | – | – |
    | Python loop of 4096 | 70,067 | 83,000 | 83,000 |

    The four `word_batch(4096)` parts sum to 712 at p50. At 4096 tuples,
    conversion plus return cost about as much as the compute.

    Other laptop figures:
    - Throughput in tuples/s: Python 61,950; Rust `word` 3.2 M; `command`
      335 k; `word_batch` 6.4 M.
    - Import adds 3.6 ms in a fresh interpreter. The Python reference
      (`src.decode.target_phi_command`, with the `src` package) adds 47.7 ms.
    - RSS: import plus table +384 KiB. The table's own footprint is 160 B.
      1,000 batches of 4,096 add 0 KiB after the first 500.
    - Against E04.3: one-tuple calls are far inside ≤1 ms p99 / ≤5 ms max on
      both backends. The 4,096-tuple worst batch misses p99 in Rust (2.2 ms)
      and both limits in Python (83 ms).
  - **Pi run, done 2026-10-08** (Pi 5, Debian 13 aarch64, parked, no
    capture, state re-read after the run: still parked). Ran from a
    `git archive 870870a` copy in `~/e06-work`, never the daemon checkout or
    venv. Raw results: `data/analyses/2026-10-08-e06-3-pi-bench.json` and
    `…-pi-build.json`.
    - Toolchain (operator approved the install 2026-10-08): rustup, rustc
      and cargo 1.93.1 (same as the laptop) in `~canary/.cargo`; maturin
      1.15.0 in `~/e06-build-venv`; PyO3 0.27.2 (abi3-py312); daemon
      Python 3.13.5. `nice -n 19` clean release build 21.3 s; wheel
      `canary_ecu_twin-0.1.0-cp312-abi3-manylinux_2_34_aarch64.whl`
      306,479 B, `.so` 761,920 B; no RUSTFLAGS, workspace release defaults.
    - Parity on the Pi: identical on every set (contract vectors, the
      committed real fixture, the stored Oct 4 session, 20,000 random tuples
      over the real table); 0 mismatches.
    - Latency (µs, p50 / p99 / max): Rust `word` 1 tuple p99 0.61, max 17.9;
      Rust `command` (full trace) p99 3.5, max 19.5; Python `predict` (today's
      path) p99 13.1, max 82.6. Batches of 4,096: Rust `word_batch` 581 /
      604 / 613 (convert 253, compute 210, return 110 at p50); a Python loop
      35,227 / 36,172 / 36,172.
    - Throughput (tuples/s): Python `target_command` 117 k; Rust `word`
      4.8 M, `command` 334 k, `word_batch` 7.1 M.
    - Startup and memory: the extension adds 2.8 ms to a fresh interpreter
      (the Python reference path adds 34.6 ms). RSS: import plus table
      +576 KiB; table footprint 160 B; 0 KiB growth from 500 to 1,000
      batches of 4,096.
    - Against E04.3: every Rust case, including the worst supported batch,
      is inside ≤1 ms p99 and ≤5 ms max; state is far under 1 MiB. Python
      single-tuple calls also pass; the Python 4,096 loop fails both.
    - This does not close E06.3: the under-load arms below are still owed,
      and the stored twin path hands the kernel no tuple today (all 174 Oct
      4 candidates unevaluable), so these timings bound a kernel the live
      twin does not yet reach.
  - *Owed under load (protocol):* missed ticks and collector loss under
    representative collection load. Two things gate it:
    - **The twin must actually call its kernel.** It needs a capture whose
      profile reads all four `back_to_back` windows of
      `target_phi_twin.yaml` (`fuel_target`, `fuel_axis`, `spark`,
      `flags_2841`) within `max_span_s` 0.25 s. The repo `tuning` profile
      lacks the X trace and the pedal flags, and the October 4 ram-study
      reads were over a second apart. A run without them measures a Rust arm
      that never runs.
    - **The extension must be importable in the daemon, behind an explicit
      backend switch.** That switch is code owed in `src/daemon/twin_live.py`.
      Placing the wheel into the daemon venv is an operator decision.

    Then, on the truck, engine running, stationary or on a repeatable loop,
    with that capture active:
    1. Run the arms A (twin off), B (twin on, Python), C (twin on, Rust) and
       A again, each ≥ 10 min (≥ 600 ticks). Switch only with the capture
       stopped and `scripts/restart_canary.sh`, then restart the capture.
       Drop each arm's first 60 s.
    2. Every 20 s, log from `/status`:
       - `vehicle_state`;
       - the inference missed-tick counter;
       - storage dropped records and the maximum `queue_depth`;
       - can0 `rx_over_errors`;
       - achieved reads/s;
       - `twin_eval`/`twin_tick` p50/p95/max and the twin's decided/kernel
         counts;
       - cgroup `memory.current`;
       - daemon CPU from `/proc/<pid>/stat`.
    3. Accept only if all of these hold:
       - B and C each add 0 missed ticks and 0 dropped records over the
         bracketing A arms. A nonzero delta is diagnosed, never averaged away.
       - `twin_eval` p99 ≤ 1 ms and max ≤ 5 ms.
       - C's RSS over A ≤ 1 MiB, reported beside B's.
       - The kernel count is above zero in B and C.
    4. Replay throughput: replay the same capture with each backend through
       E04.3's replay procedure. Report decisions/s and `twin_eval` per
       backend.

- [ ] **E06.4 — Record the adoption decision and deployment implications.**
  *(Input, 2026-10-15: [E09](E09-engine-twin-runner-and-levers.md) "Where
  Rust would pay" lists which simulator parts a port would help, per
  measured numbers: the per-tick ECU kernels and the live observer, later
  the continuous plant under a coupled rollout; not the steady Monte Carlo.)*
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
