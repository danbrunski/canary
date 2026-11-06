# ECU twin computation contract

The language-independent definition of the ECU twin's decoded arithmetic and
state ([E06.1](roadmap/tasks/E06-rust-twin-core-trial.md)). The machine-checked
artifact is [`data/ecu_twin_contract.yaml`](../data/ecu_twin_contract.yaml);
this page explains it. Contract version **1**, lookup kernel version **1**,
state model version **1**.

## What is in it

| Operation | Firmware | Python reference | Rust trial |
|---|---|---|---|
| `search_axis` | `0x027114`, `0x027194`, `0x027154` | `src.decode.ecm_lookup.search_axis` | `lookup::search_axis` |
| `interpolate_q16` | `0x026F10` | `ecm_lookup.interpolate_q16` | `lookup::interpolate_q16` |
| `lookup_2d` | `0x0269AC`, `0x026F40` | `ecm_lookup.lookup_2d` | `lookup::lookup_2d` |
| `lookup_curve_q16` | `0x026D58` | `ecm_lookup.lookup_curve_q16` | `lookup::lookup_curve_q16` |
| `lookup_shift12_q16` | `0x026C28` | `ecm_lookup.lookup_shift12_q16` | `lookup::lookup_shift12_q16` |
| `target_command` | `0x0269AC`, gate `0x055E5C`–`0x055EEC` | `src.decode.target_phi_command.target_command` | `target_phi::target_command` |
| `x_from_bfs` | `0xFFFF103E` writer | `target_phi_command.x_from_bfs` | `target_phi::x_from_bfs` |
| `load_input_curves`, `fuel_comp_load` | `FUN_0003c3a4` chain | `src.decode.fuel_comp_load` | `fuel_comp_load::*` |
| `uprev_power_on`, `uprev_step`, `loaded_slot` | map-slot selection | `src.analysis.ecu_state_models` (E05.3) | `uprev::*` |

Each entry in the YAML gives the inputs and outputs with their widths and
signedness, every arithmetic step (widening casts, shifts, truncating
divisions, saturations, the one named 32-bit truncation), the invalid-input
rules, and any state. `tests/test_ecu_twin_contract.py` validates it against
a strict schema. It also checks that every Python reference resolves and every
Rust symbol exists, and that the versions agree across the YAML, the Python
constants, the crate and the vector files.

The YAML's `inventory` lists the other E02/E05 kernels with the reason each
is not yet an operation: `byte_curve16`, `pedal_flag_next`, `trim_lookup`
(decoded, next in line), `selected_timing_trim` (blend rounding open),
`enrichment_step` (no time base), and `twin_command_change`, which composes
`target_command` and has no arithmetic of its own.

## Conventions

- **Widths.** Values are unsigned words (`u16`), bytes (`u8`) or longs
  (`u32`). The exception is idle air, a signed word carried in `i64`.
  Multiplies widen first (`u16 × u16 → u32`). Nothing relies on wrap-around
  except the declared `truncate_u32` in the map-slot history.
- **Rounding.** Divisions truncate, right shifts are logical, and
  interpolation truncates toward its first operand. Nothing rounds to nearest.
- **Refusal versus reason.** A *refusal* means the input is outside the
  domain and no answer exists (`out_of_domain`, `not_whole_count`,
  `empty_axis`, `shape_mismatch`, `curve_shape`, `table_length`,
  `axis_not_rising`, `non_monotone`, `idle_air_out_of_range`,
  `division_by_zero`, `retained_magic_unknown`). Python raises `ValueError`;
  Rust returns `Refusal`. A *reason* means the input is valid but the answer
  depends on state that was not captured (`enrichment_gate_state_unknown`, the
  `uprev_*_unknown` reasons). The result is then unevaluable, never defaulted.
- **State.** State is explicit and nullable, and `null` means unobserved. A
  state is initialised from an observed checkpoint or by the decoded reset
  (`uprev_power_on`). A gap in per-cycle inputs returns it to unknown; nothing
  bridges one. Snapshots carry `snapshot_version`.

## The executable reference and the vectors

