# CANary — Implementation Plan

Derived from `docs/CANary-overview.md`. Tasks are grouped by phase, with FR/NFR
references in parentheses. Each task is sized to be roughly one PR or one
working session. Items inside a phase are listed in execution order; cross-phase
dependencies are called out explicitly.

Status legend: `[ ]` not started · `[~]` in progress · `[x]` done.

Current status (2026-05-16): the software stack is substantially ahead of the
original phase labels. Phases 2/3, the bindings/behavior-model path, dashboard,
alerts, and model management are implemented. The latest hardware reality is
that the ELM327 adapter is dead, so OBD has moved to the Canable-native path:
Mode 01 / 03 / 09 one-shot probing, streaming Mode 01 capture, and streaming
Mode 22 DID reads are now implemented, and the engine-ECM DID space has been
explored: the 2008 Nissan ECM gates 0x22 behind UDS session `10 C0` (kept
alive by re-priming; mutually exclusive with Mode 01), now handled by
`scan_constant_dids.py --session C0` and `CanableMode22Transport`. Injector
pulse width identified as DID `0x1208` (Phase 4.E.4 enabler; scale calibration
+ a structured `data/dids.yaml` map outstanding — see
`data/analyses/2026-05-16T223328.md`). Next: resume the OBD-backed capture
backlog in `docs/data-collection-plan.md`, then proceed with Pi deployment.

---

## Phase 0 — Project Bootstrap

Goal: a runnable empty skeleton, reproducible dev env, CI green on first push.

- [x] **0.1 — Repo skeleton.** Create the directory layout from
  `CANary-overview.md` §"Project Structure" (`src/`, `tests/`, `data/dbc/`,
  `models/`, `systemd/`). Add `__init__.py` placeholders so packages import.
- [x] **0.2 — Python tooling.** `pyproject.toml` (PEP 621) targeting Python
  3.11+, with `ruff` (lint+format), `mypy` (strict on `src/`), `pytest`,
  `pytest-asyncio`. Lockfile deferred until deps stabilize. (NFR-04)
- [x] **0.3 — `requirements.txt` / extras.** Runtime deps (python-obd,
  python-can, cantools, pandas, pyarrow, scikit-learn, fastapi, uvicorn,
  pyyaml, pydantic) live in `[project.dependencies]`; `[project.optional-dependencies.dev]`
  carries tooling (ruff, mypy, pytest, pytest-asyncio, types-pyyaml,
  pandas-stubs). `[project.optional-dependencies.ml]` carries Torch for
  Phase 3 sequence/model work. `scripts/install_deps.py` is the preferred
  setup path: it creates/updates `.venv`, installs project/dev deps, detects
  the local NVIDIA driver CUDA capability, and installs Torch from the matching
  PyTorch wheel index (`cpu`, `cu118`, `cu126`, `cu128`, or `cu130`) with manual
  override support. Documented in `docs/install.md`.
  `[project.optional-dependencies.emulator]` now carries ELM327-emulator for
  the Phase 1.5 integration tests; the current wheel installs cleanly without
  the old sdist workaround.
- [x] **0.4 — Config loader.** `src/config.py` — Pydantic models mirroring the
  `canary.yaml` schema in the overview; load+validate at startup; fail fast on
  unknown keys. Ship a checked-in `canary.example.yaml`. (NFR-04)
- [x] **0.5 — Logging.** Structured logging (stdlib `logging` + JSON formatter)
  with per-module loggers. Single entry point in `src/log.py`.
- [x] **0.6 — Entry point.** `src/main.py` parses `--config`, loads YAML,
  starts an asyncio event loop, wires module lifecycles (collectors → writer
  → inference → dashboard — wire-up TODO until Phase 1), and handles
  SIGTERM/SIGINT for clean shutdown.
- [x] **0.7 — CI.** GitHub Actions: lint, type-check, unit tests on
  `ubuntu-latest` with Python 3.11 and 3.12. No hardware-dependent jobs yet.
- [ ] **0.8 — Dev container.** `Dockerfile` + `docker-compose.yml` for
  non-Pi development; mounts repo, runs `main.py` in simulation mode.
  (NFR-04, NFR-05)

---

## Phase 1 — Data Collection (Overview Phase 1)

Goal: dual-channel telemetry into Parquet, validated against the emulator.

### 1A. OBD-II collector (FR-01)

- [x] **1.1 — PID registry.** `src/collection/pids.py` — declarative list of
  required PIDs (RPM, Speed, Engine Load, Coolant, IAT, MAF, Throttle, STFT/LTFT
  B1, O2 voltages 0x14–0x1B, Baro, Run Time) with units and `obd.commands`
  references. Single source of truth for downstream code.
- [x] **1.2 — Async collector.** `src/collection/obd_collector.py` using
  `obd.Async`. Poll loop driven by `obd_poll_rate_hz`; emit
  `(timestamp, pid_name, value, unit)` records onto an `asyncio.Queue`.
  Log warning + skip on unsupported PIDs; keep running. `obd_factory`
  validates PID names BEFORE opening the connection so a typo fails fast
  with a helpful message. (FR-01)
- [x] **1.3 — Reconnect logic.** Detect adapter disconnect, retry with
  configurable interval (default 10 s), exponential backoff up to a cap.
  Implemented as `src/collection/reconnect.py` (`ReconnectPolicy` +
  `wait_for_stop_or_timeout`); used by both OBD and CAN paths. Tested via
  `test_collect_obd_retries_after_factory_failure` and the corresponding
  CAN test. (NFR-02)
- [x] **1.4 — Read-only enforcement.** `src/collection/obd_collector.py` —
  `WRITE_COMMAND_MODES = {0x04, 0x2E, 0x2F}` + `assert_read_only_command()`
  guard + `WriteCommandRejected` (subclasses `ValueError` so collector
  retry paths still surface it immediately). `resolve_pids()` rejects any
  named PID whose service mode is a write before the connection is opened;
  `enforce_read_only(conn)` wraps `Async.watch/query/unwatch` so any later
  hand-crafted `OBDCommand` (or a future Mode 22 DID extension calling
  `query()` with a raw mode byte) is blocked too. ELM327 ``AT*`` commands
  (mode=None) stay allowed. Idempotent. Covered by `tests/test_obd_collector.py`
  (rejects `CLEAR_DTC`, accepts read modes 0x03/0x09, blocks synthesized
  0x2E, passes through AT, wraps `watch`/`query`/`unwatch`, idempotency).
  (NFR-06)
