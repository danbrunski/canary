# R13 — Preprocessing reader and orchestration boundaries

Opened 2026-10-14. Shared rules: [R10](R10-refactor-README.md).
Size L. Land before R14's offline-loading extraction.

## Problem and target

The review's import graph found a dependency cycle spanning `loader`, `decode`,
`derived`, `derived_store`, `imu_stream`, `imu_session` and `session_decode`.
`loader.resample_and_segment` selects reduction/cache/IMU work; those modules
import reader functions or constants back from `loader`. `decode` also imports
`read_session` locally to avoid a module-level cycle. This mixes acquisition
of stored rows with transformation and pipeline policy.

Establish this direction within `src/preprocessing/`: raw reader and frame
contracts → row transforms → decoded readers/reduction/IMU services → cached
session pipeline. Keep existing `loader` public imports as a facade, but
internal implementations must not depend on that facade.

## Implementation checklist

- [ ] **R13.1 Inventory dependencies and behavior.**
  - [ ] Record static, function-level and type-only edges among the seven
    modules; include imports through package facades.
  - [ ] Inventory callers of `read_session`, `iter_session_parts`,
    `iter_session_numeric_parts`, `to_wide`, decoded subset readers,
    `resample_and_segment`, and private constants/helpers imported by siblings.
  - [ ] Capture baseline frames for raw/as-recorded/current decoding, CAN-only,
    DID/memory data, valid/missing/refused IMU frames and multi-segment sessions.
  - [ ] Record projection/filter pushdown, chunk size, ordering, corrupt-chunk
    reporting, storage attributes, decoded-row handling and cache fingerprints.
- [ ] **R13.2 Design a dependency map before moving bodies.**
  - [ ] Assign raw Parquet scanning, chunk planning and filters to a reader
    module, with dependencies only on storage/decode primitives and contracts.
  - [ ] Put shared column/frame contracts and coercion helpers in a leaf
    module; avoid importing the entire loader for one constant.
  - [ ] Keep row-local decoding separate from read-then-decode orchestration;
    move `read_session_decoded*` to a decoded-reader service if needed.
  - [ ] Put `resample_and_segment` and selection of `active_store`/IMU/reduction
    in an upper pipeline module. Cache/reducers consume readers, not the facade.
  - [ ] Map IMU sidecar/schema ownership separately from IMU reading/solving
    so the IMU pair does not retain a hidden reverse dependency.
  - [ ] Record the actual module plan here; choose only splits that remove
    the named cycle rather than creating a broad generic IO framework.
- [ ] **R13.3 Move in dependency order.**
  - [ ] Extract contracts and raw reader primitives first, then transforms,
    decoded-read services and orchestration in separate reviewable changes.
  - [ ] Keep `loader` exports compatible; migrate internal imports to owners
    as each phase lands. No internal module imports the compatibility facade.
  - [ ] Preserve stable long-frame sort before any order-sensitive `last`
    pivot, timestamps, timestamp basis, source/channel spelling and dtypes.
  - [ ] Preserve bounded chunk iteration and disjoint-chunk handling. Never
    replace the streaming/reduced path with whole-session concatenation.
  - [ ] Preserve cache input identity and current/as-recorded decode behavior;
    pure movement does not invalidate derived data without a stated reason.
  - [ ] Retarget test patches and remove local imports used only for the cycle.
    Keep lazy imports that exist for a measured startup-cost reason.
- [ ] **R13.4 Enforce intended internal boundaries.**
  - [ ] Add focused AST checks for reader/contract modules importing orchestration
    or the compatibility facade, covering nested and type-only imports.
  - [ ] Assert the named seven-module cycle is eliminated in the implementation
    graph; test a forbidden edge with a small temporary-source fixture.
  - [ ] Keep top-level layering checks green without introducing exemptions.
- [ ] **R13.5 Verify data and resource behavior.**
  - [ ] Run `test_preprocessing_loader.py`, `test_preprocessing_decode.py`,
    `test_bounded_session_resample.py`, `test_derived_store.py`,
    `test_imu_stream.py`, affected IMU/session tests and feature-pipeline tests.
  - [ ] Compare baseline rows, sort order, dtypes, NaN/gap/arrival masks,
    decoded values, frame attrs, segment labels and cache hits/misses.
  - [ ] Reproduce the existing bounded-read case and record peak memory on
    the same stored session/settings if the extraction changes the read path.
    Do this on the laptop; no speculative new memory ceiling.
  - [ ] Run `test_analysis_ground_truth.py`, layering, relevant slow tests and
    `ml_training`; require clean skip behavior without torch.
  - [ ] Replay a fixed session through the live pipeline if its loading path
    changes; record event/row differences and replay state.
- [ ] **R13.6 Close out.**
  - [ ] Record before/after dependency maps, facade exports and remaining lazy
    imports with their actual purpose.
  - [ ] Record fixture and resource comparisons, tests and implementation commits.

## Acceptance

Stored-row readers do not choose reduction/cache policy or import orchestration.
The named cycle is removed, old public APIs remain usable, and ordering,
decode semantics, cache behavior and bounded memory paths are retained.
