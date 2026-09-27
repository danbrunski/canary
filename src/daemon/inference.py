from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.alerts.fusion import (
    FusionScorer,
    FusionSource,
)
from src.coerce import finite_float
from src.collection import tuning_profile
from src.dashboard.broadcast import Broadcaster
from src.decode.accepted_baselines import load_accepted_baselines
from src.decode.behavior_models import (
    diagnostic_only_models,
    load_behavior_models,
    unknown_operating_modes,
)
from src.decode.bindings import load_bindings
from src.decode.dbc import DEFAULT_DBC_DIR, load_dbc_dir
from src.decode.dids import load_did_map
from src.paths import BINDINGS_YAML as DEFAULT_BINDINGS_PATH
from src.preprocessing.feature_pipeline import (
    ALL_MODE,
    DEFAULT_DIDS_PATH,
)
from src.preprocessing.live import LiveFeatureSource, build_prepared_live_tick
from src.storage.async_writer import AsyncParquetTelemetryWriter
from src.telemetry import TelemetryRecord, channel_of
from src.timefmt import format_timestamp
from src.vehicle_state import VehicleState, derive_from_live_source

from .autotag_hook import LiveAutoTagHook
from .committees_live import build_live_committees
from .components import DaemonComponents
from .inference_metrics import MISSED_COMPUTE_BUSY, InferenceTimingRecorder
from .inference_scheduling import GenerationToken, TickScheduler, TickSlot
from .inference_worker import (
    MIN_RESULT_TIMEOUT_SECONDS,
    RESULT_TIMEOUT_TICKS,
    InferenceWorker,
    WorkerTick,
)
from .lambda_delivery_live import build_live_lambda_delivery

if TYPE_CHECKING:
    # Annotation only: loaded on demand when ``plausibility.enabled``.
    from .plausibility_live import LivePlausibility


# R03.9: the detectors live in src.daemon.detectors; re-exported here so
# older imports keep working.
from src.daemon.detectors.behavior import (
    BehaviorDetector as BehaviorDetector,
)
from src.daemon.detectors.behavior import (
    _BehaviorResidual as _BehaviorResidual,
)
from src.daemon.detectors.behavior import (
    _observe_behavior as _observe_behavior,
)
from src.daemon.detectors.common import (
    _SCHEMA_RECONCILIATION_WARNED as _SCHEMA_RECONCILIATION_WARNED,
)
from src.daemon.detectors.common import (
    Detector as Detector,
)
from src.daemon.detectors.common import (
    TickContext as TickContext,
)
from src.daemon.detectors.common import (
    _log_schema_reconciliation_once as _log_schema_reconciliation_once,
)
from src.daemon.detectors.common import (
    _log_structural_verdict as _log_structural_verdict,
)
from src.daemon.detectors.common import (
    _persistence_sustained as _persistence_sustained,
)
from src.daemon.detectors.common import (
    _publish_score as _publish_score,
)
from src.daemon.detectors.common import (
    _with_effective_role as _with_effective_role,
)
from src.daemon.detectors.common import (
    gate_and_collect as gate_and_collect,
)
from src.daemon.detectors.isolation_forest import (
    _IF_TOP_FEATURES as _IF_TOP_FEATURES,
)
from src.daemon.detectors.isolation_forest import (
    IsolationForestDetector as IsolationForestDetector,
)
from src.daemon.detectors.isolation_forest import (
    _if_apply as _if_apply,
)
from src.daemon.detectors.isolation_forest import (
    _if_compute as _if_compute,
)
from src.daemon.detectors.isolation_forest import (
    _IFComputed as _IFComputed,
)
from src.daemon.detectors.isolation_forest import (
    _IFComputeJob as _IFComputeJob,
)
from src.daemon.detectors.isolation_forest import (
    _IFFusionInput as _IFFusionInput,
)
from src.daemon.detectors.isolation_forest import (
    _rank_feature_contributors as _rank_feature_contributors,
)
from src.daemon.detectors.isolation_forest import (
    _record_if_health as _record_if_health,
)
from src.daemon.detectors.isolation_forest import (
    _schema_for_loaded as _schema_for_loaded,
)
from src.daemon.detectors.isolation_forest import (
    _score_and_emit as _score_and_emit,
)
from src.daemon.detectors.sequence import (
    SequenceDetector as SequenceDetector,
)
from src.daemon.detectors.sequence import (
    _build_sequence_engine as _build_sequence_engine,
)
from src.daemon.detectors.sequence import (
    _engine_model_type as _engine_model_type,
)
from src.daemon.detectors.sequence import (
    _engine_version as _engine_version,
)
from src.daemon.detectors.sequence import (
    _publish_sequence_suppression as _publish_sequence_suppression,
)
from src.daemon.detectors.sequence import (
    _schema_for_engine as _schema_for_engine,
)
from src.daemon.detectors.sequence import (
    _sequence_observe as _sequence_observe,
)
from src.daemon.detectors.sequence import (
    _SequenceComputeJob as _SequenceComputeJob,
)
from src.daemon.detectors.sequence import (
    _suppress_channels_for as _suppress_channels_for,
)

log = logging.getLogger(__name__)

#: Old private name, re-exported (``src.daemon`` exports it): the one
#: definition is :func:`src.coerce.finite_float` (R01.3).
_finite_or_none = finite_float

#: Thread-name prefix of the one inference worker, so a test can assert the
#: executor was shut down and no thread leaked (C08.10).
INFERENCE_WORKER_THREAD_PREFIX = "canary-inference"
FEATURE_BUILD_TIMEOUT_TICKS = 3.0
FEATURE_BUILD_MIN_TIMEOUT_SECONDS = 2.0
FEATURE_BUILD_STARTUP_TIMEOUT_SECONDS = 15.0

#: Vehicle states IF/LSTM are allowed to score in (Phase 1.5). ``replay``
#: re-emits real running-vehicle data through the live pipeline, so it scores
#: like ``engine_running``. Everything else (ignition_only, engine_off,
#: no_data, unknown) is skipped — no koeo-trained model exists yet to make
#: ignition_only meaningful, so it stays gated out with the others.
_SCORABLE_VEHICLE_STATES = frozenset({"engine_running", "replay"})


