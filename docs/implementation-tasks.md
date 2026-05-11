# CANary — Implementation Plan

Derived from `docs/CANary-overview.md`. Tasks are grouped by phase, with FR/NFR
references in parentheses. Each task is sized to be roughly one PR or one
working session. Items inside a phase are listed in execution order; cross-phase
dependencies are called out explicitly.

Status legend: `[ ]` not started · `[~]` in progress · `[x]` done.

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
  ELM327-emulator deferred to a future `[emulator]` extra (Phase 1.5) —
  its sdist needs `--no-build-isolation`, not worth solving until we
  actually wire the emulator test.
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
- [ ] **1.4 — Read-only enforcement.** Wrap the OBD interface so that only
  query commands are issuable; assert no `Mode 04`/write paths reachable.
  Unit-test that an attempted write raises. (NFR-06)
- [ ] **1.5 — Unit tests with emulator.** `tests/test_obd_collector.py` —
  spin up `ELM327-emulator`, run collector for N seconds, assert expected
  PIDs appeared and unsupported PIDs produced warnings, not crashes.
  (NFR-05)

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
- [ ] **1.8 — DBC decode hook.** Optional `cantools` decoder; if a DBC path
  is configured, decode known frames into named-channel records and pass
  through unknown frames as raw. DBC is empty in Phase 1 — wire the hook,
  defer signal definitions to Phase 4. (FR-02)
  Note: the project's primary signal-decode path is now `bindings.yaml` +
  `src/decode/bindings.py` (see Phase 4.B). A DBC hook is still worth
  having for SavvyCAN/Cabana interop; defer with a follow-up task.
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
- [ ] **1.11 — Session keying.** Detect new ignition by Engine Run Time
  reset (PID 0x1F decreases or starts near 0). New session ⇒ rotate file.
  Current behavior: sessions are time-bucketed by daemon startup, not by
  ignition. (FR-03)
- [x] **1.12 — Ring buffer.** Background sweeper deletes session files
  older than `session_retention_days`. Configurable, default 30.
  Implemented as `delete_expired_sessions` and invoked from
  `run_collection` on shutdown. (FR-03)
- [x] **1.13 — Disk-space monitor.** Poll free bytes on `storage_path`;
  emit warning event when below `storage_warning_mb`. `src/storage/monitor.py`
  with `free_space_mb` + `warn_if_storage_low`. Dashboard surfacing
  deferred until Phase 5. (NFR-03)
- [ ] **1.14 — Crash-safety test.** Kill the writer mid-flush; on restart,
  verify no corrupt files (Parquet footer present) and that data prior to
  the last flush is intact. (NFR-02)

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
  Status: 4 sessions captured (two parked dev sessions, the rev protocol
  session 2026-05-11T152858, and the partial drive 2026-05-11T154648).
  Each documented under `data/analyses/<session>.md`; no consolidated
  `data/sessions.md` index yet. Blocked on more drive time; the
  slcan-tolerance fix (commit cf4bca0) unblocks long captures.

---

### 1E. Operator capture tooling

Tooling for recording / tagging sessions outside the daemon path — used
for RE captures and for collecting Phase 2 training data before the
daemon is deployed on the vehicle.

- [x] **1.E.1 — `scripts/record.py`.** CLI wrapper around the CAN
  collector: writes `frames.log` (candump format) + `manifest.json`
  per session, supports `--vehicle-state`, `--seconds`, and parallel
  OBD-II `--obd-port` capture via `pids.jsonl` sidecar.
- [x] **1.E.2 — `scripts/tag.py`.** Append-only tag CLI; writes one
  JSON record per Enter-press into `<session>/tags.jsonl`. Auto-finds
  the most recent session under `data/sessions/`.
- [x] **1.E.3 — `scripts/guided_session.py`.** Interactive protocol
  runner — walks the user through a labeled sequence (currently the
  rev protocol), one Enter press per tag. Designed for hands-busy
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
- [ ] **3.2 — Model.** `src/models/lstm_autoencoder.py` — encoder LSTM →
  latent (`latent_dim`) → decoder LSTM. Configurable depth/hidden size.
