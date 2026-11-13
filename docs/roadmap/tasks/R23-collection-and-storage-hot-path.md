# R23 — Collection and storage: the hot path, then the duplication

Part of the 2026-11-12 review ([R22](R22-refactor-README.md)). Scope:
`src/collection/`, `src/storage/`, `src/decode/`, and the root modules
`telemetry.py`, `session.py`, `columns.py`, `can_ids.py`, `jsonl_store.py`,
`atomic_write.py`. Counts and line numbers are review snapshots; locate code by
symbol. Nothing here has been implemented. Shared acceptance rules are in R22.

**Timings are laptop numbers** from throwaway scripts under `.venv/bin/python`
(WSL). Nothing was run on the Pi. Each performance item starts with a
measurement box for that reason: the mechanism was read in the code, the size
of the win on the Pi was not measured.

**In-flight work.** Another session had uncommitted edits in
`can_obd_loop.py`, `capture_quality.py`, `capture_lifetime.py` and
`obd_scheduler/cadence.py` during the review. R23.4 and R23.11 touch those
files and go after that work lands.

**What held.** R18: burst planning, slots, the budget clock and the three
turnarounds still live only in `memory_burst.MemoryBurstPlanner`; nothing
regrew in `did_window.py` or `cadence.py`. `isotp.await_response` is the one
receive policy (R02.5). The thread-to-loop hand-off already batches
(`ThreadHandoff`, 256 per turn; storage 1,024 per worker call). There is one
session-name parser.

## Performance