@dataclass(frozen=True)
class CaptureProfileGate:
    """R03.12 — may IF and the sequence detector score under this profile?

    Operator decision 2026-09-24: tuning logs are not in the anomaly
    detectors. Both learned "normal" under the default profile's channels and
    rates, so scoring a capture that polls a different set measures the
    profile change, not the engine. Only a corpus-eligible profile scores.

    ``source`` says whose profile was read: the daemon's live one, or — during
    a replay — the replayed session's capture manifest. A replay is judged by
    what it replays, never by what the daemon happens to be polling.
    """

    scoring_allowed: bool
    profile: str | None
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "scoring_allowed": self.scoring_allowed,
            "profile": self.profile,
            "source": self.source,
        }


def capture_profile_gate(components: DaemonComponents) -> CaptureProfileGate:
    """The profile half of the IF/sequence gate; no I/O (read every tick)."""
    if components.replay_controller is not None:
        replay_state = components.replay_controller.status()
        if replay_state.running:
            return CaptureProfileGate(
                scoring_allowed=replay_state.source_corpus_eligible,
                profile=replay_state.source_capture_profile,
                source="replay_manifest",
            )
    profile = tuning_profile.active_profile()
    return CaptureProfileGate(
        scoring_allowed=profile.corpus_eligible,
        profile=profile.name,
        source="live_profile",
    )


def _live_vehicle_state(
    source: LiveFeatureSource, components: DaemonComponents, now: datetime
) -> VehicleState:
    """No-I/O vehicle state for the inference hot loop.

    The same derivation ``/status`` uses when a live source is wired
    (:func:`src.vehicle_state.derive_from_live_source`), broadcast-RPM
    fallback included. This copy used to pass only the OBD rpm to the
    classifier, so during an all-DID capture it read ``engine_off`` while
    ``/status`` read ``engine_running`` (R03.1). Tuning captures stay unscored
    through the capture-profile gate (R03.12), not through this state.
    """
    return derive_from_live_source(source, replay_controller=components.replay_controller, now=now)


def _current_generation(components: DaemonComponents) -> GenerationToken:
    """The C08.5 generation token: replay + capture state and every model
    manager's serving version, read on the loop. Cheap (a few attribute
    reads) so it is taken twice per tick — at snapshot and again at apply —
    and a mismatch discards the in-flight result."""
    replay_id: str | None = None
    if components.replay_controller is not None:
        replay_state = components.replay_controller.status()
        if replay_state.running:
            replay_id = replay_state.replay_id
    capture_id: str | None = None
    if components.capture_controller is not None:
        active = components.capture_controller.active
        capture_id = active.capture_id if active is not None else None
    versions: list[tuple[str, str | None]] = []
    for name, manager in sorted(components.model_managers.items()):
        loaded = manager.current
        versions.append((name, loaded.serving_version if loaded is not None else None))
    return GenerationToken(
        replay_id=replay_id, capture_id=capture_id, model_versions=tuple(versions)
    )


#: Instantaneous performance channels (Task 1.1) — the row-local subset
#: ``compute_instantaneous_channels`` appends inside ``featurize_wide_frame``,
#: so they land on ``source.latest_operating_point`` for free every tick.
_DERIVED_INSTANTANEOUS_CHANNELS: tuple[str, ...] = (
    "derived:FUEL_RATE_LPH",
    "derived:INSTANT_ECONOMY_L_PER_100KM",
    "derived:INSTANT_ECONOMY_MPG",
    "derived:BOOST_VACUUM_KPA",
    "derived:ESTIMATED_POWER_HP",
    "derived:ESTIMATED_TORQUE_NM",
)


async def _publish_derived_channels(
    broadcaster: Broadcaster,
    source: LiveFeatureSource,
    now: datetime,
    storage_writer: AsyncParquetTelemetryWriter | None,
) -> None:
    """Stream Task 1.1's performance channels over ``/ws/telemetry`` **and**
    persist them to Parquet through the daemon's one storage writer.

    Instantaneous channels (fuel rate, instant economy, boost/vacuum,
    estimated power/torque) come off ``latest_operating_point`` — the raw
    operating-frame row ``compute_instantaneous_channels`` already appended
    inside ``featurize_wide_frame``, identical for a live window or a full
    session since none of them need history. The session-scoped trip
    integrals (distance/fuel/CO2/idle/avg-economy) come off
    ``latest_derived_channels`` — Task 1.3's accumulator, not a windowed
    cumsum (the ring buffer evicts rows continuously; cumsum-ing it would
    reset the integrals far more often than a session boundary).

    Both are reconstructable from raw signals offline, but ``query_telemetry``
    (the documented DuckDB view over stored Parquet, see docs/mcp-server.md)
    has no such reconstruction fallback — an operator querying trip/fuel/boost
    history through it must find the values already in the file. So every
    value built here also becomes a ``derived:*`` ``TelemetryRecord`` and goes
    through ``storage_writer.write_many`` — the same ``ParquetTelemetryWriter``
    instance ``run_storage_sink`` writes raw records through (Task 1.1
    follow-up), not a second Parquet-writing path. ``storage_writer`` is
    ``None`` in tests that never wire storage, in which case persistence is
    skipped and only the broadcast happens.

    Best-effort and silent on nothing-to-publish, matching every other
    broadcast call in this loop — a stalled dashboard socket, or a storage
    write failure, must never interrupt scoring.
    """
    point = source.latest_operating_point or {}
    values: dict[str, Any] = {
        name: point[name] for name in _DERIVED_INSTANTANEOUS_CHANNELS if name in point
    }
    values.update(source.latest_derived_channels or {})
    if not values:
        return
    timestamp = now.isoformat()
    records: list[TelemetryRecord] = []
    for name, value in values.items():
        if value is None or (isinstance(value, float) and math.isnan(value)):
            continue
        numeric = float(value)
        channel = channel_of(name)
        broadcaster.publish(
            {
                "timestamp": timestamp,
                "source": "derived",
                "channel": channel,
                "value": numeric,
                "unit": "",
            }
        )
        records.append(TelemetryRecord.derived(now, channel, numeric))
    if storage_writer is not None and records:
        try:
            await storage_writer.write_many(records)
        except Exception:  # noqa: BLE001 — storage is best-effort, must not kill the tick
            log.exception("failed to persist derived channels to storage")


