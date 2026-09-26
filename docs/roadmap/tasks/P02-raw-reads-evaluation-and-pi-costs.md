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

- [ ] **P02.1 Every `resample_and_segment` caller on the part-by-part
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

  Size **M**.

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

- [ ] **P02.8 Decide whether to keep persisting decoded rows.** 28 % of stored
  rows are `decoded` values: seven channels at ~84 Hz, 95 % of them
  repeating the previous value. They can all be computed from the payloads
  stored beside them. Recomputing gives the same values (P01 round 2), and
  P01's decode recomputes every binding anyway. Plausibility's stream loader
  still prefers stored rows where they exist.

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

- [ ] **P02.10 Store CAN frames change-only, with a heartbeat (operator
  decision).** On the 09-20 drive, 41 % of CAN frames repeat their id's
  previous payload.
  - **What.** Store a frame only when its payload differs from that id's
    previous frame. Add one heartbeat frame per id per second while nothing
    changes, and per-id frame counts per second.
  - **What is kept.** Every distinct payload survives. Re-decoding after a
    binding correction, and discovery on the 16 unbound ids, lose nothing.
    Liveness keeps 1 s resolution, and receive-loss counting keeps its
    per-second counts.
  - **What is lost.** Sub-second frame timing, which only bus-health work
    reads.
  - **Combined with P02.8.** Dropping decoded rows as well leaves roughly
    8 M rows for that drive instead of 17.9 M. That is still over the 5.4 M
    digest budget, so P02.1 stays needed.
  - **The parity catch.** Two readers depend on dense samples:
    - The resampler takes each bin's last value and marks a channel stale
      once its last observation is older than its max gap. Without the
      heartbeat, a steady signal would read as stale. The heartbeat must be
      no longer than the grid interval.
    - Acquisition-time pairing looks for a sample near each OBD read, so the
      reader must reconstruct as-of values. That belongs inside the reader,
      never in its callers.
  - **Mixed corpus.** Old full-rate sessions stay as they are and still read.

  **Accept:** write full-rate sessions change-only, then:
  - the 1 Hz frames and features match exactly (`check_exact`);
  - `tests/test_analysis_ground_truth.py` passes, including its 1:1
    broadcast-versus-Mode 01 check;
  - `compare_channels` gives the same pairs and verdicts on three captures;
  - rows, bytes and writer CPU are measured on the Pi;
  - a mixed old/new corpus reads.

  **Later, not here.** Masking the counter bytes on the twelve ids that
  change every frame could save more. A mask is per-vehicle knowledge,
  though, and would need its own verification.

  Size **M–L**, Pi and operator.

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