- [~] **R23.1 The parquet chunk is built row by row, with a `json.dumps` per
  row.** `TelemetryRecord.parquet_row` (`src/telemetry.py`) builds a dict per
  record and serializes `metadata` with `json.dumps(…, sort_keys=True)`;
  `_write_records_atomic` and `write_records_to_parquet`
  (`src/storage/parquet_writer.py`) then call `pa.Table.from_pylist(rows)`.
  For a CAN row the metadata is always `{"can_id", "dlc"}` plus an optional
  `frame_kind`, a few dozen distinct values per drive. Measured on 10,000 CAN
  records: `parquet_row` 6.5 µs per row (6.3 of it the dumps), `from_pylist`
  11.4 µs, 37 µs per row end to end. A column-list build with the metadata
  JSON memoized took 3.4 µs per row and produced an equal table
  (`Table.equals`). `write_accepting` also calls `publish_due()` per record,
  and `write_many` loops it per record instead of once per batch.
  This runs in the publish thread and holds the GIL the collector loop needs.
  - [~] Measure on the Pi: stored rows per second in a `tuning` capture and
    time per chunk publish (`/status` storage stats, or a py-spy sample).
    **Laptop numbers on the real pulled corpus, not Pi numbers** (2026-11-12,
    no Pi access): the batch is 10,000 records strided from three stored chunks
    of `_capture-tuning-2026-11-12T034249` (`~/canary-data/sessions/
    nissan-xterra-2008`; read back into `TelemetryRecord`s: CAN 8,270, sensor
    748, OBD 522 of which `mem:` 186 and `DID_` 125, GPS 404, derived 56), and a
    second, sensor-heavy one from `_capture-fuel-pulsation-2026-10-08T210012`
    (CAN 6,885, sensor 3,002). Medians of 15 repetitions per process, HEAD
    (`6aecb13c`) and the working tree alternated back to back, 5 rounds (4 for
    the second batch), machine load ~5. Before, on the tuning batch:
    `parquet_row` 3.01 µs per row + `from_pylist` 0.76 = **3.77 µs (265k rows/s)**
    to build the table; the end-to-end chunk (`_write_records_atomic`: build,
    snappy write, two fsyncs, rename) **4.79 µs per row, 47.9 ms per 10,000-row
    chunk (209k rows/s)**, the build being 79 % of it. The review's 37 µs per
    row did not reproduce on this machine and corpus (its dumps share did: the
    memo below is most of the win). Still to measure on the Pi, not while it
    collects: stored rows per second in a `tuning` capture and the publish time
    per chunk from `/status` storage stats, to see what the 3 µs per row (and
    the GIL it holds against the collector loop) is there.
  - [x] `records_to_table(records, schema)` in `parquet_writer.py`: one pass,
    nine column lists; the metadata JSON is memoized (`lru_cache`, 4,096
    entries, keyed by the sorted items) in `src/telemetry.metadata_json`, the
    one spelling both `parquet_row()` (which stays) and the column build call,
    beside `split_value` for the value columns. The memo keys only metadata
    whose values are `str`, `int` or `None`: `True == 1 == 1.0` as a Python key
    but `true`, `1` and `1.0` as JSON, so a `bool` or `float` value serializes
    directly (a float, `acquired_at`, is also unique per row, which no memo
    helps), and an unhashable value is the same direct path, never an error.
    Tuning batch: 8,245 memo hits, 25 misses, 1,730 rows direct, of 10,000.
    After, same conditions: build **2.19 µs per row (457k rows/s)** (3.77
    before); end-to-end **2.90 µs per row, 29.0 ms per chunk (345k rows/s)**
    (4.79 / 47.9 ms): **-39 %**. Sensor-heavy batch (30 % float metadata, which
    never memoizes): end-to-end 8.93 -> 6.05 µs per row (-32 %), build 7.11 ->
    4.53. `write_table_atomic` (snappy + fsyncs, 7.3 ms per chunk on this disk)
    is unchanged, as it should be. Scripts and raw runs are the session
    scratchpad's (`r231_bench.py`, `r231_run.sh`, `r231_ab.txt`,
    `r231_ab_pulsation.txt`).
  - [x] `write_many` calls `publish_due()` once per batch (one that recorded
    something: a batch the arming gate withheld never ticked it, and still does
    not). One exception, to keep `max_rows` exact: the record that fills the
    buffer to `max_rows` publishes it there, so a batch longer than the room
    left still cuts its chunk at `max_rows` (the daemon's batches are up to
    1,024 records; a strictly-once call would let a chunk overshoot by that
    much). `write_accepting`/`write` still publish per call. Side effect:
    `/status` `publishes_deferred` now counts per batch (plus the `max_rows`
    moment), not per record, so it reads far lower than before for the same
    stall; don't trend it across this commit. A background-mode chunk can now
    overshoot `max_rows` by up to one batch while a publish is in flight (the
    inline path stays exact). Pinned by
    `tests/test_records_to_table.py::test_write_many_publishes_once_per_batch`,
    `..._reads_the_clock_once_per_batch_not_per_record`,
    `..._still_cuts_a_chunk_at_max_rows_inside_a_long_batch`,
    `..._chunks_like_a_record_at_a_time_writer` (same chunks, row for row, as
    writing the batch one record at a time), `..._flushes_an_aged_buffer_at_the_end_of_the_batch`.
  - [x] A parity test: `tests/test_records_to_table.py`. A mixed batch (CAN
    data, error and remote frames; Mode 01 int, float, bool and no-value rows;
    `DID_` hex text; `mem:` with payload and float metadata; sensor, GPS, IMU,
    derived, K-line and UpRev rows; metadata empty, int-only, float-bearing,
    insertion-order-reversed, unhashable, NaN, non-ASCII) gives a table with
    `.equals(check_metadata=True)` the `from_pylist` one, equal schema, field
    nullability and column order, the same `chunk_schema` footer, and the
    `metadata_json` column equal to `json.dumps(r.metadata, sort_keys=True)` per
    record (an oracle independent of `parquet_row`, which shares the helper: a
    mutation that let `bool`/`float` into the memo failed three tests, and only
    this oracle caught it in the table test). The two written files are
    byte-identical. Also over **real data**: `r231_realdata_parity.py` re-built
    72 stored chunks (562,376 rows; 7 sessions; CAN, OBD, GPS, sensor, derived,
    old `decoded` rows) from their `TelemetryRecord`s, and every column of the
    new table equals the stored chunk's, `metadata_json` text included (what the
    old writer produced), with 0 mismatching chunks.
  - Must not change: column order, nullability, the `chunk_schema` footer, the
    `sort_keys=True` spelling of `metadata_json`, the arming gate and rotation.
    Held by the parity tests above, and by the 128 test files that import
    the writer, `storage.sessions`, the query, `parquet_row` or `write_many`
    (`test_parquet_writer.py`, `test_background_chunk_publish.py`,
    `test_change_only_can_storage.py`, `test_log_to_parquet.py`,
    `test_parked_state.py`, `test_daemon.py`, ...): 2,266 passed, 1 skipped, 1
    failed, the failure being `test_source_rules.py::test_the_allowlist_only_shrinks
    [parquet-glob-literal]`, a rule in the concurrent agent's uncommitted
    R24.8 work, which now sees `src/storage/telemetry_query.py` without its three
    `"telemetry-*.parquet"` literals (R23.2 imports `PARQUET_PATTERN`): its
    allowlist line `"src/storage/telemetry_query.py": 3` is to be removed.