@dataclass(frozen=True)
class _TickRequest:
    """One tick's loop-side snapshot (C08.4): everything the worker needs,
    pinned to the :class:`GenerationToken` current when it was taken."""

    slot: TickSlot
    ctx: TickContext
    token: GenerationToken
    if_job: _IFComputeJob | None
    sequence_job: _SequenceComputeJob | None
    behavior_residuals: tuple[_BehaviorResidual, ...]
    #: ``perf_counter`` when the snapshot began / how long it took, for the
    #: ``total`` and ``loop_occupancy`` stages.
    snapshot_started: float
    snapshot_seconds: float
    #: Time actually spent executing Python on the daemon loop. When feature
    #: construction runs in its child, ``snapshot_seconds`` includes the
    #: await but this value deliberately does not.
    snapshot_loop_seconds: float

    @property
    def needs_worker(self) -> bool:
        return self.if_job is not None or self.sequence_job is not None


@dataclass(frozen=True)
class _TickResult:
    """What the worker hands back: pure data plus its own stage durations
    (seconds), recorded by the loop on apply — the recorder is loop-owned."""

    if_computed: _IFComputed | None
    sequence_result: Any
    durations: Mapping[str, float]


_EMPTY_RESULT = _TickResult(if_computed=None, sequence_result=None, durations={})


def _without_sequence(request: _TickRequest) -> _TickRequest:
    """``request`` with its sequence job dropped: the detector is not ready.

    A reset the job carried still applies to this process's engine — the
    window must not span the rows that were never scored. The child clears its
    own window when it acknowledges the load.
    """
    job = request.sequence_job
    if job is None:
        return request
    if job.reset:
        job.engine.reset()
    return replace(request, sequence_job=None)


def _compute_tick(request: _TickRequest) -> _TickResult:
    """Worker-thread entry (C08.4): the IF forward pass and the sequence
    forward pass, nothing else. No broadcaster, no asyncio primitive, no
    ``LiveFeatureSource``, no emitter — every side effect waits for
    :meth:`LiveInferencePipeline._apply` on the loop."""
    durations: dict[str, float] = {}
    started = time.perf_counter()
    if_computed: _IFComputed | None = None
    if request.if_job is not None:
        stage_started = time.perf_counter()
        if_computed = IsolationForestDetector.compute(request.if_job)
        durations["isolation_forest"] = time.perf_counter() - stage_started
    sequence_result: Any = None
    if request.sequence_job is not None:
        stage_started = time.perf_counter()
        sequence_result = SequenceDetector.compute(request.sequence_job)
        durations["sequence"] = time.perf_counter() - stage_started
    durations["compute"] = time.perf_counter() - started
    return _TickResult(
        if_computed=if_computed, sequence_result=sequence_result, durations=durations
    )


