# S01 — cold start: record sooner after power-on

Opened 2026-09-22, after two fixes and a `py-spy` trace of the daemon's cold
start on the Pi. Same conventions as D03: task-level truth is the checkboxes
here, `[x]` only with the measurement in hand and the artifact linked, `[~]`
partial with the remaining work named.

## Where it stands

**The metric is the first record stored, measured from the unit starting.**
Log lines are not evidence of it: after `04b6cd0` the log said "collectors
started" 25 s before any collector ran, because the retention sweep held the
event loop in the meantime. Read the session's first `can` / `obd` timestamp
(or S01.1's `/status` block once it exists).

| cold boot, 2026-09-22 | before | after `04b6cd0` + `a870887` |
|---|---|---|
| `canary.service` starts (from kernel) | 33.8 s | 18.5 s |
| collectors running (from unit start) | 93 s | **69 s** |
| HTTP answers (from unit start) | 93 s | 72 s |
| models loaded | before collection | after collection, off the loop |

What landed:
- **`04b6cd0`** — collectors, storage and the fanout start before anything
  slow. Models load in the poller's thread, the live source and the app are
  built in threads, and the live sink joins the fanout when its source is
  ready. `canary.service` no longer waits for `network-online.target`, and
  `install_pi.sh` disables cloud-init after first boot.
- **`a870887`** — the retention sweep runs in a thread. On the loop, its first
  tick took 26 s at a cold boot (it stats 62,091 session files and opens every
  parquet to check its footer), and the collector tasks queued behind it. The
  same tick ran every hour during drives.

Where the time before recording goes. These stage times are from the
**cold-restart trace** (page cache dropped, the rest of the system idle), in
which the loop was free to start the collectors at ~43 s once `a870887`
removed the retention stall. A real boot stretches the same stages to 69 s,
because the unit now starts while the rest of the boot is still reading the
card:

| cold restart, from unit start | what | cost |
|---|---|---|
| 0 → ~10 s | interpreter + `src.main` imports | `import src.main` 9.5 s cold / 1.0 s warm |
| ~11 → ~16 s | VIN probe, ECU asleep | 3 × 2 s Mode 09 timeouts (S01.4) |
| ~17 → ~40 s | `src.daemon` imports, including **torch** | `src.daemon` 11.9 s cold / 1.8 s warm; torch samples at t ≈ 33–40 s (S01.2) |
| ~40 → ~43 s | `build_components`, collector wiring, loop start | not yet broken down (S01.1, S01.3) |

The root cause under every row is **cold reads from the SD card**: the same
work is 5–10× faster warm (imports 28.6 s cold vs 3.8 s warm; the IF load
34 s vs 3.3 s). S01.7 is the hardware answer; S01.1–S01.5 are what code can do.

**How to measure** (the method that found the retention stall; keep using it):
install `py-spy` into a scratch venv on the Pi (`python3 -m venv /tmp/spyvenv
&& /tmp/spyvenv/bin/pip install py-spy`), then with no capture active:
`restart_canary.sh --check`, stop the unit, `echo 3 >
/proc/sys/vm/drop_caches`, start it, and `py-spy dump --pid <MainPID>` every
~1.5 s until `/status` answers. The main thread's innermost `src/` frame at each
sample is the timeline. A real reboot is the final check, because a dropped
cache on a running system is not the same as a cold boot with the rest of the
system starting beside it.

## Rules

- Nothing here may delay a record to make startup look faster. The order is
  fixed: record first, score and serve second.
- Nothing may run on the event loop that blocks for more than ~50 ms. Anything
  that walks the filesystem, loads an artifact or imports a heavy module goes
  in a thread (see `poll_model_managers`, `run_retention_sweeper`,
  `serve_in_background`).
- Do not lower timeouts to buy seconds. The VIN probe's 2 s is what a sleeping
  ECM needs to be told apart from a slow one; S01.4 removes redundant waits,
  not the wait.
- No page-cache pre-warming (`vmtouch` and the like). It moves the cost, and
  reading 900 MB of venv sequentially off the SD card is not cheaper than
  reading the part that is needed.

---

## Desk

- [x] **S01.1 Make startup timing a first-class, measured thing.** Today the
  only way to know when recording began is to query the session's parquet or
  run `py-spy`. That is how a log line claiming "collectors started" hid a 25 s
  stall behind it. **Do:** record monotonic and wall timestamps for: process
  start (from `/proc/self/stat` or `time.monotonic()` at `src.main` import),
  `canary starting`, profile resolved, collector tasks created, **first record
  stored per source** (from the storage sink, not the collector), live source
  ready, each model loaded, app built, HTTP bound. Expose them as a
  `startup` block in `/status` (compact view too), with each stage as seconds
  from process start, and log one summary line when the last stage completes.
  Add the unit's start time (`systemctl show -p ExecMainStartTimestampMonotonic`
  is not readable as `canary`; take the process start time instead and say so).
  **Accept:** a test that drives `run_daemon` with a fake collector and asserts
  the stages are ordered and `first_record_stored` comes from storage; on the
  Pi, one cold reboot reports every stage, and the `first_record_stored` value
  agrees with the session's first parquet timestamp to within one flush
  interval. Record the numbers here.
  **2026-09-22 (desk half done):** `src/startup.py` `StartupTimeline` —
  stages `main_entered`, `profile_resolved`, `canary_starting`,
  `daemon_imported`, `components_built`, `collector_tasks_created`,
  `first_record_stored` (plus per source, with each source's earliest record
  timestamp), `live_source_ready`, `models_polled` (plus `model_loaded` per
  model), `app_built`, `http_bound`; seconds from the process start read from
  `/proc/self/stat` (the block's `clock` field says why, and
  `process_start.source` falls back to `timeline_created` without procfs).
  `first_record_stored` is marked by `run_storage_sink` after a successful
  write. Carried on `HealthMonitor.startup`, served as `startup` in the full
  and compact `/status`; one `startup complete: first record stored N s after
  process start` line when every expected stage is reached (a failed live
  source build is `skipped`, not pending). Tests:
  `tests/test_startup_timeline.py` (9), including
  `test_run_daemon_reports_every_stage_in_order` (a writer held shut shows
  `first_record_stored` null while the collector's record waits, then every
  stage in order and one summary line) and
  `test_status_serves_the_startup_block_in_both_views`.
  *Pi cold boot 2026-09-24 04:55:48Z (engine off; code `0d4b32b`+):* the
  summary line reported every stage, measured from process start: main 33.42 s
  (a cold interpreter start from the SD card), profile 35.55, collectors 50.46,
  **first record 51.32 s** (gps), live source 52.57, app 55.88, HTTP 56.66,
  IF loaded 68.19, LSTM 89.53. The warm restart at 16:03:22Z read first record
  5.07 s, collectors 4.88 s. **Still owed:** the cross-check against the
  session's first parquet timestamp. It is confounded on that boot because
  the records went to `2026-09-24T035130`, a name taken from the pre-GPS clock
  (no RTC cell, `docs/time-sync.md`), so the wall clock stepped under the
  stamps. It needs a boot whose clock is right at start.
  *Closed 2026-10-04, Pi reboot at 19:43:10Z with the engine already running
  (code `a1da058`):* the clock was right from the start: `rpi-rtc` set the
  system clock to 19:43:02Z at kernel boot (the RTC kept time through a reboot
  on UPS power), and chrony selected a source at 19:43:32Z with no step. The
  stages, seconds from process start (19:43:17.796Z): main 6.62, daemon
  imported 24.37, collectors 37.11, **first record stored 37.776 s** (can and
  gps; sensor and obd 37.812), live source 39.69, app 45.39, HTTP 45.66, models
  polled 78.20. `first_record_timestamp_by_source.can` = 19:43:54.913424Z,
  identical to the earliest row of `2026-10-04T194342/telemetry-00000.parquet`;
  the store happened 0.66 s after that record (19:43:55.572Z), inside the 5 s
  `flush_interval_seconds`. Session named 19:43:42 from the same correct clock.