- [ ] **3.3 — Training loop.** Train on clean sessions only; MAE loss;
  early stop on validation loss; checkpoint best weights; save manifest
  (FR-06, FR-09).
- [ ] **3.4 — Threshold calibration.** Compute per-channel and global
  reconstruction-error distributions on the training set; set threshold
  to `μ + k·σ` with configurable `k` (default 3). Persist in manifest.
  (FR-06)
- [ ] **3.5 — Real-time inference loop.** `src/models/inference.py` —
  consume a sliding window from the live preprocessing buffer, run a
  forward pass, emit anomaly score. Run on CPU; benchmark and confirm
  <50% Pi 5 CPU at 1 Hz. (FR-06, NFR-01)
- [ ] **3.6 — False-positive benchmark.** Held-out clean sessions: report
  FPR at the calibrated threshold; iterate on `k` if needed. (FR-06)
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

- [~] **4.1 — RE workflow doc.** `docs/can-re-workflow.md` — Z1 + Canable
  capture → physical-action-tagged sessions → byte-vs-tag differential →
  binding hypothesis → ELM327 cross-reference to pin scaling. Reference,
  not code. Status: the methodology is captured implicitly across
  `data/analyses/*.md` and bindings evidence; no consolidated workflow
  doc yet. Worth writing once the ELM327 closes one full hypothesis loop.
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
  scale/offset. **Blocked on ELM327 arrival.**
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
- [ ] **4.D.4 — Bindings → DBC cross-reference test.** `tests/test_dbc.py`
  equivalent — for each `confirmed` signal in bindings.yaml, decode a
  representative captured frame, assert decoded value matches the
  recorded plausible range. (NFR-05)

### 4.E — Behavior-model inference (not done)

Captures the engine's installed-state expected behavior — fuel surface,
ignition surface, VE proxy, coolant warmup curve, idle stability, etc. —
from runtime observation rather than from ECU flash. These models then
feed anomaly detection (deviation features) and the dashboard's
performance panel (task 5.12). Approach, scope guardrails, and rationale
documented in `docs/behavior-model-inference.md`. Strictly read-only —
no Mode 23 / Mode 27 / flash dumps / writes.

- [ ] **4.E.1 — Behavior-model schema.** Decide YAML structure (sibling
  to `bindings.yaml` vs. embedded). Design the Pydantic model in
  `src/decode/behavior_models.py`. Versioning, fit metadata, axis
  definitions, plausible-deviation bounds.
- [ ] **4.E.2 — 1D surface inferer.** `src/analysis/surface_1d.py` —
  coolant warmup curves and idle-stability curves. Polynomial / LOWESS
  + goodness-of-fit metric. Generalizes `cross_reference.py`'s linear
  regression. (FR-05)
- [ ] **4.E.3 — 2D surface inferer.** `src/analysis/surface_2d.py` —
  grid-binned mean per (axis A bin × axis B bin) for fuel, AFR, VE,
  and ignition surfaces. Per-operating-mode fits. Records R² and
  sample count per cell so sparse coverage is visible. (FR-05)
- [ ] **4.E.4 — Injector pulse-width DID identification.** Cross-reference
  captured Mode 22 DIDs against Mode 01 fuel-trim variations. The DID
  whose value correlates with `RPM × (1 + STFT/100)` is the injector PW.
  Adds to `data/bindings.yaml`.
- [ ] **4.E.5 — Calibration-DID scanner.** `tools/scan_constant_dids.py`
  sweeps Mode 22 DID space, splits runtime-varying from constants,
  prints the constants list. Verify Canable application is read-only
  before running.
- [ ] **4.E.6 — Behavior-model decoder integration.** Extend `src/decode/`
  to load behavior models at startup and expose
  `expected_value(operating_point)` + `deviation(actual, operating_point)`
  for the runtime decode path. (FR-05)
- [ ] **4.E.7 — Deviation features in preprocessing.** Wire the decoder's
  expected/deviation outputs into `loader.read_session`'s output so
  existing `compute_features` and `ZScoreScaler` see them as ordinary
  numeric columns. No changes needed to those modules. (FR-04)
