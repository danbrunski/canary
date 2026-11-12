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

- [ ] **R23.1 The parquet chunk is built row by row, with a `json.dumps` per
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
  - [ ] Measure on the Pi: stored rows per second in a `tuning` capture and
    time per chunk publish (`/status` storage stats, or a py-spy sample).
  - [ ] `records_to_table(records, schema)` in `parquet_writer.py`: one pass,
    nine column lists, an `lru_cache` on the metadata JSON keyed by the sorted
    items. `parquet_row()` stays for one-row callers.
  - [ ] `write_many` calls `publish_due()` once per batch.
  - [ ] A parity test: for a mixed batch (CAN, OBD, DID, memory, sensor rows)
    the new table equals the `from_pylist` one.
  - Must not change: column order, nullability, the `chunk_schema` footer, the
    `sort_keys=True` spelling of `metadata_json`, the arming gate and rotation.

- [ ] **R23.2 Three request paths walk the whole corpus to answer a question
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
  - [ ] Time the three calls against a pulled copy of the Pi's sessions.
  - [ ] `latest_session`: walk newest first and return the first session with
    telemetry. Same answer, one `has_telemetry` in the common case.
  - [ ] `list_sessions`: apply `since` / `until` / `limit` to the parsed
    directory names, then summarize the survivors.
  - [ ] `_session_paths`: list the root once, filter directories by name and
    date, glob inside the matches.
  - Must not change: captures count for `latest_session`; an empty newest
    directory is skipped; `_`-prefixed directories stay out of unscoped
    queries. Order by `session_started_at`, never by name.

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
