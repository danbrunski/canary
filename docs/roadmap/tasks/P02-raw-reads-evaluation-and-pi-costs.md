# P02 — the next costs: whole-session reads, the evaluation harness, the Pi's write and tick path

Opened 2026-09-26 (operator: "Write up the task checklist for these issues"),
after [P01](P01-derived-session-frames.md).

P01 made three changes:
- the training and evaluation chain dropped from 12–15 GB per long session
  to about 1.3 GB;
- each session's 1 Hz frame is stored once;
- the A02.11 evaluation dropped from 13.3 GB to 4.3 GB (P01.5).

Three costs remain:
- readers that still load whole sessions;
- the evaluation harness's own compute;
- the Pi's write path and its tick.

## Measured facts (2026-09-26)

**Raw storage.** Measured on one 2 h 23 min drive, `2026-09-20T152758`:

- **Files.** 1,980 parquet files, each one row group of about 10,000 rows.
  That is the writer's `max_rows=10_000` with `compression="snappy"`
  (`src/storage/parquet_writer.py`). The drive holds 17.9 M rows in 160 MB,
  about 2,090 rows/s. Walking the footers alone takes 3.07 s.
- **Row types, mid-drive file.** `can` 69 %, `decoded` 29 %, `obd` 0.9 %,
  `gps` 0.6 %, `derived` 0.3 %. The `decoded` rows can be recomputed from the
  CAN payloads stored beside them. P01 round 2 recomputed them: none of
  2.70 M same-instant pairs differed, across four sessions.
- **Bytes per column.** `timestamp` 56 %, `payload` 21 %, `channel` 7 %,
  `metadata_json` 7 %.

As P01 derived frames, the whole 67-session corpus is 53 MB, about 1 % of
its raw volume.

**Most of what is stored repeats itself** (same drive, every row counted,
2026-09-26):

| rows | share | what they are |
|---|---|---|
| 12.5 M CAN frames | 69.9 % | 33 ids at 1,461 frames/s |
| 5.07 M `decoded` | 28.3 % | 7 channels at ~84 Hz each |
| 127 k `obd` | 0.7 % | 51 polled channels, 14.9 rows/s |
| 191 k `gps` + `derived` | 1.1 % | |

The engine data CANary actually asks for is under 1 % of what it stores.

- **CAN frames.** 41.3 % repeat their id's previous payload exactly.
  - **By binding.** The 17 ids a binding decodes carry 63.4 % of the
    frames, and 32.2 % of them repeat. The 16 ids nothing decodes carry
    36.6 %, and 57.2 % of them repeat.
  - **Practically constant ids.** Six ids repeat at least 99.95 % of the
    time: 0x2A5, 0x23E, 0x215, 0x216, 0x5C5 and 0x385. Together they send
    287 frames/s, a fifth of the bus.
  - **Counter ids.** Twelve ids change on every frame, presumably from
    counter or checksum bytes. One is 0x6F7: its wheel-speed channels repeat
    95 % of the time, yet its payload never does.
- **`decoded` rows.** 94.9 % repeat their channel's previous value. For
  example, `ECM_COOLANT_TEMP` is stored at 83.7 Hz and repeats 96.9 % of
  the time. All of these rows can be recomputed from the payloads.

**Readers that load whole sessions.**

- **`resample_and_segment`.** It uses P01's part-by-part reduction only when
  a derived store is active:
  - Only `scripts/retrain.py` and `scripts/fingerprint.py` activate a store,
    and never on a Pi (`active_store()` returns `None` there).
  - Every other caller loads the whole session into the D03.6 long-form
    reduction. On the laptop that measured 390 B per raw row (2026-09-22).
  - On the Pi this covers the trip-end digest and dashboard classification.
    On the laptop it covers `label_sessions`, the trip-summary sidecar and
    `autotag`.
  - The digest child's budget assumes 800 B per row. Under a 4 GiB
    `RLIMIT_AS` that allows about 5.4 M rows (`digest_row_budget` in
    `src/control/trip_end.py`).
  - The drive above has 3.3 times that many rows, so on the Pi it gets a
    `digest_skipped.json` instead of a digest.
  - The part-by-part chain (`iter_session_numeric_parts` →
    `reduce_numeric_session` → `resample_numeric_reduced`) matches the
    whole-session chain exactly (P01 round 2). It just never runs without a
    store.
- **`src/analysis/channels.py::read_channels`.** It calls `read_session(path)`
  with no row filter, although `read_session` accepts pyarrow `filters`. With
  `acquired_time=True` it also parses `metadata_json` for every row.
  - **Callers:** the tuning frame, lambda delivery, the workbench,
    `compare_channels`, broadcast discovery and `rom_trace`.
  - **The tuning digest** makes about six full reads per session through it:
    one batch, one per bank and three single-channel reads. A bad spec sends
    it to a per-spec fallback, which adds more.
  - **MCP `_capability_payload`.** It rebuilds the tuning frame and took over
    5 minutes per capture.
  - **`test_digest_reproduces_the_reference_capture`.** It took 296 s run
    alone and 590 s under load. It skips where its capture is absent: in CI
    and in the push gate's worktree. It is not in the slow tier, so a plain
    local `pytest` run includes it.

**Evaluation harness** (A02.11, after P01.5).

- **Time and peak.** 14 min 53 s at 4.26 GB with a warm stream cache;
  19 min 50 s at 4.71 GB from a fresh `--cache-dir`. The peak now comes from
  `window()` during the injection sweep.
- **Injections.** `scripts/evaluate_detectors.py::injection_sweep` runs
  `featurize_wide_frame` over the whole slice for each of A02.11's 232
  injections.
- **Workers.** `--workers` parallelizes only session loading. Scoring is
  serial, and A02.11 runs with `--workers 1`.
- **Stream rereads.** `CachedStreams` rereads a stream's cache file every
  time it is accessed. Whether a sweep does that once per injection or once
  per drive has not been measured.
- **DBC loads.** `feature_pipeline._load_decode_inputs` loads the DBC on every
  call.

**Pi tick.** On the truck (2026-09-24, 300 calls), `stages.sequence` had a
median of 344 ms per tick, the largest stage. It runs an evidence-role
detector with a pooled rate of 40.0 false alarms per engine-hour (A02.11).
Live featurization runs off the event loop (D01.4), but the worker still
recomputes the rolling frame over the whole buffered window every tick.

**Dev loop.**
- The last two push gates took 351 s (`7bd3dfc`) and 223 s (`2eb4911`) on
  2026-09-26. The budget is about 1.5 minutes.
- Past 120 s, `scripts/ci_gate.sh` prints "re-measure with
  scripts/mark_slow_tests.py".
- `tests/slow_tests.txt` lists 134 tests and predates this week's.

## Rules

- **Parity is the acceptance, as in P01.** Outputs match today's:
  - exactly (`check_exact`) where the arithmetic is unchanged;
  - within a named tolerance, with identical verdicts, where summation order
    changes.

  Test on committed fixtures and on real sessions under `~/canary-data`, and
  name the sessions in the evidence.
- **Measure before and after.** Record peak RSS and wall time on named
  sessions. A speed-up without numbers is not ticked.
