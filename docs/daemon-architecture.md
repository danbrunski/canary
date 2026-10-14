# CANary daemon architecture

How the long-running daemon is wired together — producer threads, the
asyncio queue, fan-out to storage + dashboard + inference, hot-swap, and
the end-to-end flow that turns vehicle telemetry into alerts. The
companion to `docs/CANary-overview.md`'s "Project Structure" section, this
focuses on the *runtime assembly*: which module produces what, who
consumes it, and where the seams are.

If you're new to the codebase and want to know how to wire a new
producer, a new sink, or a new inference task, start here.

## The big diagram

```
                                                ┌────────────────────────┐
                                                │ scripts/retrain.py     │
                                                │ scripts/refit_*.py     │
                                                │ (manual, periodic)     │
                                                └──────────┬─────────────┘
                                                           │ writes
                                                           ▼
                                                ┌────────────────────────┐
                                                │ models/                │
                                                │   isolation_forest/    │
                                                │     model.joblib +     │
                                                │     manifest.json      │
                                                │   lstm_autoencoder/    │
                                                │     model.pt + …       │
                                                │ data/behavior_models.  │
                                                │   yaml                 │
                                                └──────────┬─────────────┘
                                                           │ polls (30s)
                                                           ▼
┌──────────────────┐                            ┌────────────────────────┐
│ vehicle bus +    │                            │ ModelManager (5.16)    │
│ CAN/OBD adapter  │                            │   - mtime short-circ.  │
└────────┬─────────┘                            │   - validate manifest  │
         │                                      │   - schemas_compatible │
         │ python-can (python-obd legacy)       │   - load + atomic swap │
         ▼                                      └──────────┬─────────────┘
┌──────────────────┐                                       │ current
│ collectors (1A,  │                                       │
│ 1B): collect_can,│   ┌─────────────────────────┐         │
│ collect_obd      │──▶│ asyncio.Queue           │         │
└──────────────────┘   │ (size=10k by default)   │         │
                       └────────────┬────────────┘         │
                                    │                      │
                                    ▼                      │
                       ┌────────────────────────┐          │
                       │ drain_queue_with_      │          │
                       │   broadcast (daemon)   │          │
                       └────┬───────┬───────┬───┘          │
                            │       │       │              │
                            ▼       ▼       ▼              │
              Parquet writer   tele_bus   live_source      │
              (1.10/1.11)      Broadcaster .observe()      │
                  │                │           │           │
                  │                │           │           │
                  ▼                ▼           ▼           │
              session/ dir     /ws/telemetry  LiveFeatureSource (4.D.1 / B)
              telemetry-N.       (WebSocket)      tick() every 1/resample_hz
              parquet                                  │
                                                       │ bounded snapshot
                                                       │
                                          ▼
                  run_live_inference  (TickScheduler: deadlines start + k/hz)
                    LiveInferencePipeline
                      prepare  [loop]   gaps, ring append
                      feature  [process] segment + dev + rolling features
                      snapshot [loop]   apply row, gate, behavior residuals
                      compute  [worker] IsolationForestInferenceEngine.observe
                                        SequenceInferenceEngine.observe
                      apply    [loop]   generation check, emits, fusion
                                          │ AnomalyEvent
                                          ▼
                                AnomalyEventEmitter (5.2a)
                                  - dedupe per (source, version)
                                  - EmitterCounters → /status
                                          │
                                          ▼
                          _BroadcastingLogger
                            ├──→ AnomalyEventLogger (5.2)
                            │      data/anomalies/
                            │        anomalies-YYYY-MM-DD.jsonl
                            │      ──→ /anomalies?since=
                            │
                            └──→ anomalies_bus Broadcaster
                                   ──→ /ws/anomalies (WebSocket)
```

## What lives where

