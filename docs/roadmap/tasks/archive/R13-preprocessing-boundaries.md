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

- [x] **R13.1 Inventory dependencies and behavior.**
  - [x] Record static, function-level and type-only edges among the seven
    modules; include imports through package facades.
    Scanned at `HEAD` (1fdb99dd) with the AST scanner now in
    `tests/test_preprocessing_boundaries.py` (`scan_edges`: nested, `try`,
    `TYPE_CHECKING`, relative, `from src.preprocessing import <submodule>`).
    One strongly connected component of exactly the seven named modules.
    The before map is in the close-out below; there were no type-only edges
    and no imports through the `src.preprocessing` lazy facade among them.
  - [x] Inventory callers of `read_session`, `iter_session_parts`,
    `iter_session_numeric_parts`, `to_wide`, decoded subset readers,
    `resample_and_segment`, and private constants/helpers imported by siblings.
    Counts and the retargeted patches are in the close-out. Private names
    reached from outside `loader`: `_decimate_imu_long` (derived, 2 tests),
    `_read_numeric_chunks` (1 test, also a patch target), `SESSION_PART_ROWS`
    (4 siblings, 2 `src` callers, 1 test patch).
  - [x] Capture baseline frames for raw/as-recorded/current decoding, CAN-only,
    DID/memory data, valid/missing/refused IMU frames and multi-segment sessions.
    `r13_baseline.py capture` (scratch): 297 items, 564 frames, 3,414,676
    rows, 36.9 M cells, over 13 fixed sessions: the `drive-slice` fixture
    with DID and K-line chunks, a CAN-only copy, `did-speed-slice`,
    `replay/decoded-slice` (stored `decoded` rows + raw frames), a
    multi-chunk out-of-order IMU session with a 400 s gap, and the
    `test_imu_stream` drive resolved / unresolved / no-IMU / refused / stale /
    `0x2D1`-speed; a session with a 0-byte tail chunk and one with a truncated
    chunk. Captured twice from `HEAD`: 0 diffs (deterministic).
  - [x] Record projection/filter pushdown, chunk size, ordering, corrupt-chunk
    reporting, storage attributes, decoded-row handling and cache fingerprints.
    Recorded in the close-out ("Behaviour inventory") and each is in the
    baseline: `columns=` and DNF `filters=` reads, `time_range` prune,
    parts at 4,000 / 700 rows and the default, the stable sort, the
    corrupt-chunk warning text (identical before and after), `attrs`
    (`FRAME_ATTR`, `IMU_FRAME_ATTR`) compared per frame, `on_read` vs
    `as_recorded`, and the store's `meta.json` (`on_read_hash`, `imu_frame`
    key, source file list) miss then hit.
- [x] **R13.2 Design a dependency map before moving bodies.**
  - [x] Assign raw Parquet scanning, chunk planning and filters to a reader
    module, with dependencies only on storage/decode primitives and contracts.
    `src/preprocessing/session_reader.py` (`read_session`, `iter_session_parts`,
    `iter_session_numeric_parts`, `_ReadPlan`, DNF filters, chunk grouping,
    `read_sessions`, `discover_sessions`, `filter_rows`): imports
    `session_contracts`, `decode`, `src.decode`, `src.storage`, `src.telemetry`.
  - [x] Put shared column/frame contracts and coercion helpers in a leaf
    module; avoid importing the entire loader for one constant.
    `session_contracts.py` (leaf, imports nothing from the package) holds the
    constants and types; the coercion helpers and `to_wide` are in
    `wide_frame.py` (depends on the leaf only). `SESSION_PART_ROWS` stays with
    the reader whose docstring carries its measurements.
  - [x] Keep row-local decoding separate from read-then-decode orchestration;
    move `read_session_decoded*` to a decoded-reader service if needed.
    `decoded_reader.py` now owns `read_session_decoded` and
    `read_session_decoded_subset`; `decode.py` has no reader import and
    resolves the two old names with a PEP 562 `__getattr__` (see close-out).
  - [x] Put `resample_and_segment` and selection of `active_store`/IMU/reduction
    in an upper pipeline module. Cache/reducers consume readers, not the facade.
    `session_pipeline.py` (rank 7). `derived`, `derived_store`, `imu_stream`,
    `session_decode` import `session_reader`/`wide_frame`/`imu_decimation`.
  - [x] Map IMU sidecar/schema ownership separately from IMU reading/solving
    so the IMU pair does not retain a hidden reverse dependency.
    `imu_session` owns the sidecar schema, the solver and the one session read
    (`read_imu_inputs`); it now imports `session_reader`/`decode` at the top
    and nothing above it. `imu_stream` (framed stream) imports `imu_session` and
    `session_reader`. `_decimate_imu_long` moved to `imu_decimation`
    (`decimate_imu_long`), the leaf both the stream and the pipeline use. No
    further split: the reverse edges were the lazy `loader` imports, not shared
    ownership.
  - [x] Record the actual module plan here; choose only splits that remove
    the named cycle rather than creating a broad generic IO framework.
    Plan: close-out "Module plan". Five new modules, each holding code moved
    verbatim out of `loader`/`decode`.