- **Raw parquet stays the source of truth.** New layers are rebuildable
  caches: laptop-only (P01's `refuse_on_the_pi`) and gitignored. Nothing
  rewrites or deletes a historic session as a migration shortcut; compaction,
  if any, writes a derived copy.
- **A Pi path never depends on a cache.** Whatever the Pi runs must work with
  no store present.
- **Pi items are measured on the Pi.**
  - Read `/status.vehicle_state` and the capture state first.
  - Never analyse on the Pi while it collects.
  - Deploy through the pull or bundle path, and restart with
    `scripts/restart_canary.sh`.
- **A storage-format change is readable by every reader, old sessions and
  new.** The corpus will be mixed, so the reader tests run on a session that
  contains both.

---

## Desk (laptop)

- [~] **P02.1 Every `resample_and_segment` caller on the part-by-part
  reduction, store or no store.**
  - Make P01 round 2's part-by-part chain the implementation of
    `resample_and_segment` whether or not a store is active. Memory is then
    bounded by one part plus the 1 Hz cells for every caller, including the
    Pi's trip-end digest and classification. The store only adds reuse.
  - Re-derive `DIGEST_PEAK_BYTES_PER_ROW` and the row budget from the new
    chain's measured peak. The budget may become a bound on duration and
    channel count rather than raw rows. Either way, it keeps a refusal that
    names its cause.
  - Fix the stale text. `trip_end`'s module docstring still describes the raw
    pivot and two session loads; `canary.example.yaml` still says 2,200 B/row
    and about 1.95 M rows.

  **Accept:**
  - The segmented frame, digest and fingerprint match today's exactly
    (`check_exact`), both with and without the store, on three sessions:
    - `2026-09-18T152144`;
    - `_capture-fuel-target-research-2026-09-21T195844`;
    - `2026-09-20T152758`.
  - `2026-09-20T152758` digests inside a 4 GiB `limit_address_space` on the
    laptop.
  - Peak RSS on the D03.6 benchmark session `2026-09-18T144215` is compared
    with its 1,490 MB today.
  - With the Pi connected, the digest child's peak is measured on a drive
    over the old budget.

  *Implemented and verified at the desk 2026-09-26. Remaining:* the Pi
  measurement (last Accept bullet), and the capture row budget (operator
  decision below).
  - **The part-by-part chain was not bounded, and now is.** Its parts had to
    end on a 1 s bin edge, and the writer starts a chunk in the same second
    the last one ended, so `2026-09-18T144215` (558 chunks) read as one part
    and `2026-09-20T152758` (1,980) as two: the whole session in memory, store
    or not. `iter_session_numeric_parts` now carries each part's last,
    possibly incomplete, bin into the next part instead (119-166 parts on
    `152758`). `resample_and_segment(value="value_number")` runs
    `reduce_numeric_session` whether or not a store is active; the store
    only reads the same cells back. The unbounded `_read_session_numeric` is
    gone from `src` (the test keeps it as its reference). `_reduce_part` takes
    a float column's one "observed" mask instead of three per channel and part
    (identical cells; without it the chain ran 2.4x slower).
    `test_without_a_store_the_value_number_chain_reads_one_part_at_a_time`
    (reads summed = session rows, largest read < half) and
    `test_the_value_number_chain_reads_the_store_and_matches_raw` (whole-session
    reference = parts, and = the no-store segmented frame).
  - **Parity** (`check_exact` segmented frame; fingerprint, classification,
    trip summary and digest compared as dicts), HEAD's whole-session chain vs
    this tree without a store and with one (built, then reused in the same
    run), each on the stored-decoded original and a stripped copy (decoded
    rows removed): all identical.

    | session | frame | orig | stripped | no store | store |
    |---|---|---|---|---|---|
    | `2026-09-18T152144` | 805 x 201 | 1.75 M rows | 1.24 M | = | = |
    | `_capture-fuel-target-research-2026-09-21T195844` | 1,193 x 120 | 2.87 M | 2.05 M | = | = |
    | `2026-09-20T152758` | 8,571 x 201 | 17.9 M | 12.8 M | = | = |
    | `2026-09-18T144215` | 2,359 x 201 | 5.57 M | 3.95 M | = | = |

  - **4 GiB.** `compute_digest` of `2026-09-20T152758` inside
    `limit_address_space(4 GiB)`: completes at 494 MB (orig) and 502 MB
    (stripped), digest identical to HEAD's unlimited one. Four back-to-back
    time-shifted copies of it (34,458 s, 51.4 M rows): 1,220 MB unlimited,
    1,255 MB completing inside 4 GiB.
  - **Peak RSS, before -> after** (laptop, fresh process each, back to back):

    | session | `resample_and_segment` | `compute_digest` |
    |---|---|---|
    | `144215` orig (D03.6: 1,490) | 1,568 -> 337 MB | 1,592 -> 341 MB |
    | `144215` stripped (P02.8: 1,509) | 1,555 -> 360 MB | 1,544 -> 347 MB |
    | `152758` orig | 4,237 -> 505 MB | 4,247 -> 493 MB |
    | `152758` stripped (P02.8: 4,334) | 4,190 -> 490 MB | 4,265 -> 503 MB |

    Wall time, same process alternating (`resample_and_segment` only):
    `144215` 8.3-8.8 -> 9.1-10.2 s, `152758` 26.7-31.7 -> 35.3-35.4 s.
  - **The budget is now two rules** (`digest_preflight`, `DigestBudgets`):
    - *Span, every digest* (`over_span_budget`,
      `daemon.digest_span_budget_seconds`). The chain's peak follows the
      recorded span: 322 / 347 / 503 / 1,220 MB at 805 / 2,359 / 8,571 /
      34,458 s, ~290 MB + ~27.7 KB/s at ~200 1 Hz columns. Derived as
      (address space - `DIGEST_CHAIN_FIXED_BYTES` 1 GiB) /
      `DIGEST_CHAIN_BYTES_PER_SECOND` 64 KiB/s: ~13.7 h for the Pi's 4 GiB
      child (margins ~3.5x fixed, ~2.3x slope: RLIMIT_AS counts virtual
      memory, the Pi's allocator differs, wider profiles widen the frame). A
      ceiling below the fixed cost gets 1 s, so everything is refused by name.
      Read from footer timestamps (`session_time_span_seconds`). The
      workbench's `classify` job uses it too (`select_sessions(...,
      reads="value_number")`); raw-read jobs keep the row budget.
    - *Rows, captures only* (`over_row_budget`, unchanged 800 B/row): the
      tuning section still reads a capture whole
      (`tuning_digest.has_tuning_section`, one predicate for both).
    - `digest_skipped.json` schema 2 adds `span_seconds` and
      `span_budget_seconds`; `row_budget` is `null` where no row budget
      applies. Tests: `test_a_session_over_the_span_budget_is_refused_and_no_child_starts`,
      `test_a_capture_over_the_row_budget_is_refused_and_no_child_starts`,
      `test_an_ordinary_drive_is_not_budgeted_by_rows`,
      `test_the_span_budget_is_derived_from_the_address_space_ceiling`,
      `test_classification_route_refuses_over_the_span_budget`,
      `test_classification_route_is_not_budgeted_by_rows`.
  - **Found: out of memory in the tuning section read as absent data.** Its
    reads caught `MemoryError` as an absent channel: under 4 GiB the stripped
    fuel-target capture's digest "completed", saying the required-tuple
    members were never captured, with no rates for seven polled channels.
    `MemoryError` now leaves the section, so the child reports
    `child_out_of_memory`
    (`test_a_tuning_section_out_of_memory_is_the_childs_refusal_not_absent_data`).
  - **Operator decision: the capture row budget.** The tuning section alone
    peaks at 1,408-1,595 B per stored row on four stripped captures (1.31-2.83 M
    rows, 2.0-3.8 GB), and the 2.05 M-row capture runs out inside 4 GiB, so
    800 B/row (~5.4 M rows) lets captures of ~2-5.4 M rows start a child that
    dies. Tightening it to ~3,000 B/row (~1.4 M rows, ~14 min) also tightens
    the workbench jobs that share it. Not changed here; P02.2 removes the row
    term.
  - Stale text fixed: `trip_end` module docstring, the budget constants,
    `DaemonConfig`, `canary.example.yaml`, the MCP digest tool doc (snapshot
    regenerated), `classify_and_write`, and `.claude/commands/drive.md`
    (which still said to restart the daemon near parking to keep sessions
    small).

  Size **M**.

  - **Verifier, 2026-09-26.**
    - Parity: 40 of 40 configurations identical (no store, store built,
      store reused, a HEAD-built store read by the new code; four sessions,
      original and stripped). The cells are unchanged, so the store needs no
      version bump.
    - Two defects, since fixed:
      - `tuning.read_delivery_inputs` still turned out-of-memory into "inputs
        never captured". It now re-raises, and
        `test_delivery_inputs_out_of_memory_is_not_inputs_never_captured`
        pins that site.
      - The stale 1 GiB-floor comment: a floor-sized child (a host with 2 GB
        of RAM or less) now refuses every digest and classify job by name,
        because the chain aborts under a 1 GiB limit.
    - Known limits, recorded rather than fixed:
      - The span budget's 64 KiB/s slope is 2.3x the ~27.7 KB/s measured at
        ~200 columns, so a profile above ~460 1 Hz columns (all-DID or
        memory captures, other vehicles) outgrows it. That surfaces as
        `child_out_of_memory`, not a named over-budget refusal.
      - `DIGEST_TIMEOUT_SECONDS=900` is not measured against the 13.7 h
        admission: about 200 s on the laptop, possibly over 900 s on the Pi.
        This joins the Pi measurement.
      - Chunks without footer statistics skip the pre-flight and read as one
        part. Narrow, since pyarrow writes statistics by default.
      - Captures of ~2–5.4 M rows pass the row budget and fail as
        `child_out_of_memory`. Named and safe; it waits for P02.2 (operator
        default recommended).

- [ ] **P02.2 Push channel selection into the raw read.**
  - `read_channels`, and every other `read_session(...)` followed by a channel
    filter (grep for them), passes the rows it needs down as `filters` and
    reads only the columns the result uses.
  - Each spec maps to its raw rows:
    - `obd:`, `did:`, `mem:` and `kline:` read their own rows;
    - `decoded:` and `can:` read the `can` rows of their binding's CAN id, plus
      stored decoded rows where a path still prefers them.
  - Reuse P01 round 2's translation (`decoded_subset_plan` / `keep_can_ids`)
    instead of writing a third one.
  - Give the tuning digest one filtered read for all its specs instead of six
    reads.

  **Accept:**
  - `read_channels` output matches today's exactly (`check_exact`) on three
    tuning captures.
  - Peak RSS and time are recorded before and after for:
    - `build_tuning_frame` and MCP `_capability_payload`, on two captures;
    - `test_digest_reproduces_the_reference_capture`.

  Size **M**.

- [ ] **P02.3 A per-sample channel layer for acquisition-time readers.**
  - **Why.** P01.2 left the workbench on raw samples: it pairs channels
    sample by sample, and a 1 Hz frame would bring back the aggregate
    comparison AGENTS.md forbids. P01.2 records that exclusion, and this
    layer is the per-sample alternative to it.
  - **What.** Extend P01's store with each channel's decoded samples at
    acquisition time. The layer holds exactly what `read_channels` returns,
    not the 1 Hz grid, versioned by P01.3's per-unit hashes.
  - **Readers.** `compare_channels`, pairing, the tuning frame, lambda
    delivery and the workbench then read only the channels they name.
  - **Scope.** Laptop-only.

  **Accept:**
  - The layer matches `read_channels` exactly (`check_exact`, including
    `timestamp_basis`).
  - A second read of a capture's tuning channels touches only the layer
    (measured).
  - A binding edit invalidates only that binding's channel.

  Size **M–L**. P02.2 first: it uses the same spec-to-raw translation, and
  after it this item may not pay for itself. Measure before starting.

- [x] **P02.4 Featurize an injection incrementally.** `injection_sweep`
  recomputes only the columns an injected channel can reach, and reuses the
  clean featurized frame for everything else. Reachable columns are:
  - the channel's own rolling features (`__mean`/`__std`/`__roc`/`__gap`);
  - derived channels whose declared inputs include it
    (`DERIVED_CHANNEL_INPUTS`, `ANOMALY_SIGNALS`, the vehicle-signal modules);
  - behavior deviations whose models consume it;
  - the operating mode, but only if the channel is a segmenter input.

  Also measure how often a sweep rereads its `CachedStreams` file. If it is
  once per injection, make it once per drive.

  **Accept:**
  - Incremental output equals full featurization exactly (`check_exact`) for
    every realistic fault, on two drives.
  - The A02.11 report is identical apart from `generated_at` and `command`.
  - Wall time is recorded before and after.

  Size **M**.

  *Done 2026-09-27.*
  - **Where.** `src/preprocessing/feature_pipeline.py::featurize_perturbed_frame`,
    called by `DriveScorer.injection_sweep` in `scripts/evaluate_detectors.py`.
    It finds the changed columns by comparing the injected slice with its
    clean slice; it never takes them on trust. It then recomputes:
    - derived channels reached through `DERIVED_CHANNEL_INPUTS` (and the
      optional inputs), transitively, from their declared inputs only
      (`derived_channels_reached`);
    - deviations whose models name a changed channel anywhere in their
      definition (`behavior_model_input_roots`);
    - rolling features for the changed columns only.

    It re-segments only when a segmenter input changed. If any row's mode
    moves, or the columns differ, it runs the whole transform.
  - **Declarations enforced by test**, not trusted.
    `tests/test_feature_pipeline_incremental.py` scrambles every *undeclared*
    column and requires each derived channel
    (`test_every_derived_channel_reads_only_its_declared_inputs`) and each
    behavior deviation
    (`test_every_behavior_model_reads_only_the_channels_it_names`) to stay
    put. `check_exact` against `featurize_wide_frame` covers every realistic
    fault x severity on the committed ground-truth slice
    (`test_every_realistic_fault_featurizes_incrementally_exactly`, 20 cases).
    It also asserts that the full path runs only when a mode moved. Random
    3-channel perturbations and the unchanged/new-column cases are covered as
    well.
  - **Two real drives, `check_exact`.** `2026-09-20T152758` (8,570 rows) and
    `2026-08-03T163440` (1,883 rows): 80 featurizations (5 faults x 4
    severities x {harness-style slice, whole session}), all equal. 4 took the
    full path: the thermostat fault over the whole of 152758 moves operating
    modes. Featurization alone took 69.1 s through the full transform and
    34.9 s incrementally.
  - **Stream rereads, measured.** The HEAD harness was instrumented with a
    log line per `CachedStreams` read. 12 streams were read twice per drive
    and 44 once (68 reads for 56 streams). The twice-read stream is each
    in-era drive's longest session: once for clean scoring, once for the
    whole sweep. No stream was read once per injection. `DriveScorer` now
    reads every stream once per drive (56 reads), and a fully cached re-run
    reads none.
  - **A02.11 report, HEAD vs this change.** The command is the R04.6
    default-profile one from A02.11, with `--derived-dir
    data/cache/derived_sessions`, `--workers 1 --scoring-threads 0
    --no-score-cache`, so torch keeps its default threads as before. Both
    reports are identical apart from `generated_at`, `command` and
    `behavior_models.path`, which names each run's snapshot directory: 18
    drives, 184 injections, 1,472 outcomes. Both trees ran from snapshot
    copies in the session scratchpad.

    | stream cache | before (HEAD) | after |
    |---|---|---|
    | cold (56 streams built) | 1,195.7 s, 4.55 GB | 1,014.6 s, 4.23 GB |
    | warm | 807.1 s, 4.25 GB | 723.0 s, 4.14 GB |

    Wall time is from `rss_watch`; peak is the process tree's RSS. The
    saving is modest because scoring, not featurization, dominates. P02.5
    takes the rest.

- [x] **P02.5 Score drives in parallel, and cache scores.**
  - **Parallel scoring.** Drives are independent, so score them across
    processes. Size the pool by the measured peak per worker (about 4.3 GB for
    one worker after P01.5). Pin torch threads per worker so the workers do
    not oversubscribe the cores.
  - **Score cache.** Cache each detector's scores, keyed by:
    - artifact sha256;
    - derived-frame key;
    - injection spec;
    - code version.

    A re-run then rescores only what changed. Plausibility streams are already
    cached this way (P01.5). Retrain's benchmark and alert-budget gates reuse the
    cache for the golden/baseline artifact.
  - **Catalogs.** Load the DBC and catalogs once per process.

  **Accept:**
  - The A02.11 report is identical at `--workers 1` and `--workers N`.
  - Wall time and peak RSS are recorded for N = 1, 2 and 4.
  - An unchanged re-run completes from the cache (measured).
  - A changed artifact is rescored only for its own detector.

  Size **M**.

  *Done 2026-09-27.* All runs use the A02.11 R04.6 command (see P02.4) on
  the laptop, with the stream cache warm. `--no-score-cache` is set unless a
  row says otherwise.
  - **Parallel scoring.** `score_drives` in `scripts/evaluate_detectors.py`
    runs `DriveScorer.score_drive` per drive in a **spawned** pool, largest
    drive first. Spawned, not forked: the parent has already run torch.
    Results are merged in drive order. A broken pool, such as a worker
    killed for memory, falls back to scoring in-process and reads back what
    the workers finished.
  - **Pool size.** `scoring_workers` caps `--workers` by the drive count, by
    cores ÷ torch threads, and by memory: 0.8 x the lesser of `MemAvailable`
    and the cgroup headroom, ÷ `SCORING_WORKER_PEAK_BYTES`. The headroom is
    the least `memory.max - memory.current` over the process's group and its
    parents up to the root (`max` counts as unlimited). The group is found
    through `src.daemon.health.memory_limit_status`; this is what caps
    `canary.service`'s `MemoryMax`. That constant is 3.3 GB, the
    measured worker peak, and it assumes every worker peaks at once. On this
    16 GB laptop, `--workers 4` therefore runs 3. **On the Pi** (`on_the_pi()`)
    scoring always runs in-process, with no pool and no refusal. Tests:
    `test_the_scoring_pool_is_capped_by_the_measured_worker_peak`,
    `test_the_scoring_pool_respects_the_cgroup_and_stays_in_process_on_the_pi`,
    `test_the_cgroup_headroom_is_the_tightest_level_up_to_the_root`.
  - **Threads.** `--scoring-threads` (default 1) pins torch's thread count
    in the parent and in every worker, and is part of the unit key.
    Measured: reports at 1 thread and at torch's default (6 here) differ in
    21 fields. They are the `vus_pr` estimates and bounds (max |Δ| 8.9e-6)
    and `sequence_parity.max_score_difference` (1.18e-7 vs 1.31e-7). No
    verdict, episode, rate or recall differs. The count is therefore fixed,
    never derived from `--workers`. `--scoring-threads 0` keeps the pre-P02.5
    behaviour, and P02.4 used it for the HEAD comparison.

    | workers | wall | tree peak RSS | peak per worker | report vs N=1 |
    |---|---|---|---|---|
    | 1 | 814.5 s | 4.09 GB | (in-process) | — |
    | 2 | 561.9 s | 6.80 GB | 3.27 GB | identical |
    | 3 (asked 4; capped by memory) | 492.9 s | 8.15 GB | 3.29 GB | identical |
    | 4 (memory cap lifted in a benchmark wrapper) | 443.9 s | 9.40 GB | 3.25 GB | identical |

    "Identical" means apart from `generated_at` and `command`: 18 drives,
    184 injections. The worker peak comes from the worker that scores the
    drive holding `2026-09-20T152758`.
  - **Score cache.** `src/models/evaluation_cache.py`, under
    `--cache-dir/scores` (gitignored `data/cache/`, `refuse_on_the_pi`).
    `--no-score-cache` opts out.
    - **Component key.** The artifact's sha256 plus its manifest's. A fitted
      baseline uses its pickled parameters and threshold. The untrained AE
      uses the sequence identity, seed and threshold. Plausibility uses its
      limits-catalog fingerprint. Fused uses the IF and sequence identities.
    - **Unit key.** The session frame by content (`frame_key`), the slice
      bounds, the injection spec (kind, severity, start, end, roles), and
      the featurization inputs: the whole config, behavior-models and
      bindings sha256, `data/*.yaml`, the DBCs, the profile's files, and the
      thread environment.
    - **Both keys** carry `code_version`: every `src/**/*.py`, the harness
      and `_cli.py`, the Python version, and the numpy/pandas/sklearn/scipy/
      torch versions.
    - **A run keeps nothing it cannot prove.** The code and the config file
      are hashed first, at the start of `main`, before the config is parsed.
      Every other key input is hashed *before anything loads*
      (`KeySources` / `key_inputs`): each artifact with its manifest, the
      bindings, the behavior models, and the data catalogs. The catalogs
      include two files that can sit outside `data/`: the unscoped config's
      `daemon.afr_calibration`, which is the file featurization reads
      (`test_the_key_hashes_the_afr_calibration_featurization_reads`), and
      the profile-scoped `plausibility.yaml` that the plausibility detector
      reads. Entries are staged
      under `scores/.staging/<run>/`. A scoring worker re-hashes the inputs
      before it loads anything. On a mismatch it refuses, naming the changed
      inputs, and the parent scores in-process. After scoring, the parent
      re-hashes again. Only if nothing moved does it commit the staged
      entries into the store; otherwise it discards them and warns (`score
      cache not updated: key inputs changed during the run: <names>`). A
      failed or interrupted run drops its staging (`ExitStack` callback).
      Test: `test_an_input_edited_mid_run_leaves_no_entry_under_either_key`
      (1 and 2 workers). It edits the bindings between key and score, then
      asserts that no entry exists in the store, that the workers refused,
      and that the report is still written. *Known limit:* a file edited and
      then reverted to its original bytes while the parent (which loads once
      at the start) is running is not detected, because every re-check sees
      the original bytes again.
    - **Bounded.** At the end of a committed run, `ScoreCache.prune` keeps the
      store under `SCORE_CACHE_MAX_BYTES` (1 GiB, about 29 A02.11 entry sets
      at 35 MB each; a changed IF adds about 12 MB). It deletes
      least-recently-used entries first, where a hit refreshes the entry's
      mtime. It never deletes an entry the
      current run read or wrote (mtime at or after the run's start), and it
      removes staging directories older than 24 h, left by runs that died.
      Test: `test_the_score_cache_stages_until_commit_and_prunes_least_recently_used`.
    - Results after the first verifier fixes, fresh store (`K` runs,
      2026-09-27):
      - **Cold cache, `--workers 4`** (3 workers): 510.4 s, 8.19 GB tree,
        0 of 2,374 lookups hit. No staging was left behind, and the report
        is identical to the N=1 no-cache one. After the changed-artifact run
        below, the store held 47 MB.
      - **Unchanged re-run, `--workers 1`:** 149.6 s, 1.79 GB. 2,350 of
        2,350 lookups were read back, no stream was read, and nothing was
        featurized. The report is identical. At `--workers 4` (3 workers):
        113.1 s, also identical. The remaining time is loading and hashing
        the 67 derived frames.
      - **`--workers 2`, no cache:** 551.9 s, identical.
      - **Re-confirmed on the final code** (`L` runs, after the D1/D2 fixes,
        fresh store). Cold at `--workers 4` (3 workers): 503.9 s, 8.24 GB,
        35 MB committed, no staging left. Unchanged re-run at `--workers 1`:
        150.1 s, 1.81 GB, 2,350 of 2,350 hits, no stream read. Both reports
        are identical to the N=1 no-cache report.
    - **Changed artifact.** The IF `model.joblib` was re-serialized, so the
      model is the same and the sha256 is new (`6b010b70…`). `isolation_forest`
      and `fused` were rescored, 294 of 294 units each missed; `fused`
      consumes the IF. `sequence`, `pca`, `robust_z` and
      `untrained_autoencoder` were read back (294 of 294 each), and so was
      `plausibility` (286 of 286). Wall time was 327.7 s. The report differs
      only in `artifacts.isolation_forest.path` and `.sha256`.
    - Fixture tests: `test_workers_and_the_score_cache_leave_the_report_unchanged`
      proves that the pool ran (both drives' results carry a worker pid) and
      forbids `featurize_wide_frame` on the re-run. Also
      `test_score_cache_keys_follow_content_and_entries_round_trip_exactly`.
  - **Retrain's gates reuse it.** `retrain._evaluate_on_drives` already
    passes `--cache-dir` beside the feature cache.
    `test_run_benchmark_gate_runs_the_evaluation_protocol_and_discovers_prior_baseline`
    asserts that the IF score entries keep their inode through a second
    benchmark gate and through the alert-budget gate's evaluation. A
    rescored entry is a new file renamed into place; a hit refreshes only
    the mtime.
  - **Behaviour change on the Pi.** The score cache and the derived store
    both refuse on the Pi. A Pi-side retrain (`on_the_pi()` or
    `--compute-target pi`) therefore now passes `--no-score-cache` and
    `--no-derived-store` to its gates, which reduce each session in memory
    and score it afresh. Test:
    `test_the_gates_score_without_the_score_cache_on_the_pi`. Before this,
    the score cache would have failed the gate. A derived store passed on
    the Pi made every session load fail, and the harness then exited 0
    having scored no drive.
  - **An empty result names its kind.** When no evaluation drive loads (or
    no requested session loads at all), the harness now exits 2 without
    writing a report. It prints `error: no evaluation drive loaded; N
    session load(s) failed:` followed by each session's error. Retrain
    raises on that exit code. Test:
    `test_zero_drives_because_every_load_failed_is_refused_and_named`. It
    simulates the Pi against a derived store, gets exit 2 naming `never
    built on the Pi`, and gets the same drive scored with
    `--no-derived-store`.
  - **Other.** `_profile_data_dir` now resolves `data/<slug>` from the repo
    root, not the working directory.
  - **Catalogs.** `default_decode_inputs` is memoized per process and keyed
    on the files' bytes, so an edited catalog reloads. DBC loads per A02.11
    run went from 67 to 1 per process: 1 at `--workers 1`, and one per
    loading worker otherwise. Test:
    `test_decode_catalogs_load_once_per_process_and_reload_on_an_edit`.

- [ ] **P02.6 Re-measure the slow tier.**
  - Run `scripts/mark_slow_tests.py` on a fresh full timing log and regenerate
    `tests/slow_tests.txt`.
  - Check that this week's new tests land where their measured times put them:
    the derived store, bounded resample, committees, lambda-delivery live
    parity, the replay event diff and the passive restart.
  - Place `test_digest_reproduces_the_reference_capture` by its measured time,
    unless P02.2 has already made it fast.

  **Accept:** on a quiet laptop the push gate is back near its ~1.5 min
  budget, with the time recorded, and CI still runs the slow tier.

  Size **S**.

## Pi (measure with the Pi connected)

- [ ] **P02.7 Cheaper raw storage, measured on the Pi first.** Benchmark
  writer options on the Pi against the rows of a sustained capture. The
  storage writer has been the bottleneck before: under a 25 req/s budget it
  dropped 2,491 records. Options:
  - (a) delta encoding for `timestamp`, 56 % of the bytes;
  - (b) zstd at a low level instead of snappy;
  - (c) chunks larger than `max_rows=10_000` under the same flush-latency
    bound, so fewer files per drive;
  - (d) typed columns instead of `metadata_json` on every row.

  Choose by writer CPU and `queue_depth` under load, not file size alone. Old
  and new files must both stay readable, and no historic session is rewritten.

  **Accept:**
  - Writer CPU and queue depth are recorded, before and after, under a capture
    at about 116 req/s.
  - Bytes per engine-hour are recorded.
  - The tar pull time for a 2 h drive is recorded.
  - Every reader test passes on a mixed old/new session.

  Size **M**, Pi.

- [~] **P02.8 Decide whether to keep persisting decoded rows.** 28 % of stored
  rows are `decoded` values: seven channels at ~84 Hz, 95 % of them
  repeating the previous value. They can all be computed from the payloads
  stored beside them. Recomputing gives the same values (P01 round 2), and
  P01's decode recomputes every binding anyway. Plausibility's stream loader
  still prefers stored rows where they exist.

  *Decided 2026-09-26 (operator): drop them.* New sessions store no `decoded`
  rows, and every reader decodes on read. Old sessions keep theirs; they are
  never rewritten.

  **Do:**
  1. List every reader of stored `decoded` rows: `query_telemetry` over the
     parquet, the dashboard readers, plausibility, and any others.
  2. Measure on the Pi what dropping the rows would save, in writer CPU and
     bytes.
  3. Measure what decoding on read would cost those readers.
  4. Record the operator decision: keep them, drop them, or keep them only
     for a short live window.
  5. Implement it.

  **Accept:**
  - Numbers for both sides, and the decision recorded.
  - If the rows are dropped, every listed reader returns today's values on a
    stored session.

  *Implemented and verified at the desk 2026-09-26 (Do 1, 4, 5). Remaining,
  on the Pi (Do 2, 3):* writer CPU and bytes before and after under a
  sustained capture (~116 req/s); decode-on-read latency for the Pi-side
  readers (the trip-end digest child, the `/telemetry/latest?per_channel`
  seed and the Parquet `vehicle_state`, `/analysis/binned` and the session
  chart).
  - **Nothing stores a decoded row.** `drain.storable` is the storage sink's
    fanout subscription and is repeated in `run_storage_sink`;
    `ParquetTelemetryWriter.write` refuses one. The storage `FrameDecoder`
    wiring is gone (`storage_decode_for`, `drain_kwargs["storage_decode"]`,
    `run_storage_sink(decode=)`, the harness decoder); the trip tracker keeps
    its own road-speed decode and the live source is untouched.
    `test_the_daemon_stores_no_decoded_row_whatever_config_or_profile_say`:
    the retired config key set to `[confirmed, high]`, a profile carrying
    `broadcast_decode: true` and the decoded rows a legacy replay emits → 20
    frames and 1 obd row stored, 0 decoded; the loader decodes the 20 frames.
  - **Knobs retired, still load.** `collection.persist_decoded_confidence` is
    popped with a logged warning (louder when not `[confirmed]`) and removed
    from the example configs; the profile's `broadcast_decode` is dropped with
    a once-per-process warning, gone from the model, the starter catalog, the
    editor and the TS types. Saved catalogs, the Xterra's included, load
    unchanged. What every reader decodes is `on_read_signals`: `confirmed`
    plus `persist: true` (the only value the key ever shipped with).
  - **Every reader decodes on read, one rule.**
    `preprocessing.decode.decode_on_read` writes exactly the storage
    decoder's rows, each after its frame in catalog order (file-order
    `check_exact` against the stored rows of `2026-09-18T152144`, 1.75 M rows,
    and `_capture-fuel-target-research-2026-09-21T195844`, 2.87 M). The loader
    (`read_session`, `iter_session_parts`, the `value_number` readers) drops
    stored rows and applies it, filters included; `as_recorded` is for the
    training chain (recomputes every binding) and storage accounting. Also:
    drive activity, the dashboard latest-row readers, `fetch_channel_rows`
    (in SQL), broadcast discovery, `pull_report`; plausibility decodes always;
    `decode_can_rows` replaces stored rows. Raw SQL (`query_telemetry`) reads
    a `telemetry` view of the stored rows other than `decoded`, `UNION ALL`
    each on-read binding decoded from its frames in SQL. A query naming
    `decoded` carries `note`, plus `empty_reason` when it finds nothing (no
    rows, or a zero/null aggregate). `tools/backfill_decoded.py` is retired: it
    rewrote sessions to store what is now decoded on read.
  - **Parity:** HEAD on the originals vs this tree on copies with the decoded
    rows removed: `2026-09-18T152144`,
    `_capture-fuel-target-research-2026-09-21T195844`,
    `_capture-tuning-2026-09-21T223734` and `2026-09-20T152758` (17.9 M rows;
    its whole-session reads, `read_channels` and `compare_channels`, were
    skipped). Identical: the `value_number` segmented frame,
    fingerprint, classification, trip summary and digest; the 1 Hz training
    chain (resampled, features, operating frames); `read_channels` of the 7
    decoded specs; 6 `compare_channels` verdicts; drive activity; the latest
    rows and Parquet `vehicle_state`; `fetch_channel_rows` below the row
    limit's last instant (that instant's tie order is SQL's; now broken by
    channel). Plausibility samples and modes are the same rows, each channel's
    sequence identical, only the order of channels within one instant
    differing; evaluator evidence is identical (`152144` and
    `_capture-tuning-2026-09-21T223734`, each clean plus 75 faulted runs over
    15 checked decoded channels: 0 mismatching runs).
    The behavior-validation frame (`mode-inputs`) is identical on `152144`.
    This tree on `152144` as recorded and stripped: every output
    byte-identical. `tests/test_decoded_rows_never_stored.py` (20) repeats
    this per reader on the committed slice, stored, stripped and mixed.
  - **Current catalog, old sessions too (the decision's cost).**
    `2026-08-06T151547` stored 4 of the 7 channels and now reads all 7:
    the segmented frame gains the 3 wheel speeds, classification
    `feature_columns` 63 → 66, fingerprint committee residuals move. July
    sessions stored none: `2026-07-26T225058`'s evaluation frame gains 4
    channels, and its drive grouping moves, because activity now counts the
    decoded broadcast. `decode_can="none"` (historical scope) reads no decoded
    channel at all, so that July frame is otherwise identical.
  - **Numbers, laptop** (HEAD on the original, then this tree on the stripped
    copy, back to back). Parquet bytes −14 % (`152758` 161.6 → 137.7 MB), rows
    −28 %. Digest chain in memory: `2026-09-18T144215` 1,492 → 1,509 MB peak
    (281 → 401 B per stored row); `2026-08-08T214218` 836 → 827 MB (467 B per
    stored row); `2026-09-20T152758` 4,160 → 4,222 MB,
    63 → 80 s (its numeric read 21.6 → 41.8 s: 5.07 M rows decoded).
    `read_latest_per_channel` 438 → 559 ms (`152144`) and 518 → 529 ms
    (`152758`); drive activity 0.27 → 0.63 s and 3.8 → 5.4 s;
    `fetch_channel_rows` of 3 keys over the whole session 2.6 → 4.1 s and 5.3 →
    7.9 s.


  - **Verifier, 2026-09-26: FAIL on three defects, since fixed.**
    - *Raw SQL did not decode on read.* On a stripped copy, `count(*)` of
      decoded `ECM_RPM` was 0, against 76,600 on the original. It now reads
      the view above. `2026-09-23T162918`: 63,027 on the original and on the
      stripped copy. A query that touches no decoded row costs the same on
      both (93 ms). Scans made only of files with no `payload` column (before
      2026-07-29) skip the decoded branch.
      `test_query_telemetry_decodes_on_read_and_names_the_kind_of_empty`.
    - *The digest budget's margin was stated, not measured.* P02.8 cuts stored
      rows ~28 % while the chain decodes them on read, so memory per *stored*
      row rose. `compute_digest` peak RSS on stripped copies:
      - `162918`: 1.08 M rows, 653 MB, 632 B/row;
      - `150658`: 1.35 M rows, 738 MB, 573 B/row;
      - `152758`: 12.84 M rows, 4,334 MB, 354 B/row.

      That is about 314 MB fixed plus ~313 B/row. Short sessions read high
      per row but sit far below the ceiling. Where the budget binds (~5 M
      rows) it is ~376 B/row, so `DIGEST_PEAK_BYTES_PER_ROW = 800` keeps a
      2.1x margin and stays. Its docstring and the `canary.example.yaml`
      comment (which still said 2,200 B/row) now carry these measurements.
    - *A warn-once global was never reset.* `capture_profiles._reset_for_tests`
      is registered in `process_state`, and a test pins the `broadcast_decode`
      warning.
  - **Behaviour changes not listed above.**
    - `read_session_decoded` with no bindings file returns no decoded rows
      (it used to keep stored ones).
    - The validation report drops `decoder_signals`. `decoded_expansion`
      becomes `decoded_rows_stored`. The replay block's
      `rederived_decoded_rows` becomes `emitted_by_replay` /
      `stored_in_source`.
    - `_channel_satisfied("decoded:…")` is always true.
    - `decode_can="none"` drops post-July sessions' stored decoded rows too.

  Size **M**, Pi and operator.

- [ ] **P02.9 Incremental rolling features on the live path.** Keep each
  channel's rolling mean/std/roc and gap state up to date incrementally, with
  constant work per new sample, instead of recomputing the buffered window
  every tick. The result must equal `compute_features` over the same window,
  including pandas' time-window endpoint semantics.

  **Accept:**
  - `test_features_batched_parity`, `test_reconcile_feature_frame_parity` and
    `test_preprocessing_live` pass.
  - Replay output matches on two sessions: exactly (`check_exact`), or within
    a named float tolerance with identical verdicts.
  - Featurization time per tick is recorded on the Pi, before and after.

  Size **M–L**. Built at the desk; measured on the Pi.

- [~] **P02.10 Store CAN frames on change; re-write an unchanged id once per
  stale timeout (operator decision).** On the 09-20 drive, 41 % of CAN frames
  repeat their id's previous payload.

  *Decided 2026-09-26 (operator):*
  - A CAN frame is stored only when its payload differs from that id's
    previous frame.
  - An unchanged id is written again once per *broadcast stale timeout*, so
    its value never lapses. The timeout is one config setting, default 10 s.
    The re-write happens 1 s before the timeout, derived from it rather than
    configured separately. The config refuses a timeout under 2 s.
  - No "stopped" rows.
  - Sub-second frame timing, and per-id frame counts, go to an optional
    capture profile that stores every frame.

  *Measured the same day, same drive:*
  - Change-only keeps 7.35 M of 12.5 M frames.
  - A 1 s re-write would add 94,588 rows (+1.3 %). A 10 s timeout should add
    about a tenth of that; that is an estimate, so measure it.
  - An id went silent for more than a second 818 times.
  - With P02.8 as well, the drive would be about 7.7 M rows instead of
    17.9 M. That is still over the 5.4 M digest budget, so P02.1 stays needed.

  **What changes where:**
  - **Storage sink.** A per-id filter (last payload, last write) with
    per-session state, so every id's first frame in a session is written and
    a session stands alone. The storage mode is recorded in the data itself,
    per chunk, so readers know which spans are change-only. Old sessions
    store every frame.
  - **Resampler.** Broadcast channels (`can:`/`decoded:`/`dbc:`) get their own
    stale class, set to the timeout. OBD polls and other fast channels keep
    2 s. The resampler already gives slow channels their own class (120 s).
  - **Live feature pipeline.** It consumes the same filtered stream as
    storage, so live, stored and replayed features match exactly. Consumers
    that need every frame (freshness, bus health) keep the raw stream.
  - **Readers.** Pairing for broadcast channels uses the value as of the
    instant: the last stored row before it, valid within the timeout. That
    logic lives inside the reader, never in its callers.
  - **Frame-count readers.** Anything that counts stored frames or computes
    rates must refuse a change-only span and name the cause, or point at the
    every-frame profile.
  - **Every-frame profile.** A capture-profile field selects every-frame
    storage, and a starter profile goes in `data/capture_profiles.yaml`.
    Saved catalogs are never re-seeded, so the Pi's catalog needs the profile
    added by hand.

  **Costs:**
  - A module that really stops transmitting keeps its last value for up to
    the timeout before the data shows a gap.
  - The timeout feeds the models' `__gap` features. Moving broadcast channels
    from 2 s to 10 s changes features only during real 2–10 s dropouts. Count
    the grid cells it changes across the corpus. If any change, this is a
    feature-spec change, and the detectors are retrained before deploy (a
    retrain is already owed).

  **Accept:**
  - Change-only copies of real sessions give 1 Hz frames and features equal
    (`check_exact`) to the full-rate originals under the same timeout.
  - The corpus count of cells that change between a 2 s and a 10 s broadcast
    timeout is reported, and it decides the retrain.
  - `tests/test_analysis_ground_truth.py` passes, including its 1:1
    broadcast-versus-Mode 01 check.
  - `compare_channels` gives the same verdicts on three captures.
  - Live features on the filtered stream equal offline features on the
    stored data (replay).
  - A mixed old/new corpus reads, and frame-count readers refuse change-only
    spans with a named cause.
  - On the Pi: rows, bytes and writer CPU are measured before and after.

  **Later, not here.** Masking the counter bytes on the twelve ids that
  change every frame could save more. A mask is per-vehicle knowledge,
  though, and would need its own verification.

  Size **M–L**. Built at the desk; measured on the Pi.

  *Implemented and verified at the desk 2026-09-26. Remaining:* the Pi
  measurement (rows, bytes and writer CPU before and after, last Accept
  bullet); the retrain decision (count below); the optional IMU speed hold
  (below).
  - **One rule, one module.** `src/storage/broadcast_storage.py`:
    `CanChangeFilter` (first frame of an id kept, a changed payload kept, an
    unchanged id re-written at `timeout - 1` s, a backwards clock step kept),
    `CanStorage` (the mode, in every chunk's parquet footer under
    `canary.can_storage`; no key = stored every frame), `session_can_storage`,
    `refuse_change_only` / `ChangeOnlyStorageError` (named cause, points at the
    `broadcast-every-frame` starter), `change_only_copy` (derived copy, refuses
    to write in place). Measured on `2026-09-20T152758` with the real filter:
    12.52 M frames -> 7.36 M at 10 s (10,698 re-writes, +0.15 %); 7.44 M at the
    2 s floor (94,820). Pure change-only 7.35 M. After the verifier's fixes
    (slow-id re-write, diagnostic ids kept): 7.37 M at 10 s. The parity runs
    below predate those fixes, which only add stored rows of unchanged values;
    re-run after them, `_capture-fuel-target-research-2026-09-21T195844`
    (2.87 M -> 2.26 M) is still identical in all three frames and
    `2026-09-18T152144` (1.75 M -> 1.38 M) still differs only at its 4 / 14
    silence-edge rows (0 unexplained, 0 reverse).
  - **Config.** `collection.broadcast_stale_timeout_seconds`, default 10.
    Under 2 s refused naming P02.10; at or past
    `preprocessing.session_gap_seconds` refused (a quiet CAN-only session
    would split at heartbeat gaps). Written in `canary.example.yaml` and
    `canary.pi.example.yaml`. The feature-frame cache hashes it
    (`FEATURE_FRAME_CACHE_VERSION` 6).
  - **Storage sink.** The filter lives in `ParquetTelemetryWriter` (every
    writer of a session, not only the daemon's sink), state reset on every
    session change (rotation, capture, tuning, replay), so every session
    stands alone. `can_frames_suppressed(_total)` count what it did not store.
    `write_records_to_parquet(can_storage=...)` filters exactly as the writer
    does, so a change-only footer never sits on unfiltered frames.
  - **Every-frame profile.** `CaptureProfile.store_every_frame` (refused on
    `default`), starter `broadcast-every-frame` in `data/capture_profiles.yaml`
    (explicit Mode 01 list, not `AUTO`), editor checkbox + TS type. Saved
    catalogs are not re-seeded: the Pi's catalog needs it added by hand.
  - **Resampler.** `ResampleConfig.broadcast_max_gap_seconds` (from the one
    setting via `resample_config_for`); a broadcast channel's ceiling is
    max(own class, timeout), so slow broadcast channels keep 120 s, OBD keeps
    2 s. A broadcast channel's `__arrived` reads "a stored row within the
    timeout" (in change-only storage no row means the same payload again),
    for old and new sessions alike. `refuse_shorter_read_timeout` refuses a
    read with a shorter class than a session was stored with
    (`reduce_numeric_session`, `DerivedStore.load/load_numeric`).
  - **Live.** `LiveFeatureSource` runs its own `CanChangeFilter` (same
    timeout, reset on `reset()`), so features see the stored stream; its
    broadcast `__arrived` mirrors the resampler; `can_freshness_seconds` reads
    the raw frame stamp.
  - **Consumers, raw vs filtered.** Filtered (the stored stream): storage;
    live decoded/DBC values, their gap/arrival flags, features, committees,
    behavior deviations, plausibility `sample_observer`, the live IMU frame's
    decoded speed. Raw (every frame): the dashboard broadcast sink (gauges),
    the GPS/trip tracker (decimates road speed to 2 Hz itself), live CAN
    freshness / `vehicle_state_inputs`, the lambda-delivery `record_observers`
    (called before the filter), DTC/monitor sinks (OBD only), collector and
    queue health counters, `can_activity` (reads the bus itself).
  - **Readers.** `read_channels` marks change-only broadcast specs
    (`attrs[AS_OF_ATTR]`, from the loader's per-chunk footer read);
    `align_pairs`/`compare_channels` pair stored rows as before and give each
    unmatched reference sample the held value (last stored row, within the
    timeout; `held`, `n_held_pairs`); `align_long`/`read_aligned` hold cells
    for the timeout and refuse `cell_agg="mean"`; `concat_channel_frames`
    keeps the rule through the workbench's per-spec reads. `pull_report`
    samples a change-only rpm as a step, not a line.
    `dyno_inputs.read_motion_inputs` (H07.3, coordinator request) reads one
    timeout further back and puts the held frames back (`held_series`).
  - **Frame-count readers.** Refuse by name: broadcast discovery
    (`sweep_session`), validation broadcast cadence (`channel_cadence`
    `refused`). Identities close with the suppressed count: validation
    `loss_accounting`, replay `replay_accounting`. Notes: raw SQL
    (`query_telemetry` returns `can_storage_note` for `'can'`/`decoded`).
    Refused at load: a plausibility stuck rule on a broadcast channel (counts
    identical arrivals). Checked, unaffected: capture quality / C03 tuples
    (OBD rows only), `drives.activity` (presence; heartbeats 9 s < the 300 s
    drive gap; last active second up to 9 s early), dataset health (1 Hz
    gaps via the resampler; `row_count` is stored rows), Parquet
    `vehicle_state` (newest frame; heartbeat < its 10 s threshold at the
    default), binned analysis / resampled time series (resampler).
  - **Parity** (`check_exact`, HEAD-equivalent reads of the full-rate
    original vs `change_only_copy` at 10 s, same session name; value_number
    chain `resample_and_segment`, training chain `load_resampled_session` then
    `featurize_wide_frame`):

    | session | rows orig -> copy | value_number | training 1 Hz | features |
    |---|---|---|---|---|
    | `_capture-fuel-target-research-2026-09-21T195844` | 2.87 M -> 2.26 M | = (1,193 x 120) | = (1,192 x 335) | = (1,192 x 662) |
    | `2026-09-18T144215` | 5.57 M -> 4.35 M | = (2,359 x 201) | = (2,358 x 419) | = (2,358 x 872) |
    | `2026-09-18T152144` | 1.75 M -> 1.38 M | 16 cells, 4 rows | 248 cells, 14 rows | +698 downstream |
    | `2026-09-20T152758` | 17.91 M -> 12.75 M | 16 cells, 4 rows | 194 cells, 11 rows | +592 downstream |

    Every differing 1 Hz cell is a silence edge (0 unexplained, 0 reverse):
    the id stopped transmitting (engine off at `152144` 15:34:02), its last
    *stored* frame was up to 9 s before its last frame, so the copy reads
    stale up to 9 s sooner, the operator's stated cost. Feature differences
    all fall inside the rolling window after such a row. Exact without a
    silence: `test_the_one_hz_grid_is_identical_for_full_rate_and_change_only`;
    the edge pinned: `test_a_module_that_stops_differs_only_at_the_silence_edge`.
  - **Retrain question (reported, not decided).** Over all 67 derived-store
    frames (80,330 1 Hz rows, 15.27 M broadcast cells), P02.10's rules against
    HEAD's change 16,359 value, 16,359 `__gap` and 23,789 `__arrived` cells, on
    1,135 rows (1.41 %) in 28 sessions. The 2 -> 10 s class alone (old arrival
    rule) touches 981 rows / 32,718 cells. Non-zero: by this item's rule a
    feature-spec change, retrain before deploy.
  - **Old sessions' digests, classification and labels shift too** (the
    same rule change, on the `value_number` chain: `resample_and_segment` ->
    trip-end digest, fingerprint, classification, `label_sessions` sidecars).
    On `2026-09-20T205117` (stored every frame) this tree against the same
    tree with the broadcast rules switched off (`is_broadcast_channel` ->
    `False`, i.e. HEAD's 2 s class and arrival rule): the segmented frame
    differs in 7 broadcast channels' values/`__gap` on 8 rows and `__arrived`
    on 9 (21:03:59 onward, 2-10 s after the broadcast stopped), which moves
    `operating_mode` on 5 rows (held `decoded:` values let the segmenter
    label them `koeo`), and `compute_digest` differs only in `mode_seconds`:
    koeo 7 -> 12, unknown 137 -> 132. Switched off, both equal HEAD's, as the
    H02.4 verifier found HEAD + the H02.4 hunks alone equal. Consequences:
    the Pi's trip-end digest and dashboard classification of a recording
    read after this lands differ from the ones written before it, and label
    sidecars recomputed on the laptop move by a few rows per drive; nothing
    is rewritten, the sidecars change only when regenerated.
  - **Ground truth.** `tests/test_analysis_ground_truth.py` 30 passed, with
    `test_the_one_to_one_check_holds_on_the_change_only_copy` (8 cases: same
    thresholds, same verdict, n within 2) and
    `test_the_sweep_refuses_the_change_only_copy`.
  - **`compare_channels` on three captures** (`_capture-fuel-target-research-2026-09-21T195844`,
    `_capture-tuning-2026-09-21T223734`, `_capture-tuning-2026-09-21T232153`;
    10 pairs each, 250 ms): 29 of 30 verdicts identical (refusals included),
    n_pairs identical in all 30. One differs: `232153`
    `can:ECM_COOLANT_TEMP_233 ~ obd:COOLANT_TEMP` `stable` (drift 1.38 %) ->
    `unresolved` (4 held pairs), a quantised coolant beside the 1.5 % bar. The
    held fill keeps the stored-row matcher first; inside a hold, a poll just
    before the next change still pairs with that change. A period-aware
    choice was tried and rejected (it moved verdicts on the decimated
    ground-truth slice).
  - **Live = replay.** `test_live_features_on_the_raw_stream_equal_a_replay_of_the_stored_session`
    (live over the bus stream vs live over the stored rows: `check_exact`,
    across a module stop; live gap/arrival flags = offline resampler on the
    stored session, 85 ticks).
  - **Mixed corpus.** `test_a_mixed_corpus_reads_and_says_which_sessions_are_change_only`,
    `test_frame_count_readers_refuse_a_change_only_session_by_name`.
  - **Tests:** `tests/test_change_only_can_storage.py` (36 after the second
    verifier round), including the dyno steady-cruise leg
    (`speed_too_coarse` without the hold, identical speed and acceleration
    with it).
  - **Verifier, 2026-09-26: FAIL on five defects and a minor one, since fixed.**
    - *Error and remote frames were dropped as repeats.* SocketCAN's
      `CAN_ERR_CRTL` arrives as id `0x4` (RX overflow `0001`, TX warning
      `0008`); the filter dropped 52 of 178 on `152758`. `frame_to_record` now
      tags them (`metadata.frame_kind`: `error`/`remote`, id spelling
      unchanged; no code consumer read them), and the filter keeps every one.
      So it does ISO 15765-4 diagnostic traffic (`0x7DF`, `0x7E0`-`0x7EF`,
      `0x18DB33F1`, `0x18DAxxxx`): each frame is a message, and a sniffed
      ISO-TP transfer needs every segment (+11,183 rows on `152758`; the 1 Hz
      frames decode nothing from those ids). `test_error_frames_are_tagged_and_every_repeat_is_stored`.
    - *The heartbeat lapsed for slow ids.* The re-write fired on the first
      frame 9 s after the last stored one, so a period-P id reached 9 + P s.
      The filter also keeps a frame when its age plus the id's longest recent
      inter-arrival (16 gaps, each at most the timeout) reaches the timeout.
      Lapses of a transmitting id, filter replayed over the stored drives:
      `152144` 16 -> 1, `152758` 26 -> 3, every remaining one on `0x4`, which
      those sessions stored untagged (a live recording tags it and keeps it).
      `test_a_slow_id_never_lapses_while_it_transmits` (1.5/2.5/4/7 s ids:
      1 Hz frames equal to full rate). `REWRITE_LEAD_SECONDS`'s comment says
      what it covers.
    - *A replay slower than real time read live ids as stale* (re-writes
      arrive `(timeout - 1) / speed` apart). `refuse_slow_replay` refuses
      speed < 1 for a source with change-only chunks, naming P02.10, in
      `ReplayController.start` (`ReplayRefusedError`, HTTP 422) and
      `replay_session` (the YAML collector logs it and idles). A replay that
      falls behind real time through load stretches the gaps the same way;
      documented at `refuse_slow_replay`, visible as `effective_speed`, not
      scaled. `test_a_change_only_session_is_not_replayed_slower_than_real_time`.
    - *The shorter-timeout refusal missed the training chain.* The recorded
      timeout now travels with the data: `reduce_long_frames` takes a required
      `can_storage` and `ReducedSession` carries it, so `resample_reduced` and
      `resample_numeric_reduced` refuse whatever built the cells (bounded
      chain, store, plausibility); `to_wide` carries the loader's attrs so
      `resample_wide`/`resample_long` refuse; `resample_long_session` checks
      before its decode. `test_every_training_read_path_refuses_a_shorter_read_timeout`.
    - *Dropout paths.* `anomaly_injection.dropout` keeps a broadcast
      `__arrived` true up to the ceiling (the resampler's rule);
      `train_anomaly_model` takes the ceiling from `max_gap_seconds_for`.
      `test_an_injected_broadcast_dropout_arrives_until_the_ceiling`.
    - *Minor.* `binned_summary` refuses a change-only broadcast spec;
      `tuning_digest._achieved_hz` omits it and `_rate_summary` reports
      `refused` (`test_rows_of_a_change_only_spec_are_never_a_rate_or_a_bin_count`).
      A chunk without CAN frames gets no storage footer, so UpRev imports read
      as before (`test_a_session_without_can_frames_gets_no_storage_footer`).
  - **Verifier, round 2 (2026-09-26): six fixes.**
    - *Replay timeout mismatch.* A change-only session recorded with a longer
      stale timeout than the daemon's re-wrote its quiet ids further apart
      than the live pipeline holds a value, and its `_replay-*` copy would
      carry gaps longer than its own footer says. `refuse_unfaithful_replay`
      (slow replay + this check, one call) refuses it in
      `ReplayController.start` (`ReplayRefusedError`, HTTP 422) and
      `replay_session` (the YAML collector logs it and idles), raising
      `ReplayTimeoutMismatchError` (an `UnfaithfulReplayError` and a
      `BroadcastTimeoutMismatchError`). The daemon's timeout is now a
      required argument of both (`broadcast_stale_timeout_seconds`, wired
      from `collection.broadcast_stale_timeout_seconds` in `run_daemon` and
      the validation harness), so no replay path can skip the check.
      `test_a_session_stored_with_a_longer_timeout_than_the_daemons_is_not_replayed`
      (function, `replay_session`, controller, HTTP 422, YAML collector).
    - *Row-pairing and row-counting `read_channels` callers.* One helper,
      `channels.as_of_refusal` / `refuse_as_of` (named cause, only the specs
      with rows in the frame). Called by `tuning._check_clock` and `_attach`
      (raise; the workbench reports `refused`), `lambda_delivery._long_values`
      (every delivery path: target, measured, sentinel, target agreement),
      `warmup_checks._series`, `rom_trace.cell_residency`
      (`reason: change_only_storage`), `tuning_frame.build_tuning_frame` (the
      role is unavailable with the reason), `tuning_digest` (tagged WOT
      seconds -> `unavailable` with the reason, pull evidence rpm ->
      unavailable, per-role evidence counts -> `None` plus `refused_counts`,
      measured-AFR bank counts -> `refused`, rates as before) and
      `compare.binned_summary` (now through the helper). Today's Xterra
      providers are all `did:`/`mem:`, so nothing it runs changes; the
      refusals guard a `decoded:` provider.
      `test_row_pairing_and_counting_callers_refuse_a_change_only_decoded_provider`
      (each caller on a change-only copy refuses by name; the full-rate
      original reads as before).
    - *Fixture tool.* `tools/make_ground_truth_fixture.py` refuses a
      change-only session (its per-id frame stride and its footer-less slice
      both assume every frame; exit 2, cause on stderr, nothing written).
      `test_the_ground_truth_fixture_tool_refuses_a_change_only_session`.
    - *Plausibility freshness floor.* `resolve_limits` takes
      `broadcast_stale_timeout_seconds` (`resolve_limits_for_config` passes
      the config's); a broadcast channel whose tier threshold
      (`interval_seconds x freshness_k`) is under it reports freshness
      `unchecked` with the named reason, never a check that calls every hold
      stale. The Xterra profile has no `broadcast` tier, so nothing it checks
      changes.
      `test_a_broadcast_freshness_threshold_under_the_stale_timeout_is_disabled_by_name`.
    - *Synthetic fault injection is not comparable across this change.* A
      synthetic dropout holds a channel for its class ceiling
      (`synthetic_fault_eval._max_gap_seconds_for` ->
      `ResampleConfig.max_gap_seconds_for`; `train_anomaly_model` the same).
      For broadcast channels (`can:`/`decoded:`/`dbc:`) that ceiling is now
      the stale timeout, 10 s, where it was 2 s (slow broadcast channels keep
      120 s; OBD keeps 2 s). So an injected broadcast dropout is held, and
      reads arrived, for 10 s before it goes stale. Harness, synthetic-fault
      and benchmark numbers from before this change are not comparable with
      those after it.
    - *Feature spec.* `build_feature_spec` records
      `broadcast_max_gap_seconds` beside `max_gap_seconds` and
      `slow_channel_max_gap_seconds` (`test_manifest_max_gap_seconds_honours_max_gap_factor`,
      the pinned spec in `test_train_anomaly_model.py`). Manifests written
      before this lack the key.
  - **Not done (optional, other work's files).** `imu_session._aligned` and
    `LiveImuFrame.input_max_age_s` age a decoded speed out after 3 s; a vehicle
    whose speed frame has no counter would lose quiescent windows on a steady
    leg (the Xterra's `0x2D1` carries one: verified by the verifier). The live
    frame's steadiness window and look-ahead also assume dense samples, so a
    fix there is more than a longer age.

## Desk (found 2026-09-27)

- [x] **P02.11 Featurize offline through the profile-scoped config.** The
  daemon runs `scope_config` in `main`, so live featurization read
  `data/nissan-xterra-2008/afr_calibration.yaml`. The trainers
  (`build_feature_frame`), the evaluation harness and the injection
  featurization got the unscoped config. There `daemon.afr_calibration` is
  `data/afr_calibration.yaml`, which does not exist, and
  `_append_derived_channels` reads an absent file as "no calibration".
  Every offline frame was therefore built without the calibration the served
  frame had. The same leak applied to the readers that resolve beside
  `afr_calibration`: `sequence_roles_path`, `plausibility_path`,
  `committees_path` and `profiles.profile_dir`.

  **Accept:**
  - Featurization refuses a config that is not profile-scoped while
    `profiles.enabled`, and names the incident.
  - Every offline entry point passes the scoped config.
  - Measured on two real drives: which feature columns differ, and whether
    anything in the deployed IF/LSTM manifests changes.

  *Done 2026-09-27.*
  - **The marker.** `CanaryConfig._scoped_profile` is a pydantic
    `PrivateAttr`, exposed as the read-only `scoped_profile` property.
    `scope_config` is its only writer, and no YAML can set it
    (`test_yaml_cannot_claim_to_be_scoped`). It survives `model_copy` and
    pickling, so the scoring and digest children see it. `scope_config` is
    idempotent for the same slug, where it used to nest `<slug>/<slug>/`, and
    refuses to re-scope to a different one. `resolve_profile_for_tooling(config,
    None)` on a scoped config returns the slug that config was scoped to. So
    `resolve_feature_policy`, `scoped_models_dir` and `apply_since_boundary`
    work unchanged downstream of an entry point that has already scoped. The
    marker and the refusal live in `src/config.py` (layer 0), so
    `preprocessing` imports nothing upward (`tests/test_layering.py` green).
  - **The refusal.** `src.config.require_profile_scoped` raises
    `UnscopedConfigError(ValueError)`. The error names the trainers, the
    harness and the missing `afr_calibration.yaml`. It is called by:
    - `featurize_wide_frame` and `featurize_perturbed_frame`;
    - `_append_derived_channels`, the reader itself;
    - `build_feature_frame`, before the cache lookup, so a hit cannot bypass
      it;
    - `LiveFeatureSource.__post_init__`;
    - the feature-frame cache key;
    - `build_feature_spec`'s calibration provenance;
    - `sequence_roles_path`.

    With `profiles.enabled: false`, unscoped passes, because the base paths
    are then the vehicle's. `plausibility_path`, `committees_path` and
    `profile_dir` are routed rather than refused. Refusing in `profile_dir`
    failed daemon construction in ~30 tests: `main` → `active_registry` runs
    before any scoping those tests do. Every offline caller of those three
    readers was checked and now gets a scoped config (list below).
  - **Callers swept** (`grep` for `featurize_wide_frame`,
    `build_feature_frame`, `featurize_perturbed_frame`, `LiveFeatureSource(`,
    `compute_fingerprint`, `build_evaluation_frame` and `load_config`).
    - *Now scoped once at entry,* through `scripts/_cli.py`'s
      `load_scoped_config(args, profile)` / `scoped_config`:
      - `train_anomaly_model.py`, which also covers the `--evaluate`
        injection featurization;
      - `train_sequence.py`, which also covers `sequence_roles.yaml`;
      - `retrain.py`, which also covers `accepted_baselines` and the spec's
        `behavior_models`;
      - `evaluate_detectors.py`. `_scoped` is applied once, and
        `ScorerSpec.scoped_config` is gone, because `config` is the scoped
        one;
      - `refit_behavior_models.py`, where `build_feature_frame` now gets
        `scoped`;
      - `replay.py`;
      - `benchmark_live_pipeline.py`, whose `--profile` is a workload preset,
        so it scopes to the default vehicle;
      - `fp_benchmark.py`;
      - `sequence_bakeoff.py`;
      - `label_sessions.py` and `export_dataset.py`. For these two the session
        root keeps its unscoped default and the computations get the scoped
        config.
    - *Already scoped:*
      - `validation/harness.py` and `main.py`;
      - `analyze_session.py`, `fingerprint.py`, `evaluate_behavior_models.py`,
        `inventory_behavior_inputs.py` and `evaluate_plausibility.py`;
      - `tools/check_manifest_producibility.py` (`build_producible_catalog`,
        whose docstring already *said* "must be profile-scoped");
      - the MCP `get_behavior_deviations`.
    - *Daemon side:* the digest and classify children and the dashboard
      routes. They receive the daemon's scoped config, pickled.
  - **Cache keys now hash the file actually read.**
    - *Feature-frame cache.* The key hashes `afr_calibration` at the scoped
      path and `vehicle.parameters`. It hashed neither before, so an edited
      calibration served a stale frame. `FEATURE_FRAME_CACHE_VERSION` 6 → 7.
    - *Score cache.* `KeySources.extra_data` takes
      `config.daemon.afr_calibration` from the scoped config. Before, it
      hashed the absent `data/afr_calibration.yaml`; the scoped file was
      reached only through the `profile_dir` glob.
    - *Feature spec.* `build_feature_spec` records `afr_calibration: {path,
      sha256}` (`None` = built without one).
  - **Measured on real drives.** `build_feature_frame` ran on each drive
    with `canary.yaml`, old path against scoped. The old path is the unscoped
    config with profiles off; its featurization inputs are identical to the
    pre-fix call.

    | drive | rows | columns old → new | changed values in shared columns |
    |---|---|---|---|
    | `2026-09-20T152758` | 8,570 | 873 → 899 | none |
    | `2026-08-03T163440` | 1,883 | 873 → 899 | none |

    - *The +26 columns are six channels,* each with its rolling siblings:
      `TARGET_AFR`, `AFR_ERROR_B1`, `AFR_ERROR_B2`, `AFR_B2`,
      `AFR_CENSORED_B2` and `LAMBDA_BANK_BALANCE_AF_VOLTAGE` (all
      `derived:`). The last three come from the voltage transfer's bank-2
      fallback and the B05.8 balance.
    - *Raw coverage is low.* `TARGET_AFR` covers 3.6 % and 14.4 % of rows,
      `AFR_ERROR_B1` 1.6 % and 6.5 %, and `AFR_B2` 2.8 % and 2.2 %.
      `TARGET_AFR__mean` reaches 61 % on 163440.
    - *Shared columns are identical,* `AFR_B1`, `AFR_CENSORED_B1` and
      `AFR_BANK_BALANCE` included. Bank 1 comes from the λ PID, and the
      Xterra's censor floor and ceiling, 10 and 18, equal the defaults.
  - **Retrain verdict: none forced.** No column in either deployed manifest
    (`models/nissan-xterra-2008/{isolation_forest,lstm_autoencoder}`, 295 and
    361 columns) is added, dropped or changed, so served scores are unchanged
    by construction. The A02.11 benchmark was not re-run for that reason.
    The score cache misses once anyway: the harness files and the config dump
    are both in the key.

    The *next* retrain sees the six new channels and may select some of
    them. Read the two follow-ups below before that retrain.
  - **Tests.** `tests/test_featurization_profile_scope.py` has 15 tests:
    - the marker, its idempotence, pickling, and that YAML cannot set it;
    - the refusal at every entry;
    - scoped and profile-less configs pass;
    - the known answer: `derived:TARGET_AFR` equals the Xterra target map's
      bilinear lookup exactly offline, and is absent on the old path;
    - the cache key moves on a calibration edit;
    - the spec records the calibration's sha256;
    - `train_anomaly_model`, `train_sequence`, `retrain`,
      `evaluate_detectors` and `replay` each hand on a config with
      `scoped_profile == "nissan-xterra-2008"`.

    The test configs that featurize were unscoped with profiles on. They now
    say which shape they are:
    - `daemon_test_config` (`conftest.daemon_config`) is **scoped by default**,
      the Pi's shape: profiles on, every vehicle path under
      `<path>/test-vehicle/` (`scoped_test_config`, `TEST_PROFILE`, whose
      parameters deliberately differ from the YAML block's). `scoped=False`
      is the profile-less daemon. So every `run_daemon` test runs scoped,
      including `test_run_daemon_end_to_end_inference_emits_anomaly`.
    - The two digest-route tests in `test_dashboard_api.py` run on a
      `scoped_test_config`. The same route returns 409 `UnscopedConfigError`
      on an unscoped profiles-on config (checked by hand), so their 200 covers
      the scoped chain end to end.
    - Single-vehicle harnesses say so: `tests/configs.py`'s
      `profiles_disabled` and `example_config`; `digest_config` and
      `test_dashboard_api._config` keep profiles off, because their tests seed
      exact top-level paths.
  - **Verifier round 1 (2026-09-27).**
    - *HIGH, fixed: the config hot reload made every live tick refuse.*
      `src/daemon/polling.py::poll_config` swapped
      `live_source.config = CanaryConfig.from_yaml(path)`, which is unscoped.
      On the Pi, any YAML edit left IF, sequence, behavior and the derived
      channels dead until a restart.
      - The fix is at the layer. `scope_config` now also records the
        `VehicleProfile` it applied (`CanaryConfig._scope_source`, private,
        pickled with the config). `src.profiles.rescope_like(fresh,
        reference)` re-scopes a freshly loaded YAML with that very profile:
        the same slug and the registry parameters `resolve_active_profile`
        applied, never a `resolve_profile_for_tooling` re-derivation.
      - `main`'s `scope_config` call is what records the profile, and
        `poll_config` goes through `rescope_like`, then
        `require_profile_scoped`.
      - A reload that cannot be scoped keeps the previous config and logs why.
        One case is profiles switched on under a profile-less daemon, which
        needs a restart because the VIN probe is a startup decision. The other
        is any re-scope error.
      - No other runtime path swaps a `CanaryConfig`: `grep '\.config ='`
        under `src/daemon`, `src/control` and `src/dashboard` finds only
        `poll_config` and `tuner.config`, which is a `TuningConfig`.
      - Tests: `tests/test_config_hot_reload.py` (3). A scoped reload keeps
        `scoped_profile`, `TEST_PROFILE`'s parameters, the scoped
        `afr_calibration` and storage path, and a fed tick featurizes. The
        profiles-on-under-a-profile-less-daemon case keeps the previous
        config, and so does a failing re-scope.
        `test_rescope_like_reuses_the_resolved_profile_not_a_rederived_one`
        covers the helper. The verifier's `hotreload.py` now prints `tick ok`.
    - *MEDIUM, fixed:* the daemon and digest-route fixtures now run the Pi's
      scoped shape (above).
    - *LOW, documented:*
      - `label_sessions` now runs its trend pass on the scoped config, so its
        dedup state (`trend_alert_state`) and `reference_epoch` move to
        `<base>/<slug>/`. Those are the files the daemon uses. A trend already
        alerted under the old unscoped file can alert once more.
      - `export_dataset`'s manifest `config_hash` changes, because the scoped
        paths are in the dump, and its `vehicle` metadata is now the resolved
        profile's.
  - **Follow-ups found, not changed here.**
    - `build_feature_frame`'s `behavior_models_path` defaults to the repo's
      unscoped `data/behavior_models.yaml`, which does not exist. The trainers
      never pass one, so trainer frames carry no `deviation:*` columns while
      the daemon's do. No deployed manifest has one, although B03.2/B03.4
      assume they can.
    - `derived:TARGET_AFR` looks the target map up with `obd:ENGINE_LOAD`
      (%). The map's load axis is `base_fuel_schedule` in ms (8.76–21.57), so
      the value is dimensionally wrong wherever it exists, and
      `AFR_ERROR_B*` inherits the error. This was already true on the daemon;
      this change makes offline frames match it.

- [x] **P02.12 Behavior deviations reach training, as they reach serving.**
  Operator decision 2026-09-27: yes. `build_feature_frame`'s
  `behavior_models_path` defaults to the unscoped `data/behavior_models.yaml`,
  which does not exist, and no trainer passes one. So trainer frames carry no
  `deviation:*` columns, while the daemon's live frames do (P02.11 follow-up).
  - Default it to the scoped `config.daemon.behavior_models`, as the daemon
    reads it. Prefer taking it from the config over a path argument every
    caller must remember. Sweep every `build_feature_frame` /
    `featurize_wide_frame` caller for the same default.
  - Measure before and after on ≥2 real default-profile drives: which
    `deviation:*` columns appear, their coverage, and whether feature selection
    would admit them. This adds features at the next retrain, so the measured
    diff is the deliverable, not a retrain.
  - Tuning captures stay out: behavior deviations are default-profile-only
    (R03.12/R04.6). Check that the new columns respect that gate.
  - Feature cache and spec keys must cover the behavior-model file actually
    read.

  *Done 2026-09-28.*
  - **The default is the config's file.** `build_feature_frame(...,
    behavior_models_path=None)` reads the scoped `config.daemon.behavior_models`.
    The override remains only for `refit_behavior_models --out`, whose subject is
    another artifact. `DEFAULT_BEHAVIOR_MODELS_PATH` (the repo-level
    `data/behavior_models.yaml`) is deleted, so it cannot be defaulted to again.
  - **`featurize_wide_frame` / `featurize_perturbed_frame` take
    `behavior_models` with no default** (keyword-only). A `None` default was the
    same trap one layer down. Each caller now states its set, through the new
    `configured_behavior_models(config)` (scoped; refuses unscoped) or the
    live source's hot-reloaded copy.
  - **Callers swept** (`grep` for `build_feature_frame`,
    `featurize_wide_frame`, `featurize_perturbed_frame`, `behavior_models_path`,
    `load_behavior_models`, `DEFAULT_BEHAVIOR_MODELS_PATH`, `read_behavior_models`).
    - *Were missing the deviations, fixed:*
      - the trainers' `build_feature_frame` calls in `training_entry` and
        `retrain.py`, and `datasets/export.py` (the new default);
      - `models/synthetic_fault_eval.py`, nominal and injected frames, with
        the models loaded once;
      - the `train_anomaly_model --evaluate` injection;
      - `scripts/replay.py` (`replay_sessions` defaulted to the repo file);
      - `scripts/analyze_session.py`, which fell back to the repo file when the
        scoped one was absent. It now errors, naming the scoped path;
      - `dashboard.api.create_app`, which now defaults to
        `config.daemon.behavior_models`, and `read_behavior_models`, which now
        has no default path. The daemon already passed the scoped path.
    - *Already right:* `live.py`, `evaluate_detectors` (both featurize paths),
      `behavior_validation`, `session_deviations`, `fingerprint`,
      `spec_producibility`, `daemon/inference`, the MCP tool and
      `check_manifest_producibility`.
  - **Keys.** The feature-frame cache key hashes the file the build reads,
    which is now the scoped file by default. `FEATURE_FRAME_CACHE_VERSION` goes
    7 → 8, which also covers P02.13. `build_feature_spec` records
    `behavior_models: {path, sha256}` beside `afr_calibration`. B03.4's
    per-model fingerprints are unchanged.
  - **Default profile only (R03.12/R04.6).** Training discovery already drops
    every `_`-prefixed recording. Nothing refused an explicitly named one, so
    the trainers now refuse it too, before any frame build:
    - the new `models.evaluation.refuse_non_default_profile_sessions` names each
      offending recording and cites R03.12;
    - it is called by `train_isolation_forest` and `train_sequence_detector`,
      for training and calibration sessions alike;
    - `retrain.py` calls it at session selection, exit 2.

    It uses `default_profile_exclusion`: a `_tuning-*` recording, or a capture
    whose manifest is not corpus-eligible. `refit_behavior_models` keeps its
    own stamping. It needs tuning recordings for drive grouping and refuses
    their rows by name.
  - **Measured** with `canary.yaml`, scoped, on the laptop. Built with
    `build_feature_frame`, old default (absent file) against the scoped one.

    | drive | rows | columns | shared columns changed |
    |---|---|---|---|
    | `2026-09-20T152758` | 8,570 | 899 → 959 | none |
    | `2026-08-03T163440` | 1,883 | 899 → 959 | none |

    The +60 columns are the 12 served models × (raw, `__gap`, `__mean`,
    `__std`, `__roc`). Coverage over all rows, then over classified rows:

    | model | 152758 | 163440 |
    |---|---|---|
    | `warm_idle_rpm_baseline` | 43.1 / 50.1 % | 0 / 0 % |
    | `timing_advance_vs_rpm_load` | 47.6 / 55.2 % | 41.4 / 49.7 % |
    | `timing_advance_vs_rpm_iat` | 26.3 / 30.6 % | 42.3 / 50.9 % |
    | `fuel_trim_vs_rpm_load` | 35.7 / 41.4 % | 21.2 / 25.6 % |
    | `air_mass_per_cycle_vs_rpm_load` | 53.3 / 61.9 % | 38.9 / 46.7 % |
    | `ve_vs_rpm_load` | 53.3 / 61.8 % | 38.7 / 46.5 % |
    | `total_trim_vs_rpm_load_b1` | 26.9 / 31.2 % | 3.2 / 3.9 % |
    | `total_trim_vs_rpm_load_b2` | 23.5 / 27.3 % | 3.1 / 3.7 % |
    | `stft_bank_balance_city` | 1.6 / 1.8 % | 8.5 / 10.2 % |
    | `stft_bank_balance_highway` | 0.0 / 0.0 % | 2.1 / 2.6 % |
    | `cat_warmup` | 0.9 / 1.1 % | 0 / 0 % |
    | `air_charge_vs_rpm_throttle_did` | 1.5 / 1.7 % | 1.7 / 2.0 % |
  - **Next-retrain verdict.** Selection was run on the two drives pooled,
    with `resolve_feature_policy(scoped)`.
    - *IF:* the selection goes from 178 to 189 columns, and nothing drops out.
      The eleven new columns are the families of `air_mass_per_cycle_vs_rpm_load`
      and `ve_vs_rpm_load` (4 each) and `timing_advance_vs_rpm_load` (3; its
      `__roc` is under `MIN_DETECTOR_COVERAGE` on the pool).
      `withheld_learned_feature_columns` then removes the
      `timing_advance_vs_rpm_load` three, because `behavior_policy.yaml` has that
      model as `exploratory`. **Net +8 IF features.**
    - Every other deviation column fails the 0.5 floor (`sparse_for_grid` or
      `below_row_coverage`), is all-NaN, or is withheld (`timing_advance_vs_rpm_iat`
      is demoted, `cat_warmup` quarantined).
    - *Sequence detector: +0.* `sequence_feature_selection` drops the whole
      `deviation:` family by design.
    - Two drives are not the corpus. The coverage floor is judged over whatever
      the retrain pools, so the admitted set can differ.
  - **Deployed manifests: unchanged.** `models/nissan-xterra-2008/isolation_forest`
    has 295 feature columns and `lstm_autoencoder` 361. Neither names a
    `deviation:` column, so nothing is served differently until a retrain. No
    retrain was run.
  - **Tests.**
    - `tests/test_behavior_deviations_reach_training.py` (9):
      - the default reads the configured file (known answer: Δ = +5 °);
      - the real scoped Xterra frame carries every served model's column with
        no path passed;
      - `configured_behavior_models` refuses an unscoped config;
      - both featurize functions have no default;
      - the cache key moves on a model edit, and default equals explicit;
      - the spec records the sha256;
      - `refuse_non_default_profile_sessions` names `_tuning-*` and ineligible
        captures and passes an eligible one;
      - both trainers refuse, for training and calibration.
    - The golden spec contract in `test_train_anomaly_model.py` gains the two
      new keys.
    - 31 test call sites now pass `behavior_models=None`, their old implicit
      value.

- [x] **P02.13 `derived:TARGET_AFR` refuses a load channel in the wrong
  unit.** Operator decision 2026-09-27: keep it out of detector features now,
  fix the input next. `append_afr_channels` looks the target map up with
  `obd:ENGINE_LOAD` (%). The map declares `load_axis_name:
  base_fuel_schedule` and `load_axis_unit: ms` (8.76–21.57), so every
  `TARGET_AFR` and `AFR_ERROR_B*` value is dimensionally wrong, on the daemon
  too.
  - Make the mismatch unrepresentable. The lookup takes its load channel from
    the map's declared axis (a channel whose unit is `load_axis_unit`) and
    refuses any other. With no verified base-fuel-schedule channel (0x1208 is
    only a candidate; see the vehicle skill), the two channels are absent,
    with a named reason. They are not computed from the wrong input.
  - Keep `TARGET_AFR` / `AFR_ERROR_B*` out of detector features until a
    verified axis channel exists: a feature-policy exclusion recorded in the
    manifest, not a comment.
  - Survey every consumer of `derived:TARGET_AFR` / `AFR_ERROR_B*` (dashboard,
    tuning pages, workbench, MCP, digest) and report what goes absent.
    Commanded AFR from PID 0x44 (φ) is a different quantity and is unaffected.

  *Done 2026-09-28.*
  - **How an axis resolves to a channel: vehicle data.**
    `src.signals.vehicle_signals` gains `LoadAxisChannel(axis, channel,
    evidence)`, a vehicle module's `LOAD_AXIS_CHANNELS`, and
    `load_axis_channels(axis)`. `evidence` is required: an unverified channel on
    a table axis is the defect. The declaration has **no unit field**; the
    unit is the channel catalog's (below). The Xterra module declares `()`, with the
    reason. 0x1208 is only a candidate; `dids.yaml` leaves it unscaled
    (`unit: unknown`, raw counts in the frame); its ms scale exists only in the
    workbench's `tuning_providers.yaml`, which featurization does not read. No
    channel name is in `afr.py`.
  - **The lookup cannot be handed a load.**
    - `target_afr(frame, target, *, channel_units, rpm_column)` reads the load
      column itself, through `target_load_axis(target, frame, channel_units=)`.
      The old `(rpm, load, target)` signature, which `append_afr_channels` fed
      `obd:ENGINE_LOAD`, is gone.
    - `target_afr_refusal` / `load_axis_refusal` return
      `load_axis_unverified: ...` when no vehicle declares the axis.
      `LoadAxisUnavailableError` carries the reason.
    - **The unit is resolved from the catalog at runtime** (verifier round 1,
      MEDIUM). The first version compared the declaration's own `unit`
      string; the catalog cross-check existed only in a test that failed open.
      So `obd:ABSOLUTE_LOAD` or a `mem:` channel declared "ms" passed both.
      - `load_axis_refusal` now takes a required `channel_units` resolver and
        refuses each declared channel whose catalog unit is missing, `unknown`,
        or not the map's `load_axis_unit`. No resolver (`None`) means every
        declaration is refused.
      - The catalogs sit above `src.signals` (the Mode 01 registry is in
        `collection`). So the resolver is the new
        `src/preprocessing/channel_units.py:catalog_channel_unit`, and the layer
        that may import them passes it in: `_append_derived_channels` and
        `FeaturePolicy.standing_exclusions`. `append_afr_channels`,
        `target_afr`, `target_load_axis` and `afr_error_surface` all require
        the argument, so no call can leave it out.
      - Sources: `obd:` from `MODE01_REGISTRY`; `did:` and `mem:` from
        `dids.yaml` `dids:` / `memory:`. Everything else is `None`: `decoded:`
        rows are raw counts by design, and `derived:` has no catalog.
    - `append_afr_channels` emits `TARGET_AFR` / `AFR_ERROR_B*` only when there
      is no refusal and the declared channel is in the frame. They are never
      computed from another column.
    - `afr_error_surface` bins on the axis channel and raises the refusal
      rather than return an empty surface.
    - `DERIVED_CHANNEL_INPUTS["derived:TARGET_AFR"]` is `(("obd:RPM",),
      load_axis_columns("base_fuel_schedule"))`. That group is empty today,
      so it is unsatisfiable, which `capture_profiles.model_input_warnings`
      would report for any model that read it.
  - **Feature-policy exclusion, on the manifest.**
    - `FeaturePolicy.standing_exclusions()` refuses `TARGET_AFR_CHANNELS`
      (`TARGET_AFR`, `AFR_ERROR_B1`, `AFR_ERROR_B2`) with the
      `load_axis_unverified` reason while
      `load_axis_refusal(channel_units=catalog_channel_unit)` holds. It lifts
      itself once a vehicle declares a channel whose catalog unit is the axis
      unit.
    - `exclusion_reason` returns it, before the source gate, so a present
      column lands in `feature_policy_exclusions` with this reason and never
      in `candidate_feature_columns`.
    - *The source gate agrees* (verifier round 1, LOW).
      `trainability._derived_detail` flattened the input groups, so the empty
      load group vanished and `TARGET_AFR` read as trainable on `obd:RPM`
      alone. A required group with no member now makes the channel
      non-trainable. For `TARGET_AFR_CHANNELS` it names the load-axis refusal,
      and `AFR_ERROR_B*` inherit it through their `TARGET_AFR` input.
    - `build_feature_spec` records `feature_policy_standing_exclusions` on
      every manifest, frame column or not. This is where the refusal is
      discoverable, beside the other policy refusals.
  - **Measured on the same two drives.** New code, new default, against the
    P02.12 "after" frames: 959 → 947 columns on both. Only the `TARGET_AFR` and
    `AFR_ERROR_B1/B2` families (raw, `__mean`, `__std`, `__roc`) go, and no
    other column changes. Their pre-fix coverage, all rows / classified rows:

    | column | 152758 | 163440 |
    |---|---|---|
    | `TARGET_AFR` | 3.6 / 4.1 % | 14.4 / 17.4 % |
    | `AFR_ERROR_B1` | 1.6 / 1.9 % | 6.5 / 7.8 % |
    | `AFR_ERROR_B2` | 0.0 / 0.0 % | 0.2 / 0.2 % |

    None would have passed the 0.5 coverage floor. Neither deployed manifest
    names them.
  - **Consumer survey.** `grep` over `src`, `src/dashboard/static/src`,
    `scripts`, `tools`, `tests`, `data` and `models` for `TARGET_AFR`,
    `AFR_ERROR`, `target_afr(`, `afr_error_surface` and `target_map(`.
    - *Producers, changed:* `signals/afr.py` and `signals/derived.py`
      (registry). `feature_pipeline.py` (policy) and the `feature_cache.py`
      comment and version.
    - *Dashboard* (Python and `static/src`): no reference. A generic
      live-channel listing just loses the two channels.
    - *Tuning pages and workbench* (`tuning_digest`, `rom_trace`,
      `tuning_frame`, `rom_targets`): these read the map's axes and resolve
      BFS through `tuning_providers.yaml` roles, never through
      `derived:TARGET_AFR`. Unaffected.
    - *MCP, trip digest, behavior models, sequence roles, committees,
      plausibility:* no reference.
    - *Fixtures and golden files:* none.
    - *Deployed manifests:* IF (295) and LSTM (361) name neither channel.
    - *Same name, different quantity:* `tools/pull_report.py`'s `TARGET_AFR` is
      UpRev's own `uprev:` channel, and `test_memory_channels`'
      `test_known_answer_target_afr` is the RAM φ word. Both unaffected.
    - *Commanded AFR:* PID 0x44 φ (`AfrCalibration.commanded_afr_series`) and
      `mem:ECM_TARGET_EQUIV_RATIO` do not touch the target map. Unaffected.
    - *Tests updated:*
      - `test_afr.py`, with a synthetic declared axis channel and a stub
        catalog;
      - `test_trainability.py`, which declares one so the registry-coverage
        check still sees both channels emitted. This stays after the source
        gate fix, which concerns trainability, not emission;
      - `test_bank_balance_persistence.py` (`channel_units=None`);
      - the P02.11 known answer in
      `test_featurization_profile_scope.py`, which now uses the voltage
      transfer's `AFR_B1`.
    - *Docs:* `docs/roadmap/capture-profiles.md` gains notes on items 4.3 and 4.5.
  - **What goes absent for the operator.** After the next Pi deploy and
    restart, `derived:TARGET_AFR` and `derived:AFR_ERROR_B1/B2`, with their
    rolling siblings, leave the live operating and feature frames. Nothing
    reads them by name, and no model was trained on them.
  - **Tests.**
    - `tests/test_target_afr_load_axis.py` (23 cases):
      - the registry resolves the map's default axis;
      - no `TARGET_AFR_CHANNELS` input group holds `obd:ENGINE_LOAD`;
      - `derived_channels_reached({TARGET_AFR}) ⊆ TARGET_AFR_CHANNELS`;
      - no vehicle declares an axis today;
      - the policy refuses by name, is absent from candidates, and is recorded
        on the spec even for a frame without the columns;
      - the exclusion lifts for a declaration the real catalog puts in ms
        (`did:ECM_INJECTOR_PULSE_1`, a stand-in), and stays refused with no
        catalog or on `obd:ENGINE_LOAD` (catalog `%`);
      - `catalog_channel_unit` per namespace: `obd:ENGINE_LOAD` and
        `obd:ABSOLUTE_LOAD` `%`, `did:ECM_AIR_CHARGE` `unknown`,
        `did:ECM_INJECTOR_PULSE_1` `ms`, `mem:ECM_ENGINE_PERIOD` `unknown`,
        `mem:ECM_TARGET_EQUIV_RATIO` `phi`, and `decoded:`/`derived:`/unknown
        DID `None`;
      - declaring any of `obd:ENGINE_LOAD`, `obd:ABSOLUTE_LOAD`,
        `did:ECM_AIR_CHARGE`, `mem:ECM_ENGINE_PERIOD` or
        `mem:ECM_TARGET_EQUIV_RATIO` is refused at runtime, and the manifest
        exclusion carries the same reason. `LoadAxisChannel` has no `unit`
        field;
      - the source gate refuses `TARGET_AFR` naming `load_axis_unverified`, and
        `AFR_ERROR_B*` through it.
    - `tests/test_afr.py`, 13 new cases (22 in the file):
      - `obd:ENGINE_LOAD` never reaches the lookup, and `target_afr` and
        `afr_error_surface` refuse;
      - `target_afr` has no load parameter;
      - through the real catalog, a declaration of `obd:ENGINE_LOAD`,
        `obd:ABSOLUTE_LOAD`, `did:ECM_AIR_CHARGE`, `mem:ECM_ENGINE_PERIOD`,
        `mem:ECM_TARGET_EQUIV_RATIO`, `decoded:ECM_RPM` or a `derived:` channel
        is refused, and yields no target;
      - the check can pass: a catalog-ms DID produces a target;
      - no catalog means no target;
      - a declared channel absent from the frame yields nothing;
      - a declaration must name its evidence.
    - `tests/test_retrain_script.py::test_retrain_refuses_a_named_tuning_session_before_building_a_frame`
      (verifier round 1, LOW) covers P02.12's retrain refusal. A named
      `_tuning-*` session gives exit 2 and names R03.12 and the session. The
      frame build and the trainer are patched to fail if reached.

- [x] **P02.14 DID 0x1208 in ms is the Xterra's target-map load axis.**
  Operator decision 2026-10-02: accept `did:ECM_AIR_CHARGE` as the verified
  base fuel schedule. The evidence is UpRev's own stream definition, which puts
  B-FUEL SCHDL at DID 0x1208, and raw × 0.00049 reproduces its displayed ms on
  405/405 rows (2026-09-20). Declare it as the load-axis channel so
  `derived:TARGET_AFR` / `AFR_ERROR_B*` come back, computed correctly. This
  lifts P02.13.

  *Done 2026-10-02.*
  - **Pre-checks.**
    - *Is the DID a deployed feature? Yes.*
      - IF: `did:ECM_AIR_CHARGE`, `__mean`, `__roc`, `__std`.
      - LSTM: raw and `__mean`.
      - Also `sequence_roles.yaml` (target `air_charge`), behavior model
        `air_charge_vs_rpm_throttle_did` (`output_unit: raw`) and
        `fault_injection.yaml` (`airflow`).
      - Committees, plausibility and fingerprints: no reference.

      Scaling the DID in place would change served feature values, which is a
      retrain. So it keeps decoding to raw counts, and a separate channel
      carries ms.
    - *Double scaling.* `tuning_providers.yaml` already scales the DID by
      0.00049. Sweep of `ECM_AIR_CHARGE` / `0x1208` / `DID_1208` over `src`,
      `scripts`, `tools`, `data`, `tests`, fixtures and `models`:
      - The workbench (`tuning_frame` role `base_fuel_schedule`, and so
        `tuning_digest`, `rom_trace`, `workbench`, `dashboard/routes/rom_maps`)
        scales the raw DID once. Unchanged.
      - Everything else reads raw counts and is unchanged: decode, `live.py`,
        `capture_quality`, `record.py`, `validation/scenarios`, the analysis
        recipe, capture profiles, the dashboard fixtures and the migrate tool.
      - The ground-truth `catalog_pins` and expectations name no DID.
    - *Polling on `default`.* `ECM_AIR_CHARGE` is in the `mode22` list at the
      default weight, with no DID cadence override. Measured: read once per
      **60.6 s** median on `2026-09-20T152758` (122 reads in 8,570 s) and
      **60.9 s** on `2026-08-03T163440` (24 in 1,884 s). The poll list is not
      changed (D03.34).
  - **Mechanism: a DID view, `derived:BASE_FUEL_SCHEDULE`.**
    - `src.signals.vehicle_signals.DidView(channel, source, scale_method)` and
      a vehicle module's `DID_VIEWS`. The declaration carries no scale and no
      unit.
    - The scale is the `constant` of the source DID's one `dids.yaml`
      evidence record whose `method` is `scale_method`. Its `constant_unit`
      (`"<unit> per count"`) is the unit. For the Xterra that is
      `uprev_stream_definition_vs_displayed_csv`: 0.00049, `ms per count`.
      The constant is stated once, beside its evidence.
    - `src.preprocessing.channel_units`:
      - `did_view_refusal` / `did_view_scale` resolve the view.
      - `catalog_channel_unit` resolves `derived:` for views only.
      - `append_did_views` computes the channel, copying the source's `__gap`
        and `__arrived`. `_append_derived_channels` runs it before the target
        lookup.
    - **Double scaling cannot be written.** A view is refused while its source
      has a `scale` or a unit of its own: the frame would hold scaled values.
      A missing or duplicated evidence record is refused too. A refused view is
      not emitted, has no catalog unit, and so the load axis is refused with it.
    - **The catalog that decoded the frame is the one that judges it**
      (verifier round 1, MEDIUM). The first version's guards always read the
      repo `dids.yaml`, but a frame can be decoded with a caller's catalog:
      `build_feature_frame(dids_path=)`, `analyze_session(dids_path=)`,
      `build_evaluation_frame(dids_path=)`, and the `--dids` CLIs. A catalog
      that scales 0x1208 to ms gave a view scaled twice (max 0.0088) and no
      `TARGET_AFR`, silently. P02.13's resolver had the same mismatch.
      - `featurize_wide_frame` and `featurize_perturbed_frame` take
        `did_map: DidMap | None` as a **required keyword with no default**,
        like `behavior_models`. `_append_derived_channels` passes it to
        `append_did_views(did_map=)` and to `channel_units_for(did_map)`, the
        resolver the target lookup reads.
      - `FeaturePolicy` gains the required field `did_map`, and
        `standing_exclusions` resolves the load-axis units through it.
        `resolve_feature_policy(dids_path=)` defaults to the same repo path
        `build_feature_frame` decodes with.
      - Every function in `channel_units` takes the catalog, required:
        `catalog_channel_unit(channel, *, did_map)`, `did_view_scale`,
        `did_view_refusal`, `append_did_views`. They no longer read the repo
        file. A `None` catalog gives no `did:`/`mem:` unit, and it refuses
        every view with "no DID catalog decoded this frame". Through the view,
        it also refuses the DID-backed load axis.
      - Callers pass the catalog they decoded with:
        - `build_feature_frame`: `inputs.did_map` from `dids_path`.
        - The live tick: `PreparedLiveTick.did_map`, the source's own catalog.
          It is about 68 KB pickled, 0.6 ms a tick across the process
          boundary.
        - `session_deviations`, `behavior_validation`, `spec_producibility`:
          the map loaded from their `dids_path`.
        - `evaluate_detectors`: `default_decode_inputs().did_map`, the
          catalog `load_resampled_session` decodes with; `DriveScorer` gains a
          required `did_map`.
        - The daemon's IF audit policy: `load_repo_did_map()`, which the live
          source decodes with.
        - `None`, with the reason in a comment, for chains that decode no
          DID: `fingerprint` (the `value_number` chain), `replay.py` and
          `train_anomaly_model.py`'s evaluation (no `decode_did_rows`).
        - The synthetic fault harness's `evaluate_detectors` takes it as a
          required keyword.
    - The registry gets `derived:BASE_FUEL_SCHEDULE: ((did:ECM_AIR_CHARGE,),)`.
      The Xterra's `LOAD_AXIS_CHANNELS` declares it for `base_fuel_schedule`,
      citing the 405/405 evidence and its steady-state envelope.
    - *Rejected alternatives.* A second `dids.yaml` entry for 0x1208 is refused
      by `DidMap` (one entry per id). A decode-level alias would have to be
      threaded through every decode path (`decode_did_rows`, the live mirror,
      the derived store, the dashboard) and through the serving gate's DID
      names.
  - **Feature policy.**
    - P02.13's standing exclusion lifts by itself: `load_axis_refusal` returns
      `None` on the real catalog. `TARGET_AFR` / `AFR_ERROR_B*` pass the
      source gate (`obd:RPM` + a polled DID).
    - The view is refused by name (`DID_VIEW_EXCLUSION`, `did_view: ...`) in
      `standing_exclusions` and on every manifest. It is the source times a
      constant, so it would be a second copy of one feature.
    - **Next-retrain verdict: they would not pass the coverage floor**
      (`MIN_DETECTOR_COVERAGE` 0.5, `sparse_for_grid`). Two limits:
      - 0x1208 is read once a minute on `default`;
      - the table's domain is rpm 1,200–5,200 and BFS 8.76–21.57 ms, and
        `target_afr` leaves rows off the map empty.

      | column | 152758 rows / grid density | 163440 rows / grid density |
      |---|---|---|
      | `did:ECM_AIR_CHARGE`, `derived:BASE_FUEL_SCHEDULE` | 2.8 / 3.3 % | 2.5 / 3.1 % |
      | `derived:TARGET_AFR` | 0.0 / 0.0 % (2 rows) | 0.4 / 0.5 % |
      | `derived:AFR_ERROR_B1` | 0.0 / 0.0 % | 0.2 / 0.2 % |
      | `derived:AFR_ERROR_B2` | 0.0 / 0.0 % | 0.3 / 0.3 % |
    - **Deployed manifests are unaffected.** HEAD and the change were built on
      both drives with `canary.yaml`, scoped. Frames grow 890 → 908 columns
      (operating 459 → 465), and **no shared column changes**, the
      `did:ECM_AIR_CHARGE` family included. The 18 new columns are the
      `BASE_FUEL_SCHEDULE`, `TARGET_AFR` and `AFR_ERROR_B1/B2` families. IF
      and LSTM manifests name none of them. `FEATURE_FRAME_CACHE_VERSION`
      8 → 9.
  - **BFS distribution (engine running, rpm ≥ 400).**
    - Idle (< 1,000 rpm) reads 2.7–5.0 ms. At ≥ 1,000 rpm the range is
      1.7–16.6 ms, median 3.7 ms on 152758 and 4.8 ms on 163440.
    - Below the first breakpoint (8.76 ms): **98.8 %** of 86 running reads on
      152758, which is mostly idle, and **68.2 %** of 22 on 163440.
    - With the key on and the engine off, the register holds a frozen value:
      36 reads at 13.59–17.20 ms on 152758.
    - On the known-answer slice (below): 1.64–17.99 ms. 53 of 119 rows are on
      the map, and 24 of those are richer than 14.7.
  - **Known answer.** A real slice, committed as
    `tests/fixtures/base_fuel_schedule/_capture-tuning-2026-09-23T164007`
    (2 min, 72 KB). It holds a pull to 5,475 rpm on ZS44A-20260920.
    `TARGET_AFR` equals an independently written four-cell bilinear lookup on
    raw × 0.00049 × rpm to `rtol` 1e-9. Mutation check: a 1.48× axis fails it.
  - **Tests.**
    - `tests/test_base_fuel_schedule.py` (20), covering:
      - the DID still decodes to raw;
      - the view's scale is the evidence record;
      - the workbench provider equals it, end to end on the slice;
      - refusals for a source scaled in place and for 0 or 2 evidence
        records;
      - the gap and arrived flags, including a held finite value whose
        source `__gap` is True (verifier round 1, LOW: the copy was unpinned);
      - decoding the slice through `build_feature_frame(dids_path=)` with a
        catalog that scales 0x1208: no view, no `TARGET_AFR`, and the policy
        on that catalog names the refusal. The repo catalog gives both. A
        mutation that reads the repo catalog again fails it;
      - a `None` catalog: a named refusal, and no view or target;
      - `did_map` has no default on either featurizer or on `FeaturePolicy`;
      - declaration checks, and a duplicate-name refusal;
      - the policy;
      - the known answer, the axis sanity check, and AFR error as measured
        minus target.
    - Updated: `test_target_afr_load_axis.py` (its refusal tests remove the
      declaration first, plus new lift and source-gate cases),
      `test_afr.py`, `test_trainability.py` (the emission check runs the real
      view), the `test_train_anomaly_model.py` golden feature-spec contract
      (its standing exclusions are now the view alone), and the
      `test_dids.py` / `test_featurization_profile_scope.py` docstrings.
    - `tests/test_preprocessing_live.py`: the live tick carries the source's
      catalog through a pickle round trip. It computes the view with one and
      emits none without.
    - The catalog threading adds `did_map=` to every featurization and
      `FeaturePolicy` call in the tests: `load_repo_did_map()`, the catalog
      those frames would be decoded with. The files are test_feature_pipeline,
      `_incremental`, `_policy`, test_evaluation_detectors,
      test_evaluate_detectors_script / `_default_profile`,
      test_retrain_script, test_behavior_serving_contracts,
      test_synthetic_fault_eval and test_trainability.
    - Gate after the verifier fix:
      - 1,113 passed (1 xfailed) over the 50 affected files, including every
        featurization caller's tests, test_layering and the CLI snapshot;
      - `-m ml_training -x`: 122 passed;
      - `-m "not integration and not slow"`: 6,467 passed, 1 skipped;
      - `tests/test_replay_isolation.py` (integration): passed;
      - ruff, format and mypy are clean.
  - **Pi cost, unmeasured.** Each live tick builds 18 more feature columns
    (about 2 %): the `BASE_FUEL_SCHEDULE`, `TARGET_AFR` and `AFR_ERROR_B1/B2`
    families. The DID catalog also crosses to the feature process every tick.
    Neither is measured on the Pi.
  - **Docs.** `dids.yaml` 0x1208 is `confidence: confirmed`, with notes and a
    comment on why `scale` stays null. The `tuning_providers.yaml` comment no
    longer says "candidate". The vehicle skill files are updated too.
  - **Open, for the operator.** These are findings, not changes.
    - UpRev's TARGET AFR shows that the ECM clamps BFS below the first
      breakpoint to the first column. On the 2026-08-04 logs, 30 of 44 rows
      with BFS < 8.76 ms at 4,400–5,200 rpm match the first column to
      ≤ 0.05 AFR, and the largest miss is 0.18. `target_afr` leaves those rows
      empty.
    - UpRev's "Fuel Target X Trace" reads ≈ 1.48 × BFS − 0.09 on six logs,
      so it is not BFS ms. `rom_viewer.yaml` keeps its axis mapping
      `unverified`.

- [x] **P02.15 The target map clamps to its edges like the ECM, and has no
  value in fuel cut.** Operator-approved 2026-10-03, from the P02.14 finding:
  UpRev's own TARGET AFR shows the ECM reads a BFS below 8.76 ms as the first
  load column, and `target_afr` left those rows NaN. The rpm clamp above
  5,200 was approved the same day, after the verifier found its evidence.

  *Done 2026-10-03; verifier round 1 fixes folded in.*
  - **Declared, evidenced properties of the map.**
    - `TargetAfrMap` gains `load_axis_out_of_range` and
      `rpm_axis_out_of_range`. Each is an `AxisOutOfRange` with one record per
      edge (`below_first`, `above_last`).
    - A record is an `AxisEdgeClamp`, and every field but `notes` is
      required: `behaviour: clamp`, `method`, `logs`, `revision`,
      `rpm_min`/`rpm_max`, `rows`, `rows_within_tolerance`, `tolerance_afr`,
      `max_error_afr`, `median_error_afr`, `alternatives` and `verified_on`.
      A clamp cannot be declared without its evidence.
    - A map without the blocks, or an edge without a record, keeps NaN.
      `target_afr` clips a point only past a declared edge. No numbers are in
      shared code.
  - **The data declares two edges.** Both sit on the k106 anchor in
    `afr_calibration.yaml`, and the 2026-09-20 map inherits them. They are
    ECM program behaviour, and that flash changed one cell.
    - BFS below 8.76 ms reads the first column. BFS above 21.57 ms has no
      record: no logged row has BFS above 21.57 ms (UpRev max 18.70 ms,
      CANary max ~18).
    - rpm above 5,200 reads the last row. rpm below 1,200 has no record:
      every cell there would be 14.7, so a clamp and anything else read
      the same.
    - *Correction (verifier round 1, LOW).* The first version said "the logs
      cannot show whether the ECM clamps rpm" for both rpm edges. That holds
      only below 1,200. Above 5,200 the evidence exists (below), and the
      operator approved the clamp.
  - **Re-verified on the 2026-08-04 UpRev logs**
    (`data/sessions/_uprev/2026-08-04T{163645,164301,232712}`, ZS44A-k106).
    - **BFS below the axis**, at 4,400–5,200 rpm, with 5 fuel-cut rows (30.00)
      excluded:
      - the clamp matches **32 of 46** within 0.05 AFR and 44 within 0.10,
        max error **0.178**, median 0.031;
      - strictly inside the band it is **30 of 44**, P02.14's figure;
      - the relation can fail: linear extrapolation from the first two
        columns matches 2 of 46 (median 2.23), and the second column 0
        (median 0.99);
      - below 4,400 rpm all 404 non-cut rows read 14.70. They cannot tell a
        clamp from extrapolation, since both columns are 14.7.
    - **rpm above the axis**: 105 rows at 5,213–5,950 rpm, all from
      `2026-08-04T164301`, none of them fuel cut.
      - The last rpm row, at the row's BFS, matches **62 of 105** within
        0.05 and 90 within 0.10. The max error is **0.307**, the median 0.035.
      - Linear extrapolation along rpm from the last two rows matches
        **0 of 105** (3 within 0.10, median 0.277, max 1.691).
      - 40 of the 43 misses read 11.83, the last load column at 5,200 rpm.
        Near-WOT pedal enrichment selects that column whatever the BFS (vehicle
        skill), so they disagree about the column, not the row.
    - For scale, on-map rows match the plain lookup no better: 275 of 582
      within 0.05, median 0.061, max 2.07. UpRev reads rpm, BFS and target one
      after another over K-line, so they are not same-instant.
  - **Fuel cut: a sentinel is a state, not a number** (verifier round 1,
    MEDIUM).
    - All 374 fuel-cut rows in the fixture (UpRev 30.00, BFS 1.7–2.1 ms) are
      below the BFS axis. They were NaN, and the clamp gave them 14.7 or the
      first column.
    - `src.signals.afr.fuel_cut_rows(frame)` marks rows whose commanded
      equivalence ratio (J1979 PID 0x44, `obd:COMMANDED_EQUIV_RATIO`) reads
      exactly 0. That is the ECM commanding no fuel, the state UpRev shows as
      30.00, and `AfrCalibration.commanded_afr_series` already reads it that
      way.
    - `target_afr_by_revision` withholds the target there and counts the rows
      (`TargetAfrLookup.fuel_cut_rows`). `DERIVED_CHANNEL_OPTIONAL_INPUTS`
      declares the ratio as a `TARGET_AFR` input, so a perturbed or
      incremental featurization recomputes the target when the ratio changes.
    - A row with no commanded ratio is not marked: its state is unknown, not
      "cut". The DID-only `tuning-did` profile polls no Mode 01, so its
      fuel-cut rows keep a target. AFR_ERROR there is still censored at the
      lean ceiling (18 AFR).
  - **FUEL_STATUS (PID 0x03) cannot separate cut.**
    - Its 0x04 code is "open loop for load *or* decel fuel cut" by J1979.
    - On the two default drives it reads 0x04 on 154 rows that also carry phi:
      114 at phi 0, 35 near stoich (0.97–1.03), 2 lean and 3 under
      enrichment (phi 1.15–1.16, `2026-08-03T163440`). Using it would withhold
      real targets.
    - The other way round, phi 0 agrees with it: 114 of the 117 phi-0 rows
      that carry a status read 0x04, and the 3 others are closed-loop rows on
      `2026-09-20T152758`, which is polling skew.
    - The lambda sensor lags but agrees: phi 0 has PID 0x24 on the 2.0 rail on
      87 of 98 rows (152758) and 36 of 40 (163440).
    - **"BFS < 2.1 ms means cut" is false.** Across the corpus (40 sessions
      poll 0x1208), 57 BFS reads below 2.1 ms at ≥ 1,200 rpm have a status
      within 1.5 s on 20. 10 of those read closed loop and 10 read 0x04. Of
      the 19 with phi, 7 are 0, 10 are 0 < phi ≤ 1 and 2 are above 1. No BFS
      threshold was used.
  - **Known answers.** The committed slice is
    `tests/fixtures/load_axis_clamp/uprev_2026-08-04_target_afr.csv`: every
    08-04 row holding rpm, BFS and target, 1,585 rows, 95 KB.
    `tests/test_target_afr_load_clamp.py` (11 tests):
    - the BFS clamp reproduces its declaration (46 rows, 32 within 0.05, max
      0.178, median 0.031), plus 44 within 0.10 and the strict 30 of 44; the
      alternatives score 2 and 0;
    - **the rpm clamp reproduces its declaration** (105 rows, 62 within 0.05,
      max 0.307, median 0.035, 40 of 43 misses at 11.83), and extrapolation
      scores 0;
    - all 404 rows below 4,400 rpm give 14.7;
    - **below 1,200 rpm stays NaN**: 74 logged rows, 68 of them also below the
      BFS axis;
    - the undeclared BFS upper edge stays NaN;
    - a map without the declarations keeps NaN off both axes;
    - k106 and 2026-09-20 declare exactly the two edges, and the UpRev base
      map none;
    - **fuel cut**: through `append_afr_channels`, all 374 fuel-cut rows (ratio
      0) get no target. Without the ratio column the clamp would give all 374
      a value, and non-cut rows are unchanged. Exact zero is cut; 1e-6, NaN
      and a missing column are not;
    - a clamp without evidence, or with impossible counts, is refused by the
      schema.

    `test_base_fuel_schedule.py`'s independent bilinear formula applies both
    declared clamps. Its known answer still holds at `rtol` 1e-9.
  - **Coverage on the two default drives**, all rows / classified rows /
    grid density, from `featurize_wide_frame` with `canary.yaml` scoped. HEAD
    is emulated as no clamps, `active_revision` on every row and no fuel-cut
    mask.

    | column | 152758 HEAD | 152758 after | 163440 HEAD | 163440 after |
    |---|---|---|---|---|
    | `TARGET_AFR` | 2 rows, 0.02 / 0.03 / 0.03 % | 16 rows, 0.19 / 0.22 / 0.22 % | 8 rows, 0.42 / 0.51 / 0.51 % | 19 rows, 1.01 / 1.22 / 1.22 % |
    | `AFR_ERROR_B1` | 1, 0.01 % | 12, 0.14 / 0.16 / 0.16 % | 3, 0.16 / 0.19 % | 10, 0.53 / 0.64 / 0.64 % |
    | `AFR_ERROR_B2` | 2, 0.02 / 0.03 % | 12, 0.14 / 0.16 / 0.16 % | 5, 0.27 / 0.32 % | 13, 0.69 / 0.83 / 0.83 % |

    - With the clamps alone, 152758 had 18 rows. The fuel-cut mask removes 2:
      phi 0, lambda on the rail, BFS 1.78 and 1.75 ms.
    - The rpm clamp adds no row on these drives: no 0x1208 read above
      5,200 rpm.
    - **Floor verdict: still below `MIN_DETECTOR_COVERAGE` 0.5 on both drives,
      by a factor of about 40–230.** 0x1208 is read once a minute on
      `default` (P02.14: 60.6 s and 60.9 s).
  - **Deployed manifests are unaffected.** On both drives the only columns
    that change are the `TARGET_AFR` / `AFR_ERROR_B1/B2` raw, `__mean` and
    `__std` columns. No column is added or removed. The IF (295) and LSTM
    (361) manifests in `models/nissan-xterra-2008/` name none of them.
    `FEATURE_FRAME_CACHE_VERSION` 9 → 10.

- [x] **P02.16 Target AFR uses the map revision flashed when the row was
  recorded.** Operator-approved 2026-10-03. `append_afr_channels` applied the
  calibration's `active_revision` (ZS44A-20260920) to every session, so
  pre-flash sessions got the wrong map.

  *Done 2026-10-03; verifier round 1 fixes and the operator's ROM provenance
  (2026-10-03) folded in.*
  - **Per row, from the row's own timestamp.** It is not resolved per session.
    - `TuneRevisionRegistry.revisions_at(timestamps)` is `at()` vectorised,
      by the same rule: flashed at or before the instant, `None` before the
      first flash or for NaT, naive stamps read as UTC.
    - A frame that concatenates sessions, or one session that spans a flash,
      gets each side's map. That is stricter than `resolve_session_revision`,
      which assigns by session start. No session is ordered by name.
  - **The lookup cannot be handed one revision.**
    - `append_afr_channels(..., flash_history: FlashHistory)` is required; the
      old `tune_revision` parameter is gone.
    - `target_afr_by_revision` reads `frame["timestamp"]` itself.
    - `FlashHistory` is a protocol in `src.signals.afr` (layer 0), which the
      registry (`src.vehicle`, layer 1) implements structurally.
      `NoFlashHistory(reason)` stands in when there is none.
  - **No `active_revision` fallback anywhere** (verifier round 1, LOW).
    - `AfrCalibration.target_map(revision: str)` is exact-match only, with no
      default. The fallback used to make `target_map()` or `target_map(None)`
      return the active map.
    - The 6 test call sites pass their revision.
    - `tuning_digest` refuses a session with no resolvable revision by name
      (`revision_unknown: ...`, `available: false`). It no longer labels the
      active map as that session's.
    - `test_tuning_digest.py::test_a_capture_with_no_resolvable_revision_names_it_and_gets_no_map`.
  - **Live, offline and replay call the same function.**
    - `src.vehicle.profiles.active_flash_history(config)` returns
      `active_registry(config)`. It returns a `NoFlashHistory` naming the
      cause for a missing file or a `RegistryConflictError`. It is cached on
      the registry files' mtimes.
    - Offline, `featurize_wide_frame` uses it by default.
    - Live rows are stamped now, so they resolve to the revision currently
      flashed. That is the rule `/control/calibration` reports as
      `running_revision`, and a test checks the two agree.
  - **Runtime replay reads the recorded instant** (verifier round 1, MEDIUM).
    - `replay_session` rebases every row to wall-clock at emit. So a replayed
      k106 or pre-flash session resolved to ZS44A-20260920, while the same
      session offline gave k106 or a refusal.
    - The fix is on the record. Every record a replay emits carries its
      recorded epoch under `src.telemetry.REPLAY_SOURCE_AT_KEY`
      (`replay_source_at`). This covers every replay entry point: the control
      plane, the YAML knob and the validation harness.
    - `LiveFeatureSource.observe` keeps the newest emit-minus-recorded shift,
      and a parallel deque holds it per buffered row. `reset()`, the replay
      boundary, clears it.
    - `prepare_tick` puts a `flash_history` on the `PreparedLiveTick`
      (required, no default). It is the vehicle's history, or a
      `RecordedClockFlashHistory(base, stamped_ns, shift_s)` that maps each
      row back to its recorded instant when any buffered row came from a
      replay. `build_prepared_live_tick` passes it to `featurize_wide_frame`.
      It is pickle-safe and crosses to the feature process.
  - **Refusals are named, never another revision's map.**
    `target_map_refusal` matches exactly.
    - A revision with no map gives `target_map_unrecorded: ...`.
    - A row with no revision gives `tune_revision_unknown: ...`.
    - `append_afr_channels` logs each reason once per process.
  - **Target maps now recorded: the UpRev base, for the first two flashes**
    (operator, 2026-10-03).
    - **What the files are, per the operator.** The `ROM Store/ZS44A.OsirisROM`
      is UpRev's own stock ZS44A ROM, the base every edit started from. The
      first flash (K 1.07) and k105 were never saved as separate files: each
      went into `ZS44A-Tuned.OsirisROM`, later overwritten by k106. The
      operator edited no fuel-target cell before k106, only K. So both
      revisions carry the base's map.
    - **The base decodes with the same definition as k106.**
      - `src.analysis.osiris.Container` parses it and round-trips it
        byte-exact.
      - The decoded image is 1,048,576 bytes, SHA-256 `8201208033e0926b…`.
      - Its BE32 sum/XOR checksum passes under `rom_targets.yaml`.
      - Its raw rpm axis (0xCAA5) and BFS axis (0xC894) equal k106's.
      - Container SHA-256 `8468e11b…b292`, mtime 2026-07-26 14:40:53 MDT,
        byte-identical to `~/Uprev/ROMs/ZS44A.OsirisROM`.
      - The base's K was not read: the repo has no verified K address
        (`docs/uprev-rom-table-verification.md`, remaining work).
    - **Recorded** in `afr_calibration.yaml` as `ZS44A-osiris-initial` with
      `verified_rom_decode` provenance: file, sha256, decoded hash, method,
      and the operator's statement. `ZS44A-k105` is the same map through a
      YAML merge (`<<: *targets_uprev_base`), so it cannot drift. Only its
      provenance notes differ.
      - The registry is not edited. Its `rom_sha256` names the flashed file,
        and the base is not that file.
      - In this registry "osiris" is UpRev's file format, not a kind of map.
    - **No clamp declarations on the base map.** The edge evidence was logged
      on k106, so this map stays NaN off both axes.
      - The decoded base and k106 differ only in 248 bytes in 9 regions:
        checksums at 0x9598, 15 bytes at 0xCB2A, and UpRev's map slots at
        0xE00xx–0xE27xx. That suggests the same lookup code, but it is not a
        logged match, so it is not used as evidence.
    - **k106 against the base: 10 target cells, all in the two top rpm rows.**
      - Row 6 (4,400 rpm), columns 4–7, go 2 counts richer: 12.14/12.30 →
        11.98/12.14.
      - Row 7 (5,200 rpm), columns 2–7, go leaner: 11.83 / 11.27 / 11.27 /
        11.47 / 11.47 / 11.47 → 11.98 / 11.76 / 11.61 / 11.83 / 11.83 / 11.83.
      - That removes the base's dip at row 7 columns 3–4 (A7, the richest
        cells in the map) and narrows the step between the two rows. It is
        consistent with the registry's "smoothed AFR targets". Rows 0–5 are
        unchanged.
    - **Stock ZS04C stays unrecorded.** Operator, 2026-10-03: the
      `ROM Backup/23710 - ZS04C_VIN Not Found*.bin` dumps (both sha256
      `01cf15e7…abe0`, 1,048,592 bytes) are the true factory data, read from
      the Xterra's ECU before any UpRev flash. `ZS44A.OsirisROM` is the
      "stock" ROM UpRev supplies to flash before tuning; how it relates to the
      factory ZS04C data is unknown, so neither is evidence for the other.
      - The dumps are not plain images: high entropy throughout, no erased
        (0xFF) runs, and neither the base table nor its axes appear in them.
        They also do not decode with the Osiris container's scheme
        (`src/analysis/osiris.py`: CRC-keyed XOR and the 16-bit word
        transform), tried with every candidate key from the leading and
        trailing 16 bytes, the ZS44A key and the CRCs (2026-10-03, desk).
        UpRev's backup format is different, likely real encryption.
      - UpRev support will not help (operator). Reverse-engineering the
        backup format is not planned. The remaining route is Osiris itself
        opening the backup and saving it as an `.OsirisROM`, if it can.
      - UpRev's `* Back To Stock.OsirisROM` files are not used: UpRev says
        flashing one clears the license (operator, 2026-10-03).
  - **Measured: corpus revision assignment.** `discover_sessions` gives 116
    sessions.
    - **90 of 116 change assignment** from ZS44A-20260920: 46 to k106 (one
      cell differs), 25 to the UpRev base map (24 osiris-initial, 1 k105),
      and 19 to ZS04C-stock, which gets no target.
    - One session straddles a flash: `2026-07-26T214348`, stock to
      osiris-initial. Its 64 0x1208 reads give no target either side: the
      stock rows have no map, and no osiris-initial read lands on the map.
  - **Measured: the 25 base-map sessions.**
    - 5 regain `TARGET_AFR`: 26 rows in all. `2026-07-30T210101` has 12
      (density 0.87 %), `07-29T002731` 7, `07-28T164657` 5, `07-26T225058` 1,
      and `07-31T145209` (k105) 1.
    - 15 poll 0x1208 but no read lands on the map. With no clamp declarations,
      a BFS below 8.76 ms stays NaN.
    - 5 never polled 0x1208.
    - None reaches the coverage floor.
    - The A/F sensors' 10.0 floor: no CANary row in these sessions reads
      ≤ 10.0 AFR. The pegged 7-29 logs are UpRev's K-line logs, not CANary
      sessions. The censor mask (`AFR_CENSORED_B*` at ≤ 10.0 or ≥ 18.0 → no
      `AFR_ERROR`) is pinned by
      `test_afr.py::test_decel_fuel_cut_is_censored_like_the_rich_floor`, and
      no floor row carries an error.
  - **Measured: the two drives.** No `TARGET_AFR` value changes from the
    revision change. Both run entirely on k106 by row timestamps, and no row
    touches the one cell the maps differ in. The changes are the clamp and the
    fuel-cut rows above.
  - **Tests.** `tests/test_target_afr_revision.py` (16):
    - the k106 and 2026-09-20 maps differ in exactly cell [7][4];
    - a two-session frame straddling the 2026-09-20 flash gets 11.6148 before
      and 11.5436 after. This holds through `append_afr_channels` and through
      the production featurizer;
    - one session spanning the flash changes map at the flash;
    - a synthetic stock-era instant is refused as `target_map_unrecorded`,
      naming ZS04C-stock;
    - osiris-initial and k105 share the base map: 11.267 at row 7 col 3,
      against k106's 11.760;
    - `active_revision` is never a fallback;
    - the three `tune_revision_unknown` cases;
    - `revisions_at` equals `at`;
    - the live tick uses the revision flashed at "now", and agrees with
      `running_revision`;
    - an edited registry is re-read;
    - **replaying a k106 session through `replay_session` gets the k106 map**.
      Its emit stamps alone resolve to ZS44A-20260920. After `reset()`, live
      rows read the flash of now;
    - **replaying a stock-era session gives no target and logs
      `target_map_unrecorded` naming ZS04C-stock**;
    - `RecordedClockFlashHistory` maps each row through its own shift.

    `test_replay.py` pins the new metadata key, which every replayed record
    carries and which adds nothing else.
  - **Gate, after the round-1 fixes.**
    - 1,499 passed (1 xfailed) over 69 test files. They are every file that
      mentions the target lookup, tune revisions, the AFR calibration, the
      feature cache, `target_map(`, `replay_session`, `LiveFeatureSource` or
      the replay key, plus `test_layering.py`, `test_script_cli_snapshot.py`,
      `test_analysis_ground_truth.py`, `test_feature_pipeline*.py` and
      `test_profiles.py`.
    - `-m ml_training -x`: 122 passed.
    - `-m "not integration and not slow"`, after the last fix: 6,464 passed,
      1 skipped. One collection error, `tests/test_alerts_emitter.py`
      (`IsolationForestInferenceEngine` no longer in `src.alerts.emitter`),
      belongs to a concurrent session's uncommitted alerts → models move, not
      to this change; ml_training ran with that file ignored.
    - ruff, format and mypy are clean.
  - **Behaviour changes (verifier round 2, LOW).**
    - The feature-frame cache key now hashes both `tune_revisions.yaml` files
      (`active_registry_sources`). Recording any flash, including a dashboard
      `append_flash` to the operational copy, invalidates every cached
      feature frame, and the next build re-featurizes everything.
    - Fuel-cut masking follows φ's as-of hold (1 Hz grid, `max_gap` 2 s, φ
      polled every ~3 s on default). A target can be withheld for up to ~2 s
      after fuel resumes, and a cut can go unmasked for the same window at
      its start.
    - A replay whose registry cannot be read logs the registry's own cause
      (the wrapped `NoFlashHistory.reason`), not the generic refusal.
    - Open, informational: `TargetEditor.from_bytes` refuses the UpRev base
      ROM because two revisions (first flash, k105) now match it; before,
      none did, so the refusal is not new, but its "exactly one" message is
      now misleading. `AfrCalibration.active_revision` has no reader left
      and is provenance only.
  - Verified by an independent verifier (round 2): all acceptance holds,
    and HEAD plus these files alone pass mypy and 926 targeted tests.

- [ ] **P02.17 Session-name time and first-row time disagree on some
  sessions.** Low priority. Found by the P02.16 verifier, 2026-10-03.
  - 7 of 150 sessions have a first row more than 2 minutes after their
    directory name:

    | session | first row after name |
    |---|---|
    | 07-22T143129 | +978 s |
    | 07-27T010704 | +8,070 s |
    | 07-28T011050 | +5,473 s |
    | 07-29T002731 | +372 s |
    | 07-29T205841 | +11,413 s |
    | 08-03T163440 | +23,834 s |
    | 08-06T002019 | +434 s |
  - Only `2026-08-03T163440` crosses a flash. `resolve_session_revision`, by
    name, says k105, while P02.16's per-row lookup says k106. The two maps
    differ (k105 carries the UpRev base map), so the tuning tools and
    `TARGET_AFR` disagree about this session.
  - Proposed: resolve a session by its first row, or flag the disagreement
    where they differ. Not implemented.

## Tracked elsewhere

- **The sequence detector's tick cost is A06.6's.** It is not duplicated
  here. A06.6 already covers a lower cadence or post-drive scoring, measured
  on the Pi. P02 adds a third option to that list: keep the forward pass on
  every tick but make it cheaper, with int8 dynamic quantization or an ONNX
  Runtime export. This option is unmeasured here, and it would be judged by
  replay score parity.

## Order

1. **P02.6.** It takes minutes, and every later item's gate runs faster for
   it.
2. **P02.1.** Long drives get a digest on the Pi, and the laptop scripts stop
   loading whole sessions.
3. **P02.2.** Tuning analyses go from minutes to seconds. Measure it before
   deciding on P02.3.
4. **P02.4 and P02.5.** Evaluation and retrain-gate wall time.
5. **Pi, once connected:**
   1. P02.8 and P02.10 first. They cut volume at the source, so every raw
      reader above gets roughly half the input. Both need the operator's
      decision.
   2. P02.7, sharing one writer benchmark with them.
   3. A06.6, which needs the operator's choice.
   4. P02.9.

## Relations

- **P01** is the base: the store, per-unit invalidation, `refuse_on_the_pi`
  and the part-by-part chain.
- **D03.6** built the long-form reduction that P02.1 replaces on the Pi.
- **A06.6** owns the sequence detector's cadence and cost.
- **D01.4** moved live featurization off the event loop; P02.9 finishes it.
- **A02.11** is the benchmark for P02.4 and P02.5.
- **T01** covers test reliability; P02.6 is its timing side.

## Not in scope

- Rewriting historic sessions into a new format.
- A derived store on the Pi.
- Any change to a model's features or thresholds. Parity is the acceptance.