class LiveInferencePipeline:
    """The per-tick body of :func:`run_live_inference` (C08.4/C08.5/C08.6).

    Four phases, three execution contexts:

    1. **prepare** (loop) — snapshot observations into the bounded ring.
    2. **feature** (persistent spawned process in production) — run the exact
       shared pandas transform, then apply its compact result on the loop.
    3. **snapshot** (loop) — derived-channel
       publication, vehicle-state gate, autotag, behavior residuals, and each
       detector's ``prepare``; the result is an immutable :class:`_TickRequest`
       tagged with the current :class:`GenerationToken`.
    4. **compute/apply** — :func:`_compute_tick` in the scoring worker, then
       discard on the loop if the generation changed, else each
       detector's ``apply`` (health, ``/ws/scores``, persistence, emits) and
       fusion.

    At most one compute job is ever in flight: a tick whose deadline arrives
    while the previous job is still scoring is *skipped* and counted
    (``missed_by_reason.compute_busy``), never queued. A tick with nothing to
    score (no model loaded, gated vehicle state, no row) applies inline
    without touching the worker.

    ``wall_clock`` and both executors are injectable for tests; production
    uses ``datetime.now(UTC)``, an owned scoring thread and one owned feature
    process.
    """

    def __init__(
        self,
        source: LiveFeatureSource,
        components: DaemonComponents,
        *,
        interval_seconds: float,
        recorder: InferenceTimingRecorder,
        executor: ThreadPoolExecutor | None = None,
        wall_clock: Callable[[], datetime] | None = None,
        worker: InferenceWorker | None = None,
        feature_executor: Executor | None = None,
        own_feature_executor: bool = False,
    ) -> None:
        self._source = source
        self._components = components
        self._recorder = recorder
        self._fusion = FusionScorer(
            components.config.models.fusion, roles=components.config.models.roles.by_source()
        )
        self._severity_threshold = components.config.models.fusion.severity_threshold
        self._if = IsolationForestDetector(components)
        self._sequence = SequenceDetector(components)
        self._behavior = BehaviorDetector(components, self._fusion, source)
        # Phase 2a.6 — ships config-gated off (AutoTagConfig.live_enabled
        # defaults False); LiveAutoTagHook.tick is itself a no-op while
        # disabled, so constructing it unconditionally costs nothing and keeps
        # a config hot-reload flipping it on mid-session picked up next tick.
        self._autotag_hook = LiveAutoTagHook(
            config=components.config.autotag,
            session_dir=lambda: (
                components.storage_writer.paths.root
                if components.storage_writer is not None
                else None
            ),
            _tick_seconds=interval_seconds,
        )
        #: A05.9 — deterministic plausibility checks over arrived samples.
        #: ``None`` unless ``plausibility.enabled`` (default off).
        self._plausibility = _build_plausibility(components, source, recorder)
        #: H04.2 — sensor committees: instrument cross-checks that flag. Run
        #: under every capture profile (R03.12 reasoning in committees_live).
        self._committees = build_live_committees(components, source, recorder)
        #: B06.4 — lambda delivery error as a tuning readout: the offline
        #: engine's rows, decided per tick; never alerts, not profile-gated.
        self._lambda_delivery = build_live_lambda_delivery(components, source, recorder)
        #: Tick period, for naming a compute that outran it (D02.3).
        self._interval_seconds = interval_seconds
        self._owns_executor = executor is None
        self._executor = executor if executor is not None else _new_inference_executor()
        self._wall_clock = wall_clock if wall_clock is not None else _utc_now
        self._inflight: asyncio.Task[None] | None = None
        #: Depth-1 pipeline queue (D01.9): the snapshot taken while a compute
        #: was still in flight, waiting for the worker. Newest wins.
        self._pending: _TickRequest | None = None
        #: Out-of-process scoring (:mod:`src.daemon.inference_worker`).
        #: ``None`` keeps every pass in this process — what tests get, and
        #: any host where the child could not start.
        self._worker = worker
        self._feature_executor = feature_executor
        self._owns_feature_executor = own_feature_executor
        self._feature_executor_ready = False

    def _worker_tick(self, request: _TickRequest) -> WorkerTick:
        """The portable half of a request: the row and the two versions.

        Deliberately *not* the jobs — those hold the loaded model and the
        sequence engine, and pushing either through a pipe every tick would
        cost more than the scoring it is meant to offload.
        """
        row = request.if_job.row if request.if_job is not None else None
        if row is None and request.sequence_job is not None:
            row = request.sequence_job.row
        return WorkerTick(
            index=request.slot.index,
            row=row,
            want_if=request.if_job is not None,
            want_sequence=request.sequence_job is not None,
            if_version=self._if.model_version,
            sequence_version=self._sequence.model_version,
            reset_sequence=request.sequence_job is not None and request.sequence_job.reset,
        )

    @property
    def inflight(self) -> asyncio.Task[None] | None:
        """The compute→apply task for the last worker-bound tick, or ``None``."""
        return self._inflight

    @property
    def detectors(self) -> tuple[IsolationForestDetector, SequenceDetector, BehaviorDetector]:
        return (self._if, self._sequence, self._behavior)

    async def run_tick(self, slot: TickSlot) -> None:
        """Scheduler entry: snapshot on the loop, then hand off or queue.

        D01.9 — the snapshot runs **even while a compute is in flight**. Before
        this, a tick whose deadline arrived mid-score was skipped whole, so the
        feature build and the score could only ever run back to back: on the Pi
        that summed a ~165 ms snapshot and a ~250 ms score into a ~440 ms tick,
        and every miss the truck recorded was `compute_busy`. With scoring in a
        child process (D01.1) the parent is idle during the score, so phase 1
        of tick N+1 can overlap phase 2 of tick N and the tick costs roughly
        the larger of the two instead of the sum.

        What is *not* pipelined, deliberately: **one compute at a time**. The
        sequence engine's window is state advanced only inside `compute`, so
        two concurrent scores would interleave it. Ordering therefore still
        holds — computes are dispatched in tick order and applied in dispatch
        order — and the queue is depth 1.

        Whether the worker is busy is decided *after* the snapshot, never from
        a flag read before it (2026-09-24). A score that finishes while this
        tick's features are built drains the queue on its way out; a tick
        parked on the stale "busy" answer then sat in the queue with nothing in
        flight, the next tick dispatched ahead of it, and the older row was
        scored after the newer one. Every such row restarts the sequence window
        (time went backwards, A01.3), so on the Pi — a ~300 ms score against a
        ~250 ms feature build — no 30-row window ever filled and the r2 LSTM
        scored nothing for a whole drive. :meth:`_dispatch` refuses an older
        tick outright, so no path can reintroduce the reordering.
        """
        request = await self._snapshot(slot)
        if not request.needs_worker:
            self._apply(request, _EMPTY_RESULT)
            return
        if self._inflight is not None and not self._inflight.done():
            # Depth-1 queue, newest wins. A deeper queue would score rows that
            # are already stale by the time they reach the detector and would
            # let a slow scorer grow memory and lag without bound; dropping the
            # older pending row is the same loss the old skip took, one tick
            # later and with the fresher features kept.
            if self._pending is not None:
                self._recorder.note_missed(MISSED_COMPUTE_BUSY)
                log.debug(
                    "dropping queued inference tick %d for fresher tick %d",
                    self._pending.slot.index,
                    slot.index,
                )
            self._pending = request
            return
        self._dispatch(request)

    def _dispatch(self, request: _TickRequest) -> None:
        """Start the one compute task for ``request``.

        Refuses a tick older than the last one dispatched: its row would reach
        the sequence engine after a newer one and restart the window (A01.3).
        Counted as a ``compute_busy`` miss — the tick lost its turn to the busy
        worker — and logged, since reaching it means the queue discipline in
        :meth:`run_tick` broke again.
        """
        if request.slot.index <= self._inflight_index:
            self._recorder.note_missed(MISSED_COMPUTE_BUSY)
            log.warning(
                "not scoring inference tick %d after tick %d: an older row would "
                "restart the sequence window",
                request.slot.index,
                self._inflight_index,
            )
            return
        self._inflight_index = request.slot.index
        self._inflight = asyncio.create_task(
            self._compute_and_apply(request),
            name=f"inference-compute-{request.slot.index}",
        )

    def _drain_pending(self) -> None:
        """Dispatch the queued tick, if any, now the worker is free again."""
        request = self._pending
        self._pending = None
        if request is not None:
            self._dispatch(request)

    _inflight_index: int = -1

    async def _snapshot(self, slot: TickSlot) -> _TickRequest:
        """Phase 1 — everything that reads or mutates loop-owned state."""
        import pandas as pd

        source = self._source
        components = self._components
        recorder = self._recorder
        snapshot_started = time.perf_counter()
        now = self._wall_clock()
        stage_started = time.perf_counter()
        feature_wait_seconds = 0.0
        if self._feature_executor is None:
            # Exact legacy path for single-process debugging and tests that
            # instrument/replace ``tick`` as their public seam.
            row = source.tick(now)
        else:
            prepared = source.prepare_tick(now)
            if prepared is None:
                row = None
            if prepared is not None:
                loop = asyncio.get_running_loop()
                wait_started = time.perf_counter()
                try:
                    future = loop.run_in_executor(
                        self._feature_executor, build_prepared_live_tick, prepared
                    )
                    built = await asyncio.wait_for(
                        future,
                        timeout=(
                            max(
                                FEATURE_BUILD_MIN_TIMEOUT_SECONDS,
                                self._interval_seconds * FEATURE_BUILD_TIMEOUT_TICKS,
                            )
                            if self._feature_executor_ready
                            else FEATURE_BUILD_STARTUP_TIMEOUT_SECONDS
                        ),
                    )
                    self._feature_executor_ready = True
                except Exception:  # noqa: BLE001 — live inference is best-effort
                    log.exception(
                        "live feature worker failed for tick %d; falling back to a thread",
                        slot.index,
                    )
                    self._disable_feature_executor()
                    # A degraded tick must still preserve state progression. This
                    # fallback is rare; it stays off the event loop, though Python
                    # GIL contention may make it slower than the process path.
                    built = await asyncio.to_thread(build_prepared_live_tick, prepared)
                feature_wait_seconds = time.perf_counter() - wait_started
                recorder.record_stage("feature_wait", feature_wait_seconds)
                row = source.apply_built_tick(prepared, built)
        recorder.record_stage("feature", time.perf_counter() - stage_started)
        timestamp_str: str | None = None
        operating_mode: str | None = None
        if row is not None:
            # Canonical T-separated/Z-suffixed ISO-8601 — a bare
            # ``str(pd.Timestamp)`` is space-separated, which broke the
            # dashboard's lexicographic ``since`` filter for every
            # behavior/fused event (they sorted before any 'T…' bound).
            timestamp_str = format_timestamp(row["timestamp"].iloc[0])
            mode_val = row["operating_mode"].iloc[0] if "operating_mode" in row else None
            operating_mode = None if mode_val is None or pd.isna(mode_val) else str(mode_val)
            stage_started = time.perf_counter()
            await _publish_derived_channels(
                components.telemetry_broadcaster, source, now, components.storage_writer
            )
            recorder.record_stage("publish_derived", time.perf_counter() - stage_started)
            # 2a.6 — no-op while AutoTagConfig.live_enabled is False (the
            # shipped default); reads the same raw operating point the
            # behavior overlay reads, not the engineered feature row.
            self._autotag_hook.tick(source.latest_operating_point, now)

        if self._plausibility is not None:
            try:
                self._plausibility.tick_live(components, now, operating_mode)
            except Exception:  # noqa: BLE001 — plausibility is best-effort
                log.exception("plausibility tick failed")

        if self._committees is not None:
            stage_started = time.perf_counter()
            try:
                self._committees.tick_live(
                    components, source.latest_operating_point, now, operating_mode
                )
            except Exception:  # noqa: BLE001 — committees are best-effort
                log.exception("sensor committee tick failed")
            recorder.record_stage("committees", time.perf_counter() - stage_started)

        if self._lambda_delivery is not None:
            try:
                self._lambda_delivery.tick_live(components)
            except Exception:  # noqa: BLE001 — a readout is best-effort
                log.exception("lambda delivery readout tick failed")

        # 4.7 — the segmenter falling back to constant "all" mode means
        # every mode-gated behavior surface silently never fires; make
        # that visible instead of nowhere, same edge-triggered pattern as
        # IF's blind_modes (2.1).
        newly_fallback = components.health_monitor.observe_segmenter_fallback(
            source.last_segmenter_fallback
        )
        if newly_fallback:
            log.warning(
                "operating-mode segmenter fell back to constant %r mode — segmenter "
                "input columns are missing from the live stream; every mode-gated "
                "behavior surface is suppressed until it recovers. See /status "
                "detectors.segmenter.fallback_active.",
                ALL_MODE,
            )

        scoring_allowed = True
        vehicle_state: VehicleState | None = None
        # R03.12 — asked every tick: the behavior detector obeys it even on a
        # tick with no feature row (R03.12b).
        profile_gate = capture_profile_gate(components)
        components.health_monitor.observe_capture_profile_gate(
            {
                **profile_gate.to_dict(),
                "behavior_residuals_withheld": self._behavior.withheld_by_capture_profile,
            }
        )
        if row is not None:
            vehicle_state = _live_vehicle_state(source, components, now)
            scoring_allowed = vehicle_state.state in _SCORABLE_VEHICLE_STATES
            # R03.12 — two gates, two counters, so an empty score stream says
            # which kind of empty it is. Vehicle state is asked first: a
            # profile refusal therefore means "the engine was running, and
            # the capture profile is why nothing scored".
            if not scoring_allowed:
                components.emitter.counters.skipped_vehicle_state += 1
            elif not profile_gate.scoring_allowed:
                scoring_allowed = False
                components.emitter.counters.skipped_capture_profile += 1

        # Spec §3.7.6 — escalate a sustained parked-with-power state to a
        # poweroff. Ships disabled; `idle_shutdown` is None unless the
        # operator opted in. Classified unconditionally because the state
        # that matters most here (`no_data`) is exactly the one where
        # `row` is None, so the block above would never see it.
        if components.idle_shutdown is not None:
            if vehicle_state is None:
                vehicle_state = _live_vehicle_state(source, components, now)
            if components.idle_shutdown.observe(state=vehicle_state.state, now=time.monotonic()):
                components.idle_shutdown.trigger()

        ctx = TickContext(
            now=now,
            operating_mode=operating_mode,
            timestamp_str=timestamp_str,
            scoring_allowed=scoring_allowed,
            capture_profile_allows=profile_gate.scoring_allowed,
        )
        token = _current_generation(components)
        if_job = self._if.prepare(row, ctx)
        sequence_job = self._sequence.prepare(row, ctx, generation=token)
        stage_started = time.perf_counter()
        residuals = tuple(self._behavior.prepare(row, ctx))
        recorder.record_stage("behavior", time.perf_counter() - stage_started)
        snapshot_seconds = time.perf_counter() - snapshot_started
        snapshot_loop_seconds = max(0.0, snapshot_seconds - feature_wait_seconds)
        recorder.record_stage("snapshot", snapshot_seconds)
        return _TickRequest(
            slot=slot,
            ctx=ctx,
            token=token,
            if_job=if_job,
            sequence_job=sequence_job,
            behavior_residuals=residuals,
            snapshot_started=snapshot_started,
            snapshot_seconds=snapshot_seconds,
            snapshot_loop_seconds=snapshot_loop_seconds,
        )

    async def _compute_and_apply(self, request: _TickRequest) -> None:
        """Phase 2 on the worker, then phase 3 back on the loop. A worker
        exception is counted and logged; the loop keeps ticking."""
        loop = asyncio.get_running_loop()
        submitted = time.perf_counter()
        try:
            result = await loop.run_in_executor(self._executor, self._compute, request)
        except Exception:  # noqa: BLE001 — live inference is best-effort
            self._recorder.note_compute_failure()
            log.exception("live inference compute failed for tick %d", request.slot.index)
            # The queued tick must still go, or one compute failure stalls the
            # pipeline until the next deadline that finds the worker free.
            self._drain_pending()
            return
        compute_wait = time.perf_counter() - submitted
        self._recorder.record_stage("compute_wait", compute_wait)
        # A compute that outran the tick period is why the next tick cannot
        # start (D02.3). Name it here rather than leaving it visible only as a
        # `compute_wait` max nobody reads.
        if self._interval_seconds and compute_wait > self._interval_seconds:
            self._recorder.note_slow_compute()
        if self._worker is not None:
            self._recorder.set_worker_fallbacks(self._worker.fallbacks)
            self._recorder.set_worker_stale_replies(self._worker.stale_replies)
            self._recorder.set_worker_swaps(self._worker.swaps)
        try:
            self._apply(request, result)
        except Exception:  # noqa: BLE001 — live inference is best-effort
            log.exception("live inference apply failed for tick %d", request.slot.index)
        finally:
            # Dispatch the queued tick only after this one has been applied, so
            # results reach the detectors in tick order (D01.9).
            self._drain_pending()

    def _compute(self, request: _TickRequest) -> _TickResult:
        """Score one tick out-of-process when a worker is up, here otherwise.

        Runs on the pipeline's worker thread either way; what changes is
        *where the model runs*. A worker that is down, wedged or broken
        returns ``None`` and we score in-process instead — a briefly slower
        detector costs far less than a stalled collector, which is the whole
        trade the worker exists to make.

        The Isolation Forest scores one row, so its fallback is exact. The
        sequence engine here only ever sees fallback ticks; its window clears
        on the time gap since its last row (A01.2/A01.3), so after a single
        timeout it has nothing to score, and it scores again only once a run
        of consecutive fallback ticks refills it — never a window stitched from
        rows hours apart.

        A child that is loading models — at startup or for a hot-swap — is not
        a timeout (A01.6). Nothing is sent to it; the Isolation Forest scores
        here and the sequence detector is *not ready* for the tick, exactly as
        while its window refills.
        """
        worker = self._worker
        if worker is not None:
            worker_tick = self._worker_tick(request)
            if not worker.ready_for(worker_tick):
                return _compute_tick(_without_sequence(request))
            result = worker.compute(worker_tick)
            if result is not None:
                return result
        return _compute_tick(request)

    def _apply(self, request: _TickRequest, result: _TickResult) -> None:
        """Phase 3 — every side effect, on the loop, exactly once per
        accepted result, and only for the generation it was computed under."""
        recorder = self._recorder
        apply_started = time.perf_counter()
        for name, seconds in result.durations.items():
            recorder.record_stage(name, seconds)
        if _current_generation(self._components) != request.token:
            recorder.note_stale_discarded()
            log.info(
                "discarding inference result for tick %d: generation changed while scoring",
                request.slot.index,
            )
            return
        ctx = request.ctx
        sources: list[FusionSource] = []
        if result.if_computed is not None:
            sources.extend(self._if.apply(result.if_computed, ctx))
        if result.sequence_result is not None:
            sequence_result = result.sequence_result
            recorder.note_sequence_window(
                rows=getattr(sequence_result, "window_rows", None),
                span_seconds=getattr(sequence_result, "window_span_seconds", None),
                max_step_seconds=getattr(sequence_result, "window_max_step_seconds", None),
            )
            recorder.note_sequence_envelope(
                getattr(sequence_result, "envelope", None),
                model_version=getattr(sequence_result, "model_version", None),
            )
            sources.extend(self._sequence.apply(sequence_result, ctx))
        sources.extend(self._behavior.apply(list(request.behavior_residuals), ctx))
        self._fuse(sources, ctx)
        finished = time.perf_counter()
        apply_seconds = finished - apply_started
        recorder.record_stage("apply", apply_seconds)
        recorder.record_stage("loop_occupancy", request.snapshot_loop_seconds + apply_seconds)
        recorder.record_stage("total", finished - request.snapshot_started)
        recorder.note_tick_completed()

    def _fuse(self, sources: list[FusionSource], ctx: TickContext) -> None:
        if not (self._fusion.enabled and sources and ctx.timestamp_str is not None):
            return
        fused_version = (
            f"if={self._if.model_version or '-'}|lstm={self._sequence.model_version or '-'}"
        )
        fused = self._fusion.fuse(
            sources, timestamp=ctx.timestamp_str, operating_mode=ctx.operating_mode
        )
        _publish_score(
            self._components.scores_broadcaster,
            source="fused",
            score=fused.severity,
            threshold=self._severity_threshold,
            operating_mode=ctx.operating_mode,
            model_version=fused_version,
            timestamp=ctx.timestamp_str,
            missing_features=False,
        )
        self._components.emitter.emit_fused(
            fused,
            model_version=fused_version,
            severity_threshold=self._severity_threshold,
        )

    async def close(self, *, timeout_seconds: float) -> None:
        """Join the in-flight job (bounded) and shut the owned executor down.

        A worker pass can't be cancelled mid-forward-pass; if it doesn't
        return within ``timeout_seconds`` the executor is released without
        waiting and the thread ends when the pass does — logged, so a
        shutdown that outlives its worker is visible rather than hung.
        """
        # Drop the queue first: joining the in-flight task would otherwise
        # dispatch it on the way out and leave a compute running past close.
        self._pending = None
        inflight = self._inflight
        if inflight is not None and not inflight.done():
            try:
                await asyncio.wait_for(asyncio.shield(inflight), timeout=timeout_seconds)
            except TimeoutError:
                log.warning(
                    "in-flight inference compute did not finish within %.1fs; "
                    "releasing the worker without waiting",
                    timeout_seconds,
                )
                if self._owns_executor:
                    self._executor.shutdown(wait=False, cancel_futures=True)
                self._disable_feature_executor()
                return
        if self._owns_executor:
            self._executor.shutdown(wait=True, cancel_futures=True)
        if self._owns_feature_executor and self._feature_executor is not None:
            self._feature_executor.shutdown(wait=True, cancel_futures=True)
            self._feature_executor = None

    def _disable_feature_executor(self) -> None:
        """Drop a failed owned feature process without blocking the loop."""
        executor = self._feature_executor
        self._feature_executor = None
        if executor is None or not self._owns_feature_executor:
            return
        process_map = getattr(executor, "_processes", None)
        processes = list((process_map or {}).values())
        executor.shutdown(wait=False, cancel_futures=True)
        for process in processes:
            try:
                process.terminate()
            except Exception:  # noqa: BLE001 — already gone is the goal
                log.debug("feature worker already exited", exc_info=True)