| Layer | Module | Role |
|---|---|---|
| **Producers** | `src/collection/can_obd_collector.py`, `src/collection/can_collector.py`, `src/collection/obd_collector.py` | Push `TelemetryRecord` onto an `asyncio.Queue`. Default current hardware path is `can_obd_collector.py`: one SocketCAN bus (Waveshare MCP2515 HAT or Canable USB) handles broadcast CAN plus active OBD Mode 01 / selected Mode 22 polling. `can_collector.py` is CAN-only; `obd_collector.py` is legacy ELM327/emulator fallback. |
| **Storage** | `src/storage/parquet_writer.py` | Buffers records and flushes to dated session dirs. 1.11 session keying rotates on Engine Run Time reset; the `run_retention_sweeper` task runs periodically inside the production daemon (cadence `daemon.retention_sweep_seconds`), and on every tick — always protecting the live `.paths.root` plus pinned sessions — applies in order: an age sweep (`session_retention_days`), a byte-cap ring buffer (`collection.storage_max_mb`, oldest-first), and an emergency low-space eviction when filesystem free space falls below `collection.storage_warning_mb`. It is a periodic in-process sweep, **not** a shutdown-only pass. `warn_if_storage_low` stays the cheap log-only hot-path check; the acting logic lives only in the sweeper (see Phase 8 / `docs/storage-lifecycle-fix.md`); 1.14 atomic-write contract: `.tmp → rename` so a crash mid-flush never leaves a corrupt `.parquet` visible to the discovery glob. |
| **Live preprocessing** | `src/features/live.py` | `LiveFeatureSource` is the row-by-row analog of `build_feature_frame`. `observe(record)` updates loop-owned current values; `prepare_tick(now)` appends a bounded snapshot, the pure shared transform runs in a persistent spawned process, and `apply_built_tick` updates the operating point and trip integrals on the loop. `tick(now)` composes the same three steps synchronously for tests/debugging. |
| **Inference** | `src/models/inference.py` + `src/alerts/emitter.py` | `SequenceInferenceEngine` (windowed LSTM/TCN) + `IsolationForestInferenceEngine` (row-by-row wrapper around `TrainedIsolationForest.score`). `AnomalyEventEmitter` decides when a result is an event (`is_anomaly` / label==-1), dedupes per (source, model_version), counts `emitted` / `suppressed_*` for `/status`. |
| **Inference scheduling** | `src/daemon/inference.py` (`LiveInferencePipeline`), `src/daemon/inference_scheduling.py`, `src/daemon/inference_metrics.py` | C08/D01.4: absolute monotonic deadlines prevent cadence drift. Loop-owned feature preparation/apply surrounds a pure pandas transform in one spawned process; IF + sequence scoring uses its separate bounded worker. A depth-one newest-wins queue overlaps the next snapshot with current scoring. Generation checks protect model/replay transitions; stage timing distinguishes `feature_wait` from `loop_occupancy` on `/status`. See `docs/inference-scheduling.md`. |
| **Persistence + push** | `src/alerts/event_logger.py` + `src/daemon/broadcast.py` | `AnomalyEventLogger` writes `anomalies-YYYY-MM-DD.jsonl` (daily, flushed-per-write). `Broadcaster` is the in-memory pub/sub fan-out with bounded per-subscriber queues — slow consumers drop oldest, producers never block. The `_BroadcastingLogger` wrapper writes both at one call site. |
| **Hot-swap** | `src/models/manager.py` | `ModelManager` polls the artifact path; on a newer manifest, validates `schemas_compatible` against the running model and atomically swaps. Per-poll `SwapAttempt(kind=swapped\|no_change\|rejected\|missing)` is the observable outcome. |
| **Dashboard + control API** | `src/dashboard/api.py` + `src/dashboard/routes/{public,control,streams}.py`, `src/control/*` | FastAPI app: `/status`, `/config`, `/anomalies?since=`, `/telemetry/latest?n=`, `/ws/telemetry`, `/ws/anomalies`, plus authenticated `/control/*` endpoints for capture windows, tags, session export, and allow-listed background jobs. `create_app(...)` is mostly stateless for read endpoints; the daemon injects broadcasters plus the control controllers. |
| **Assembly** | `src/daemon/` package | `build_components(config)` (`components.py`) builds the broadcasters, event logger, emitter, and model managers. `run_daemon(config, stop_event)` (`runtime.py`) spawns collectors + writer + dashboard + inference tasks in one event loop, joins them on `stop_event`. Drain, inference, polling, and retention each have their own module. |
| **Entry points** | `src/main.py`, `scripts/serve_dashboard.py` | `src/main.py` is the daemon CLI (always the full daemon; the `--collection-only` bare-collection fallback was retired 2026-09-25, R03.11). `scripts/serve_dashboard.py` is the dashboard-only entry — useful for development against an existing data/anomalies dir. |

## The flow, in one paragraph

CAN records, plus OBD records when a streaming OBD producer is configured,
arrive on a single `asyncio.Queue` from the collector tasks.
`drain_queue_with_broadcast` pulls each record and tees it
three ways: into the Parquet writer (session-keyed by Engine Run Time
reset), onto the telemetry `Broadcaster` (which `/ws/telemetry`
subscribers see immediately), and into the `LiveFeatureSource.observe`
side-effect. Every `1/resample_hz` seconds — on an absolute deadline grid,
so a slow tick delays nothing but itself — `run_live_inference` pulls a
one-row feature frame from the live source on the loop, hands the
schema-aligned row to `IsolationForestInferenceEngine.observe` and
`SequenceInferenceEngine.observe` in its one worker thread, and applies the
results back on the loop (discarding them if a model swap or replay
transition happened meanwhile). Each result that's an
anomaly goes through `AnomalyEventEmitter`, which dedupes by (source,
model_version) and writes through `_BroadcastingLogger` — that one call
appends to the JSONL *and* publishes onto the anomalies broadcaster, so
`/anomalies?since=` and `/ws/anomalies` are guaranteed-consistent
views. Meanwhile, `ModelManager.poll()` runs every 30 seconds; if a new
artifact + manifest lands (from `scripts/retrain.py` or
`scripts/refit_behavior_models.py`, or `/control/models/{type}/artifact`),
the manager validates the schema match and atomically swaps the in-memory
model. The next inference tick uses it. Remote operator actions are recorded
to the control audit JSONL and published on `/ws/control`.

