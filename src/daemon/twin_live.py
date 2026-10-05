"""The target-phi twin on the live tick: bounded, off the loop (E04.1/E04.2).

Reads reach the twin through :attr:`LiveFeatureSource.record_observers`, as
they reach the lambda delivery readout: the observer matches the twin's
declared channels, copies the read's value, acquisition stamp and timestamp
basis into a **bounded intake**, and returns. That is all the collector path
ever does for the twin.

The 1 Hz inference tick is only the publication cadence. Each tick hands the
intake to **one dedicated worker thread** and returns without waiting; the
worker runs :class:`~src.analysis.target_phi_stream.TwinStream` -- the stored
evaluator's own per-candidate judgement over a bounded window of reads, on the
acquisition clock (never one-second means) -- and the next tick publishes what
it decided. While the worker is still busy, ticks do not queue work: they are
counted as ``busy_ticks`` and the intake keeps filling up to its capacity,
past which reads are dropped and counted (``intake.dropped``). A stalled
consumer therefore costs counted reads, never memory or collector time.

**Which bytes the ECM read** comes from the resolver the stored brain job uses
(:class:`~src.control.twin_identity.TwinResolver`): the session's capture
manifest, else the flash history at the session's first sample, split into
segments at every later flash instant; the program from the image; one stream
per slot whose table resolves, and the same named refusal where it does not.
A read is routed to the segment its own acquisition time falls in; a segment
is flushed once the data clock is past its end, as a stored windowed
evaluation of that segment decides it. During a replay the recorded instants
ride on each record (:data:`~src.telemetry.REPLAY_SOURCE_ACQUIRED_KEY`,
:data:`~src.telemetry.REPLAY_SOURCE_AT_KEY`), so a replayed session is dated,
segmented and paired exactly as the stored session is -- not at emit time.

Everything slow happens on the worker, never the loop and never the
collector: the profile's twin definition, the identity resolution (published
as its own stage, ``twin_identity``) and the tables. The twin writes nothing:
no evidence, no learning state, no references -- so a replay, scoped to its
own ``_replay-*`` session key, cannot touch the live vehicle's memory. A
session change flushes the old session's streams and starts fresh ones. After
a restart the streams start empty: samples within their lookback are
unevaluable by name, never filled.

Off by default (``twin_live.enabled``): E04.3 measures its cost on the Pi
before live enablement. A prediction is the ECM's *command*; it is not a
delivery residual and never a fault.
"""

from __future__ import annotations

import logging
import math
import time
from collections import Counter, deque
from collections.abc import Callable
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np
import pandas as pd

from src.analysis.target_phi import (
    TWIN_FILE,
    ResolvedTable,
    TwinDefinition,
    TwinRefusalError,
    evaluated_specs,
)
from src.analysis.target_phi_stream import TwinStream
from src.control.twin_identity import (
    IdentitySegment,
    TwinIdentity,
    TwinResolver,
    candidate_slots,
)
from src.daemon.lambda_delivery_live import (
    UnsupportedSpecError,
    _basis,
    _SpecReader,
    _stored_seconds,
    spec_reader,
)
from src.decode.dids import DidMap, load_did_map
from src.paths import DIDS_YAML
from src.signals.pairing import to_seconds
from src.telemetry import REPLAY_SOURCE_ACQUIRED_KEY, REPLAY_SOURCE_AT_KEY, TelemetryRecord

if TYPE_CHECKING:
    from src.daemon.components import DaemonComponents
    from src.daemon.inference_metrics import InferenceTimingRecorder
    from src.features.live import LiveFeatureSource

log = logging.getLogger(__name__)

