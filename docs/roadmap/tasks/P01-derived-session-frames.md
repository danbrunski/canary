# P01 — derived session frames: reduce first, store once, invalidate per channel

Opened 2026-09-26 (operator: "let's implement those things") after the A02.11
baseline regeneration was OOM-killed at ~15 GB RSS on the 15 GB laptop while
decoding one 2 h session.

## Why

Model fitting is cheap (seconds to minutes). The cost is in building training
and evaluation rows:

- Every consumer rebuilds 1 Hz features from raw long-format parquet:
  - retrain, `evaluate_detectors`, the digest, the workbench, fingerprints;
  - raw means ~100 Hz CAN broadcast plus every OBD/DID read.
- It pivots to a wide table at *raw* resolution before downsampling. That
  costs about 1.5 KB per row, millions of rows per drive, and 10–15 GB for one
  long session.
- Every decode-catalog edit (`bindings.yaml`, `dids.yaml`) invalidates every
  cached session. Re-decoding one takes about 4 min and 10 GB.
- The trip-end digest hit the same wall and was fixed by reducing before the
  pivot (the replay-OOM root cause, 2026-09-17). The evaluation and training
  loader never got that fix.

The corpus is tens of hours. This should need hundreds of MB, not 15 GB.

## Rules

- **Parity is the acceptance.** The reduced path must produce what the
  current path produces (`resample_and_segment` → `featurize_wide_frame`),
  including gap flags, arrival flags, rolling windows, the fuel-cut
  sentinel's state rows and decoded K-line/DID/memory channels. The check is
  `assert_frame_equal(check_exact=True)`, or a named, justified tolerance where
  float summation order changes. Test it on committed fixtures and on at least
  two real sessions under `~/canary-data`: one long default-profile drive and
  one tuning capture.
- **Train/serve parity holds.** The daemon's live featurizer is untouched.
  The offline path must still equal it on the same rows; the
  `features_batched_parity` and `reconcile_feature_frame_parity` tests stay
  green.
- **Raw parquet stays the source of truth.** Derived frames are a cache. They
  can always be rebuilt, are never shipped to or computed on the Pi, and live
  under a gitignored directory.
- **An empty or refused result says which kind it is.** A missing derived
  frame, a stale version or a refused decode each name their cause.

---