- [x] **R13.3 Move in dependency order.**
  - [x] Extract contracts and raw reader primitives first, then transforms,
    decoded-read services and orchestration in separate reviewable changes.
    Done in that order, one new module per phase (`session_contracts` →
    `session_reader` → `wide_frame`/`imu_decimation` → `decoded_reader` →
    `session_pipeline` → `loader` facade). The result is one working-tree
    diff; the orchestrator chooses commit granularity.
  - [x] Keep `loader` exports compatible; migrate internal imports to owners
    as each phase lands. No internal module imports the compatibility facade.
    `test_loader_names_are_the_owners_objects` (17 parametrized names, each
    `loader.X is owner.X`), `test_loader_exports_nothing_the_table_does_not_name`,
    `test_nothing_in_the_package_imports_a_compatibility_facade`.
  - [x] Preserve stable long-frame sort before any order-sensitive `last`
    pivot, timestamps, timestamp basis, source/channel spelling and dtypes.
    Function bodies moved verbatim: AST comparison of all 58 top-level
    definitions of the old `loader.py`/`decode.py` against their new homes
    (identifiers `_VALUE_COLUMNS`/`_NUMERIC_COLUMNS`/`_decimate_imu_long`
    renamed, local imports removed) differs only in `resample_and_segment`,
    whose `diff` is the three removed local imports (kept, see below). Baseline
    compare: 0 diffs.
  - [x] Preserve bounded chunk iteration and disjoint-chunk handling. Never
    replace the streaming/reduced path with whole-session concatenation.
    `test_without_a_store_the_value_number_chain_reads_one_part_at_a_time`
    (patched to the new owners) passes; peak RSS equal (close-out table).
  - [x] Preserve cache input identity and current/as-recorded decode behavior;
    pure movement does not invalidate derived data without a stated reason.
    `DERIVED_CODE_VERSION`, `NUMERIC_CODE_VERSION`, `ImuFramePlan.key` and
    `on_read_hash` untouched; the store's `meta.json` (hashes, source file
    list, `imu_frame`, intervals) is identical before and after, and the
    second load is a `reused` hit (baseline `store_load[...,hit]`).
  - [x] Retarget test patches and remove local imports used only for the cycle.
    Keep lazy imports that exist for a measured startup-cost reason.
    Removed: 2 in `decode`, 3 in `derived_store`, 2 in `derived`, 1 in
    `imu_stream`, 2 in `imu_session`. Kept lazy for start-up cost, the 3
    inside `resample_and_segment` (which moved with it):
    `session_pipeline.resample_and_segment` imports `derived`,
    `derived_store`, `imu_stream` (importing them eagerly added 13 modules and
    ~22 ms warm to the daemon's boot through `models.sequence_dataset` →
    `loader`); pinned by `test_importing_the_loader_facade_loads_none_of_the_services`
    and `test_function_level_imports_in_the_chain_are_the_declared_ones`.
    Patches retargeted: `test_derived_store` (`SESSION_PART_ROWS` now on
    `session_reader`, `derived`, `imu_stream`; `_read_numeric_chunks`;
    `decimate_imu_long`), `test_imu_lon_mean`, `test_session_time_prune`,
    `test_channels_raw_read`.