#: Stage names in ``/status`` ``detectors.inference.stages``: the loop's share
#: (hand-off and publication), the worker's evaluation, and the identity
#: resolution (registry/manifest/model reads, a possible ROM hash) apart.
STAGE_TICK = "twin_tick"
STAGE_EVAL = "twin_eval"
STAGE_IDENTITY = "twin_identity"
ROLE = "ecu_command_prediction"
WHAT_IT_IS = (
    "predicts the ECM's commanded equivalence ratio from its decoded Target AFR "
    "lookup and the observed lookup inputs; a difference is about the ECU computation, "
    "never fuel delivery, and a zero difference makes no engine-health claim"
)
#: Reads the intake holds between two worker hand-offs. At the twin's own
#: channels (~14, polled at up to ~15 Hz) a tick brings a few hundred.
INTAKE_CAPACITY = 8192
#: Data-clock seconds between re-reading the flash history and manifest. Flash
#: instants already in the history are segment bounds from the start; this
#: catches a flash *recorded* while a session runs.
IDENTITY_RECHECK_S = 60.0
#: Invalidations and finished segments kept for publication.
INVALIDATIONS_KEPT = 8


class Resolver(Protocol):
    """What the live twin needs from :class:`~src.control.twin_identity.TwinResolver`."""

    definition: TwinDefinition
    definition_sha256: str

    def segments(
        self, session: Path | None, *, first_read: float | None
    ) -> list[IdentitySegment]: ...

    def table(self, identity: TwinIdentity, slot: int) -> ResolvedTable: ...


ResolverFactory = Callable[[], Resolver]


# ---------------------------------------------------------------------------
# The bounded intake (collector side: append or count, nothing else)


Read = tuple[str, float, float, float, str]


class TwinIntake:
    """Reads of the twin's channels since the last hand-off, bounded."""

    def __init__(self, capacity: int = INTAKE_CAPACITY) -> None:
        self.capacity = capacity
        self.reads: list[Read] = []
        self.dropped = 0
        self.observed = 0
        self.failures = 0
        self._exact: dict[str, list[_SpecReader]] = {}
        self._folded: dict[str, list[_SpecReader]] = {}

    def watch(self, readers: list[_SpecReader]) -> None:
        exact: dict[str, list[_SpecReader]] = {}
        folded: dict[str, list[_SpecReader]] = {}
        for reader in readers:
            if reader.kind == "obd":
                folded.setdefault(reader.channel.casefold(), []).append(reader)
            else:
                exact.setdefault(reader.channel, []).append(reader)
        # Two assignments; the observer reads each once per record.
        self._exact, self._folded = exact, folded

    def observe(self, record: TelemetryRecord) -> None:
        if record.source != "obd":
            return
        readers = self._exact.get(record.channel) or self._folded.get(record.channel.casefold())
        if not readers:
            return
        acquired, basis = _basis(record)
        recorded = record.metadata.get(REPLAY_SOURCE_AT_KEY)
        if isinstance(recorded, (int, float)) and not isinstance(recorded, bool):
            # A replayed read: its recorded instants, not the emit-time rebase
            # (whose per-row shift carries the replay's pacing jitter).
            original = record.metadata.get(REPLAY_SOURCE_ACQUIRED_KEY)
            acquired = (
                float(original)
                if isinstance(original, (int, float)) and not isinstance(original, bool)
                else None
            )
            stored = float(recorded)
        else:
            stored = _stored_seconds(record.timestamp)
        for reader in readers:
            value = reader.value(record)
            if value is None:
                continue
            if len(self.reads) >= self.capacity:
                self.dropped += 1
                continue
            self.observed += 1
            self.reads.append(
                (reader.spec, math.nan if acquired is None else acquired, stored, value, basis)
            )

    def take(self) -> list[Read]:
        reads, self.reads = self.reads, []
        return reads


def _by_spec(reads: list[Read]) -> dict[str, tuple[np.ndarray, np.ndarray, list[str]]]:
    """The intake's reads per channel on the float acquisition clock, converted
    exactly as a session read converts them (``acquired_at`` through
    ``pd.to_datetime(unit="s")``; a read without one keeps its stored stamp)."""
    if not reads:
        return {}
    acquired = np.array([r[1] for r in reads], dtype=float)
    times = np.array([r[2] for r in reads], dtype=float)
    has = ~np.isnan(acquired)
    if has.any():
        times[has] = to_seconds(pd.to_datetime(pd.Series(acquired[has]), unit="s", utc=True))
    grouped: dict[str, list[int]] = {}
    for i, read in enumerate(reads):
        grouped.setdefault(read[0], []).append(i)
    return {
        spec: (
            times[index],
            np.array([reads[i][3] for i in index], dtype=float),
            [reads[i][4] for i in index],
        )
        for spec, index in grouped.items()
    }


