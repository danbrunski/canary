"""The brain's next-measurement proposals on the live tick (E03.4 on E04.1's seam).

:func:`src.analysis.measurement_plan.propose` decides, from the ranked
measurements of the open delivery investigation and the live state, whether a
capture would be worth proposing *now*. This module is where the daemon asks it.

What stays off the tick:

* **Everything that reads a file** -- the fault model, the measurement plan,
  the capture-profile catalog, the memory ledger and the investigation's
  ranking (:func:`~src.analysis.hypothesis_tracking.rank_investigation`, the
  order :func:`~src.analysis.hypothesis_tracking.update_investigation` would
  record) -- runs on **one worker thread**. The tick hands it a refresh every
  :data:`REFRESH_S` and never waits: while the worker is busy the tick counts
  ``busy_ticks`` and proposes from the ranking it already has. The ledger is
  re-ranked only when its file changed (size and mtime).
* **On the collector path** a record observer keeps the newest value and
  acquisition time of the handful of channels the plan's region predicates
  and instruments name: one dict lookup per record, bounded by that set.

On the tick: one :func:`propose` call over a few measurements, with each
reading's age on the wall clock, the vehicle state the tick already derived,
and the capture state ``/status`` reports. Published as ``/status``
``detectors.inference.proposals`` and ``/control/brain/status``
``live_proposals``.

It **writes nothing and acts on nothing**: a proposal is the brain's state,
shown on the dashboard. ``open_capture`` is never constructed while any
capture runs (:class:`~src.analysis.measurement_plan.Proposal` refuses it),
physical actions are ``after_drive`` review items, and nothing is delivered
to the driver. A vehicle without a fault model or measurement plan says
``not_declared``.
"""

from __future__ import annotations

import logging
import math
import time
from collections import Counter, deque
from collections.abc import Callable
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from src.analysis.delivery_investigation import open_investigation
from src.analysis.fault_model import FAULT_MODEL_FILE, FaultModel, load_fault_model
from src.analysis.hypothesis_tracking import MeasurementRanking, rank_investigation
from src.analysis.measurement_plan import (
    MEASUREMENT_PLAN_FILE,
    CaptureState,
    LiveContext,
    LiveReading,
    MeasurementPlan,
    Proposal,
    load_measurement_plan,
    propose,
)
from src.analysis.tuning_context import TuningContext
from src.collection import tuning_profile
from src.collection.capture_profiles import (
    CaptureProfileCatalog,
    CaptureProfileSources,
    load_capture_profiles,
)
from src.collection.obd_scheduler.spec import default_bus_budget
from src.config import CanaryConfig
from src.evidence.ledger import memory_ledger_for
from src.telemetry import TelemetryRecord
from src.vehicle.profiles import profile_dir

if TYPE_CHECKING:
    from src.daemon.components import DaemonComponents
    from src.daemon.inference_metrics import InferenceTimingRecorder
    from src.features.live import LiveFeatureSource

log = logging.getLogger(__name__)

#: ``/status`` ``detectors.inference.stages`` names: the loop's share and the
#: worker's load/re-rank.
STAGE_TICK = "proposals_tick"
STAGE_REFRESH = "proposals_refresh"
ROLE = "measurement_proposals"
#: Seconds between worker refreshes (catalog re-read; ledger re-ranked when
#: its file changed).
REFRESH_S = 60.0
#: ``open_capture`` proposals kept for review (each the tick it first appeared).
OPENED_KEPT = 16
WRITES = "nothing: proposals only; the operator opens any capture"


# ---------------------------------------------------------------------------
# What the worker loads