- [x] **R13.4 Enforce intended internal boundaries.**
  - [x] Add focused AST checks for reader/contract modules importing orchestration
    or the compatibility facade, covering nested and type-only imports.
    `tests/test_preprocessing_boundaries.py`: `test_readers_and_contracts_import_no_orchestration_or_facade`,
    `test_imports_inside_the_chain_run_strictly_down` (rank table),
    `test_nothing_in_the_package_imports_a_compatibility_facade`,
    `test_the_only_name_based_import_is_the_decode_compat_shim`.
  - [x] Assert the named seven-module cycle is eliminated in the implementation
    graph; test a forbidden edge with a small temporary-source fixture.
    `test_the_named_seven_module_cycle_is_gone`, `test_the_package_has_no_other_import_cycle`;
    fixtures: `test_a_forbidden_reader_edge_is_found` (7 placements: top,
    function, `TYPE_CHECKING`, relative, relative-package, plain `import`,
    method inside `try`), `test_a_facade_import_is_found_even_through_the_package`,
    `test_the_old_cycle_shape_is_reported_as_a_cycle`. Run against the `HEAD`
    sources the scanner reports the component
    `[decode, derived, derived_store, imu_session, imu_stream, loader, session_decode]`
    and 8 facade-importing import statements; now none.
  - [x] Keep top-level layering checks green without introducing exemptions.
    `tests/test_layering.py`: all tests pass; `OWED`, `OWED_PAIRS`, `STANDING`
    untouched; no new top-level package.
- [x] **R13.5 Verify data and resource behavior.**
  - [x] Run `test_preprocessing_loader.py`, `test_preprocessing_decode.py`,
    `test_bounded_session_resample.py`, `test_derived_store.py`,
    `test_imu_stream.py`, affected IMU/session tests and feature-pipeline tests.
    `pytest -n 4 -m "not integration"` on the 119 test files that mention
    `preprocessing`/`read_session`/`resample_and_segment`/`feature_pipeline`
    (includes the named ones, `test_imu_session`, `test_imu_lon_mean`,
    `test_imu_speed_hold`, `test_feature_pipeline`, the `slow` and
    `ml_training` tests in them): 2207 passed, 8 skipped.
  - [x] Compare baseline rows, sort order, dtypes, NaN/gap/arrival masks,
    decoded values, frame attrs, segment labels and cache hits/misses.
    `r13_baseline.py compare` after the moves: 297 items, 564 frames,
    3,414,676 rows: 0 diffs (`assert_frame_equal(check_exact=True,
    check_dtype=True)`, column order, `isna()` masks, `attrs`, store reports
    and `meta.json`).
  - [x] Reproduce the existing bounded-read case and record peak memory on
    the same stored session/settings if the extraction changes the read path.
    The read path is not changed (verbatim bodies), and it was measured
    anyway: same 1.1 M-row session, fresh process each, peak RSS before →
    after: `resample_and_segment` 215 → 210 MB, `iter_session_parts`
    281 → 287, `iter_session_numeric_parts` 185 → 188, whole `read_session`
    831 → 828. No new ceiling.
  - [x] Run `test_analysis_ground_truth.py`, layering, relevant slow tests and
    `ml_training`; require clean skip behavior without torch.
    `test_analysis_ground_truth.py` + `test_layering.py` + boundaries: pass.
    `pytest -m ml_training -n 4` (whole tier; `src/preprocessing/` is in
    `scripts/ml_paths.txt`): 125 passed, 2 skipped. The slow tests are in the
    119-file run above. Without torch (a `torch` that raises
    `ModuleNotFoundError` first on `PYTHONPATH`): cold-start, boundaries,
    loader, feature-pipeline, LSTM training/inference, sequence dataset,
    derived-store and IMU-stream files: 143 passed, 53 skipped, 0 failed.
  - [x] Replay a fixed session through the live pipeline if its loading path
    changes; record event/row differences and replay state.
    The loading path is moved, not changed, and the live serve path imports
    none of the moved modules (`src/collection`, `src/daemon`,
    `src/features/live.py`: no `preprocessing.{loader,decode,derived*,
    session_*,imu_*}` import). The offline library replay (the path that does
    use them) was run from the `HEAD` tree and the working tree on 4 fixed
    sessions: 24 frame/report items, 142,520 rows, identical (exact values,
    dtypes, columns, NaN masks, report dicts). No daemon replay was started
    (offline-only run); replay state not applicable.