- [x] **1.5 — Integration tests with ELM327-emulator.** `tests/test_obd_emulator.py`
  — 8 tests against a live `elm.Elm` instance on a pty, with python-obd
  driving the real `Async` loop. Verifies the integration path that the
  unit-test fakes in `test_obd_collector.py` can't:
    * `obd_factory` connects + reports real `supports()` booleans
    * `obd_factory.watch → callback` actually produces `ObdReading`s
      (RPM ≈ 620, SPEED 0 in the emulator's idle profile)
    * unsupported PIDs are warn-and-skipped without crashing (hooks the
      `src.collection.obd_collector` logger directly — pytest's caplog
      can be interfered with by python-obd's import-time logging setup)
    * `supervise_obd_capture` produces readings end-to-end including
      reconnect-policy support
    * round-trip through `JsonlReadingWriter` (the `record.py` path)
    * read-only enforcement (Mode 04 `CLEAR_DTC`) blocks before the
      adapter is opened, against a real adapter not just unit fakes
    * two back-to-back factory builds in one process don't interfere
  Added `[project.optional-dependencies].emulator` to `pyproject.toml`
  pinning `ELM327-emulator>=3.0.5`. The 3.0.5 wheel installs cleanly
  without `--no-build-isolation` (old sdist quirk gone), so the standard
  `pip install '.[emulator]'` works. `pytest.importorskip("elm", ...)` at
  module top means the file skips cleanly when the package isn't present,
  so CI without the extra still passes. **Marked `pytest.mark.integration`
  and excluded by default** (`pyproject.toml`'s
  `addopts = "-m 'not integration'"`) so the ~45 s pty bring-up doesn't
  burn the inner-loop suite cadence; run explicitly with
  `pytest -m integration` or by path. (NFR-05)

### 1B. CAN collector (FR-02)

- [x] **1.6 — SocketCAN bring-up doc.** `docs/can-setup.md` — `ip link set
  can0 up type can bitrate 500000 listen-only on` + udev rule. Reference,
  not code.
- [x] **1.7 — Async CAN reader.** `src/collection/can_collector.py` —
  `python-can` Bus on the configured channel (slcan for the Canable on
  laptop dev, socketcan for the Pi target). `collect_can` /
  `collect_can_from_config` push frames onto the asyncio queue. Wraps
  `bus.recv()` with `try/except (ValueError, can.CanError)` so a single
  corrupt slcan frame doesn't kill multi-hour recordings (counted into
  `RecordingResult.parse_errors` + manifest). (FR-02)
- [x] **1.8 — DBC decode hook (reader).** `src/decode/dbc.py` —
  `load_dbc_database(path)` and `load_dbc_dir(directory) → cantools
  Database | None` (silent `None` when missing/empty, mirroring the
  "no bindings.yaml on disk" fallback). `decode_can_rows_dbc(long_frame,
  database)` walks the CAN rows, looks up each known frame_id, calls
  `message.decode(payload, decode_choices=False)`, and emits new
  `source="dbc"` rows (one per signal) appended to the long frame —
  unknown frames pass through untouched, malformed payloads are caught +
  skipped so a single bad frame can't kill an offline pipeline. Named
  choices / enums are skipped (numeric path only). Wired into
  `build_feature_frame` after the bindings.yaml decode and before
  `to_wide`, so DBC signals show up as `dbc:<SIGNAL>` columns alongside
  bindings' `decoded:<NAME>`. One-way: bindings.yaml stays source of
  truth; the DBC emitter side (4.D.2) is still skipped per the SavvyCAN
  decision. Covered by `tests/test_decode_dbc.py` (12 tests: loader
  variants, encode/decode round-trip, unknown-ID pass-through, empty
  frame, too-short payload, no-can-rows path, multi-frame coexistence).
  (FR-02)
- [x] **1.9 — Synthetic CAN replay test.** Use `python-can`'s
  `VirtualBus` to inject canned frames; assert collector emits expected
  records. `test_record_captures_frames_and_writes_manifest` runs an
  end-to-end sender/receiver over `VirtualBus`. (NFR-05)

### 1C. Storage (FR-03, NFR-03)

- [x] **1.10 — Parquet writer.** `src/storage/parquet_writer.py` — buffer
  records in memory, flush every N seconds or M rows to a Parquet file
  partitioned per ignition session. Schema: timestamp, channel, value,
  unit, source (`obd`|`can`). Uses `pyarrow` writer with snappy
  compression. Implemented as `ParquetTelemetryWriter` (streaming, live)
  + `write_records_to_parquet` (batch, offline) sharing TELEMETRY_SCHEMA;
  daemon-produced and offline-converted Parquet are byte-equivalent.
- [x] **1.11 — Session keying.** `src/storage/parquet_writer.py` —
  `ParquetTelemetryWriter._rotate_if_engine_run_time_reset` watches Engine
  Run Time PID readings (`RUN_TIME` and its lowercase aliases via
  `ENGINE_RUN_TIME_CHANNELS`) and rotates the session directory either
  when the value decreases (engine restart with the previous reading still
  recent) or when it dips back below the
  `engine_run_time_reset_threshold_seconds` floor after having been above
  it (default 5 s — guards against the noisy first reading sometimes seen
  right after `usbipd attach`). New session ⇒ fresh timestamped directory
  from `make_session_dir`; chunk index resets so file numbering doesn't
  collide. Tracked sessions accumulate in `writer.session_paths`. Covered
  by `tests/test_parquet_writer.py::test_writer_rotates_session_when_engine_run_time_resets`.
  (FR-03)
- [x] **1.12 — Ring buffer.** Background sweeper deletes session files
  older than `session_retention_days`. Configurable, default 30.
  Implemented as `delete_expired_sessions` and invoked from
  `run_collection` on shutdown. (FR-03)
- [x] **1.13 — Disk-space monitor.** Poll free bytes on `storage_path`;
  emit warning event when below `storage_warning_mb`. `src/storage/monitor.py`
  with `free_space_mb` + `warn_if_storage_low`. Dashboard surfacing
  deferred until Phase 5. (NFR-03)
- [x] **1.14 — Crash-safety test.** `tests/test_parquet_writer.py` —
  three tests covering the atomic-write contract: (1)
  `test_failed_atomic_write_does_not_publish_final_file` — a mid-flush
  crash leaves no published `.parquet`; the buffer is preserved and the
  next successful flush emits the same data. (2)
  `test_crash_mid_flush_preserves_prior_chunks` — a corrupting
  `pq.write_table` (writes a truncated `PAR1` header and raises) after a
  successful flush #1; the intact chunk reads fine, the lingering `.tmp`
  isn't picked up by `discover_sessions` / `read_session` (the
  `telemetry-*.parquet` glob doesn't match `*.tmp`). (3)
  `test_crash_recovery_with_fresh_writer_appends_new_chunks` — a new
  writer over the same root after a "crash" (no `close()`) lands in a
  fresh dated session directory; the prior session's surviving chunk is
  untouched. The on-disk contract: dated session directories isolate
  writers; the same-directory `.tmp → rename` keeps surviving chunks'
  footers correct under any process kill. (NFR-02)

### 1D. Phase-1 milestone

- [x] **1.15 — End-to-end smoke run.** Run `main.py` against
  `ELM327-emulator` + `VirtualBus` for 5 minutes; verify Parquet files
  open in `pandas` and contain both OBD and CAN rows.
  Covered by `test_run_collection_smoke_writes_obd_and_can_records` —
  uses fake collectors in lieu of the emulator extra, but exercises the
  full queue → writer → Parquet path end-to-end.
- [~] **1.16 — Vehicle data collection.** Log 10+ diverse drives (cold
  start, idle, city, highway, off-road). Document each in
  `data/sessions.md` (date, conditions, notes). Inputs to Phase 2 training.
  Status: ~12 sessions captured (early parked/dev ones, the rev protocol
  2026-05-11T152858, the partial/tagged drives 2026-05-11T154648/211240,
  the 18-min drive 2026-05-11T222447, and the three 2026-05-13 ELM327
  rig-bring-up sessions: KOEO baseline, rev-with-OBD, NT510-active). Each
  documented under `data/analyses/<session>.md`; `data/sessions.md` is the
  consolidated index now. `docs/data-collection-plan.md` lays out the
  remaining captures (cold-start / idle baseline / city / highway /
  load-sweep / off-road / A/C / Mode-22 DIDs) and how much is enough to
  start the ML + behavior-model work. Parallel OBD (`record.py --obd-port`)
  is proven and the default `--obd-pids` set is the fuller ML/mapping one.
  Still gated on drive time and hardware validation: the legacy ELM327 adapter
  is dead, and the Canable-native streaming path is now the OBD-backed capture
  route that needs the next real drive validation.

---

### 1E. Operator capture tooling

Tooling for recording / tagging sessions outside the daemon path — used
for RE captures and for collecting Phase 2 training data before the
daemon is deployed on the vehicle.

- [x] **1.E.1 — `scripts/record.py`.** CLI wrapper around the CAN
  collector: writes `frames.log` (candump format) + `manifest.json`
  per session, supports `--vehicle-state`, `--seconds`, and parallel
  OBD-II `--obd-port` capture via `pids.jsonl` sidecar. The OBD side runs
  in a supervised daemon thread (`obd_collector.supervise_obd_capture`)
  that survives the ELM327 dropping off USB and reattaching (watches the
  device file + python-obd's connection state + a reading-staleness
  heartbeat); an OBD failure never aborts the CAN capture. Default
  `--obd-pids` is the fuller ML/behavior-model set (~19 PIDs incl. MAP,
  both banks' fuel trims, pre-cat O2, baro, run-time, fuel-status).
- [x] **1.E.2 — `scripts/tag.py`.** Append-only tag CLI; writes one
  JSON record per Enter-press into `<session>/tags.jsonl`. Auto-finds
  the most recent session under `data/sessions/`.
- [x] **1.E.3 — `scripts/guided_session.py`.** Interactive protocol
  runner — walks the user through a labeled sequence, one Enter press per
  tag (or `s`+Enter to skip a step that doesn't apply). Protocols: `rev`
  (idle/blip/sustain), `nt510` (module scan, ABS live data + steering/brake
  actions, ECU/ABS actuator tests), and `drive` (cold-start → idle →
  city → highway → coast/DFCO → WOT → 4-Lo → A/C → engine-off — the
  operating-envelope capture for ML training). Designed for hands-busy
  in-vehicle use where typing labels is impractical.
- [x] **1.E.4 — `tools/log_to_parquet.py`.** Offline batch converter
  from candump `frames.log` + `pids.jsonl` to chunked Parquet using
  the canonical TELEMETRY_SCHEMA. Lets pre-daemon captures feed the
  same downstream pipelines as live ones.
- [x] **1.E.5 — `--timestamp` flag on `scripts/tag.py`.** Backfill
  tags after the fact from voice-memo notes (or other post-hoc
  sources). Shipped two mutually-exclusive flags: `--timestamp` takes
  ISO 8601 (with or without tz; naive = UTC) or a Unix epoch; `--offset`
  takes a duration relative to the session's `started_at` in plain
  seconds, suffix-form (`12m`, `1h30m`, `90s`, `1h 2m 3s`), or
  colon-form (`12:30` mm:ss, `01:12:30` hh:mm:ss). Reads the session
  manifest to compute offset baseline and warns to stderr if the
  resolved timestamp falls outside the session window (still writes
  the tag — outside-bounds tags are valid but worth flagging). 41
  unit tests cover the parsers, manifest loading, the resolver, the
  bounds-warning logic, and the full CLI flow.
- [x] **1.E.6 — Canable-native OBD streaming collector + wiring.** Wrap
  `src/collection/can_obd_client.py` in the same supervised producer pattern
  as the ELM327 OBD collector, emit the same `TelemetryRecord` / `pids.jsonl`
  shape, and add config/daemon/recorder wiring so `canable` becomes the default
  OBD source and `elm327` is only a legacy fallback. The one-shot pieces are
  already proven:
  `tools/can_obd_probe.py` walks supported Mode 01 PIDs, reads Mode 03 DTCs,
  and uses the same Canable hardware as the broadcast sniffer. This is the
  highest-priority software task because it unblocks OBD-backed cold-start,
  warm-idle, city/highway, and load-sweep captures after the ELM327 failure.
- [x] **1.E.7 — Canable-native Mode 22 DID reads.** Extend
  `CanObdClient` and the new streaming collector to issue read-only UDS Mode 22
  `ReadDataByIdentifier` requests over the Canable, using a configurable
  physical request ID (default engine ECU `0x7E0`). Emit `DID_<hex>` readings
  into `pids.jsonl` / telemetry in the same shape as the legacy
  `record.py --obd-dids` path so existing cross-reference tools keep working.

---

## Phase 2 — Preprocessing & Isolation Forest (Overview Phase 2)

Goal: a reproducible feature pipeline + a working baseline detector.

- [x] **2.1 — Parquet session loader.** `src/preprocessing/loader.py` —
  read a session (or list of sessions) into a long-format DataFrame; pivot
  to wide (one column per channel) on demand.
- [x] **2.2 — Resampler.** `src/preprocessing/resampler.py` — uniform time
  grid at `resample_hz` (default 1). Forward-fill slow sensors (coolant,
  baro). Gap-flag fast sensors (RPM, MAF) when gap exceeds 2× expected
  period. (FR-04)
- [x] **2.3 — Operating mode segmenter.** `src/preprocessing/segmenter.py`
  — rule-based classifier driven by the YAML thresholds (idle, city,
  highway, WOT, off-road). Returns a per-row mode label. User-extensible:
  unknown modes from config flow through without code changes. (FR-04)
- [x] **2.4 — Rolling features.** `src/preprocessing/features.py` —
  windowed mean, std, and rate-of-change over `rolling_window_seconds`.
  Vectorized with pandas `.rolling()`. Time-based window respects
  session boundaries (no leakage from session A into session B's
  warmup). Rate-of-change is `(value − value.shift(n_rows)) /
  window_seconds`, deriving `n_rows` from the median inter-row delta so
  the same config works at any resample frequency. Skips gap-flag
  columns, mode column, and non-numeric columns automatically; safe to
  call twice in a row. (FR-04)
- [x] **2.5 — Normalization.** Z-score scaler fit on the labeled clean
  baseline only; persisted alongside the model in the manifest. Reject
  inference-time data whose channels don't match the fit schema.
  Implemented as `src/preprocessing/normalization.py` (`ZScoreScaler`,
  frozen dataclass). `fit` auto-discovers numeric feature columns (same
  policy as `features.compute_features`), uses population std (ddof=0),
  errors on all-NaN columns. `transform` raises `SchemaMismatchError` on
  missing fitted columns and passes non-fitted columns through. Constant
  channels (σ < `std_floor`) get σ=1 — centering only, no amplification.
  `to_dict`/`from_dict` give a plain-JSON round-trip for the manifest;
  older manifests without `std_floor` still load. (FR-04, FR-09)
- [x] **2.6 — Synthetic anomaly injector.** Splice spikes, drift, dropouts,
  and stuck-sensor patterns into a clean session. Used by 2.8 and Phase 3
  evaluation. Implemented as `src/preprocessing/anomaly_injection.py`
  (`AnomalyInjector` builder + `InjectionWindow` records) rather than
  `tests/anomaly_injector.py` — placing it under `src/` lets the
  evaluation scripts and future replay harness import it cleanly without
  test-path hacks. `windows_to_records` serializes injection metadata for
  reports. Four kinds: `.spike(value=…)`, `.drift(total_delta=…)`,
  `.dropout()`, `.stuck()` (freezes at the value from the row immediately
  before the window). Validated end-to-end against the z-score scaler:
  spike → max \|z\|≈2200, drift → ≈350, dropout → NaN propagation,
  stuck → raw \|z\|≈1.4 (correctly invisible to raw z-score, which is
  why rolling features exist). (NFR-05)
- [x] **2.7 — Isolation Forest training.** `src/models/isolation_forest.py`
  — fit one sklearn `IsolationForest` per operating mode using
  `n_estimators`, `contamination`, and `random_state` from config;
  fit/persist the
  `ZScoreScaler` on the same complete training rows; score rows with
  larger-is-worse anomaly scores plus sklearn's `1`/`-1` labels; label
  missing-feature rows anomalous for dropout visibility. Serializes a
  `joblib` artifact plus JSON manifest with training data hash, date
  range, sessions, hyperparams, scaler params, schema, and per-mode
  sample summaries. Covered by `tests/test_isolation_forest.py`.
  (FR-05, FR-09)
- [x] **2.8 — Synthetic anomaly evaluation.** Score injected sessions;
  report per-mode precision/recall and the anomaly-score distribution.
  Tune `contamination` per mode based on results. Implemented as
  `src/models/isolation_forest_evaluation.py`: returns scored rows plus
  a JSON-safe report with overall/per-mode confusion metrics, finite-score
  quantiles, non-finite score counts, injection-window records, and
  best-F1 per-mode contamination recommendations. `IsolationForestConfig`
  now accepts optional `per_mode_contamination` overrides consumed by the
  trainer and recorded in the model manifest. Covered by
  `tests/test_isolation_forest_evaluation.py`. (FR-05)
- [x] **2.9 — Unit tests.** Resampler edge cases (gaps, leading NaN),
  segmenter boundary conditions, feature-window correctness on
  hand-crafted inputs. Coverage lives in `tests/test_preprocessing_resampler.py`,
  `tests/test_preprocessing_segmenter.py`, and
  `tests/test_preprocessing_features.py`: leading fast/slow NaNs, strict
  max-gap behavior, inclusive min/max segmenter thresholds, session-boundary
  isolation, rolling mean/std/ROC hand calculations, higher-Hz windows,
  feature toggles, column selection, and empty-input schemas. (NFR-05)
- [x] **2.10 — Training CLI.** `scripts/train_anomaly_model.py` — the
  Phase 2 capstone. Runs the full pipeline (read_session → decode CAN via
  bindings.yaml → to_wide(coerce_numeric) → resample → operating-mode
  segment, or a single `all` mode when no OBD PIDs are present → rolling
  features), fits the per-mode Isolation Forest + z-score scaler, writes
  the joblib artifact + JSON manifest. `--evaluate <session>` additionally
  splices a synthetic spike+dropout into a held-out session and prints
  per-mode precision / recall / score-distribution + a recommended
  `contamination` (the tuning feedback Task 2.8 calls for). Verified
  end-to-end against the 18-minute drive session 2026-05-11T222447:
  trained on 92 feature columns (23 decoded signals + their rolling
  derivatives), single `all` mode (no OBD), artifact written; the
  injected-spike evaluation produced a sensible-but-untuned baseline
  (precision ≈ 0.18, recall ≈ 0.5 at the default contamination=0.01 —
  exactly the signal that contamination needs raising). (FR-05)

---

## Phase 3 — LSTM Autoencoder (Overview Phase 3)

Goal: deep model that catches temporal anomalies the IF misses.

- [x] **3.1 — Sliding-window dataset.** `src/models/sequence_dataset.py`
  — PyTorch map-style `Dataset` yielding `(T, F)` windows that a
  `DataLoader` batches to `(B, T, F)`. Includes a pure-pandas window
  builder for CI coverage without the optional `ml` extra; `__getitem__`
  imports torch lazily and raises `MissingSequenceDependencyError` when
  torch is absent. Supports `from_sessions(...)` to load Parquet sessions
  through the existing preprocessing path, stride control, explicit feature
  columns, metadata lookup, and filters out windows spanning sessions,
  containing gap flags, or containing missing feature values. Added
  `[project.optional-dependencies].ml = ["torch>=2.0"]`. Covered by
  `tests/test_sequence_dataset.py`. (FR-06)
- [x] **3.2 — Model.** `src/models/lstm_autoencoder.py` — encoder LSTM →
  latent (`latent_dim`) → decoder LSTM. Configurable depth/hidden size.
  Implemented as a lazy-Torch module so non-ML imports still work without
  the optional dependency. `LSTMAutoencoder` consumes `(B, T, F)` batches,
  exposes `encode`, `decode`, `forward`, serializable architecture metadata,
  and MAE reconstruction-error reductions for future threshold calibration
  (`none`, `sample`, `timestep`, `channel`, `global`). `build_lstm_autoencoder`
  constructs from config and supports device/dtype placement. Added
  `hidden_size`, `num_layers`, and `dropout` to `LSTMAutoencoderConfig` and
  `canary.example.yaml`. Covered by `tests/test_lstm_autoencoder.py`. (FR-06)
- [x] **3.3 — Training loop.** Train on clean sessions only; MAE loss;
  early stop on validation loss; checkpoint best weights; save manifest.
  Implemented as `src/models/lstm_training.py`: `train_lstm_autoencoder`
  consumes a `SequenceWindowDataset` built from clean baseline sessions,
  performs a deterministic train/validation split, trains with Adam + MAE,
  monitors validation loss when a validation split exists (otherwise train
  loss), restores the best checkpoint, and returns `TrainedLSTMAutoencoder`.
  `save_lstm_autoencoder_model` writes a portable Torch checkpoint plus
  `<artifact>.manifest.json`; `load_lstm_autoencoder_model` restores the
  architecture and weights. The manifest records training hash, sessions,
  time range, feature schema, architecture, hyperparams, split sizes,
  best/stopped epochs, early-stop status, and per-epoch losses. Added
  `batch_size`, `learning_rate`, `max_epochs`, `validation_fraction`,
  `early_stopping_patience`, `min_delta`, and `random_state` to
  `LSTMAutoencoderConfig` / `canary.example.yaml`. Covered by
  `tests/test_lstm_training.py`. The one-command path from recorded
  sessions to a calibrated checkpoint is `scripts/train_lstm.py` (the LSTM
  counterpart to `scripts/train_anomaly_model.py`): it runs the shared
  feature pipeline (`src/preprocessing/feature_pipeline.py` — read session →
  decode CAN via `bindings.yaml` → wide → resample → operating-mode segment →
  rolling features, factored out of the IF trainer so both behave
  identically), builds sliding windows, trains + calibrates, writes
  `model.pt` + `model.manifest.json`, and `--evaluate <session>…` runs the
  Task 3.6 false-positive benchmark on held-out clean sessions (auto-excluded
  from the training set when sessions are discovered). Covered by
  `tests/test_train_lstm.py`. (FR-06, FR-09)
- [x] **3.4 — Threshold calibration.** Compute per-channel and global
  reconstruction-error distributions on the training set; set threshold
  to `μ + k·σ` with configurable `k` (default 3). Persist in manifest.
  Implemented in `src/models/lstm_training.py` as
  `calibrate_lstm_thresholds`, and run by default at the end of
  `train_lstm_autoencoder` using `config.threshold_k`. Global thresholds
  use per-window MAE across all timesteps/channels; per-channel thresholds
  use per-window MAE across timesteps for each feature column. The manifest
  `thresholds` block records `threshold_k`, calibration timestamp, training
  data hash, window count, and count/mean/std/min/max/p50/p90/p95/p99 plus
  `threshold` for both global and per-channel error distributions. Save/load
  preserves the calibrated thresholds. Covered by `tests/test_lstm_training.py`.
  (FR-06)
- [~] **3.5 — Real-time inference loop.** `src/models/inference.py` —
  consume a sliding window from the live preprocessing buffer, run a
  forward pass, emit anomaly score. Software path complete:
    * `LSTMSequenceBuffer` accepts model-ready feature rows, resets on
      session changes / missing features / non-finite values, emits a
      full `(T, F)` window only after `sequence_length` clean rows.
    * `LSTMInferenceEngine` requires calibrated thresholds, runs CPU
      inference by default, computes global and per-channel reconstruction
      errors, compares them to manifest thresholds, emits anomalous
      channels, anomaly flag, model version, window timestamps, latency.
    * Live feature source: `src/preprocessing/live.py:LiveFeatureSource`
      (Milestone B) is the row-by-row analog of `build_feature_frame`.
    * Daemon-side IF inference: `src/daemon.run_live_inference` drives
      `IsolationForestInferenceEngine` off the live source at the
      configured resample cadence.
    * Daemon-side LSTM inference: `src/daemon.run_live_inference_lstm`
      with `_lstm_observe` schema-alignment helper — NaN-fills any
      trained feature column the live source hasn't seen yet (the buffer
      resets on non-finite values during warmup, exactly the "wait for
      schema" behavior we want).
    * Both inference tasks are spawned by `run_daemon` alongside the
      collectors + writer + dashboard, all in one event loop. Records
      flow producer → `asyncio.Queue` → `drain_queue_with_broadcast`
      → Parquet + telemetry broadcaster + `live_source.observe`; per
      `resample_hz` tick the inference tasks pull a feature row and
      score → emit → log + anomalies broadcaster.
  Remaining work before marking done: run on the Pi 5 target and confirm
  the 1 Hz CPU budget is <50%. (FR-06, NFR-01)
- [x] **3.6 — False-positive benchmark.** `src/models/false_positive_benchmark.py`
  + `scripts/fp_benchmark.py` — feed held-out *clean* Parquet sessions through
  the same `from_sessions` preprocessing the LSTM was trained on, score every
  sliding window, and report: the false-positive rate at the manifest's
  calibrated threshold (FPR = flagged / total — every flag on clean data is by
  definition a false positive), the implied `alerts_per_hour` vs. the v1
  done-definition budget (≤ 1 alert / hour, derived from the wall-clock span of
  the held-out windows), a sweep of `k` multipliers (default `1 2 3 4 5 6 8 10`
  plus the calibrated `k`, each recomputing `threshold = mean + k·σ` from the
  *training* error distribution stored in the manifest so a tighter/looser
  threshold can be picked without retraining — the "iterate on `k`" feedback),
  plus per-channel exceedance counts and per-session FPR/alert rates. The
  combined flag mirrors live inference (`global error > global threshold` OR any
  channel over its threshold). `benchmark_false_positives` validates the
  held-out dataset against the trained model (calibrated thresholds present,
  matching feature columns, matching sequence length) and returns a JSON-safe
  `FalsePositiveBenchmarkReport`. Covered by
  `tests/test_false_positive_benchmark.py` (loose/tight thresholds,
  k-sweep monotonicity, JSON round-trip, the four validation errors, and an
  end-to-end Parquet → train → save → benchmark CLI run). Wired into
  `scripts/train_lstm.py --evaluate`; running it on real drives is pending more
  held-out clean captures (see 1.16). (FR-06)
- [x] **3.7 — Replay harness.** `scripts/replay.py` — feed a recorded
  Parquet session through preprocessing + inference end-to-end, no
  hardware needed. Used in CI. Implemented as an importable CLI that loads
  one or more Parquet session directories, pivots numeric telemetry,
  resamples, segments, computes rolling features, optionally loads an
  IsolationForest artifact for scoring, and emits a JSON report with
  row counts, source/channel counts, mode counts, numeric/rolling/model
  feature columns, complete model-input rows, and score distribution.
  CAN-only payload captures replay cleanly but surface zero complete
  numeric model rows until decoded CAN/binding features land. Covered by
  `tests/test_replay_script.py`. (NFR-05)

---

## Phase 4 — CAN Reverse Engineering & Signal Bindings

Runs in parallel with Phases 2–3. Bottlenecked on RE work, not software.

**Status note:** the project's primary signal-decode artifact is
`data/bindings.yaml` + the Pydantic-typed `Signal` model, NOT a DBC file.
This was a deliberate pivot — `bindings.yaml` carries confidence levels,
evidence trails, and plausibility bounds that a DBC can't express, which
matters more during active RE than DBC's external-tool interop does. DBC
emission stays viable as a one-way derived artifact (4.D.2 below).

### 4.A — Capture protocols & methodology

- [x] **4.1 — RE workflow doc.** `docs/can-re-workflow.md` — the rig
  (Canable sniffer / ELM327 / NT510), the capture workflow (`record.py` +
  `guided_session.py` protocols, physical-action tags, engine-state
  discipline), the analysis pipeline (inventory → byte-vs-tag differential
  → `cross_reference_broadcast.py` / `cross_reference.py` / `scan_constant_dids.py`
  → FSM cross-reference → `decode_bindings.py` validation), the
  confidence ladder + evidence-trail conventions for `bindings.yaml`, and
  a worked example (`ECM_RPM`). Written now that the ELM327 closed a full
  hypothesis loop. Reference, not code.
- [x] **4.A.1 — Physical-action capture protocol.** Codified as the
  `rev` protocol in `scripts/guided_session.py`. Key engineering choice:
  tags mark physical events on the vehicle (idle, blip, sustained,
  recover, engine off), not NT510 screen selections. The first attempt
  used screen-selection tags and produced noisy bindings; the rev
  session repaired this.

### 4.B — RE analysis tools (built)

- [x] **4.B.1 — ISO-TP / UDS decoder.** `src/analysis/iso_tp.py` +
  `tools/decode_isotp.py` — reassemble multi-frame UDS messages,
  decode services 0x10/0x14/0x19/0x21/0x22/0x27/0x2F/0x3E, group by
  tag-defined windows for offline analysis.
- [x] **4.B.2 — Broadcast inventory.** `tools/inventory_broadcast.py` —
  per-CAN-ID frame counts, median periods, DLC, per-byte unique-value
  counts. Surfaces which IDs are signal-bearing vs. counter/padding.
- [x] **4.B.3 — Byte-vs-tag differential.** `tools/broadcast_signals.py`
  — for every `(can_id, byte_index)` pair, computes per-tag-window
  mean/range/uniques, ranks bytes by cross-window mean spread (Δ). High
  Δ = real signal; low Δ = counter or padding.
- [x] **4.B.4 — Mode 22 ↔ Mode 01 cross-reference.** `tools/cross_reference.py`
  + `src/analysis/cross_reference.py` — pair Mode 22 DID reads against
  python-obd Mode 01 PIDs by timestamp, linear-regress to derive
  scale/offset. ELM327 is now connected and working (protocol 6 ISO
  15765-4 CAN 11/500). Companion: `tools/cross_reference_broadcast.py` +
  `src/analysis/broadcast_correlate.py` — the broadcast-byte ↔ Mode 01
  PID counterpart (regress a CAN byte's raw value against each PID;
  probes default to `bindings.yaml`'s unpinned signals). The original ELM327
  session pinned `ECM_RPM` (0x1F9:2-3, raw÷8, r²=1.000) and confirmed
  `ECM_COOLANT_TEMP` (0x551:0, raw−40, r²=0.999); future PID ground-truth
  captures use the Canable-native OBD path.
- [x] **4.B.5 — FSM signal extraction.** `tools/extract_fsm_signals.py`
  + `src/analysis/fsm.py` — parse Xterra Factory Service Manual PDFs
  (Hitachi/Nissan CONSULT-III tables) into `data/fsm/signals.json` (333
  named parameters across 8 modules) including numeric expected ranges.
- [x] **4.B.6 — FSM range-match scoring.** `src/analysis/range_match.py`
  — score candidate FSM parameters against captured DID raw ranges
  across a grid of common scalings + offsets. Tripod with module
  attribution and tag-fuzzy matching, used in `tools/decode_isotp.py`.

### 4.C — Bindings system (built)

- [x] **4.C.1 — Canonical bindings file.** `data/bindings.yaml` — typed,
  hand-edited record of broadcast-byte signal mappings with confidence
  level (`confirmed`|`high`|`medium`|`low`|`hypothesized`), evidence
  trail (per-session + method + finding), notes, and optional
  plausibility bounds. Schema-versioned for forward-compat.
- [x] **4.C.2 — Bindings decoder library.** `src/decode/bindings.py` —
  strict Pydantic schema (typos in YAML fail at load time);
  `decode_raw(signal, data)` extracts the integer; `apply_scaling`
  applies scale+offset; `is_plausible` checks against declared bounds.
  Foundation for the daemon's eventual runtime decode path.
- [x] **4.C.3 — Bindings validation reporter.** `tools/decode_bindings.py`
  — walks a session, decodes every binding against every matching
  frame, reports per-signal raw + decoded ranges + plausibility status
  grouped by confidence level. Catches binding bugs automatically (the
  COOLANT engine-off −40°C sentinel was found by this tool).

### 4.D — Future integration (not done)

- [x] **4.D.1 — Wire bindings decoder into preprocessing.** Decoded CAN
  signals now flow into the ML pipeline as numeric columns the model can
  learn from — closing the gap between the RE half (bindings) and the ML
  half (loader → resample → segment → features → scaler → IsolationForest).
  Implemented as `src/preprocessing/decode.py`:
    * `decode_can_rows(long_frame, bindings, confidence_levels=…)` appends
      `source="decoded"`, `channel=<signal name>` rows whose value is the
      RAW integer from the binding's byte range (scaling is for human
      reports, not the model — the scaler normalizes anyway, and most
      bindings have no pinned scale yet).
    * `read_session_decoded(session_dir, bindings_path=…)` = `read_session`
      + `decode_can_rows`; falls back to plain `read_session` if the
      bindings file is absent.
    * `to_wide` gained `coerce_numeric=True` (default): after the pivot,
      any value column whose non-null entries are all numbers is cast to
      numeric dtype. This fixed a pre-existing `to_wide` bug — it always
      produced `object`-dtype columns (it pivots on the coalesced `value`
      column), so even OBD numeric channels were being skipped by
      `compute_features` / `ZScoreScaler`. Nothing exercised the
      `to_wide → resample → features` path on real data until now.
  End-to-end smoke against session 2026-05-11T222447: 2.2M long rows →
  1.15M wide rows × 55 cols → resampled 694 × 107 → features 694 × 266;
  all 23 decoded signals numeric and reaching the scaler; coolant decodes
  to 75-94degC; throttle-transient varies 0-255. (FR-04)
- [ ] **4.D.2 — DBC emitter from bindings.yaml.** Optional companion
  artifact: `tools/emit_dbc.py` produces a draft `data/dbc/xterra_2008.dbc`
  from `bindings.yaml` for SavvyCAN/Cabana interop and external sharing.
  One-way: bindings.yaml is source of truth, DBC is derived. (FR-02)
- [ ] **4.D.3 — Bearing-wear validation.** Score a session with known
  uneven wheel-speed behavior (synthetic if no real example exists);
  confirm anomaly fires before a DTC would. Requires wheel-speed
  bindings to graduate to `confirmed` confidence first (currently
  hypothesized: `WHEEL_SPEED_PAIR_A/B`). (Overview Phase 4 goal)
- [x] **4.D.4 — Bindings → captured-frame validation.** `tests/test_bindings_validation.py`
  — for every `confidence: confirmed` signal in `data/bindings.yaml`,
  walks `data/sessions/*/frames.log`, finds the first frame whose
  arbitration ID matches the binding, decodes it via the same
  `decode_raw` + `apply_scaling` path the daemon uses, and asserts the
  result is inside the declared `plausible_min`/`plausible_max` band.
  Skipped cleanly when no captured frames are available (CI without
  vehicle data). Plus a guard test (`test_at_least_one_confirmed_signal_observed`)
  that fails — rather than silently skips — if every confirmed signal's
  CAN ID has fallen out of the captured corpus, so stale bindings can't
  ride along undetected. Catches the bug class `decode_bindings.py`
  already found once (`ECM_COOLANT_TEMP` engine-off −40 °C sentinel),
  plus off-by-one `byte_start` errors and sign/endianness mistakes. No
  test fixture is committed — the test reads whatever sessions are on
  disk, per the "no committed fixtures" decision. (NFR-05)

### 4.E — Behavior-model inference (not done)

Captures the engine's installed-state expected behavior — fuel surface,
ignition surface, VE proxy, coolant warmup curve, idle stability, etc. —
from runtime observation rather than from ECU flash. These models then
feed anomaly detection (deviation features) and the dashboard's
performance panel (task 5.12). Approach, scope guardrails, and rationale
documented in `docs/behavior-model-inference.md`. Strictly read-only —
no Mode 23 / Mode 27 / flash dumps / writes.

- [x] **4.E.1 — Behavior-model schema.** `src/decode/behavior_models.py`
  — strict Pydantic schema (sibling file `data/behavior_models.yaml`,
  loader/dumper, `extra="forbid"` with `protected_namespaces=()`).
  Discriminated union: `surface_1d` carries an `Axis1D` (channel + unit +
  inclusive `domain`) and a `Fit1D` discriminated on `kind` —
  `PolynomialFit` (coefficients lowest-order first, numpy convention) or
  `LookupTableFit` (strictly-increasing `x_knots` + matching `y_knots`).
  `surface_2d` carries two `Axis2D`s (strictly-increasing `bin_edges`)
  and a `GridBinnedMeanFit` (row-major flat list of `GridCell{mean,std,count}`,
  enforced length = `axes[0].bin_count * axes[1].bin_count`). Optional
  `operating_mode` filter, `version`, `notes`. Shared `_BaseFit` bookkeeping
  (sample_count, r_squared, sessions, fitted_at ISO-8601, training_data_hash).
  `DeviationConfig` (absolute / relative bands; relative on 1D needs an
  explicit `reference_std`; warning < critical enforced). Helper:
  `flatten_grid(rows)` row-major flatten with rectangular-shape validation.
  Covered by `tests/test_behavior_models.py` (17 tests: axis validation,
  fit validation, deviation rules, 2D cell-count enforcement, YAML
  round-trip, unknown-field rejection, discriminator selection).
- [x] **4.E.2 — 1D surface inferer.** `src/analysis/surface_1d.py` —
  `fit_polynomial` (closed-form via `numpy.polynomial.polynomial.polyfit`
  — coefficients lowest-order first matching the schema; needs ≥ `degree+2`
  clean rows) and `fit_binned_lookup` (quantile bins → bin-mean lookup
  table — a piecewise-linear LOWESS substitute that doesn't pull scipy in;
  needs ≥ `n_knots*2` rows and ≥ 2 distinct x values). Both compute R² in
  the same residual-vs-mean form so persisted fit quality matches what the
  runtime evaluator sees, drop NaN rows pre-fit, and stamp a stable
  `training_data_hash` (sha256 of the (x,y) row tuples) so refit
  invalidation is detectable. `evaluate_1d(model, x)` returns the
  expected value or `None` outside the fit domain (no polynomial
  extrapolation — easy way to manufacture nonsense). Pure
  numpy/pandas; no scipy/sklearn dependency added. Covered by
  `tests/test_surface_1d.py` (13 tests). (FR-05)
- [x] **4.E.3 — 2D surface inferer.** `src/analysis/surface_2d.py` —
  `fit_grid_binned_mean` bins `(x, y)` into the configured grid and writes
  per-cell `{mean, std (ddof=0 to match `ZScoreScaler`), count}`. Empty
  cells are persisted with `count=0` so the evaluator can return `None`
  ("operating point never observed") instead of fabricating a value. NaN
  rows and out-of-range rows are dropped before binning; the top edge is
  closed so `x.max()` lands in the last bin. `r_squared` is left `None`
  (the empirical mean has no closed-form comparison). `evaluate_2d(model,
  x, y)` returns the full `GridCell` (not just `mean`) so the decoder
  layer can compute `|delta| / cell.std` for relative deviations. Pure
  numpy/pandas. Covered by `tests/test_surface_2d.py` (10 tests: grid
  layout, per-cell tracking of underlying relationship, empty-cell
  marking, NaN/out-of-range drops, edge-monotonicity, right-edge
  inclusiveness). (FR-05)
- [~] **4.E.4 — Injector pulse-width DID identification.** Cross-reference
  captured Mode 22 DIDs against Mode 01 fuel-trim variations. The DID
  whose value correlates with `RPM × (1 + STFT/100)` is the injector PW.
  Adds to `data/bindings.yaml`. Tooling is fully in place:
    * DID sweep — `tools/scan_constant_dids.py` (4.E.5);
    * Mode 22 ↔ Mode 01 pairing — `tools/cross_reference.py` (4.B.4);
    * **Live DID polling** — `scripts/record.py --obd-source canable --obd-dids 1101,F190 [--obd-did-header 7E0]`
      now issues Mode 22 requests over the Canable and writes `DID_<hex>`
      readings into `pids.jsonl` exactly like Mode 01 readings. The legacy
      ELM327 path still supports `--obd-port --obd-dids` for emulator/fallback
      coverage, but it is no longer required.
  Covered by `tests/test_can_obd_client.py`, `tests/test_can_obd_collector.py`,
  `tests/test_obd_collector.py`, and `tests/test_record_script.py`.
- [x] **4.E.5 — Calibration-DID scanner.** `tools/scan_constant_dids.py`
  + `src/analysis/did_scan.py` — sweeps Mode 22 DID space over the
  ELM327 (read-only, service 0x22 only; `Elm327Mode22Transport`), takes
  ≥2 passes at different engine states, and classifies each DID as
  varying / constant / intermittent (`classify_dids`). `parse_mode22_response`
  + the reply parser are unit-tested; the serial parsing layer still needs
  validation against the actual adapter at the vehicle. Verify the Canable
  is in its sniffing role only before running.
- [x] **4.E.6 — Behavior-model decoder integration.** `src/decode/behavior_decoder.py`
  exposes the runtime API. Scalar: `expected_value(model, operating_point)`
  → expected output or `None` (mode mismatch / missing input / outside
  domain / empty 2D cell); `deviation(model, actual, operating_point)`
  → `DeviationResult{expected, actual, deviation, severity}` graded against
  the model's `DeviationConfig` (`absolute` uses `|delta|`; `relative` uses
  `|delta|/std` where std is the 2D cell std or the 1D `reference_std`).
  Severity bands are inclusive lower-bounds (`score >= band`) so threshold
  semantics stay intuitive. Vectorized: `expected_value_column(frame,
  model)` returns a float series with NaN where the model has no
  expectation. Covered by `tests/test_behavior_decoder.py` (19 tests).
  (FR-05)
- [x] **4.E.7 — Deviation features in preprocessing.** `append_deviation_columns(frame, models)`
  writes a `deviation:<model_name>` column per behavior model;
  `build_feature_frame` (the shared offline pipeline used by both the IF
  trainer and the LSTM trainer) loads `data/behavior_models.yaml` when
  present and inserts this step between `segment_or_constant` and
  `compute_features`, so the deviation columns pick up rolling
  mean/std/roc derivatives just like raw/decoded channels and flow into
  `ZScoreScaler` via `trainable_feature_columns` unchanged. NaN is the
  correct value when the model has no expectation or the actual column
  isn't present — the existing scaler/feature stages already handle that.
  `output_columns` override lets a caller point at a non-`decoded:<output>`
  actual column (e.g. an OBD PID). Covered by `tests/test_behavior_decoder.py`.
  (FR-04)
- [x] **4.E.8 — Refit CLI.** `scripts/refit_behavior_models.py` +
  `src/analysis/refit.py` — declarative-recipe batch driver over the 1D
  / 2D inferers. The CLI pulls sessions discovered under `--sessions-root`
  filtered by `--days` (default 30; `--all` overrides), runs the shared
  `build_feature_frame` pipeline (so deviation columns from any
  *previously*-published behavior models flow through the feature stage
  too), and runs every recipe in `default_recipes()` — or the subset
  named via repeatable `--recipe`. Atomic write (temp file + rename) to
  `data/behavior_models.yaml`.

  Recipes are typed dataclasses (`Surface1DRecipe`, `Surface2DRecipe`)
  carrying everything the fitter needs plus a `min_samples` floor and an
  optional `operating_mode` pre-filter. `fit_recipe` never raises: missing
  columns, too-few samples, empty-after-mode-filter, and any internal
  fitter exception all return a `RecipeResult(skipped=True, reason=...)`,
  logged + reported in the CLI summary — partial coverage beats crash.

  `refit_all(recipes, frame, existing=...)` does the versioning bookkeeping:
  successfully refitted models get their `version` bumped one above any
  prior entry of the same name; skipped recipes *inherit* the prior model
  unchanged (a refit that lost data doesn't lose previously-known
  surfaces); models in `existing` not in the current recipe set are
  dropped (catalog edit is how surfaces retire). Recipe order is preserved
  in the output for stable diffs.

  Default catalog: `coolant_warmup` (1D binned-lookup over RUN_TIME →
  COOLANT — warmup is sigmoid-ish, polys overshoot), `idle_rpm_vs_coolant`
  (1D binned-lookup, mode=idle), `timing_advance_vs_rpm` (1D poly2),
  `timing_advance_vs_rpm_load` (2D grid over RPM × ENGINE_LOAD), and
  `fuel_trim_vs_rpm_load` (2D grid → STFT B1). All use standard Mode 01
  PIDs — no Mode 22 DIDs required, so all of these are fittable today as
  soon as a drive with the expanded `--obd-pids` set is captured. Fuel /
  AFR / VE surfaces from `docs/behavior-model-inference.md` wait on
  injector-PW DID identification (4.E.4) before joining the catalog.
  2D recipes use quantile-based auto-edges (`auto_bin_count` knots) by
  default; pass `x_edges` / `y_edges` for an explicit grid (matching a
  published OEM map, e.g.).

  Covered by `tests/test_refit.py` (18 tests: recipe success paths, every
  skip path, versioning, carry-over, retirement, recipe ordering) and
  `tests/test_refit_behavior_models_script.py` (10 tests: session-age
  filter, recipe selection / validation, dry-run, write-out, version-bump
  on repeat). Smoke-tested against a converted-from-candump session to
  confirm the CLI prints actionable skip reasons against real data.
  (FR-09)
- [x] **4.E.9 — Performance panel data feed.** `GET /models/behavior`
  on the dashboard backend returns the full `data/behavior_models.yaml`
  contents (Pydantic `model_dump(mode="json")` of `BehaviorModels`),
  re-read per request so a `scripts/refit_behavior_models.py` run lands
  without a daemon restart. Frontend wire-up: `useBehaviorModels` hook
  polls the endpoint at 60 s cadence; `BehaviorModelsPanel` renders each
  surface — 1D fits via the `Curve` component (canvas, samples the fit
  closed-form for polys, linearly interpolates for lookup tables) and 2D
  fits via the `Heatmap` component (canvas grid; empty cells render dark
  so coverage gaps are immediately visible). Each card shows name, version,
  output channel + unit, operating-mode filter, sample count, R²,
  and the surface's notes from the recipe. Covered by 4 new backend tests
  (`/models/behavior` endpoint + the underlying `read_behavior_models`
  helper, both with file-present and file-missing). (FR-08)

---

## Phase 5 — Dashboard, Alerts & Deployment (Overview Phase 5)

### 5A. Alert system (FR-07)

- [x] **5.1 — Anomaly event schema.** `src/alerts/events.py` — `AnomalyEvent`
  (Pydantic, `extra=forbid`): timestamp, source (`isolation_forest`|
  `lstm_autoencoder`), model_version, score, optional threshold,
  affected_channels, operating_mode, session, window timestamps, latency,
  free-form `detail`. Adapter constructors `from_lstm_result(...)` and
  `from_isolation_forest_row(...)` keep the per-detector mapping in one
  place. Covered by `tests/test_alerts.py`.
- [x] **5.2a — Inference → AnomalyEventLogger bridge.** `src/alerts/emitter.py` —
  `AnomalyEventEmitter` turns per-detector results into `AnomalyEvent`
  records via the existing `from_lstm_result` / `from_isolation_forest_row`
  adapters and persists them through `AnomalyEventLogger`. Owns
  emission policy (LSTM: `result.is_anomaly`; IF: label==-1 OR
  `isolation_forest_missing_features`), dedupe (per-(`source`,`model_version`)
  minimum-gap, default 1s, configurable to 0 for replay), and
  observable `EmitterCounters` (emitted / suppressed_not_anomaly /
  suppressed_dedupe — surfaceable from /status). Dropout events are
  tagged with `detail["missing_features"] = True` so the dashboard can
  render them differently from "real" outliers. Plus
  `IsolationForestInferenceEngine.observe(row)` — streaming wrapper
  around `TrainedIsolationForest.score` so the daemon row-processing
  loop is symmetric with `LSTMInferenceEngine.observe`. Pure wiring; no
  torch/sklearn imports at module top so the emitter is cheap to
  construct. Covered by `tests/test_alerts_emitter.py` (16 tests:
  LSTM emit/suppress/dedupe + window math; IF outlier / inlier /
  missing-features / dropout-with-NA-label; counter reset; bad
  timestamp; Z-suffix timestamps; engine end-to-end). (FR-07)
- [x] **5.2 — Event logger.** `src/alerts/event_logger.py` —
  `AnomalyEventLogger`: append-only, flushed per write (crash-safe), daily
  rotation via dated filenames (`anomalies-YYYY-MM-DD.jsonl` under the
  configured directory — a new file on the UTC date roll, no rename). Plus
  `read_events` / `read_events_dir` for the dashboard's `since=` query.
  Covered by `tests/test_alerts.py`.

### 5B. FastAPI backend (FR-08)

- [x] **5.3 — REST endpoints.** `src/dashboard/api.py` — read-only
  FastAPI app over the files the daemon already writes; no live producer
  wiring (WebSockets land in 5.4). Routes delegate to pure helpers so
  the same logic is unit-testable without spinning up the web stack.
    * `GET /status` → `build_status(config, models_dir=…)`: storage
      path + free MB + most recent session under `collection.storage_path`
      via `discover_sessions` + loaded model versions (best-effort,
      reads `<models_dir>/*.manifest.json`'s `version` field) + a
      "year make model" vehicle line.
    * `GET /config` → `echo_config(config)`: Pydantic
      `model_dump(mode="json")` of the loaded `canary.yaml`.
    * `GET /anomalies?since=…` → `read_anomalies_since(anomalies_dir,
      since=…)`: walks the `AnomalyEventLogger`'s
      `anomalies-YYYY-MM-DD.jsonl` files (5.2) sorted by timestamp;
      ISO-8601 `since` is filterable by lexicographic compare (events
      persist as ISO too); garbage `since` → HTTP 400.
    * `GET /telemetry/latest?n=…` → `read_latest_telemetry_rows(storage_path,
      n)`: last `n` rows from the newest Parquet chunk in the newest
      session; `(value_number, value_text, value_bool)` collapsed into
      a single `value` field; `n` bounded [1, 10_000] (FastAPI 422 on
      out-of-range).
  Factory `create_app(config, *, anomalies_dir, models_dir, ..., emitter)`
  stays stateless — every request re-reads from disk (cheap for v1;
  `since=` bounds the work as the corpus grows). When the daemon passes
  its `AnomalyEventEmitter`, `/status` surfaces `emitter_counters`
  (`{emitted, suppressed_not_anomaly, suppressed_dedupe}`) — the
  "is the anomaly path firing?" check the dashboard can render without
  polling the JSONL. Bind-host policy (5.5) belongs at the uvicorn entry
  point. Added `httpx>=0.27` to dev deps for the FastAPI TestClient.
  Covered by `tests/test_dashboard_api.py` (22 tests: unit helpers + full
  route surface with `TestClient` + counters round-trip).
- [x] **5.4 — WebSocket stream.** `src/dashboard/broadcast.py` (in-memory
  pub/sub) + `/ws/telemetry` and `/ws/anomalies` routes in
  `src/dashboard/api.py`. `Broadcaster(queue_size=100)` — fan-out to N
  bounded per-subscriber queues; if a subscriber falls behind the queue
  fills and we drop the *oldest* (freshest data wins, producer never
  blocks). `Subscription.dropped` is observable per subscriber. Async-only
  `publish()` plus a `publish_threadsafe(loop, ...)` escape hatch for the
  OBD/CAN supervisor threads (1.E.1) to publish from off-loop without
  racing the queue. The FastAPI handler accepts the WebSocket, subscribes,
  pumps each message as JSON until `WebSocketDisconnect`, then
  unsubscribes in `finally` — slow clients can't leak subscriber slots.
  `create_app(..., telemetry_broadcaster=, anomalies_broadcaster=)` lets
  the daemon inject the same instances its producers publish into;
  defaults to fresh per-app instances stashed on `app.state` so tests
  / the bare-app case still work. Covered by `tests/test_dashboard_broadcast.py`
  (10 tests: delivery, no-sub no-op, unsubscribe, drop-oldest backpressure,
  dropped counter, threadsafe, async-for iterator, count tracking,
  idempotent unsubscribe, slow-subscriber isolation) plus 4
  `tests/test_dashboard_api.py` WebSocket integration tests using
  `TestClient.websocket_connect`.
- [x] **5.5 — Bind-host entry point.** `scripts/serve_dashboard.py` is
  the uvicorn entry the systemd unit will run. `--host` / `--port` flags
  default to `canary.yaml`'s `dashboard.{host,port}`; `--anomalies-dir`
  and `--models-dir` configure where `create_app` reads the alerts log
  and model manifests from. `warn_if_public_bind(host)` logs a loud
  warning when binding to anything other than loopback / link-local /
  RFC1918 private (and conservatively warns on hostnames the script
  can't classify without a DNS roundtrip) — v1 has no auth on the
  dashboard, so the warning is the cheap guard. Covered by
  `tests/test_serve_dashboard_script.py` (9 tests: silent for loopback /
  RFC1918 / link-local; loud for 0.0.0.0 / public IPs / hostnames;
  CLI defaults from canary.yaml; CLI override; --anomalies-dir /
  --models-dir pass-through to create_app). (NFR-06)

### 5C. Dashboard frontend (FR-08)

- [x] **5.6 — Display strategy.** `docs/dashboard-display-strategy.md`
  — full React/TypeScript PWA served by the Pi for phone/head-unit
  mirroring; native Android Auto deferred as a constrained companion.
- [x] **5.7 — Stack: React + TypeScript + Vite.** Hand-scaffolded under
  `src/dashboard/static/` (no `pnpm create vite` — every dep we pull is
  declared in `package.json`). pnpm is the package manager (declared via
  `packageManager` field for corepack); lockfile is `pnpm-lock.yaml`.
  `tsconfig.app.json` is strict (noUnusedLocals, noUnusedParameters,
  noFallthroughCasesInSwitch). Build via `pnpm build` → `dist/`
  (155 KB JS, 5.8 KB CSS, 50 KB gzipped); the FastAPI backend auto-mounts
  `dist/` on `/` when present. `src/dashboard/static/README.md` documents
  build/dev/typecheck flow.
- [x] **5.8 — PWA + head-unit mirroring mode.** `public/manifest.webmanifest`
  declares fullscreen + landscape + theme color; iOS web-app meta tags in
  `index.html`. Landscape-first CSS grid layout with a portrait fallback;
  dark high-contrast palette tuned for daylight visibility. `DashboardShell`
  shows connection state per stream (two colored dots: telemetry +
  anomalies), a current-mode badge, and a `→ parked` / `→ driving` toggle
  that gates the `DiagnosticsPanel`. The `SystemPanel` surfaces a stale-data
  warning (newest channel timestamp > 10 s ago).
- [x] **5.9 — Live PID panel.** `panels/TelemetryPanel.tsx` +
  `hooks/useTelemetryStream.ts`. The hook seeds from `GET /telemetry/latest`
  so values render immediately, then subscribes to `/ws/telemetry` and
  maintains per-channel ring buffers (120 samples ≈ 2 min at 1 Hz). Panel
  renders a primary grid (RPM, SPEED, COOLANT_TEMP, MAF, THROTTLE_POS,
  voltage) with canvas-based sparklines (`Sparkline.tsx`, no chart-lib
  dep), and a collapsible secondary grid for everything else the
  collectors produce.
- [x] **5.10 — Anomaly timeline.** `panels/AnomalyPanel.tsx` +
  `hooks/useAnomalyStream.ts`. Seeds from `GET /anomalies` (last day's
  JSONL), subscribes to `/ws/anomalies`, dedupes by
  `(timestamp, source, model_version)`. Renders a score sparkline (last 60
  events) + a filterable event list with per-event timestamp, source
  badge (colored per detector), operating mode, score/threshold,
  affected-channel preview, and a "missing features" badge for dropout
  events.
- [x] **5.11 — Operating-mode indicator.** `panels/OperatingModeIndicator.tsx`
  reads the most recent anomaly event's `operating_mode` field as the
  freshest cue the daemon publishes (avoids needing a second producer
  for live mode). Colored by mode (idle = green, city/highway = accent,
  WOT = warn).
- [x] **5.12 — Model performance panel.** `panels/ModelPanel.tsx` reads
  `/status` for `model_versions` (live values from ModelManager) + the new
  `emitter_counters` (emitted / suppressed_not_anomaly / suppressed_dedupe);
  derives mean LSTM inference latency from the rolling event stream's
  `latency_ms` field. Behavior-model deviation heatmaps (4.E.9) will land
  here next.
- [x] **5.13 — TPMS panel (placeholder).** `panels/TpmsPanel.tsx` reads
  `decoded:TPMS_FL_PRESSURE` etc. from the channel map; when those
  bindings haven't been pinned (current state), surfaces "TPMS bindings
  not yet pinned — see `data/bindings.yaml`" instead of showing fake
  zeros. Once the bindings land, the panel takes over with no
  shell-level change.
- [x] **5.14 — Parked diagnostics panel (gate).** `panels/DiagnosticsPanel.tsx`
  is the parked-mode-only surface for proprietary protocol reads + any
  future control actions. Driving mode hides the panel entirely (not
  "shows it disabled") so a mis-tap can't land here. The frontend gate
  is for the operator's ergonomics; the backend must enforce parked
  mode separately when controls are added (per
  `docs/dashboard-display-strategy.md`'s safety boundary). The panel is
  currently a stub that documents the rule — the actual tools land as
  they're built.

Backend wire-up for the SPA: `create_app(..., static_dir=, enable_dev_cors=)`
mounts the built React bundle on `/` (404s cleanly until `pnpm build`
runs — API routes still work without the SPA) and optionally enables CORS
for `http://localhost:5173` so `pnpm dev` can talk to the backend
directly. `scripts/serve_dashboard.py` gets matching `--static-dir` and
`--dev-cors` flags. `tests/test_dashboard_api.py` covers static-mount
present / absent and the CORS dev-origin allow.

### 5D. Model management (FR-09)

- [x] **5.15 — Shared model manifest schema.** `src/models/manifest.py`
  — Pydantic `ModelManifestCommon` (strict, `extra="forbid"` with
  `protected_namespaces=()`) listing every field both
  `IsolationForestManifest` and `LSTMAutoencoderManifest` are
  contractually obliged to carry: `artifact_format_version`,
  `model_type` ∈ `KNOWN_MODEL_TYPES`, `model_version`, `created_at`,
  `training_data_hash`, training timestamps + sessions, `input_rows`,
  `hyperparams`, `schema` (aliased to `schema_` since Pydantic reserves
  the name). Detector-specific keys (`modes` for the IF,
  `thresholds`/`architecture` for the LSTM) flow through `extras` so
  the schema is forward-compatible: per-trainer manifests can grow new
  fields without bumping `CURRENT_MANIFEST_FORMAT_VERSION` (only
  consumers that *use* those new fields opt in). `validate_manifest_common`
  / `load_manifest_common` are the single entry the dashboard
  (`build_status` reads `version` from the same manifests), eventual
  hot-swap (5.16), and retraining CLI (5.17) all consume through.
  `schemas_compatible(a, b)` checks `feature_columns` match for the
  hot-swap "is the new model substitutable for the running one?"
  decision. `ManifestValidationError` is the single exception type
  callers catch. A conformance test in `tests/test_model_manifest.py`
  pins the trainer dataclasses to the shared field names so a future
  refactor that drops a shared field fails loudly. `KNOWN_MODEL_TYPES`
  is asserted equal to `AnomalyEvent.AnomalySource` so dashboard payloads
  round-trip cleanly. 18 tests; full suite 711 passed, 1 skipped;
  ruff/format/mypy clean (55 source files). (FR-09)
- [x] **5.16 — Hot swap (`ModelManager`).** `src/models/manager.py` —
  generic `ModelManager[T]` polls an artifact path, validates a newer
  sidecar manifest against the running one via `schemas_compatible`
  (5.15), and atomically swaps the in-memory reference. Two safety
  policies: (1) **manifest validation first** — a new manifest is
  rejected if it doesn't satisfy `ModelManifestCommon`, has a mismatched
  `feature_columns`, has an unexpected `model_type` (when
  `allowed_model_types` is set), or if the loader raises; the running
  model stays in place. (2) **lock-guarded load+validate+assign** —
  two pollers can't race a half-loaded checkpoint into place. mtime
  short-circuit avoids re-validating unchanged manifests on every tick.
  `current` returns a frozen `LoadedModel[T]` snapshot; the prior
  snapshot is unaffected by a swap. `SwapAttempt(kind, detail,
  new_version)` is the observable per-poll outcome — the daemon's
  /status path will surface counts of each kind. Polling rather than
  fsnotify (no extra dep; refit cadence is minutes-to-hours so polling
  cost is irrelevant). `manager_for_artifact(...)` convenience constructor
  loads the initial artifact in one call. Covered by `tests/test_model_manager.py`
  (12 tests: load_initial / missing files / no-change / swap / feature_columns
  mismatch / model_type mismatch / malformed manifest / loader exception /
  parallel-poller lock / snapshot immutability / typed convenience).
  (FR-09)
- [x] **5.17 — Retraining CLI.** `scripts/retrain.py` orchestrates
  `train_anomaly_model.train` and `train_lstm.train` over the same
  age-filtered session set in one process. Each trainer writes its own
  artifact + manifest under `--if-out` / `--lstm-out`; the daemon's
  `ModelManager` (5.16) picks them up on next poll. `--skip-if` /
  `--skip-lstm` for partial refreshes; `--days N` / `--all` mirror
  `refit_behavior_models.py`. Continues if one trainer fails (logged +
  reported in the stdout summary with `✗`); exit code non-zero in that
  case. Sessions filter reuses `_filter_sessions_by_age` from the
  refit CLI so both trainers see the same window-of-recent semantics.
  Covered by `tests/test_retrain_script.py` (6 tests: both trainers
  fire, --skip-if, --skip-lstm, both-skip rejected, no-sessions error,
  one-failure-doesn't-stop-the-other). (FR-09)

### 5E. Deployment (NFR-02, NFR-04)

- [ ] **5.18 — systemd unit.** `systemd/canary.service` — Type=notify,
  Restart=on-failure, runs as a non-root `canary` user, reads config
  from `/etc/canary/canary.yaml`, writes to `/data/canary`.
- [ ] **5.19 — Install script.** `scripts/install.sh` — creates user,
  installs venv, copies unit file, enables service. Idempotent.
- [ ] **5.20 — Pi 5 image bring-up doc.** `docs/pi-setup.md` — Bookworm
  install, SocketCAN modules, USB device permissions, fstab for the SSD.
- [ ] **5.21 — Vehicle install.** Mount Pi, run buck converter from ACC,
  verify clean shutdown on ignition off. (Hardware task; software side
  is graceful-shutdown handling.)

---

## Cross-Cutting Concerns

These get touched in multiple phases — keep them in mind, don't defer to the end.

- **Configuration drift.** Every new tunable goes through `canary.yaml` +
  the Pydantic model + `canary.example.yaml`. No magic constants in code.
  (NFR-04)
- **Time semantics.** All timestamps are UTC `datetime` with timezone;
  Parquet stores them as `timestamp[us, UTC]`. Decide once, enforce in
  the writer. Avoids the "is this local or UTC?" debugging tax later.
- **Schema evolution.** When a new PID or CAN signal is added, downstream
  pipelines must not crash on older Parquet files missing that column.
  Loader fills missing columns with NaN.
- **Test data fixtures.** Commit a small (~1 MB) anonymized Parquet
  session under `tests/fixtures/`; most preprocessing/model tests should
  use it instead of regenerating synthetic data each run.
- **CPU budget.** Profile after each phase; NFR-01 says <50% Pi 5 CPU
  steady-state. LSTM inference is the likely pinch point — measure
  before optimizing.

---

## Risks & Open Questions

| # | Risk / Question | Mitigation / When to resolve |
|---|---|---|
| R1 | Active Canable-native OBD polling and passive broadcast sniffing share one adapter/bus. | `can_obd_collector.py` owns one Bus and multiplexes both paths; keep request rates bounded and validate with long captures before deployment. |
| R2 | Nissan Consult III RE is open-ended; could slip Phase 4 indefinitely. | Phase 4 is parallel and additive — Phases 1–3 don't depend on it. Ship without it if needed. |
| R3 | LSTM may be too heavy for the Pi 5 at the target window length. | Benchmark in 3.5; fall back to smaller `latent_dim` or shorter window. CNN-LSTM is explicitly Phase 3+ in overview. |
| R4 | Per-mode IF models could overfit sparse modes (off-road, WOT). | Pool with global model when per-mode sample count is below a threshold. Decide in 2.8. |
| R5 | Dashboard auth deferred to v2 — anyone on the LAN can see live data. | Document in 5.5; revisit once a hotspot/AP design is chosen. |
| R6 | No remote access path for alerts when phone is off the vehicle's LAN. | Out of scope for v1; "optional push notification" in FR-07 is a v2 feature. |

---

## Done Definition (v1)

v1 ships when, on the Pi 5 in the Xterra:

1. systemd-managed service starts on boot, reconnects on adapter drop.
2. OBD + CAN data continuously logged to Parquet for ≥1 week without
   manual intervention.
3. Both Isolation Forest and LSTM Autoencoder are running inference live.
4. Synthetic anomaly injected via replay produces a dashboard alert
   within 5 s. (NFR-01)
5. Dashboard reachable from a phone on the vehicle's hotspot.
6. False-positive rate on a held-out clean drive ≤ 1 alert / hour.

CAN-decoded signals (Phase 4) are a stretch goal for v1 and a hard
requirement for v1.1.