- [~] **R23.2 Three request paths walk the whole corpus to answer a question
  about the newest session.**
  - `storage.sessions.latest_session` evaluates `has_telemetry` (a glob and
    stats) for every session, then returns the last. `/status`, the dashboard
    readers and `daemon/runtime.py` call it; `/status` is polled.
  - `storage.sessions.list_sessions` runs `summarize_session` (chunk glob,
    `dir_size` over every file, tags and manifest reads) for every directory;
    `filter_sessions(limit=N)` trims afterwards. `GET /control/sessions?limit=`
    pays for all of them. Its docstring says to revisit "if cost becomes a
    measured problem".
  - `storage.telemetry_query._session_paths` runs
    `sorted(rglob("telemetry-*.parquet"))` over the tree on every unscoped or
    date-filtered query and parses the parent directory's name once per chunk.
  Locally (37 sessions, 2,002 chunks) `latest_session` takes 10 ms. The Pi
  holds far more.
  - [x] Time the three calls against a pulled copy of the Pi's sessions.
    **Laptop numbers on the real pulled corpus, not Pi numbers** (2026-11-12):
    `~/canary-data/sessions/nissan-xterra-2008`, read only: 264 directories (182
    live sessions, 82 `_` ones), 91,313 chunk files (37,429 in live sessions,
    15,829 in `_capture-*` / `_tuning-*`, 38,055 under `_archive/`). Warm
    dentry cache; each figure is the median over 3 alternated rounds (HEAD
    `6aecb13c` then the working tree, back to back) of the median of 3 calls;
    load was ~5 in round 1 and ~1 in rounds 2-3, so the per-round figures are
    shown. (`_session_paths` in the item is `TelemetryQuery._matching_files`.)
    Script: `r232_bench.py` / `r232_run.sh` in the session scratchpad.

    | call | before ms | after ms | per round, before / after |
    |---|---|---|---|
    | `latest_session` | 25.7 | 4.4 (5.9x) | 43.0, 25.7, 22.4 / 4.4, 4.5, 4.1 |
    | `list_sessions(limit=20)` (route shape: filter after) | 537 | 23.4 (23x) | 867, 537, 440 / 23.4, 24.1, 21.3 |
    | `list_sessions(captures, limit=20)` | 216 | 82 (2.6x) | 375, 216, 190 / 82, 83, 74 |
    | `list_sessions(since=2026-11-01)` (26 sessions) | 456 | 24.2 (19x) | 816, 456, 438 / 24.2, 25.0, 23.3 |
    | `list_sessions()` unbounded (182 sessions) | 438 | 465 (no bound to apply) | 851, 438, 433 / 467, 465, 442 |
    | `_matching_files()` unscoped (37,429 paths) | 3,570 | 38.9 (92x) | 7,160, 3,570, 3,509 / 38.9, 38.7, 41.1 |
    | `_matching_files(since, until)` (5,285) | 4,026 | 14.9 (270x) | 8,248, 4,026, 3,943 / 14.5, 14.9, 15.4 |
    | `_matching_files(since=2026-11-01)` (1,571) | 3,985 | 11.4 (348x) | 7,177, 3,985, 3,913 / 11.7, 11.4, 11.0 |
    | `_matching_files()` from the parent root | 3,971 | 37.6 (106x) | 3,662, 3,971, 4,223 / 37.8, 37.2, 37.6 |

    Where it went: `latest_session` ran `has_telemetry` for 182 sessions and
    now runs it once (the empty newest directory `2026-11-12T064649` is
    skipped to `2026-11-12T040404`, as before). `rglob` listed 91k chunk files
    to keep 37k and built and sorted a `Path` for each; the sort of 37k `Path`
    objects was 70 % of the new walk's first version, so chunks are now ordered
    per directory, and by `Path` only when one directory holds another that
    holds chunks too. Not measured: the Pi's own figures (its corpus is larger
    and its disk slower), and the end-to-end `GET /control/sessions?limit=`
    request (the route still has the old call shape, below).
  - [x] `latest_session`: walks `iter_sessions` newest first (parsed start
    time, never name) and returns the first session with data; one
    `has_telemetry` in the common case
    (`tests/test_session_walks.py::test_latest_session_asks_once_when_the_newest_holds_data`:
    30 sessions, 1 call). Old vs new over the **real corpus**: identical at
    three roots (`r232_equiv.py`).
  - [x] `list_sessions`: takes `since` / `until` / `limit`, applied to the parsed
    directory names before `summarize_session` runs, so only the survivors are
    summarized (`test_list_sessions_summarizes_only_what_it_returns`); the
    bounds share one predicate (`_within`) with `filter_sessions`, which stays
    for a caller holding summaries. Same answer as
    `filter_sessions(list_sessions(root), ...)`: 96 parametrized cases in
    `test_list_sessions_with_bounds_is_the_old_list_then_filter` against a
    verbatim copy of the old `filter_sessions`, and over the **real corpus**
    71 bound / limit / captures cases (1,212 summaries) identical.
    `GET /control/sessions` (`get_control_sessions`) now passes its bounds to
    `list_sessions` instead of filtering every summary afterwards
    (`tests/test_control_api.py`, `tests/test_session.py` pass).
  - [x] `_session_paths` (`TelemetryQuery._matching_files`): `_chunk_files`
    lists each directory once and only the ones the query needs: an unscoped
    sweep never enters a `_` directory, a dated one never lists a session
    outside the range (catalog lookup once per directory, it was once per
    chunk), a named one lists the layers above the sessions and the session.
    Names are parsed once per directory. A directory named like a session is a
    session: nothing below it is searched for further sessions when it is
    pruned (a named lookup, a dated sweep outside the range). Unscoped, every
    non-`_` directory is still listed in full, so a chunk nested below a
    session directory is still found, as `rglob` found it. 35 scenarios (unscoped, dated
    with naive / offset / session-id bounds, exact instant, empty, named live /
    capture / archived / absent, from the vehicle directory and the root above
    it) over the **real corpus**: 125,048 paths, same order, identical to
    HEAD's. Over the synthetic tree, 25 scenarios x 2 roots plus 25 with a
    catalog whose start for one session differs from its name.
  - Must not change: captures count for `latest_session`; an empty newest
    directory is skipped; `_`-prefixed directories stay out of unscoped
    queries. Order by `session_started_at`, never by name.
    `tests/test_session_walks.py` (189 tests) holds the old answers verbatim
    and runs them against the new over a tree with a capture newer than every
    live session (`_capture-aaa-` sorts before `_tuning-` by name), an empty
    newest directory, a zero-byte chunk, `_archive` and `_replay-*`, chunk
    lookalikes (`.tmp`, `xtelemetry-...`), a directory holding a directory that
    holds chunks, and `loose` / `loose-b` (where `-` < `/` as characters but not
    as path parts). Three mutations were each caught: the nested-directory order
    fallback removed (5 failed), a bound made exclusive (6), `latest_session`
    scanning oldest first (2, by the call-count spies). `tests/test_source_rules.py`'s
    sessions-ordered-by-name ratchet: none of the new code lists a directory
    into `sorted`/`min`/`max`, so no count changes.