## Seams a contributor will care about

| What you want to do | Where to look |
|---|---|
| Add a new producer | Subclass / wrap the existing collector pattern: an `async` function `(queue, stop) → None`. Wire into `_default_collectors` or pass via `collectors=` to `run_daemon`. |
| Add a new dashboard endpoint | `src/dashboard/routes/public.py` (unauthenticated reads) or the domain module under `routes/control/` (bearer-auth control plane; each `build_router(ctx)` is authenticated at router level) — `create_app` in `api.py` registers both. `tests/test_route_table_snapshot.py` pins the route table: regenerate it with `CANARY_UPDATE_ROUTE_SNAPSHOT=1` for an intended change. Use the existing pure helpers (`build_status`, `read_anomalies_since`, etc.) as templates: route → helper → unit test the helper. |
| Add a new remote operator action | Add a small controller method under `src/control/`, expose it from the matching module under `src/dashboard/routes/control/` (router-level bearer auth), then add the `scripts/canaryctl.py` command. Do not add an arbitrary shell endpoint. |
| Add a new detector | Implement an inference-engine class with `observe(row) → result`. Mirror `IsolationForestInferenceEngine`'s surface. Pair with an `AnomalyEvent.from_<source>_result` adapter on `events.py` and a new `emit_<source>` method on `AnomalyEventEmitter`. Add a detector to `LiveInferencePipeline` in `src/daemon/inference.py` with the `prepare` (loop) / `compute` (worker, pure) / `apply` (loop) split — never a second tick task, which would double-tick the buffer. |
| Add a new behavior-model surface | Add a recipe to `shared_recipes()` in `src/analysis/recipes/shared.py` (or, for a vehicle's own DIDs, `recipes()` in its `src/analysis/recipes/<profile>.py`). Pure declarative — `scripts/refit_behavior_models.py` will fit it on the next run. |
| Change what `/status` shows | `StatusSnapshot` in `src/dashboard/status.py`. Add the field, add it to `to_dict()`, surface it from `build_status`, add a test. |
| Tune the live-pipeline tick rate | `preprocessing.resample_hz` in `canary.yaml` sets the *model* cadence (`tick_seconds = 1/resample_hz`); it is independent of collector acquisition rates, and the deployed models were trained at 1 Hz — a new cadence needs train/serve validation (C08.8). Overload and lateness are visible on `/status` `detectors.inference`. |
| Swap a model without restart | Drop the new artifact + manifest into `<daemon.models_dir>/<name>/`. `ModelManager.poll()` will pick it up within `daemon.model_poll_seconds` (default 30). Rejected swaps stay on `/status` as the prior version. |

## What's *not* in here

This doc covers the daemon's *runtime* assembly. Out of scope:

* **The RE workflow** — how you go from "a CAN ID and 8 mystery bytes" to a typed entry in `data/bindings.yaml`. See `docs/can-re-workflow.md`.
* **The behavior-model rationale** — why we infer surfaces from runtime data rather than reading factory calibration tables. See `docs/behavior-model-inference.md`.
* **The data-collection plan** — what drives we still need before the ML side has enough coverage. See `docs/roadmap/archive/data-collection-plan.md`.
* **Deployment** — systemd units (`canary`, `canary-mcp`, `ups-shutdown`, `can0`), `scripts/install_pi.sh`, Pi setup. Shipped and live in-vehicle; see `docs/pi-setup.md`.

## Performance touchpoints (NFR-01 budget)

| Path | Measured cost (laptop) | Notes |
|---|---|---|
| `LiveFeatureSource.tick()` | 39 ms median / 43 ms p95 @ 16 channels, 181-row buffer (workstation, 2026-09-06) | `scripts/benchmark_live_pipeline.py` — feature-only microbenchmark; detector scoring, collectors, network and disk are untimed. Raw report: `data/analyses/2026-09-06-benchmark-live-pipeline-baseline.md`. No Pi projection is made: the Pi measurement is pending (roadmap C05.11) and the full-pipeline harness is C10. |
| `compute_features` rolling window | O(N) per tick, N ≈ window_seconds × hz | Recomputed each tick — simpler than incremental. At 30 s × 1 Hz = 30 rows × ~40 channels = tiny. Revisit if a future need pushes hz to >5. |
| `Broadcaster.publish` | O(subscribers) | Bounded per-subscriber queue, never blocks. With 1 dashboard client and 1 inference task = O(2). |
| `ModelManager.poll` | mtime stat, free | mtime short-circuit makes ticks free when nothing changed. The actual load (joblib / torch) only runs on a real swap. |
| `ParquetTelemetryWriter.flush` | I/O-bound, periodic | Default 5 s flush interval × 10 k row buffer. Atomic-write tmp + rename means flushes don't block readers. |

Tight loops on a Pi 5 won't be CPU-bound. The realistic pinch points are
disk write amplification (SD card) and the LSTM forward pass scaling
with `sequence_length × latent_dim`. Both are measurable with this
benchmark + the per-model LSTM benchmark already wired in
`src/models/lstm_training.py`.