The Python kernels are the reference. `ecm_lookup`, `target_phi_command` and
`fuel_comp_load` import only each other and the standard library, with no
session, daemon, storage, pandas or pydantic, and a fresh-interpreter test
enforces that. The stored evaluator (`target_phi.evaluate_session`) and the
live twin (`TwinStream` via `src/daemon/twin_live.py`) both reach
`target_command` through `target_phi.predict`. A spy test shows the two make
the identical kernel call.

`tests/fixtures/ecu_twin/lookup_vectors.json` and `state_vectors.json` hold
the language-neutral vectors, one per line. `tests/ecu_twin_vectors.py`
builds them (`.venv/bin/python -m tests.ecu_twin_vectors`), and the tests
fail if a committed file differs from what the reference builds. A vector
falls into one of three groups:

- **hand/capture** (`source: hand:…` / `capture:…`): known answers copied from
  the tests that worked them by hand from the listing, and the October 4
  enrichment commands from `tests/fixtures/ground_truth/target_phi.yaml`.
  Their `pinned` block is the independent answer.
- **generated**: edge cases at axis edges, clamps, rounding points, word and
  byte boundaries, both branches, the gate band, sentinels and missing
  history. These hold the reference's answer, so they make implementations
  agree but do not make either correct.
- **control** (`kind: control`): a wrong rpm decode, a wrong flag bit or a
  transposed table applied to a known answer. `must_differ` names the answer
  it must not reproduce.

## Rust trial (E06.2)

`rust/` is a Cargo workspace. `ecu_twin_core` is `no_std` with `alloc`,
`forbid(unsafe_code)`, has no dependencies and does no I/O. It is the only
default member. `ecu_twin_py` is the thin optional PyO3 binding (module
`canary_ecu_twin`, abi3 for CPython ≥ 3.12). It validates a table once into
an owned immutable object (`TargetPhiTable`, `LoadCurves`) and offers
per-tuple calls and a bounded `word_batch`, which detaches the interpreter.

```sh
cargo test --manifest-path rust/Cargo.toml            # debug: overflow panics on
cargo test --manifest-path rust/Cargo.toml --release  # release: same vectors
maturin build --release -m rust/ecu_twin_py/Cargo.toml -o <dir>
PYTHONPATH=<unpacked wheel> .venv/bin/python -m pytest tests/test_ecu_twin_core_rust.py
```

E06.3's paired run and boundary benchmark is `tools/bench_ecu_twin.py`. It
uses the same vector runner as the test and the binding's `bench_*` probes,
which time conversion, detach, compute and return separately. On the Pi it
runs through `scripts/e06_pi_bench.sh`, which refuses unless the truck is
parked/bench/engine_off with no capture, and never installs into the daemon's
venv.

The Python gate never builds or installs the extension.
`tests/test_ecu_twin_core_rust.py` skips by name without it, and CI has no
Rust toolchain.

## Resolved from the listing

The signedness of the lookup arithmetic was an open blocker in the first draft
of contract v1. It is resolved: `0x026F10` compares with `cmp/hs` (unsigned),
every caller zero-extends its operands (`extu.w` / `extu.b`), the axis searches
compare with `cmp/hi`, and the fraction divide at `0x027448` is `div0u`. Hand
vectors pinned from this (`source: hand:listing 0x026F10 …`) use operands at
and above `0x8000`, so a signed implementation fails an independent vector.

## Blockers (named, never defaulted)

The YAML's `blockers` list says what the contract cannot yet state. An
implementation does not choose an answer for any of these:

- `final_command_path`, `enrichment_gate_latch`, `ram_copy_identity`: the
  E02 qualification blockers.
- `bfs_saturation`: where the ECM saturates a BFS above a word is unknown.
- `uprev_indicator_meaning`: the map-switch indicator bit is inferred.
- `enrichment_tick_and_cycle`: E05.3's timer tick and cycle period are not
  decoded, so the enrichment request/commit machine stays Python-only and
  outside this contract version.
- `coolant_blend_divide`: the signed divide `0x021C48` used by the timing-trim
  blend truncates toward zero (resolved 2026-10-17), but the blend itself is
  not implemented yet.