# ---------------------------------------------------------------------------
# One session's segments and streams (worker side)


@dataclass
class _Segment:
    segment: IdentitySegment
    streams: dict[int, TwinStream] = field(default_factory=dict)
    refusals: dict[int, dict[str, Any]] = field(default_factory=dict)
    finished: bool = False
    started: str | None = None


@dataclass
class _Session:
    key: str
    directory: Path | None
    segments: list[_Segment] = field(default_factory=list)
    clock_s: float = -math.inf
    identity_checked_at: float = -math.inf
    late_closed_segment: int = 0


class _InlineExecutor(Executor):
    """Runs a submitted call at once (tests, and a host with no thread)."""

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        future: Future[Any] = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 — delivered through the future
            future.set_exception(exc)
        return future


RecordsSink = Callable[[int, int, list[dict[str, Any]]], None]


class LiveTwin:
    """Bounded intake on the collector side, one worker for the streams, and
    the ``/status`` block on the tick."""

    def __init__(
        self,
        *,
        resolver: ResolverFactory,
        recorder: InferenceTimingRecorder | None,
        did_map: DidMap | None,
        catalog: DidMap | None = None,
        session: str = "live",
        intake_capacity: int = INTAKE_CAPACITY,
        executor: Executor | None = None,
        identity_recheck_s: float = IDENTITY_RECHECK_S,
        on_records: RecordsSink | None = None,
    ) -> None:
        self._make_resolver = resolver
        self._recorder = recorder
        self._did_map = did_map
        self._catalog = catalog
        self._owns_executor = executor is None
        self._executor = (
            executor
            if executor is not None
            else ThreadPoolExecutor(max_workers=1, thread_name_prefix="twin")
        )
        self._recheck_s = identity_recheck_s
        self._on_records = on_records
        self.intake = TwinIntake(intake_capacity)
        self._future: Future[tuple[dict[str, Any], float, float, list[_SpecReader] | None]] | None
        self._future = None
        self.busy_ticks = 0
        self.batches = 0
        self.worker_failures = 0
        self._last_tick_ms = 0.0
        self._last_eval_ms = 0.0
        self._max_eval_ms = 0.0
        # Worker-owned from here down: touched only inside _consume.
        self._resolver: Resolver | None = None
        self._not_declared: str | None = None
        self._session = _Session(session, None)
        self.invalidations: deque[dict[str, Any]] = deque(maxlen=INVALIDATIONS_KEPT)
        self.finished: deque[dict[str, Any]] = deque(maxlen=INVALIDATIONS_KEPT)
        self.session_changes = 0
        self.reads_unevaluated = 0
        self._block: dict[str, Any] = self._compose(vehicle_state=None)

    # -- loop side -----------------------------------------------------------

    def attach(self, source: LiveFeatureSource) -> None:
        intake = self.intake

        def observe(record: TelemetryRecord) -> None:
            try:
                intake.observe(record)
            except Exception:  # noqa: BLE001 — the twin must never cost a record
                intake.failures += 1
                log.exception("live twin failed to take a read")

        source.record_observers.append(observe)

    def tick(
        self, *, session: str, vehicle_state: str | None, session_dir: Path | None = None
    ) -> None:
        """Publish what the worker decided and hand it the new reads. Never
        waits for the worker."""
        started = time.perf_counter()
        future = self._future
        if future is not None and not future.done():
            self.busy_ticks += 1
        else:
            if future is not None:
                self._collect(future)
            self.batches += 1
            self._future = self._executor.submit(
                self._consume, self.intake.take(), session, session_dir, vehicle_state, False
            )
            if self._future.done():  # an inline executor: publish now
                self._collect(self._future)
        self._last_tick_ms = (time.perf_counter() - started) * 1000.0
        if self._recorder is not None:
            self._recorder.record_stage(STAGE_TICK, self._last_tick_ms / 1000.0)
            self._recorder.set_twin(self.snapshot())

    def tick_live(self, components: DaemonComponents, vehicle_state: str | None) -> None:
        from src.daemon.plausibility_live import live_session_key

        session, replay_id = live_session_key(components)
        directory: Path | None = None
        if replay_id is not None and components.replay_controller is not None:
            source = components.replay_controller.status().source_session_id
            if source:
                directory = components.config.collection.storage_path / source
        elif components.storage_writer is not None:
            try:
                directory = components.storage_writer.paths.root
            except Exception:  # noqa: BLE001 — a writer between sessions has no paths
                directory = None
        self.tick(session=session, vehicle_state=vehicle_state, session_dir=directory)

    def flush(self, *, session: str | None = None, session_dir: Path | None = None) -> None:
        """Decide everything left (tests, a session's end); waits."""
        if self._future is not None:
            self._collect(self._future, wait=True)
        key = session or self._session.key
        directory = session_dir if session_dir is not None else self._session.directory
        future = self._executor.submit(
            self._consume, self.intake.take(), key, directory, None, True
        )
        self._collect(future, wait=True)

    def _collect(self, future: Future[Any], *, wait: bool = False) -> None:
        self._future = None
        try:
            block, seconds, identity_seconds, readers = (
                future.result() if wait else future.result(timeout=0)
            )
        except Exception:  # noqa: BLE001 — the twin is best-effort
            self.worker_failures += 1
            log.exception("live twin evaluation failed")
            return
        if readers is not None:
            self.intake.watch(readers)
        self._block = block
        self._last_eval_ms = seconds * 1000.0
        self._max_eval_ms = max(self._max_eval_ms, self._last_eval_ms)
        if self._recorder is not None:
            self._recorder.record_stage(STAGE_EVAL, seconds)
            if identity_seconds:
                self._recorder.record_stage(STAGE_IDENTITY, identity_seconds)

    def snapshot(self) -> dict[str, Any]:
        block = dict(self._block)
        block["intake"] = {
            "capacity": self.intake.capacity,
            "waiting": len(self.intake.reads),
            "observed": self.intake.observed,
            "dropped": self.intake.dropped,
            "observe_failures": self.intake.failures,
            "busy_ticks": self.busy_ticks,
            "batches": self.batches,
            "worker_failures": self.worker_failures,
        }
        block["cost"] = {
            "last_tick_ms": round(self._last_tick_ms, 3),
            "last_eval_ms": round(self._last_eval_ms, 3),
            "max_eval_ms": round(self._max_eval_ms, 3),
            "stages": [STAGE_TICK, STAGE_EVAL, STAGE_IDENTITY],
        }
        return block

    def close(self) -> None:
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)

    # -- worker side -----------------------------------------------------------

    def _consume(
        self,
        reads: list[Read],
        session: str,
        session_dir: Path | None,
        vehicle_state: str | None,
        flush: bool,
    ) -> tuple[dict[str, Any], float, float, list[_SpecReader] | None]:
        started = time.perf_counter()
        identity_seconds = 0.0
        readers: list[_SpecReader] | None = None
        if self._resolver is None and self._not_declared is None:
            began = time.perf_counter()
            readers = self._load()
            identity_seconds += time.perf_counter() - began
        if session != self._session.key:
            self._end_session("session_changed", f"now {session}")
            self._session = _Session(session, session_dir)
            self.session_changes += 1
        elif session_dir is not None and self._session.directory is None:
            self._session.directory = session_dir
        grouped = _by_spec(reads)
        if self._resolver is None:
            self.reads_unevaluated += sum(len(t) for t, _v, _b in grouped.values())
        elif grouped:
            current = self._session
            first = min(float(np.min(t)) for t, _v, _b in grouped.values())
            clock = max(float(np.max(t)) for t, _v, _b in grouped.values())
            if not current.segments or clock - current.identity_checked_at >= self._recheck_s:
                began = time.perf_counter()
                self._resolve(current, first if not current.segments else clock)
                identity_seconds += time.perf_counter() - began
            current.clock_s = max(current.clock_s, clock)
            self._route(current, grouped)
        for index, segment in enumerate(self._session.segments):
            if segment.finished:
                continue
            ended = segment.segment.end is not None and (
                self._session.clock_s - self._settle_s() >= segment.segment.end
            )
            if flush or ended:
                self._finish(index, segment)
            else:
                for slot, stream in segment.streams.items():
                    self._emit(index, slot, stream.advance())
        block = self._compose(vehicle_state=vehicle_state)
        return block, time.perf_counter() - started, identity_seconds, readers

    def _settle_s(self) -> float:
        from src.analysis.lambda_delivery_stream import DEFAULT_SETTLE_S

        return DEFAULT_SETTLE_S

    def _load(self) -> list[_SpecReader]:
        """The resolver (profile definition, model, viewer) and the channels to
        observe. A vehicle with no twin definition is ``not_declared``."""
        try:
            resolver = self._make_resolver()
        except FileNotFoundError as exc:
            self._not_declared = f"no {TWIN_FILE} for this vehicle: {exc}"
            return []
        self._resolver = resolver
        did_map = self._did_map if self._did_map is not None else self._catalog_map()
        readers = []
        for spec in evaluated_specs(resolver.definition):
            try:
                readers.append(spec_reader(spec, did_map))
            except UnsupportedSpecError:
                continue  # a gate source the live stream cannot supply: never observed
        return readers

    def _catalog_map(self) -> DidMap:
        if self._catalog is None:
            self._catalog = load_did_map(DIDS_YAML)
        return self._catalog

    def _resolve(self, current: _Session, at: float) -> None:
        """Resolve (or re-resolve) the session's identity segments with the
        shared resolver. A re-resolution that changes the identity in force
        from ``at`` on finishes the segment in force and continues on the new
        identity from ``at``; past decisions are never re-made."""
        assert self._resolver is not None
        current.identity_checked_at = at
        try:
            segments = self._resolver.segments(current.directory, first_read=at)
        except Exception as exc:  # noqa: BLE001 — named, not raised into the tick
            log.exception("live twin identity resolution failed")
            self.invalidations.append(
                {
                    "kind": "identity_unavailable",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "at": datetime.now(UTC).isoformat(),
                    "discarded_pending": 0,
                }
            )
            return
        if not current.segments:
            current.segments = [_Segment(s) for s in segments]
            return
        live = [s for s in current.segments if not s.finished]
        before = live[0].segment.identity if live else None
        after = next((s for s in segments if s.contains(at)), segments[-1])
        tail_now = [
            (s.segment.end, s.segment.identity)
            for s in live
            if s.segment.end is None or s.segment.end > at
        ]
        tail_new = [(s.end, s.identity) for s in segments if s.end is None or s.end > at]
        if tail_now == tail_new:
            return
        kind = (
            "program_changed"
            if before is not None and before.program_status != after.identity.program_status
            else "calibration_changed"
        )
        pending = 0
        for index, segment in enumerate(current.segments):
            if not segment.finished:
                pending += sum(s.undecided for s in segment.streams.values())
                self._finish(index, segment)
        self.invalidations.append(
            {
                "kind": kind,
                "detail": f"{None if before is None else before.to_dict()} -> "
                f"{after.identity.to_dict()}",
                "at": datetime.now(UTC).isoformat(),
                "flushed_pending": pending,
                "transient": "not carried across (not reconstructible)",
            }
        )
        tail = [s for s in segments if s.end is None or s.end > at]
        rebased = [
            IdentitySegment(at if i == 0 else s.start, s.end, s.identity)
            for i, s in enumerate(tail)
        ]
        current.segments += [_Segment(s) for s in rebased]

    def _route(
        self, current: _Session, grouped: dict[str, tuple[np.ndarray, np.ndarray, list[str]]]
    ) -> None:
        """Each read to the newest segment its own time falls in (vectorised);
        a read whose segment is already finished is counted, never folded in."""
        for spec, (times, values, basis) in grouped.items():
            unassigned = np.ones(len(times), dtype=bool)
            for index in range(len(current.segments) - 1, -1, -1):
                if not unassigned.any():
                    break
                segment = current.segments[index]
                bounds = segment.segment
                mask = unassigned.copy()
                if bounds.start is not None:
                    mask &= times >= bounds.start
                if bounds.end is not None:
                    mask &= times < bounds.end
                if not mask.any():
                    continue
                unassigned &= ~mask
                if segment.finished:
                    current.late_closed_segment += int(mask.sum())
                    continue
                self._open(segment)
                chosen = (
                    basis
                    if mask.all()
                    else [b for b, m in zip(basis, mask.tolist(), strict=True) if m]
                )
                for stream in segment.streams.values():
                    stream.extend(spec, times[mask], values[mask], chosen)
            current.late_closed_segment += int(unassigned.sum())

    def _open(self, segment: _Segment) -> None:
        """One stream per slot whose table resolves; the refusal otherwise."""
        if segment.started is not None:
            return
        assert self._resolver is not None
        resolver = self._resolver
        segment.started = datetime.now(UTC).isoformat()
        for slot in candidate_slots(resolver.definition):
            try:
                table = resolver.table(segment.segment.identity, slot)
            except TwinRefusalError as exc:
                segment.refusals[slot] = {"reason": exc.reason, "detail": exc.message}
                continue
            segment.streams[slot] = TwinStream(
                resolver.definition,
                table,
                definition_sha256=resolver.definition_sha256,
                catalog=self._catalog_map(),
            )

    def _emit(self, index: int, slot: int, records: list[dict[str, Any]]) -> None:
        if records and self._on_records is not None:
            self._on_records(index, slot, records)

    def _finish(self, index: int, segment: _Segment) -> None:
        for slot, stream in segment.streams.items():
            self._emit(index, slot, stream.flush())
        segment.finished = True
        if segment.started is not None:  # a segment the data never reached is not one
            self.finished.append(self._segment_block(segment))

    def _end_session(self, kind: str, detail: str) -> None:
        """Flush the old session's streams: they decide with the reads they hold,
        as the stored evaluation of that session does at its end."""
        pending = 0
        for index, segment in enumerate(self._session.segments):
            if not segment.finished:
                pending += sum(s.undecided for s in segment.streams.values())
                self._finish(index, segment)
        self.invalidations.append(
            {
                "kind": kind,
                "detail": detail,
                "at": datetime.now(UTC).isoformat(),
                "flushed_pending": pending,
                "transient": "not carried across (not reconstructible)",
            }
        )

    # -- publication -------------------------------------------------------------

    def _segment_block(self, segment: _Segment) -> dict[str, Any]:
        slots: dict[str, Any] = {}
        for slot, stream in segment.streams.items():
            summary = stream.summary()
            slots[str(slot)] = {
                "refusal": summary["refusal"],
                "accounting": summary["accounting"],
                "pending": summary["stream"]["pending"],
            }
        for slot, refusal in segment.refusals.items():
            slots[str(slot)] = {"refusal": refusal}
        return {**segment.segment.to_dict(), "finished": segment.finished, "slots": slots}

    def _compose(self, *, vehicle_state: str | None) -> dict[str, Any]:
        current = self._session
        block: dict[str, Any] = {
            "enabled": True,
            "role": ROLE,
            "alerts": False,
            "what": WHAT_IT_IS,
            "session": current.key,
            "replay": current.key.startswith("_replay-"),
            "vehicle_state": vehicle_state,
            "conclusions_require": (
                "read vehicle_state first: engine_off/ignition_only/no_data mean no command "
                "samples; replay means a stored session, not the truck"
            ),
            "writes": "nothing (no evidence, learning state or references)",
            "invalidations": list(self.invalidations),
            "session_changes": self.session_changes,
            # Segments the data has reached; a later flash instant is not one yet.
            "segments": [self._segment_block(s) for s in current.segments if s.started is not None][
                -INVALIDATIONS_KEPT:
            ],
            "finished_segments": list(self.finished),
            "late_closed_segment": current.late_closed_segment,
            "reads_unevaluated": self.reads_unevaluated,
        }
        resolver = self._resolver
        segment = next(
            (s for s in reversed(current.segments) if s.started is not None),
            None,
        )
        default = None if resolver is None else resolver.definition.map_identity.default_slot.slot
        stream = None if segment is None or default is None else segment.streams.get(default)
        if resolver is None or segment is None or stream is None:
            refusal = None
            if segment is not None and default is not None:
                refusal = segment.refusals.get(default)
            if self._not_declared is not None:
                result, reason, detail = "not_declared", "not_declared", self._not_declared
            elif refusal is not None:
                result, reason, detail = "withheld", refusal["reason"], refusal["detail"]
            elif resolver is None:
                result, reason = "starting", None
                detail = "the worker has not read the profile's twin definition yet"
            else:
                result, reason = "inputs_not_observed", None
                detail = (
                    "no read of the twin's channels in this session yet: identity is "
                    "resolved at the session's first sample"
                )
            block.update(
                result=result,
                reason=reason,
                detail=detail,
                identity=None if segment is None else segment.segment.identity.to_dict(),
                transient={"state": "unknown", "reason": "not_reconstructible"},
            )
            return block
        summary = stream.summary()
        states = Counter(summary["accounting"]["states"])
        if summary["refusal"] is not None:
            result, reason = "withheld", summary["refusal"]["reason"]
        else:
            result, reason = _result(states, stream, resolver.definition)
        block.update(
            result=result,
            reason=reason,
            identity={**segment.segment.identity.to_dict(), "slot": default},
            transient={
                "state": "unknown_before",
                "since": segment.started,
                "reason": "not_reconstructible",
                "detail": (
                    "no ECU state before this stream's first read is known (a restart, a "
                    "session or identity change starts empty); samples whose lookback "
                    "reaches before it are unevaluable by name, never filled"
                ),
            },
            applicability={
                "qualified": not summary["qualification_blockers"],
                "qualification_blockers": summary["qualification_blockers"],
                "capture_requirement": summary["capture_requirement"],
                "coverage": summary["coverage"],
                "coolant_sources": summary["coolant_sources"],
            },
            accounting=summary["accounting"],
            stream=summary["stream"],
            pairing=summary["pairing"],
            provenance=summary["provenance"],
            latest_prediction=summary["latest_prediction"],
            recent=summary["recent"],
        )
        return block