- [ ] **4.E.8 — Refit CLI.** `scripts/refit_behavior_models.py` — pulls
  clean sessions from the last N days, refits all surfaces, writes
  versioned model artifacts. Manual trigger in v1; cron later. (FR-09)
- [ ] **4.E.9 — Performance panel data feed.** Backend endpoints
  surfacing current model versions, deviation heatmaps, warmup curves
  vs. baseline. Consumed by dashboard task 5.12. (FR-08)

---

## Phase 5 — Dashboard, Alerts & Deployment (Overview Phase 5)

### 5A. Alert system (FR-07)

- [ ] **5.1 — Anomaly event schema.** `src/alerts/events.py` — Pydantic
  model with timestamp, source (`isolation_forest`|`lstm`),
  affected_channels, anomaly_score, operating_mode, model_version.
- [ ] **5.2 — Event logger.** `src/alerts/event_logger.py` — append-only
  JSONL file at `storage_path/anomalies.jsonl`. Rotated daily.

### 5B. FastAPI backend (FR-08)

- [ ] **5.3 — REST endpoints.** `GET /telemetry/latest`,
  `GET /anomalies?since=...`, `GET /status` (health, storage, model
  version), `GET /config` (read-only echo).
- [ ] **5.4 — WebSocket stream.** `/ws/telemetry` pushes live PID values;
  `/ws/anomalies` pushes anomaly events as they fire.
- [ ] **5.5 — Bind to LAN only by default.** Honor `host`/`port` from
  config; default `0.0.0.0:8000` but document that the Pi should be
  firewalled / hotspot-only. No auth in v1; document that. (NFR-06)

### 5C. Dashboard frontend (FR-08)

- [ ] **5.6 — Display strategy.** Adopt the approach in
  `docs/dashboard-display-strategy.md`: full React/TypeScript PWA served
  by the Pi for phone/head-unit mirroring; optional native Android Auto
  companion later for constrained status/alert views only.
- [ ] **5.7 — Stack decision.** Use React + TypeScript + Vite unless a
  proof-of-concept shows a simpler stack wins. Document build/deploy
  details in `src/dashboard/static/README.md`. Single-page app, built
  artifacts checked in or built at deploy.
- [ ] **5.8 — PWA + head-unit mirroring mode.** Fullscreen landscape UI
  sized for a phone mirrored to the Kenwood receiver. Show connection
  state, stale-data warnings, and a parked/maintenance-mode gate for any
  future diagnostic controls.
- [ ] **5.9 — Live PID panel.** Subscribe to `/ws/telemetry`, render a
  grid of current values + sparklines for last 60 s.
- [ ] **5.10 — Anomaly timeline.** Anomaly score chart; recent-events list
  with filter by source and mode.
- [ ] **5.11 — Operating-mode indicator.** Current mode badge + recent
  transitions.
- [ ] **5.12 — Model performance panel.** Show active model version,
  inference latency, thresholds, anomaly score distribution, and LSTM
  reconstruction error once the model layer exists.
- [ ] **5.13 — TPMS panel.** Once TPMS signals are decoded, show per-tire
  pressure/temperature/status and stale sensor warnings.
- [ ] **5.14 — Parked diagnostics panel.** Maintenance-only UI for
  reverse-engineered proprietary protocol reads and any future control
  actions. Requires explicit confirmations and backend enforcement.

### 5D. Model management (FR-09)

- [ ] **5.15 — Model manifest format.** YAML alongside each artifact:
  version, training data hash + date range, hyperparams, scaler params,
  threshold, schema. (FR-09)
- [ ] **5.16 — Hot swap.** `ModelManager` watches the models directory;
  on a new manifest, validate schema match and atomically swap the
  in-memory model without restarting the process. (FR-09)
- [ ] **5.17 — Retraining CLI.** `scripts/retrain.py` — pulls clean
  sessions from the last N days, retrains both models, writes new
  artifacts + manifests. Manual trigger in v1; cron later. (FR-09)

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
| R1 | ELM327 + Canable on the same OBD-II port may conflict if Canable isn't truly listen-only. | Validate listen-only with `candump` showing no TX before connecting ELM327. Phase 1. |
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