@dataclass(frozen=True)
class PlannerState:
    """Everything :func:`propose` needs besides the live state, or why not."""

    result: str
    detail: str = ""
    plan: MeasurementPlan | None = None
    plan_sha256: str | None = None
    model: FaultModel | None = None
    catalog: CaptureProfileCatalog | None = None
    default_budget: float = 0.0
    investigation: str | None = None
    ranking: MeasurementRanking | None = None
    ledger_stamp: tuple[int, int] | None = None
    ranked_at: str | None = None

    @property
    def ready(self) -> bool:
        return self.result == "proposing"

    def channels(self) -> frozenset[str]:
        """The specs the plan reads live: region predicates and instruments."""
        if self.plan is None:
            return frozenset()
        specs = {p.channel for region in self.plan.regions.values() for p in region.all}
        for entry in self.plan.measurements:
            specs.update(entry.acquisition.instruments)
        return frozenset(specs)


class Planner(Protocol):
    def refresh(self, previous: PlannerState | None) -> PlannerState: ...


class ConfigPlanner:
    """Loads the vehicle's fault model, plan and catalog, and ranks the open
    delivery investigation from the memory ledger. Worker side only."""

    def __init__(self, config: CanaryConfig) -> None:
        self._config = config

    def refresh(self, previous: PlannerState | None) -> PlannerState:
        config = self._config
        if config.scoped_profile is None:
            return PlannerState(
                "unscoped", "investigation memory is per vehicle; this config names none"
            )
        static = previous if previous is not None and previous.plan is not None else None
        if static is None:
            data = profile_dir(config)
            model_path, plan_path = data / FAULT_MODEL_FILE, data / MEASUREMENT_PLAN_FILE
            missing = [p.name for p in (model_path, plan_path) if not p.exists()]
            if missing:
                return PlannerState("not_declared", f"no {', '.join(missing)} for this vehicle")
            model, _model_sha = load_fault_model(model_path)
            plan_sha: str | None
            plan, plan_sha = load_measurement_plan(
                plan_path, model, mode01_wot=TuningContext.from_config(config).mode01_wot
            )
        else:
            assert static.model is not None and static.plan is not None
            model, plan, plan_sha = static.model, static.plan, static.plan_sha256
        catalog = load_capture_profiles(CaptureProfileSources.of(config.daemon), config.collection)
        budget = default_bus_budget(config.collection.obd_poll_rate_hz)
        ledger = memory_ledger_for(config)
        try:
            stat = ledger.path.stat()
            stamp: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            stamp = None
        if static is not None and stamp == static.ledger_stamp:
            investigation, ranking, ranked_at = (
                static.investigation,
                static.ranking,
                static.ranked_at,
            )
        else:
            record = None if stamp is None else open_investigation(ledger)
            investigation = None if record is None else record.id
            ranking = (
                None
                if record is None
                else rank_investigation(ledger, model, plan, record.observations).ranking
            )
            ranked_at = datetime.now(UTC).isoformat()
        if investigation is None:
            result, detail = (
                "no_open_investigation",
                "the memory holds no open delivery investigation",
            )
        else:
            result, detail = "proposing", ""
        return PlannerState(
            result,
            detail,
            plan=plan,
            plan_sha256=plan_sha,
            model=model,
            catalog=catalog,
            default_budget=budget,
            investigation=investigation,
            ranking=ranking,
            ledger_stamp=stamp,
            ranked_at=ranked_at,
        )


# ---------------------------------------------------------------------------
# The live side


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _watch_key(spec: str) -> tuple[str, str] | None:
    source, sep, name = spec.partition(":")
    return (source, name.casefold()) if sep and name else None