def _result(
    states: Counter[str], stream: TwinStream, definition: TwinDefinition
) -> tuple[str, str | None]:
    reference = definition.reference
    seen = stream.reads_seen
    if not seen[reference.channel] and not seen[reference.sentinel_channel]:
        return "inputs_not_observed", f"no read of {reference.channel} in this session"
    if sum(states.values()) == 0:
        return "pending", "reads arrived; samples are decided past the decision horizon"
    if states.get("eligible", 0):
        return "eligible", None
    return "no_eligible_samples", "every decided sample is unevaluable, excluded or pending"


# ---------------------------------------------------------------------------
# Daemon wiring


def build_live_twin(
    components: DaemonComponents,
    source: LiveFeatureSource,
    recorder: InferenceTimingRecorder,
) -> LiveTwin | None:
    """The live twin, or ``None`` with ``/status`` saying why. Nothing here
    reads a file: the worker preloads on its first hand-off."""
    config = components.config
    if not config.twin_live.enabled:
        recorder.set_twin(
            {
                "enabled": False,
                "result": "disabled",
                "reason": (
                    "twin_live.enabled is off: E04.3 measures the twin's cost on the Pi "
                    "before live enablement"
                ),
            }
        )
        return None

    def resolver() -> Resolver:
        return TwinResolver(config)

    twin = LiveTwin(
        resolver=resolver, recorder=recorder, did_map=source.did_map, catalog=source.did_map
    )
    twin.attach(source)
    recorder.set_twin(twin.snapshot())
    return twin


__all__ = [
    "INTAKE_CAPACITY",
    "ROLE",
    "STAGE_EVAL",
    "STAGE_IDENTITY",
    "STAGE_TICK",
    "LiveTwin",
    "Resolver",
    "TwinIntake",
    "build_live_twin",
]