def _build_plausibility(
    components: DaemonComponents,
    source: LiveFeatureSource,
    recorder: InferenceTimingRecorder,
) -> LivePlausibility | None:
    """The live plausibility evaluator (A05.9), or ``None``.

    Imported only when enabled, so a daemon with the checks off loads none of
    it.
    """
    if not components.config.plausibility.enabled:
        return None
    from src.daemon.plausibility_live import LivePlausibility

    try:
        return LivePlausibility.build(components, source, recorder)
    except Exception as exc:  # noqa: BLE001 — a broken catalog must not stop scoring
        log.exception("plausibility evaluator failed to start; checks are off")
        # Enabled-but-broken must not read like ``enabled: false`` (null).
        recorder.set_plausibility(
            {"enabled": True, "withheld": f"evaluator failed to start: {exc!r}"}
        )
        return None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_inference_executor() -> ThreadPoolExecutor:
    """The one owned worker (C08.4/C08.6): ``max_workers=1`` is the bound
    on in-flight computation, not a tuning knob."""
    return ThreadPoolExecutor(max_workers=1, thread_name_prefix=INFERENCE_WORKER_THREAD_PREFIX)


async def run_live_inference(
    source: LiveFeatureSource,
    components: DaemonComponents,
    stop_event: asyncio.Event,
    *,
    tick_seconds: float | None = None,
) -> None:
    """Unified per-tick inference: IF + LSTM + behavior → fusion (Phase 7.2).

    Caller wires record-stream → ``source.observe(record)`` somewhere else.
    Each tick (``tick_seconds``, default ``1 / preprocessing.resample_hz``)
    pulls *one* feature row and runs every detector against it (Task 3.1 —
    ``IsolationForestDetector``, ``SequenceDetector``, ``BehaviorDetector``),
    each owning its own observe → per-source emit → persistence →
    fusion-source shape:

    * **IF** → per-source ``isolation_forest`` event (unchanged) + an IF
      fusion source from the 7.2 calibrated threshold;
    * **LSTM|TCN** → engine built/hot-swapped inside the detector,
      per-source ``lstm_autoencoder`` event (unchanged) + a sequence fusion
      source;
    * **behavior residuals** → the live overlay (unchanged) + a fusion
      source per graded model.

    The fusion sources are combined into one agreement-aware calibrated
    ``fused`` event (deduped/tuned through the same emitter), emitted
    *alongside* the per-source events. A single unified task ticks the
    source once per interval (the old split IF/LSTM tasks double-ticked the
    buffer; the standalone LSTM-only task, ``run_live_inference_lstm``, was
    production-dead by the time of Task 4.1 and was deleted).

    **Scheduling (C08).** Ticks run on an absolute monotonic deadline grid
    (:class:`~src.daemon.inference_scheduling.TickScheduler`): the period
    is ``tick_seconds`` regardless of how long a tick's work takes, an
    overrun skips to the next future deadline (counted, never a catch-up
    burst), and the expensive IF/sequence forward passes run in one owned
    worker thread while the GIL-heavy feature transform runs in a persistent
    spawned process. ``LiveFeatureSource`` mutation, broadcasts, persistence
    and emits stay on the loop (:class:`LiveInferencePipeline`). Timing and
    overload counters land in ``components.inference_timing`` (``/status``
    ``detectors.inference``). ``tick_seconds`` is the *model* cadence — it is
    derived from ``resample_hz`` and independent of any collection rate
    (C08.8); see ``docs/inference-scheduling.md``.

    **Vehicle-state gating (Phase 1.5).** IF and the sequence detector only
    score ticks where ``_live_vehicle_state`` classifies as
    ``engine_running`` or ``replay`` — ``_SCORABLE_VEHICLE_STATES``. A
    key-out / KOEO / no-data tick is counted in
    ``emitter.counters.skipped_vehicle_state`` instead of scored, since
    neither detector was trained on that regime and scoring it just adds
    noise. The behavior overlay is unaffected — it keeps its own
    per-model ``operating_mode`` gating and still observes every tick.

    **Capture-profile gating (R03.12).** A tick the vehicle state allows is
    still withheld from IF and the sequence detector while the active profile
    is not corpus-eligible (:func:`capture_profile_gate`; during a replay, the
    replayed session's manifest). Counted in
    ``emitter.counters.skipped_capture_profile``. Behavior-model deviations
    stop under the same gate (R03.12b, operator decision 2026-09-25): no
    event, fusion vote or score publish, counted as
    ``capture_profile_gate.behavior_residuals_withheld`` in ``/status``; the
    live overlay still shows expected/actual. Plausibility checks keep running
    (R03.12c).

    Best-effort: every error inside the loop is caught + logged.
    """
    interval = tick_seconds or 1.0 / max(components.config.preprocessing.resample_hz, 1.0)
    if components.model_managers.get("isolation_forest") is None:
        log.info(
            "isolation_forest model manager absent; IF scoring disabled — the "
            "behavior overlay + fusion still run off the live feature source"
        )
    recorder = components.inference_timing
    worker = _start_inference_worker(components, tick_seconds=interval)
    feature_executor = _start_feature_executor(components)
    pipeline = LiveInferencePipeline(
        source,
        components,
        interval_seconds=interval,
        recorder=recorder,
        worker=worker,
        feature_executor=feature_executor,
        own_feature_executor=feature_executor is not None,
    )
    scheduler = TickScheduler(interval, recorder=recorder)
    try:
        await scheduler.run(pipeline.run_tick, stop_event)
    finally:
        await pipeline.close(timeout_seconds=max(interval, 1.0))
        if worker is not None:
            worker.stop()