class LiveProposals:
    """Latest values on the collector side, one worker for loading and
    ranking, :func:`propose` on the tick."""

    def __init__(
        self,
        *,
        planner: Planner,
        recorder: InferenceTimingRecorder | None,
        executor: Executor | None = None,
        refresh_s: float = REFRESH_S,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._planner = planner
        self._recorder = recorder
        self._owns_executor = executor is None
        self._executor = (
            executor
            if executor is not None
            else ThreadPoolExecutor(max_workers=1, thread_name_prefix="proposals")
        )
        self._refresh_s = refresh_s
        self._clock = clock
        self._monotonic = monotonic
        self._future: Future[tuple[PlannerState, float]] | None = None
        self._state: PlannerState | None = None
        self._refreshed_at = -math.inf
        #: (source, casefolded channel) -> spec; replaced whole, read per record.
        self._watch: dict[tuple[str, str], str] = {}
        self._latest: dict[str, tuple[float, float]] = {}
        self._open_now: set[tuple[str, str]] = set()
        self.opened: deque[dict[str, Any]] = deque(maxlen=OPENED_KEPT)
        self.ticks = 0
        self.busy_ticks = 0
        self.refreshes = 0
        self.worker_failures = 0
        self.propose_failures = 0
        self.observe_failures = 0
        self._last_tick_ms = 0.0
        self._last_refresh_ms = 0.0
        self._failure: str | None = None
        self._block: dict[str, Any] = self._compose([], None, None)

    # -- collector side ----------------------------------------------------

    def attach(self, source: LiveFeatureSource) -> None:
        source.record_observers.append(self.observe)

    def observe(self, record: TelemetryRecord) -> None:
        try:
            spec = self._watch.get((record.source, record.channel.casefold()))
            if spec is None:
                return
            value = _number(record.value)
            if value is None:
                return
            acquired = _number(record.metadata.get("acquired_at"))
            if acquired is None:
                stamp = record.timestamp
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=UTC)
                acquired = stamp.timestamp()
            self._latest[spec] = (value, acquired)
        except Exception:  # noqa: BLE001 — proposals must never cost a record
            self.observe_failures += 1
            log.exception("live proposals failed to take a read")

    # -- loop side ---------------------------------------------------------

    def tick(self, *, vehicle_state: str | None, capture: CaptureState) -> None:
        """Propose from what is loaded; hand the worker a due refresh. Never waits."""
        started = time.perf_counter()
        self.ticks += 1
        future = self._future
        if future is not None and future.done():
            self._collect(future)
            future = None
        due = self._monotonic() - self._refreshed_at >= self._refresh_s
        if due:
            if future is not None:
                self.busy_ticks += 1
            else:
                self._refreshed_at = self._monotonic()
                self._future = self._executor.submit(self._refresh, self._state)
                if self._future.done():  # an inline executor: use it now
                    self._collect(self._future)
        proposals: list[Proposal] = []
        state = self._state
        if state is not None and state.ready:
            proposals = self._propose(state, vehicle_state, capture)
        self._block = self._compose(proposals, vehicle_state, capture)
        self._last_tick_ms = (time.perf_counter() - started) * 1000.0
        if self._recorder is not None:
            self._recorder.record_stage(STAGE_TICK, self._last_tick_ms / 1000.0)
            self._recorder.set_proposals(self.snapshot())

    def tick_live(self, vehicle_state: str | None) -> None:
        """:meth:`tick` with the capture state ``/status`` reports."""
        self.tick(
            vehicle_state=vehicle_state,
            capture=CaptureState.from_snapshot(tuning_profile.snapshot()),
        )

    def _propose(
        self, state: PlannerState, vehicle_state: str | None, capture: CaptureState
    ) -> list[Proposal]:
        assert state.plan is not None and state.model is not None and state.catalog is not None
        assert state.ranking is not None
        now = self._clock()
        readings = {
            spec: LiveReading(value, max(0.0, now - at))
            for spec, (value, at) in self._latest.items()
        }
        live = LiveContext(str(vehicle_state), readings, capture)
        try:
            proposals = propose(
                state.plan,
                state.model,
                state.catalog,
                state.ranking.measurements,
                live,
                default_budget=state.default_budget,
            )
        except Exception as exc:  # noqa: BLE001 — named in the block, never raised into the tick
            self.propose_failures += 1
            self._failure = f"{type(exc).__name__}: {exc}"
            log.exception("live proposals failed")
            return []
        self._failure = None
        self._note_opened(proposals)
        return proposals

    def _note_opened(self, proposals: list[Proposal]) -> None:
        now_open = {(p.measurement, p.residual) for p in proposals if p.kind == "open_capture"}
        at = datetime.now(UTC).isoformat()
        for p in proposals:
            key = (p.measurement, p.residual)
            if p.kind == "open_capture" and key not in self._open_now:
                self.opened.append(
                    {
                        "at": at,
                        "measurement": p.measurement,
                        "residual": p.residual,
                        "region": p.region,
                        "profile": p.profile,
                    }
                )
        self._open_now = now_open

    def _refresh(self, previous: PlannerState | None) -> tuple[PlannerState, float]:
        started = time.perf_counter()
        state = self._planner.refresh(previous)
        return state, time.perf_counter() - started

    def _collect(self, future: Future[tuple[PlannerState, float]]) -> None:
        self._future = None
        try:
            state, seconds = future.result(timeout=0)
        except Exception as exc:  # noqa: BLE001 — named in the block
            self.worker_failures += 1
            self._failure = f"{type(exc).__name__}: {exc}"
            log.exception("live proposals refresh failed")
            return
        self.refreshes += 1
        self._state = state
        self._failure = None
        self._last_refresh_ms = seconds * 1000.0
        watch = {key: spec for spec in state.channels() if (key := _watch_key(spec))}
        self._watch = watch
        if self._recorder is not None:
            self._recorder.record_stage(STAGE_REFRESH, seconds)

    def snapshot(self) -> dict[str, Any]:
        return dict(self._block)

    def close(self) -> None:
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)

    # -- publication -------------------------------------------------------

    def _compose(
        self,
        proposals: list[Proposal],
        vehicle_state: str | None,
        capture: CaptureState | None,
    ) -> dict[str, Any]:
        state = self._state
        if self._failure is not None:
            result, detail = "failed", self._failure
        elif state is None:
            result, detail = "starting", "the worker has not loaded the plan yet"
        else:
            result, detail = state.result, state.detail
        ranking = None if state is None else state.ranking
        plan = None if state is None else state.plan
        return {
            "enabled": True,
            "role": ROLE,
            "result": result,
            "detail": detail,
            "delivers_to_driver": False,
            "writes": WRITES,
            "vehicle_state": vehicle_state,
            "capture": None if capture is None else asdict(capture),
            "investigation": None if state is None else state.investigation,
            "method": None if ranking is None else ranking.method,
            "cost_unit": None if ranking is None else ranking.cost_unit,
            "missing_likelihoods": [] if ranking is None else list(ranking.missing_likelihoods),
            "proposals": [asdict(p) for p in proposals],
            "counts": dict(Counter(p.kind for p in proposals)),
            "opened": list(self.opened),
            "plan": None
            if plan is None or state is None
            else {
                "version": plan.version,
                "sha256": state.plan_sha256,
                "max_input_age_s": plan.max_input_age_s,
            },
            "ranked_at": None if state is None else state.ranked_at,
            "watched": sorted(self._watch.values()),
            "ticks": self.ticks,
            "busy_ticks": self.busy_ticks,
            "refreshes": self.refreshes,
            "worker_failures": self.worker_failures,
            "propose_failures": self.propose_failures,
            "observe_failures": self.observe_failures,
            "cost": {
                "last_tick_ms": round(self._last_tick_ms, 3),
                "last_refresh_ms": round(self._last_refresh_ms, 3),
                "stages": [STAGE_TICK, STAGE_REFRESH],
            },
        }


def build_live_proposals(
    components: DaemonComponents,
    source: LiveFeatureSource,
    recorder: InferenceTimingRecorder,
) -> LiveProposals:
    """The live proposals, attached to ``source``. Nothing here reads a file:
    the worker loads on the first tick."""
    proposals = LiveProposals(planner=ConfigPlanner(components.config), recorder=recorder)
    proposals.attach(source)
    recorder.set_proposals(proposals.snapshot())
    return proposals


__all__ = [
    "OPENED_KEPT",
    "REFRESH_S",
    "ROLE",
    "STAGE_REFRESH",
    "STAGE_TICK",
    "ConfigPlanner",
    "LiveProposals",
    "Planner",
    "PlannerState",
    "build_live_proposals",
]