- [ ] **R23.3 The OBD loop re-derives eligibility for every request on every
  tick.** `_loop_request_verdict` (`can_obd_loop.py`),
  `obd_scheduler/selection._count_active_requests`, `_tick_controller`, the
  priority key in `_select_next_due_request`, `cadence.note_attempt` and both
  `_update_response_state` calls each go through a `tuning_profile.*` getter.
  Each getter takes the lock and allocates a `ProfileView`, and
  `ProfileView.selection_exclusion` rebuilds its upper-cased sets per call.
  Measured 3–5 µs per verdict; with 40–60 planned requests that is
  0.2–0.3 ms per tick on the laptop against a ~10 ms request slot. The share
  of the loop thread on the Pi is an estimate.
  - [ ] Measure with `scripts/sustained_validation.py` under a focused
    profile, before and after.
  - [ ] Cache: the policy is pure in `(request, ProfileView)` (R02.3) and the
    loop already sees profile changes through `capture_generation`. Build the
    eligibility table once per generation (and on pause, resume, and when
    discovery appends a request), or make the sets a `cached_property` of
    `ProfileView`.
  - Must not change: the C06.7 verdicts and reasons that `/status` reports,
    `trigger_poll`'s forced lift. Pinned by `test_session_window_predicate.py`,
    `test_memory_burst_traces.py` (30 digests), `test_hybrid_scheduling.py`,
    `test_diagnostic_discovery.py`.