def _start_inference_worker(
    components: DaemonComponents, *, tick_seconds: float | None = None
) -> InferenceWorker | None:
    """Bring up out-of-process scoring, or return ``None`` to stay in-process.

    ``None`` is a supported outcome, not an error: the pipeline then scores
    in-process exactly as it did before.

    The worker is handed ``components.config`` — the *live* object — rather
    than a config path. ``main`` rewrites the config through ``scope_config``
    so every vehicle-specific path sits under the active profile slug, and a
    child that re-read the YAML would look for artifacts in an unscoped
    ``models/`` and quietly score with nothing loaded.
    """
    if not components.config.models.out_of_process_scoring:
        return None
    worker = InferenceWorker(
        components.config,
        result_timeout_seconds=(
            None
            if tick_seconds is None
            else max(MIN_RESULT_TIMEOUT_SECONDS, tick_seconds * RESULT_TIMEOUT_TICKS)
        ),
    )
    if not worker.start():
        return None
    return worker


def _start_feature_executor(components: DaemonComponents) -> Executor | None:
    """Create the one persistent process that owns pandas feature builds."""
    if not components.config.models.out_of_process_feature_build:
        return None
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    return ProcessPoolExecutor(
        max_workers=1,
        mp_context=multiprocessing.get_context("spawn"),
    )