- [x] **R13.6 Close out.**
  - [x] Record before/after dependency maps, facade exports and remaining lazy
    imports with their actual purpose.
    Close-out record below.
  - [x] Record fixture and resource comparisons, tests and implementation commits.
    Fixture and resource comparisons and tests are in the close-out; the
    implementation commit is the commit that archives this file.

## Acceptance

Stored-row readers do not choose reduction/cache policy or import orchestration.
The named cycle is removed, old public APIs remain usable, and ordering,
decode semantics, cache behavior and bounded memory paths are retained.

## Close-out record (2026-10-14)

Boundary test: `tests/test_preprocessing_boundaries.py` (39 tests).

### Module plan (rank = may import only strictly lower ranks)

| Rank | Module | Owns | Imports (package) |
|---|---|---|---|
| 0 | `session_contracts` (new, leaf) | `INDEX_COLUMNS`, `TelemetryValueKind`, `DecodedRows`, `STORED_COLUMNS`, `KEY_COLUMNS`, `VALUE_COLUMNS`, `NUMERIC_COLUMNS` | none |
| 0 | `imu_decimation` | wide decimation + `decimate_imu_long` (was `loader._decimate_imu_long`) | none |
| 0 | `decode` | row-local decode (`decode_on_read`, `decode_can_rows`, `decoded_subset_plan`, `keep_can_ids`, `on_read_hash`) | none |
| 1 | `session_reader` (new) | Parquet scan, `_ReadPlan`/DNF filters, chunk grouping, `read_session`, `read_sessions`, `iter_session_parts`, `iter_session_numeric_parts`, `discover_sessions`, `filter_rows`, `SESSION_PART_ROWS` | `session_contracts`, `decode` |
| 1 | `wide_frame` (new) | `to_wide`, `coerce_numeric_columns`, `non_numeric_columns`, `numeric_value_columns` | `session_contracts` |
| 2 | `decoded_reader` (new) | `read_session_decoded`, `read_session_decoded_subset` | `decode`, `session_reader` |
| 2 | `imu_session` | IMU sidecar schema, solver, `read_imu_inputs`, `resolve_session_imu_frame` | `decode`, `imu_decimation`, `session_reader` |
| 3 | `imu_stream` | framed `value_number` stream | `imu_decimation`, `imu_session`, `session_reader` |
| 4 | `derived` | bounded reduction | `imu_decimation`, `imu_stream`, `session_contracts`, `session_reader`, `wide_frame` |
| 5 | `session_decode` | decoded reduction | `decode`, `derived`, `session_reader` |
| 6 | `derived_store` | cache | `decode`, `derived`, `imu_stream`, `session_decode`, `session_reader` |
| 7 | `session_pipeline` (new) | `resample_and_segment` | `session_contracts`, `session_reader`, `wide_frame`, `imu_decimation`; lazily `derived`, `derived_store`, `imu_stream` |
| 8 | `loader` | compatibility facade (84 lines) | the owners above; imported by nothing in the package |

`resampler`, `segmenter`, `quality_gate`, `imu_frame` stay support code and
import no ranked module (checked).

### Dependency map, before and after

Before (`HEAD`: 22 import statements among the seven modules over 19
module pairs, 12 of them function-level, 8 into `loader`): one
component `{decode, derived, derived_store, imu_session, imu_stream, loader,
session_decode}`. Back edges into `loader`: `decode` (function ×2:
`read_session`), `derived` (top: `INDEX_COLUMNS`, `coerce_numeric_columns`,
`non_numeric_columns`; function: `SESSION_PART_ROWS`, `_decimate_imu_long`),
`derived_store` (top: `SESSION_PART_ROWS`), `imu_session` (function:
`read_session`), `imu_stream` (function: `SESSION_PART_ROWS`,
`iter_session_numeric_parts`), `session_decode` (top: `SESSION_PART_ROWS`,
`iter_session_parts`). Forward edges: `loader` → `decode` (top), `derived`,
`derived_store`, `imu_stream` (function ×3). Further function-level edges:
`derived_store` → `decode`, `derived`, `imu_stream`; `imu_session` → `decode`.