- [x] **P01.1 Reduce before the pivot.** Aggregate raw rows into 1 Hz time
  buckets per channel directly from parquet. Read only the channels a caller
  needs, and do it out of core (DuckDB is already a dependency;
  pyarrow/polars are acceptable if already installed). Only then build the
  wide frame. Decode happens before or inside the reduction without first
  materialising all decoded rows:
  - CAN payloads through the bindings;
  - DIDs and memory through `src/decode/slices.py`;
  - the fuel-cut sentinel as a state.

  The resampler's semantics define the aggregation. Read them from
  `src/preprocessing/resampler.py`: which value a bucket keeps, `__gap` /
  `__arrived` flags, and the max-gap rule. Where DuckDB cannot express one,
  do that step in numpy on the already-reduced data. **Accept:**
  - parity as in the rules;
  - peak RSS measured before and after on `2026-09-20T152758` (2 h, 161 MB)
    and `2026-09-18T213818`, the two sessions A02.11 recorded as OOM, with a
    target well under 2 GB;
  - wall time for the same two sessions recorded.

  *Done 2026-09-26.* `src/preprocessing/derived.py` (`reduce_long_frames`,
  `resample_reduced`), driven by `feature_pipeline.reduce_session` /
  `resample_session_bounded`, the one per-session chain every offline caller
  now runs (`_decode_session_to_resampled`, `load_resampled_session`).
  - **Out of core by parts, not DuckDB.** `loader.iter_session_parts` reads a
    session in time-disjoint parts of ~100k raw rows, grouped by each chunk's
    footer `timestamp` statistics, so no timestamp spans two parts. Each part
    goes through the existing decoders (`_decode_long_frame`: bindings with
    their sentinel state rows, `.dbc`, DIDs), which are row-local, so a part
    decodes exactly as the whole session would. DuckDB cannot run those
    decoders; the reduction runs in numpy on each decoded part and only its
    output is kept.
  - **What is kept.** Per channel and 1 Hz bin: the first and last cell of
    each "observed" kind the resampler reads (any row, for the grid and the
    `session_gap_seconds` split; a non-null value, for value and `__gap`; a
    value `pd.to_numeric` parses, for `__arrived`), each after the quality
    gate. The derivation from `resampler.py` is in the module docstring. The
    wide frame is then built from the kept cells and resampled by the real
    `resample_wide`, a channel batch at a time.
  - **Two whole-session facts, carried not guessed.** `to_wide` keeps a column
    `object` if any cell in the session is non-numeric, and the gate compares
    float32 and float64 differently (150.000001 passes a 150.0 limit as
    float32, fails as float64). Cells are kept for both readings and the
    verdict is accumulated per channel; `coerce_numeric_columns` (split out
    of `to_wide`) and `apply_quality_gate` then run on the kept cells.
  - **Memory, K-line.** The daemon stores `obd:mem:*` and `kline:*` rows
    already decoded; the offline chain passes them through as recorded
    channels, so they reduce like any other.
  - **Parity** (`assert_frame_equal(check_exact=True)` against
    `resample_long_session(read_session(...))`):
    - `tests/test_bounded_session_resample.py` (10): the ground-truth drive
      slice as overlapping chunks plus a 0-byte chunk, with the real
      bindings/DBC/DID catalogs; a parquet session with text, bool and
      numeric-string channels and both gate edges; a long frame at 1, 2, 0.5
      and 0.25 Hz with a midnight crossing, a gap bridged by empty rows, a real
      segment split, grid-exact stamps, same-instant duplicates, a channel
      that dies and gate rejects; projection; refusals. Each of dropping the
      float32 mask, the numeric mask, the row extremes, or taking the first
      instead of the last same-instant value fails at least one test.
    - `tests/test_derived_store.py` adds a DID channel and a `kline:ABS:1210`
      channel (a `<module>:<hex>` name) to the slice.
    - Real sessions, resampled frame *and* `featurize_wide_frame` output:
      `2026-09-18T152144` (default drive, 1.75M rows) and
      `_capture-fuel-target-research-2026-09-21T195844` (tuning capture: six
      `obd:mem:*` channels including the fuel-cut
      `ECM_TARGET_EQUIV_RATIO_SENTINEL` state, six DIDs, stored `decoded:`
      rows) both equal; so do `_capture-tuning-2026-09-23T142926`,
      `2026-09-18T171938` and six smaller sessions. No laptop session has
      K-line rows.
    - `features_batched_parity`, `reconcile_feature_frame_parity`,
      `test_feature_pipeline.py`, `test_analysis_ground_truth.py` and the
      `ml_training` suites on the touched paths stay green.
  - **Peak RSS and wall time** (`/usr/bin/time -v`, `load_resampled_session`,
    nothing else running):

    | session | before | after |
    |---|---|---|
    | `2026-09-20T152758` (17.9M rows) | `read_session` alone 11.6 GB; killed at 12.1 GB 68 s into the decode (A02.11 run: OOM at ~15 GB) | **1.31 GB, 234 s** |
    | `2026-09-18T213818` (10.6M rows) | `read_session` alone 7.2 GB; killed at 12.5 GB 91 s into the decode | **1.06 GB, 141 s** |

    Part size on `213818`: 500k rows 2.44 GB, 200k 1.46 GB, 100k 1.04 GB, same
    wall time; the default is 100k (`SESSION_PART_ROWS`).
  - The DBC decode no longer builds a Series per frame (`iterrows`): 5.5x
    faster on 145k-201k CAN rows, output identical (it was ~90 % of a
    session's decode time).
  - **Precondition made a refusal (verifier, 2026-09-26).** The reduction is
    exact only when `session_gap_seconds` is at least one grid interval: a
    bin keeps only its first and last row, so a shorter session gap could
    split a segment inside a bin (fuzzed at 0.25 Hz / 1 s gap: 26 of 30
    frames differed). `grid_interval_ns` now refuses that config, naming why,
    and every caller goes through it (`test_refusals_name_their_cause`). At
    gap = interval the fuzz is 0 of 120 across 0.25/0.5/1/2 Hz.
  - **Stored `decoded:` rows (checked, comment fixed).** The batch decode
    recomputes every binding, stored or not, because its chunks hold CAN rows
    only, and the recomputed row is the pivot's `last`. That matches the live
    path, which decodes every frame with the current bindings. On real
    sessions the two copies never differ: 0 of 2.70M same-instant pairs
    across `2026-07-29T205841`, `2026-08-07T161543`, `2026-09-18T160921` and
    `_capture-fuel-target-research-2026-09-21T195844`. The behaviour stays;
    the comments in `_decode_long_frame` and `decode_can_rows` now say what
    happens.

- [x] **P01.2 Store a derived layer once.** Persist the reduced per-session
  frame (parquet), versioned by the hashes of the inputs that shape it:
  - the decode catalogs (bindings, dids, memory, K-line profile);
  - the resample config;
  - a code-version constant bumped on any semantic change.

  It is built on the laptop after a pull (a CLI command, and lazily on first
  use by any consumer) and never on the Pi. Every offline consumer reads it
  through one function: retrain/trainers, `evaluate_detectors`,
  `detector_benchmark`'s successor, digest recompute, fingerprints, the
  workbench jobs. It replaces the ad hoc pickle caches
  (`data/cache/evaluation_frames`, `feature_frames`) or subsumes them.
  **Accept:**
  - a second run over the corpus reads only derived frames (measured);
  - a stale frame is detected by hash and rebuilt, never silently used;
  - the directory is gitignored and excluded from training discovery
    (`_`-prefix or out of the sessions tree);
  - `R07.2`'s repo guard stays happy, with tests writing to `tmp_path`.

  *Done 2026-09-26.*
  `src/preprocessing/derived_store.py`: `DerivedStore(root).load(session,
  decoders, resample_config)` returns the reduced frame and a `DerivedLoad`
  that says `built` / `reused` / `refreshed` and why. One directory per
  session (`<name>-<path hash>`, so same-named sessions under two roots never
  collide) holding a per-write `cells-<pid>-<uuid>.parquet` and the
  `meta.json` that names it, so one rename publishes the pair (P01.3's
  concurrency note). `resample_session_bounded(..., store=)` is the one
  reader.
  - **Versioning.** Whole-frame inputs: the session's chunk list (name, size,
    mtime), the grid interval, the gate limits (`PHYSICAL_LIMITS`) and
    `DERIVED_CODE_VERSION`; a change to any rebuilds the frame and names the
    cause. The resample config's gap rules are applied at read time, not
    stored, so only the interval is versioned. Catalogs are versioned per
    decode unit (P01.3). The memory catalog and the K-line profile do not
    shape the offline frame today (the daemon stores `obd:mem:*` and
    `kline:*` already decoded), so those channels are versioned as recorded
    channels.
  - **Consumers wired.** The trainers: `scripts/retrain.py` selects the store
    for its whole run (`use_store`, which sets `CANARY_DERIVED_DIR` for the
    trainers' process pool and restores it after), beside its feature cache,
    so the defaults give `data/cache/derived_sessions`, and passes it to its
    `evaluate_detectors` gates. `evaluate_detectors` reads through the store
    (`--derived-dir`, default `data/cache/derived_sessions`;
    `--no-derived-store` reduces in memory); its `*-resampled.pkl` cache under
    `data/cache/evaluation_frames` is gone, and `--cache-dir` now holds only
    the detector score caches. `--cache-only` skips sessions whose derived
    frame is missing or stale, naming why.
  - **Built on the laptop, never on the Pi.** `scripts/derive_sessions.py`
    builds or refreshes frames after a pull (`--check` reports without
    building); consumers build lazily. The store refuses on a Raspberry Pi
    (device tree), and no store is active unless a process asks: the daemon
    and the MCP server's `analyze_session` reduce in memory and write nothing.
  - **Measured on the A02.11 corpus (67 sessions).** First
    `derive_sessions.py` run: 67 built in 28 min 22 s, peak 1.66 GB, 53 MB on
    disk. Second run: 67 `reused` in 2.5 s wall, peak 0.66 GB, no session
    decoded.
  - **Stale is rebuilt, never served.** Tests in `tests/test_derived_store.py`
    (8): a second load decodes nothing
    (`test_a_second_load_reads_the_stored_frame_and_decodes_nothing`); a new
    chunk file or a code-version bump rebuilds with the cause named
    (`test_a_stale_frame_is_rebuilt_and_says_why`); every frame read back
    equals the in-memory chain with `check_exact=True`.
  - **Gitignored, outside discovery, R07.2.** `data/cache/` is gitignored
    (`git check-ignore` confirms `data/cache/derived_sessions/`), outside every
    sessions root. Every new test writes under `tmp_path`, and the two
    `evaluate_detectors` test files pass `--derived-dir` into it.

  - **Digest, fingerprints, classification.** These read sessions through
    `loader.resample_and_segment(value="value_number")`, a different chain
    from the trainers': no decode, no gate, IMU acceleration decimated per
    tick, `resample_long`. Routing them through the trainers' frame would
    change their numbers, so the store holds a second frame per session for
    that chain (`DerivedStore.load_numeric`, `<frame>/numeric/`): the
    `value_number` rows reduced by the same per-bin rule, read in parts that
    end only on a tick edge (`loader.iter_session_numeric_parts`) so the IMU
    decimation never sees half a tick, then turned back into long rows for
    the real `resample_long` (`derived.resample_numeric_reduced`). It is
    versioned as a whole (chunk list, interval, code version), since no
    catalog shapes it.
    - `resample_and_segment` uses it whenever a store is active.
      `scripts/fingerprint.py` selects the store for its run (`--derived-dir`,
      `--no-derived-store`).
    - **Never on the Pi.** `active_store()` returns `None` on a Raspberry Pi
      whatever `CANARY_DERIVED_DIR` says, so the daemon's trip-end digest and
      workbench children keep the in-memory path there. Test:
      `test_the_store_is_never_active_on_the_pi`.
    - Parity: `test_the_value_number_chain_reads_the_store_and_matches_raw`
      (IMU at 50 Hz across 17 overlapping chunks, parts of 700 and 5,000
      rows, built and reused). Real sessions `2026-09-18T152144`,
      `_capture-fuel-target-research-2026-09-21T195844` and
      `2026-09-20T152758`: the segmented frame and `compute_fingerprint`
      are identical through the store. On `152758`: 17 s raw, 27 s to build,
      2.3 s to reuse. Dropping the tick alignment fails the test.
  - **Workbench jobs: deliberately not on the derived frame.** The Logs and
    Analysis pages (`src/analysis/workbench.py`) pair channels at the sample
    level (`read_channels`, `align_long`, the delivery engine's same-instant
    pairing), never on a 1 Hz grid. A derived frame keeps one value per
    second, so serving them from it would bring back the aggregate
    comparison that `compare_channels` exists to refuse. They keep reading
    raw samples, bounded by their child's row budget.
  - `data/cache/feature_frames` stays: it caches the *featurized* corpus
    (behavior models applied), one layer above the derived frames. Its builds
    now read derived frames.

- [x] **P01.3 Invalidate per channel.** Version derived frames at channel
  granularity, so a `bindings.yaml` edit re-derives only the channels whose
  decode spec changed. Examples: a per-channel spec hash stored in the frame's
  metadata, or per-channel column files. **Accept:**
  - editing one binding re-derives only that channel on the next load
    (measured: the other columns are read, not recomputed);
  - adding a DID adds a column without touching the rest.

  *Done 2026-09-26.* A frame's channels are versioned by *decode unit*, the
  smallest piece of catalog that makes its own channels. `binding:<NAME>`
  covers one signal: `decoded:<NAME>` and its `_SENTINEL` state channel.
  `did:<NAME>` covers the DID entries with that name, `dbc` the `.dbc`
  database, and `raw:<source>:<channel>` any recorded channel no decoder
  claims. Each unit's hash is its spec (`meta.json` `units`). On load, a
  unit whose hash changed, a new unit or a removed one is re-derived in one
  pass. That pass reads only the rows the changed units need (parquet
  filters, e.g. the binding's CAN id in every recorded spelling plus stored
  rows of its channel), and every other channel's cells are read back. This
  is exact because the reduction is per channel: a channel's kept cells
  depend on its own rows only, and the grid's bin edges are the min/max over
  each channel's own first and last rows.
  - `test_editing_one_binding_rederives_only_that_channel`: one filtered
    `reduce_session` call, with that one signal and no DBC/DID decoder; all
    other channels reused; only that channel's columns move; the result
    equals the full chain with the edit.
  - `test_adding_a_did_adds_its_column_and_touches_nothing_else`: every
    earlier column is identical.
  - `test_removing_a_binding_drops_its_channel`, and
    `test_every_unit_rederived_alone_rebuilds_the_one_pass_frame` (all units
    marked stale, which reproduces the single full pass exactly).
  - Real session (`2026-09-18T152144`, 139 channels): build 38.2 s. After an
    edit to `ECM_COOLANT_TEMP`'s `byte_start`, the refresh took 1.8 s,
    re-derived `binding:ECM_COOLANT_TEMP` only and reused 138 of 139
    channels, and the result equals `resample_long_session` with the edit.
    Reverting the edit re-derived that unit again (2.4 s); the next load with
    nothing changed was a reuse (0.0 s).
  - **A DID id declared twice is refused (verifier, 2026-09-26).** Units were
    keyed by DID name while the decoder indexes the catalog by id (last entry
    wins), so a second entry for an existing id made a refreshed frame differ
    from a rebuild ((1631, 350) vs (1631, 353)) and then be reused. `DidMap`
    now refuses a repeated id or channel name when loaded, naming both
    entries. The store repeats the refusal for a map built with `model_copy`,
    which skips validators. Test:
    `test_a_did_id_declared_twice_is_refused_by_the_catalog_and_the_store`.
  - **Concurrent writers cannot mix their files (verifier, 2026-09-26).**
    - *The bug.* Cells and meta had fixed names, each with its own rename.
      Two writers with different catalogs could therefore leave one's
      `meta.json` over the other's `cells.parquet`. The next load then served
      the mix as "every input hash matches". The verifier reproduced this with
      an edited `ECM_COOLANT_TEMP` binding: `decoded:ECM_COOLANT_TEMP` came from
      the wrong catalog.
    - *The fix.* Each write puts its cells in its own
      `cells-<pid>-<uuid>.parquet`, written under a dot name and renamed into
      place. Its `meta.json` records that file name and is renamed into place
      last. A meta can therefore only describe the cells it names, a reader
      opens exactly those, and a crash leaves the previous pair intact.
    - *Cleanup.* Superseded cells files are deleted after the publish. A
      reader or racing writer whose file is gone gets a rebuild that says so:
      "cells file … is gone (superseded by a concurrent write)".
    - *Migration.* The layout change bumped `DERIVED_CODE_VERSION` to 2, so
      version-1 frames are rebuilt, never read. The first write over one
      deletes its fixed-name `cells.parquet`. The numeric frame is published
      the same way.
    - *Tests.* Writer B runs its whole load inside writer A's meta rename:
      `test_racing_writers_never_serve_one_writers_meta_over_the_others_cells`.
      After it, the next v1 load rebuilds and equals the v1 chain exactly; it
      served the v2 channel before the fix. Also
      `test_a_frame_whose_cells_file_is_gone_is_rebuilt_and_says_why`,
      `test_a_version_1_frame_is_rebuilt_and_its_fixed_name_cells_deleted`,
      and the numeric check in
      `test_the_value_number_chain_reads_the_store_and_matches_raw`.
    - *Result.* The verifier's `race.py` now prints "v1 frame correct". The
      targeted files, 90 tests, all pass.

- [x] **P01.4 Re-run A02.11 on the new path.** Run the regenerated
  default-profile baseline (R04.6's remainder) end to end on the laptop with
  no out-of-memory, and record the table in A02.11 and R04.6.

  *Done 2026-09-26.* The A02.11 command (recorded in A02.11) ran end to end
  twice with exit 0, and every session loaded, `152758` and `213818`
  included. The first run used the bounded decode: 1 h 43 min. The second
  read only P01 derived frames (all 67 `derived session frame reused`):
  19 min 40 s, with the detector score caches warm from the first run. The
  two reports are identical apart from `generated_at` and `command`. The
  table is in A02.11 and R04.6.
  - Peak RSS of those two runs was 13.3-14.1 GB, which was not the decode.
    P01.5 found and fixed the cause: the same command now peaks at 4.3 GB
    with warm caches and 4.7 GB cold, with an identical report.

- [x] **P01.5 Bound the evaluation harness's own memory.** After P01.4 the
  A02.11 run still peaked at 13.3-14.1 GB of the laptop's 15 GB, with
  sessions loading at 1.3 GB or less. Target: well under 8 GB, with an
  identical report.

  *Done 2026-09-26.* Profiled by sampling RSS and the main thread's stack
  every 0.25 s during the real A02.11 run (memray/py-spy are not installed).
  The suspect, `align_features_to_schema`, was not it.
  - **Hotspot 1: the plausibility detector's raw streams.** `load_raw_streams`
    → `PlausibilityDetector.load` → `plausibility.offline.load_stream` read
    each session whole (`read_session_decoded_subset`: every non-CAN row plus
    the checked CAN ids). On `2026-09-20T152758` that alone reached 13.2 GB,
    the run's peak. `load_stream` now reads the session in the same
    time-disjoint parts, and decodes and samples each part. Stored-decoded
    precedence uses the whole session's stored set, read up front from two
    parquet columns. The 1 Hz segmenter frame for the modes comes from the
    P01.1 reduction. Standalone on `152758`: 11.5 GB → 2.34 GB peak.
    Samples and modes are exactly equal on the drive slice (six chunks plus
    stored decoded rows; a per-part stored set fails the test), on
    `2026-09-18T200256` and on
    `_capture-fuel-target-research-2026-09-21T195844`
    (`test_the_part_by_part_stream_equals_the_whole_session_one`).
  - **Hotspot 2: every stream held at once.** RSS went from 2.3 GB to 6.0 GB
    while the streams loaded: 2.4 GB of samples plus what the allocator
    kept. Each stream is used at most twice. `load_raw_streams` now builds
    each stream into its cache file and drops it; `CachedStreams` reads one
    back on access.
  - **The stream cache never hit.** Its key hashed `repr(catalog)`, and the
    catalog holds frozensets whose order follows `PYTHONHASHSEED`, so every
    run rebuilt every stream and left about 1 GB of orphaned files. New
    `LimitsCatalog.fingerprint()` hashes a canonical form with sets sorted
    (`test_the_catalog_fingerprint_is_stable_across_hash_seeds`, three seeds).
    The old files under `data/cache/evaluation_frames/plausibility/` are dead
    and can be deleted.
  - **Measured** (`/usr/bin/time -v`, the A02.11 command, outputs to the
    scratchpad and compared with the recorded report: identical apart from
    `generated_at` and `command`, JSON and Markdown):

    | run | peak RSS | wall |
    |---|---|---|
    | before (P01.4, run 2) | 13.3 GB | 19 min 40 s |
    | after, streams alone fixed | 7.07 GB | 20 min 12 s |
    | after, warm stream cache | **4.26 GB** | 14 min 53 s |
    | after, fresh `--cache-dir` | **4.71 GB** | 19 min 50 s |

    The remaining peak is `window()` during injection sweeps, over the one
    stream in use.

## Not in scope

- The Pi's live path (`src/preprocessing/live.py`, the daemon). It already
  works on bounded windows.
- Torch training cost. It runs on a CPU with no GPU, which is acceptable at
  this scale.