def build_live_feature_source(
    components: DaemonComponents,
    *,
    bindings_path: Path | None = None,
    behavior_models_path: Path | None = None,
    accepted_baselines_path: Path | None = None,
) -> LiveFeatureSource:
    """Construct a ``LiveFeatureSource`` from the daemon's config + sidecars.

    Convenience for the daemon entry: loads ``bindings.yaml``,
    ``behavior_models.yaml``, and ``accepted_baselines.yaml`` if present at
    the given paths (defaults are the canonical, profile-scoped locations),
    and wires them into the source so CAN payloads decode, deviation
    columns appear automatically, and the behavior detector (2.2) can
    consult active overrides.
    """
    # Repo-root-anchored (see api.DEFAULT_BINDINGS_PATH) so live CAN decode
    # finds bindings.yaml regardless of the daemon's launch cwd.
    bindings_path = bindings_path or DEFAULT_BINDINGS_PATH
    # Scoped per vehicle profile (config.daemon.behavior_models is rewritten to
    # <base>/<slug>/behavior_models.yaml by src.vehicle.profiles.scope_config).
    behavior_models_path = behavior_models_path or components.config.daemon.behavior_models
    # Likewise profile-scoped (config.daemon.accepted_baselines); a missing
    # file is a valid, empty document (load_accepted_baselines handles that),
    # so this is always safe to load unconditionally.
    accepted_baselines_path = accepted_baselines_path or components.config.daemon.accepted_baselines
    bindings = load_bindings(bindings_path) if bindings_path.exists() else None
    behavior_models = (
        load_behavior_models(behavior_models_path) if behavior_models_path.exists() else None
    )
    if behavior_models is not None:
        mismatched = unknown_operating_modes(
            behavior_models,
            components.config.preprocessing.operating_modes,
        )
        if mismatched:
            log.warning(
                "behavior models gated on operating modes the live segmenter "
                "can't emit — they will NEVER evaluate; add the modes to "
                "preprocessing.operating_modes",
                extra={"models": mismatched},
            )
        diagnostic = diagnostic_only_models(behavior_models)
        if diagnostic:
            log.warning(
                "behavior models loaded diagnostic-only — evaluated and shown, "
                "but they will not raise alerts until refit with a serving "
                "contract and held-out validation",
                extra={"models": diagnostic},
            )
    accepted_baselines = load_accepted_baselines(accepted_baselines_path)
    # The training feature pipeline decodes CAN twice — bindings.yaml
    # (decoded:*) AND the DBC files (dbc:*). The live source must mirror both
    # or every inference tick fails on the model's required dbc:* columns.
    dbc_database = load_dbc_dir(DEFAULT_DBC_DIR)
    # Mode-22 DID map — lets the live source decode raw DID_<hex> poll
    # responses into named did:<NAME> values, matching the offline
    # build_feature_frame path so did:* feature columns line up.
    did_map = load_did_map(DEFAULT_DIDS_PATH) if DEFAULT_DIDS_PATH.exists() else None
    return LiveFeatureSource(
        config=components.config,
        bindings=bindings,
        behavior_models=behavior_models,
        accepted_baselines=accepted_baselines,
        dbc_database=dbc_database,
        did_map=did_map,
    )


__all__ = [
    "build_live_feature_source",
    "run_live_inference",
    "_finite_or_none",
    "_sequence_observe",
    "_observe_behavior",
    "_publish_score",
    "_score_and_emit",
    "INFERENCE_WORKER_THREAD_PREFIX",
    "LiveInferencePipeline",
]