- [x] **S01.2 Don't import torch until a sequence model loads.** Chain, found
  with a `sys.meta_path` finder on the Pi: `src/daemon/runtime.py` →
  `src/control/replay_control.py` → `src/alerts/emitter.py` (imports
  `src.models.isolation_forest`, which executes the package `__init__`) →
  `src/models/__init__.py:3` → `src/models/inference.py:20` →
  `src/models/lstm_autoencoder.py:80` `_module_base()` →
  `importlib.import_module("torch.nn")`. So any `src.models.*` import loads
  torch (620 MB on disk) on the main thread before the loop starts, costing
  roughly t ≈ 33–40 s of the cold trace. **Do:** make `src/models/__init__.py`
  import nothing eagerly. Nothing in `src/`, `tests/`, `scripts/` or `tools/`
  does `from src.models import …` today (checked 2026-09-22), but re-check, and
  use a module `__getattr__` if a re-export must stay. Then check the rest of
  the chain: `lstm_autoencoder`'s class bases need torch at class-definition
  time, so it must only be imported by the loader (`sequence_registry._load_lstm`
  already imports lazily), never by `inference.py` at module level. The child
  scorer loads models itself and is unaffected; the parent's fallback copy is
  loaded by the poller in a thread, which is where torch should arrive.
  **Accept:** a test asserting that after `import src.daemon` (and
  `from src.collection.collectors import default_collectors`), `"torch" not in
  sys.modules` — it fails today. The LSTM still loads and scores (existing
  sequence-detector tests), and a cold reboot shows collectors running earlier
  by about the torch share (report the before/after with S01.1's block).
  **2026-09-22 (desk half done):** `src/models/__init__.py` imports nothing;
  its names resolve lazily through a module `__getattr__` (re-checked: no
  `from src.models import` anywhere in src/tests/scripts/tools).
  `models/inference.py` imports `MissingLSTMAutoencoderDependencyError` only
  inside `_torch_module`'s `except ImportError`. The emitter no longer imports
  `isolation_forest` at all (S01.3). `tests/test_cold_start_imports.py`:
  `test_recording_path_does_not_import_torch` (fresh interpreter: `import
  src.main; import src.daemon; import src.daemon.runtime; from
  src.collection.collectors import default_collectors` → no `torch`; failed at
  HEAD), `test_models_package_imports_nothing_eagerly`,
  `test_sequence_inference_module_does_not_import_torch`,
  `test_models_package_lazy_reexports_still_resolve`. LSTM load/score:
  `test_lstm_inference.py`, `test_lstm_autoencoder.py`,
  `test_sequence_detector_control.py`, `test_scoring_continuity.py`,
  `test_inference_worker.py` green. Laptop, warm, min of 7: `import
  src.daemon` after `import src.main` 2,746 ms / 2,198 modules → 165 ms /
  1,064 modules. *Pi before/after (cold boots):* first record 69 s after the
  unit starts (after `04b6cd0`/`a870887`, this file's header) → **51.3 s** on
  the 2026-09-24 04:55Z cold boot (collectors 50.5 s). Torch now arrives only
  with the LSTM (89.5 s), after recording began.

- [~] **S01.3 Nothing on the path to recording imports what recording doesn't
  need.** After S01.2, repeat the cold trace and list every heavy import still
  on the main thread before the collector tasks run. Known candidates:
  `src/daemon/server.py` imports `src.dashboard.api.create_app` at module top,
  so the whole API module tree (FastAPI, every route's dependencies) loads
  before collection; `src.daemon.inference` (pandas feature pipeline, via
  `runtime.py`'s top-level imports); scikit-learn/scipy via
  `src.models.isolation_forest` (1.7 s of sklearn import warm, from
  `cProfile`, several times that cold). **Do:** move each to a lazy import at
  its first use (inside the thread that builds it), one at a time, measuring
  each. Keep module-level imports where the symbol is needed for type hints
  only (`TYPE_CHECKING`). **Accept:** a cold trace with no non-collection
  imports before `first_record_stored`, and a test in the style of S01.2's
  for each module moved, so it cannot creep back.
  **2026-09-22 (desk half done), laptop `-X importtime` + a `sys.meta_path`
  tracer, warm, incremental cost after the collection imports:**
  `src.dashboard.api` 601 ms → built in `server._build_app`'s thread (with
  FastAPI and uvicorn); `src.daemon.inference` 328 ms → imported by
  `runtime.build_live_feature_source` in the live-source thread (the
  `src.daemon` facade re-exports it through a module `__getattr__`);
  `src.models.isolation_forest` 167 ms (feature pipeline, FSM limits,
  pdfplumber) → the emitter resolves its five column names at the first IF
  row, `build_components`' IF manager gets `components._load_isolation_forest`,
  which imports the loader at the poller's first (threaded) load, and
  `replay_control`, `polling` and `drain` import `LiveFeatureSource` under
  `TYPE_CHECKING` only; `src.control.trip_end` 155 ms (digest, fingerprint,
  surfaces, session classifier) → `runtime.handle_trip_end_off_loop` imports
  it in a thread when a trip ends; `src.preprocessing/__init__.py` no longer
  imports every submodule (lazy `__getattr__`); python-OBD + pint (~400 ms
  warm) → `ObdReading`, `AcquiredClock`, `JsonlReadingWriter`,
  `reading_to_record` (+ `acquisition_metadata`, `TS_SCHEMA_VERSION`) moved to
  `src/collection/obd_reading.py`, which imports neither (`obd_collector`
  re-exports them), and `collectors.py` imports `ObdSpec`/`REQUIRED_PIDS`/
  `collect_obd` only in its ELM327 branch; httpx (~42 ms) → the notifier
  builds its client in a thread at the first push. sklearn was already off
  the path. Guards: `tests/test_cold_start_imports.py::test_recording_path_does_not_import`
  (18 parametrized cases, including `obd`, `pint`, `httpx`, `pdfminer`); the
  subprocess also runs `build_components(<IF + LSTM enabled>,
  load_models=False)` and `default_collectors(config)` exactly as
  `run_daemon` does (restoring the eager IF import fails 5 of them); plus
  lazy-re-export tests for both packages. Laptop, warm: `import src.main` +
  `src.daemon` + `build_components` + `default_collectors` loads 921 modules
  (1,064 before this round, 2,198 at HEAD). **Still on the path, not moved:**
  `sequence_training`/`sequence_dataset` via `components` →
  `sequence_detector` → `sequence_registry` (~14 ms incremental; those files
  belong to the concurrent A02 work), and pandas/numpy, which the recording
  path's own modules import at top level (`src.timefmt`, `decode.dids`,
  `collection.replay`, `preprocessing.loader` via `control.sessions`,
  `analysis.derived` via `capture_profiles`: 17 modules). Moving pandas is a
  refactor of collection and storage code, not a lazy import, and was not
  attempted. `build_components` and the `FrameDecoder.from_path` bindings
  read still run before the collector tasks and are not broken down.
  **Still owed:** the Pi cold trace.

- [x] **S01.4 One silent Mode 09 answer is enough.** `probe_vehicle_info`
  (`src/profiles.py`) asks for the VIN, the calibration ID and the CVN in
  sequence, each with `vin_probe_timeout_seconds` (2 s). With the ECU asleep
  all three time out: 6 s on the cold trace (socket opened at 25.86 s,
  profile resolved at 31.86 s). **Do:** if the VIN request gets no response
  at all (not an NRC, not an implausible payload — silence), skip the other
  two, because the same module will not answer them either, and log that they
  were skipped. An NRC or a partial answer keeps today's behaviour (each
  InfoType independent). **Accept:** tests with a scripted ECU: silent → one
  request and one timeout; answering → all three read; VIN NRC → the others
  still asked. On the Pi, a key-off boot's probe takes ~2 s.
  **2026-09-22 (desk half done):** `probe_vehicle_info` taps every frame the
  client receives (`CanObdClient(on_frame=...)`); a VIN request that returns
  nothing *and* heard no frame in `0x7E8–0x7EF` skips the calibration ID and
  CVN and logs "calibration ID and CVN skipped (no responder on the bus)". An
  NRC, an implausible payload or a first frame whose transfer never
  completes all count as heard. No timeout changed. Scripted-ECU tests in
  `tests/test_profiles.py` (a real `CanObdClient` against an ECU that
  segments ISO-TP and waits for flow control):
  `test_probe_silent_ecu_costs_one_request_and_one_timeout` (one request,
  elapsed < 2 × timeout; failed at HEAD with three requests),
  `test_probe_answering_ecu_reads_all_three`,
  `test_probe_vin_nrc_still_asks_the_others`,
  `test_probe_implausible_vin_still_asks_the_others`,
  `test_probe_partial_vin_answer_still_asks_the_others`.
  *Pi key-off boot 2026-09-24 04:55Z:* journal "VIN probe: no Mode 09
  response; calibration ID and CVN skipped (no responder on the bus)"; the
  probe cost profile_resolved − main_entered = **2.13 s** (was 6 s).

- [~] **S01.5 Make a retention tick cheap, not just off the loop.** `a870887`
  moved the tick into a thread, but it still stats every session file and
  opens every parquet to read its last four bytes (`_is_truncated_parquet`),
  every hour: 62,091 files on the Pi today, growing with the corpus. Off the
  loop it no longer stalls sinks, but it is still disk I/O and GIL time during
  a drive, and the collector competes with it. **Measure first:** log each
  tick's duration and the number of files it touched, and read it on the Pi
  (warm, parked; then once during a drive). **Do, if it is more than a second
  or two:** check footers only on files the previous tick has not seen (their
  mtime is newer than the last tick, or the session is newer than the last
  tick), and take session sizes from one walk per tick instead of two
  (`_session_sizes` and `enforce_storage_cap` each walk the tree). A session's
  files do not change after it closes, so a closed session verified once
  never needs its footers read again. **Accept:** a test that a second tick
  over an unchanged tree opens no parquet files; the tick duration on the Pi
  before and after, recorded here.
  **2026-09-22 (desk half done):** every tick logs `retention tick` with
  `duration_ms`, `sessions`, `files_stat` and `footers_read`. One walk per
  tick (`src.storage.retention.scan_sessions`, timestamp-named sessions only:
  `_archive`, `_capture-*` and `_replay-*` are no longer walked for nothing)
  feeds the reclaimed-bytes figures, the husk check and the byte cap
  (`enforce_storage_cap(sizes=...)`). `FooterLedger`, kept by the sweeper
  across ticks, holds each session verified with its parquet fingerprint
  (count, bytes, newest mtime); a session that gains, loses or rewrites a
  chunk is read again. Implemented without waiting for the Pi measurement
  because the laptop already put a tick over the "one or two seconds" bar.
  Tests: `tests/test_retention_tick.py` (6) —
  `test_second_tick_over_an_unchanged_tree_opens_no_parquet`,
  `test_a_tick_lists_each_session_once`, `test_a_changed_session_is_verified_again`,
  `test_tick_logs_its_duration_and_the_files_it_touched`,
  `test_scan_sizes_match_a_full_walk`,
  `test_byte_cap_still_evicts_oldest_first_from_the_scan`; the existing
  `test_parquet_writer.py` retention tests and `test_daemon.py` sweeper tests
  green. Laptop, a hard-linked copy of `~/canary-data/sessions/nissan-xterra-2008`
  (73 sessions, 18,074 session files, ~31k more under `_`-prefixed dirs),
  warm, host load ~16: HEAD 3,314 / 3,044 ms per tick (7,724 ms the first);
  now 1,290 ms for the first tick (17,768 footers read), then 588 and
  1,066 ms with `footers_read: 0`. *Pi, parked (2026-09-24, 120–121
  sessions, 33,273 files):* the first tick after a start reads every footer
  (49.8 s on the cold boot, 6.6–7.0 s after warm restarts), then every later
  tick reads **0 footers in 0.59–0.86 s** (10 hourly ticks). **Still owed:**
  a tick during a drive. The pre-change tick was never logged on the Pi, so
  the "before" is the laptop's 3.0–3.3 s measurement above.

## Needs the truck

- [~] **S01.6 Key-on after the startup changes.** After the 16:32Z reboot on
  2026-09-22 `/status` read `no_data`, and the kernel's `can0` counter showed
  0 frames received since boot (70 transmitted, no errors). That is below
  anything the daemon does, and the most likely reading is that the bus had
  gone to sleep since key-off (the 16:21Z boot, still inside the wake window,
  stored 41k CAN frames). It has not been confirmed. **Do:** at the next key-on,
  read `vehicle_state` (expect `ignition_only`, then `engine_running` after
  start) and confirm `can0` RX is counting. Then one cold power-on with the
  engine started at the same moment: report `first_record_stored` per source
  (S01.1, or the session's first parquet timestamp if S01.1 has not landed)
  measured from power-on. **Accept:** both states observed, CAN and OBD rows
  stored, and the power-on figure recorded here.
  **2026-09-22 (partial):** bus sleep confirmed as the reading of the 16:32Z
  `no_data` boot — after key-on can0 counted 16,038 → 539,295 frames and
  `/status` read `engine_running` (16:54:02Z), later `ignition_only` at
  17:18:50Z on key-off, so the classifier and the interface are both fine.
  The `ignition_only` *edge at key-on* was not sampled: key-on to start took
  < 27 s and fell between a manual read and the 5 s poller's first sample.
  Still owed: that edge, plus the cold power-on figure (the Pi was already up
  for 20 min). Next drive: have the poller running before the key turns.

## Operator decision

- [ ] **S01.7 Move the root filesystem off the SD card?** Every remaining
  number above is dominated by cold random reads from the SD card: the same
  imports take 28.6 s cold and 3.8 s warm, and the IF artifact 34 s and 3.3 s.
  The Pi 5 has a PCIe slot. An NVMe drive (HAT and drive, plus cloning the
  current card and setting the EEPROM boot order) would cut the cold side by
  several times, for startup and for any cold read during a drive (the
  trip-end digest, sessions pulled for analysis). **Cost of yes:** the
  hardware, a clone and a boot-order change done in the driveway (a Pi that
  fails to boot loses nothing there), and the extra power draw to re-check against the UPS
  shutdown gate. **Cost of no:** S01.1–S01.5 recover
  what code can recover, and the rest stays. If yes, the migration becomes its
  own task list here (clone, boot order, verify `memory.max` and the cmdline
  edit survive, re-measure with S01.1).
