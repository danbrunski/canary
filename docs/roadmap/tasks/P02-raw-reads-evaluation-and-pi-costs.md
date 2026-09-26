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

- [ ] **P02.4 Featurize an injection incrementally.** `injection_sweep`
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

- [ ] **P02.5 Score drives in parallel, and cache scores.**
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

- [ ] **P02.10 Store CAN frames on change; re-write an unchanged id once per
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