After: 0 components; 9 import statements among the seven, all top-level and
all down the rank table; 0 into `loader` or the package facade. The only function-level edges in the chain are the three
`session_pipeline` ones (start-up cost). `src/preprocessing/__init__.py`
exports now point at owners (`read_session` → `session_reader`, `to_wide` →
`wide_frame`, `read_session_decoded` → `decoded_reader`, `channel_key` →
`src.telemetry`).

### Facade exports and shim

`loader` re-exports (direct, `is`-identical to the owner): `INDEX_COLUMNS`,
`KEY_COLUMNS`, `STORED_COLUMNS`, `DecodedRows`, `TelemetryValueKind`,
`SESSION_PART_ROWS`, `discover_sessions`, `filter_rows`,
`iter_session_numeric_parts`, `iter_session_parts`, `read_session`,
`read_sessions`, `coerce_numeric_columns`, `non_numeric_columns`,
`numeric_value_columns`, `to_wide`, `resample_and_segment`, `channel_key`.
Dropped from `loader` (private, no remaining caller): `_decimate_imu_long`
(→ `imu_decimation.decimate_imu_long`), `_read_numeric_chunks`, `_ReadPlan` and
the other `_` helpers (→ `session_reader`), and the module attribute `pd`.
`decode.read_session_decoded` / `read_session_decoded_subset` keep resolving
through `decode.__getattr__` (`importlib.import_module` by name, because a static
import would put the reader back under `decode`); the boundary test allows
exactly that one name-based import. Callers: the two library users
(`analysis/behavior_validation.py`, `plausibility/offline.py`) now import
`decoded_reader`; their tests and the other ~40 external callers keep the
`loader` path.

### Caller inventory (before, outside the package unless noted)

`loader.read_session` 32 files (18 tests, 6 `src`, 2 scripts, 3 tools, 1
analysis script, 2 in the package); `resample_and_segment` 15; `to_wide` 11;
`discover_sessions` 10; `iter_session_parts` 4; `read_sessions` 3;
`iter_session_numeric_parts` 2; `numeric_value_columns` 2;
`decode.read_session_decoded_subset` 3 (2 `src`, 1 test),
`read_session_decoded` 1 (test). Patch targets on `loader`:
`SESSION_PART_ROWS`, `_read_numeric_chunks`, `pd.read_parquet` ×2.

### Behaviour inventory (unchanged, pinned by the baseline)

Projection: `columns=` reads the sort key + named stored columns and adds
`payload` only when a catalog frame can pass the filter. Pushdown: DNF
`filters` go to `pd.read_parquet`, widened by the on-read catalog's frames and
re-applied after the decode. Chunking: chunks grouped by footer `timestamp`
min/max into time-disjoint parts of ≥ `target_rows` (default
`SESSION_PART_ROWS` = 100,000), a part ends strictly after the last timestamp
in it; the numeric chain carries the last partial bin across parts. Order:
chunk-name order then `sort_values(["timestamp","source","channel"],
kind="stable")`. Corrupt chunks: 0-byte and unparseable chunks skipped with
one warning naming them (text identical before and after); a truncated
chunk makes `session_can_storage` raise (pre-existing, see report). Storage
attributes: `attrs[FRAME_ATTR]` on every long and wide frame,
`attrs[IMU_FRAME_ATTR]` on `resample_and_segment`. Cache fingerprints:
`on_read_hash`, `ImuFramePlan.key()`, chunk list, grid interval.

### Fixture and resource comparisons

| Check | Result |
|---|---|
| Baseline (13 sessions, 297 items) before vs after | 564 frames, 3,414,676 rows, 36.9 M cells, 543 scalars: 0 diffs |
| Library replay, 4 sessions, `HEAD` tree vs working tree | 24 items, 142,520 rows: identical |
| Corrupt-chunk warning text | identical |
| Peak RSS, 1.1 M-row session (resample / parts / numeric parts / whole read) | 215/281/185/831 MB → 210/287/188/828 MB |
| Daemon boot modules (`import src.main, src.daemon, src.daemon.runtime`) | 989 → 993 (the 4 new modules that split `loader`; no service modules) |
| Tiers | 119 affected files 2207 passed / 8 skipped; `ml_training` 125 passed / 2 skipped; no-torch run 143 passed / 53 skipped; layering, ground truth, cold start green |
| Gate | `ruff check .`, `ruff format --check .`, `mypy src` (556 files) clean |

Implementation commit: the commit that archives this file.