- [ ] **R23.4 The USB ADC fits its sample clock and parses every line on the
  event loop.** `collect_usb_adc` calls `_Assembler.line` inline;
  `SampleClock._refit` runs every 256 samples (about twice a second at
  503.9 Hz) and measured 1.7 ms median, 8.5 ms worst, on the laptop. Each line
  is matched by three regexes, two of them on the same text. Only the serial
  read is threaded. This is the path a full-rate fuel-pressure capture arms.
  - [ ] Measure event-loop lag on the Pi with `analog_full_rate` on.
  - [ ] Run the assembler in the reader thread and hand records over through
    `ThreadHandoff`; parse each line once.
  - [ ] Check `hat_adc_collector.py` for the same shape (not reviewed).

- [ ] **R23.5 The last two `iterrows` loops in `src/`.**
  `collection/replay.py` `_read_chunk` and `decode/dids.py` `decode_did_rows`.
  `decode/dbc.py` already records what this costs ("building a Series per
  frame was ~90 % of this function's time", 2026-09-25). `decode_did_rows`
  runs on every session load that carries raw `DID_<hex>` rows.
  - [ ] Column lists in both; in `decode_did_rows`, decode each distinct
    `(channel, value_text)` once.
  - [ ] A source test that refuses `.iterrows()` under `src/`, next to
    `tests/test_yaml_load.py`. This has now been found three times.

- [ ] **R23.6 Per-frame object cost, taken together.** About 15 µs per
  broadcast frame on the laptop: `frame_to_record` 9.8 µs (a `FrameArrival`
  dataclass, `datetime.fromtimestamp`, an `astimezone(UTC)` on a datetime that
  is already UTC, a frozen dataclass init, a metadata dict, an f-string
  channel name) and `CanChangeFilter.keep_record` 3.7 µs (converts the
  datetime back to a float, copies the payload, re-parses the hex id out of
  the channel name, takes `max()` of a 16-deque). No single hot spot, so it
  ranks below R23.1–R23.3.
  - [ ] `slots=True` on `TelemetryRecord`; skip `astimezone` when the tzinfo
    is already UTC; `keep_record` reads `metadata["can_id"]`; a running
    maximum for the inter-arrival.

## Duplication and structure

- [ ] **R23.7 `_tick`, `_serve_single` and `_issue_batched_request` repeat the
  same bookkeeping.** In `can_obd_loop.py` `_CanObdLoop`:
  - the wake-up call
    `self._drain_until(_wake_for(now, self.controller, self.request_states, …).at)`
    is written five times (four in `_tick`, one in `_ensure_round`), differing
    in two arguments;
  - emitting and crediting a reading is written twice, once per serve path
    (`readings_emitted`, `rounds.on_observed`, `stats.record_read`,
    `on_reading`, `_update_response_state`, and the timeout branch with
    `note_miss`). A new accounting field has to be remembered in both;
  - the "focused capture" predicate
    (`tuning_profile.is_active() and tuning_profile.request_eligible(…)`)
    appears three times;
  - request selection in `_tick` is three branches of one choice.
  - [ ] `_sleep_until(now, *, budget_at=None, next_due=None)`;
    `_credit_answer` and `_credit_timeout` used by both serve paths;
    `_is_focused_capture(index, request)`; `_choose_request`.
  - No behavior change. `test_bus_budget.py` counts source occurrences of
    `_spend_budget_slot` and `bus_budget.effective`; keep those call sites.
    Pinned by `test_memory_burst_traces.py`, `test_batch_*`, and the new
    `test_capture_request_ledger.py`.

- [ ] **R23.8 `CanObdClient` sends a request in six hand-copied blocks.**
  `query_mode_01_batch`, `query_mode_22_batch`, `query_raw`,
  `query_body_or_nrc`, `_request_or_nrc`, `_request_many`: each builds a
  `can.Message`, calls `tx_gate.send(…, holder=self._tx_holder)`, catches
  `can.CanError`, logs, and returns its own empty value. W08.7's `holder`
  argument had to be threaded through all of them. `_registry_width` is
  byte-identical to `obd_scheduler/requests.mode01_registry_width`.
  - [ ] `CanObdClient._send(arbitration_id, data, *, what) -> bool`.
  - [ ] One width helper, in `obd_registry.py` beside `lookup`.
  - `can_obd_io`'s send stays separate: its `CanError` has to reach the loop's
    reconnect.

- [ ] **R23.9 The broadcast tap and the parse-error catch exist twice.**
  `can_collector.CanReader.run` and `on_frame` inside
  `can_obd_collector.collect_can_obd_from_config` both do: skip private
  traffic, `note_bus_frame()` unless it is a tester request, hand off
  `frame_to_record(msg)`. R02.1 made `can_bus.receive_frame` and
  `FRAME_PARSE_ERRORS` the one place a parse error is caught;
  `can_collector.py` still spells `except (ValueError, IndexError)` twice.
  - [ ] One `broadcast_tap(power, push)` factory; the two catches use
    `FRAME_PARSE_ERRORS`.

- [ ] **R23.10 The K-line collector writes its session lifecycle twice.**
  `KlineCollector._session` and `_run_action` each carry about thirty lines of
  claim, open, start, stop (swallowing the same three exception types), close,
  record wakes, release. `_run` and `obd_collector.supervise_obd_capture`
  hand-track a reconnect `delay` that `run_with_reconnect` hides for the other
  nine collectors.
  - [ ] An `@asynccontextmanager _module_session(address, profile)`; a small
    `Backoff` on `ReconnectPolicy`.

- [ ] **R23.11 `evaluate_capture_quality` re-parses a loose profile dict that
  two other modules also re-parse.** The first thirty of its 181 lines
  normalize `weights`, `target`, `required` and `floor` out of a
  `Mapping[str, Any]`; `storage/capture_manifest.py` and
  `analysis/capture_coverage.py` do the `required_tuple` part again. The tail
  interleaves eight independent `notes.append` blocks with scoring.
  - [ ] A frozen `ProfileExpectations.from_snapshot(profile, requested_rate_hz)`
    shared by the three; `_notes(…)` taking the computed pieces. The `notes`
    text is asserted by tests and stays.

- [ ] **R23.12 `capture_profiles.py` (1,268 lines) holds the schema, the
  catalog layering, two serving caches and the preview store.**
  `serving_mode22_dids` and `serving_mode01_channels` are the same fifteen
  lines with separate cache dicts; their consumers are trainers, and they sit
  in a module the poll loop imports.
  - [ ] `capture_profile_model.py` (model, catalog, constants, digest),
    `capture_profile_files.py` (sources, overrides, `layer_catalog`, load and
    save, `seed_catalog`), the serving readers on one `_stamp_cached` helper in
    a third. Re-export from `capture_profiles`. Same package, so
    `tests/test_layering.py` needs no new unit.

- [ ] **R23.13 Four durable atomic writes beside `atomic_write.py`.**
  `os.fsync` appears in exactly four files, each with its own
  tmp + fsync + replace + fsync-directory sequence:
  `analysis/gps_tracker.py`, `control/paired_devices.py`,
  `provisioning/network.py`, `storage/parquet_writer.py`.
  `atomic_write_text` does not fsync and uses a fixed `<name>.tmp`, so
  `features/feature_cache.py` and `control/rom_library.py` hand-build a
  per-process temp name.
  - [ ] `atomic_write_bytes(path, data, *, durable=False, unique_tmp=False)`,
    the same flags on `atomic_write_text`, `fsync_dir(path)`, and
    `publish_atomic(tmp, path, durable=True)` for the pyarrow case.
  - Must not change: a parquet chunk is durable when the write returns.

- [ ] **R23.14 Two PID tables for one fact.** `PID_DECODERS` and
  `MODE01_REGISTRY` carry the same name and decoder for about forty PIDs, and
  `assert_pid_tables_consistent()` runs at import because they once drifted.
  `_MODE01_SPEC` is 92 rows of nine-field positional tuples.
  `decode_obd_standard` and `decode_fuel_type` have identical bodies.
  - [ ] Derive one table from the other and delete the assertion; keyword
    fields on the spec rows; a `linear(width, scale, offset, unit)` factory for
    the formula-only decoders.
  - Declined before and still declined (R02): merging the two λ decoders
    (their names carry φ versus λ) or the `decode_*` families across modules.
  - Pin with the ground-truth suite's `catalog_pins`.

- [ ] **R23.15 Smaller moves.**
  - `can_obd_collector.py` re-exports 66 names, 45 of which nothing imports.
    Repoint the dozen real users at `can_obd_loop` / `can_obd_io` /
    `obd_response_decode` and delete the rest.
  - `decode/dids.py`: seven verification helpers (`check_known_answer`,
    `memory_checks`, `relation_reads`, `neighbour_span`, …) whose only callers
    are `analysis/memory_verification.py` and `control/calibration.py` belong
    in `decode/memory_relations.py`; `decode_did_rows` is the only reason the
    module imports pandas.

## Looked at and fine

`DidWindowController` (38 methods) is one state machine with a clear protocol.
Per-reading cost (`dataclasses.replace` 9.7 µs, `reading_to_record` 8.3 µs) is
about 0.2 % of a core at 100 readings a second. `tx_gate.send` is one lock.
Transport differences are deliberate: ELM327 decodes through python-obd and
K-line through its own protocol module; the shared parts (`ReconnectPolicy`,
`run_with_reconnect`, `receive_frame`, `tx_gate`) are used where the transport
allows.

## Not reviewed

GPS, IMU, supply-voltage and sensor-CAN collectors, `decode/engineering.py`,
`decode/dbc.py`, `hat_adc_board.py`, K-line transport and sniffer,
`custom_pids.py`, `vehicle_probe.py`.
