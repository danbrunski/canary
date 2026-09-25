"""Runtime control for replay-into-collector simulation.

The existing :mod:`src.collection.replay` backend re-emits a recorded
session onto the live telemetry queue so the daemon exercises its full
pipeline (drain → Parquet → inference → emitter → broadcaster) with no
hardware. Until now it was activated only by a startup-time YAML knob.
:class:`ReplayController` exposes start/stop over the control plane and
isolates outputs under ``_replay-*`` paths so a replay never contaminates
real telemetry, training corpora, or anomaly feedback.

Isolation contract:

* Parquet writes go to ``<storage>/_replay-<replay_id>/``. The leading
  underscore excludes the directory from
  :func:`src.preprocessing.loader.discover_sessions`, from the
  ``GET /control/sessions`` regex filter, and from the timestamp-based
  retention sweep.
* Anomaly JSONL writes go to ``<anomalies_dir>/_replay/<replay_id>.jsonl``
  (one file per replay, no daily rotation). The ``_replay/`` subdir is
  outside :func:`src.alerts.event_logger.read_events_dir`'s glob, so
  feedback / refit jobs never see replay anomalies as real.
* WebSocket broadcasts still fire normally so the dashboard surfaces the
  in-progress replay live.
* **In-memory isolation (Phase 7.1).** Storage/JSONL isolation above says
  nothing about the daemon's live in-memory state — replay re-emits onto
  the *same* queue the live collectors feed, so it flows through the same
  ``LiveFeatureSource`` buffer, ``PersistenceGate`` M-of-N streaks, and
  emitter dedupe/counters. ``start``/``_reap_finished_task`` reset all
  three on both replay boundaries (entering AND exiting) so a live drive's
  state can't leak into a replay run's results, and a replay's tail state
  can't leak back into live once it stops.
* **Live producers are withheld for the whole replay (D02.9).** Collectors
  feed the queue through a closable ingress (``src.daemon.ingress``). ``start``
  closes it *before* anything switches, lets the pipeline settle so live
  records already queued land in the live session, and only then redirects
  the writer; ``stop`` lets the replay's own tail settle into ``_replay-*``
  before switching back, then reopens the ingress. Withheld live rows are
  counted per source in ``live_rows_withheld``. On 2026-09-11 the Pi's own GPS
  put ~22,800 rows into a replay's output because nothing did this.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.event_logger import AnomalyEventLogger
from src.alerts.fusion import PersistenceGate
from src.collection import capture_generation
from src.collection.replay import ReplayEmissionStats, replay_session
from src.control.capture_manifest import read_capture_manifest
from src.session import SessionKind, format_session_timestamp, session_kind
from src.storage.async_writer import AsyncParquetTelemetryWriter, ensure_async_writer
from src.storage.parquet_writer import ParquetTelemetryWriter
from src.telemetry import TelemetryRecord

if TYPE_CHECKING:
    # Annotation only: importing it loads the feature pipeline, and this
    # module is on the daemon's path to recording (S01.3).
    from src.preprocessing.live import LiveFeatureSource

log = logging.getLogger(__name__)


#: How long a replay boundary waits for already-queued records to be written
#: before it switches directories anyway (and says so in the status).
BOUNDARY_SETTLE_SECONDS = 5.0


class LiveInputGate(Protocol):
    """What the controller needs of the live collectors' ingress
    (``src.daemon.ingress.LiveIngress``)."""

    def close(self, reason: str) -> None: ...

    def open(self) -> dict[str, int]: ...

    def withheld(self) -> dict[str, int]: ...


#: ``PipelineBarrier.settle``: join every queue in the pipeline, bounded.
PipelineSettle = Callable[[float], Awaitable[bool]]


class ReplayError(RuntimeError):
    """Base class for replay control failures."""


class ReplayConflictError(ReplayError):
    """A replay is already running."""


class ReplayNotFoundError(ReplayError):
    """The requested source session doesn't exist."""


class ReplayNotRunningError(ReplayError):
    """``stop`` was called with no replay in flight."""


@dataclass(frozen=True)
class ReplayStatus:
    """Snapshot of the controller state for ``GET /control/replay``.

    The row counters (D02.9) are live: they come from the in-flight replay's
    :class:`~src.collection.replay.ReplayEmissionStats` every time
    :meth:`ReplayController.status` is called. Reporting them is the point —
    D02.9's "the replay wrote more rows than its input" was an inference from
    chunk counts, made because nothing published what was emitted.
    ``rederivable`` says which of the two emit policies this replay is running
    under (see :func:`src.collection.replay.probe_rederivability`).

    ``speed`` is the *requested* sleep multiplier only. Because every recorded
    gap is clamped at ``_MAX_GAP_SECONDS``, the replay covers recording time
    faster than that number says — 18.9x on one real session at ``speed: 1.0``
    (D02.9). ``effective_speed`` is the measured ratio, ``source_time_progress``
    the fraction of the recording covered, and ``pass_reconciled`` whether a
    completed pass accounts for every source row as emitted, skipped or dropped.
    """

    running: bool
    replay_id: str | None = None
    source_session_id: str | None = None
    started_at: str | None = None
    speed: float | None = None
    effective_speed: float | None = None
    source_span_s: float | None = None
    source_time_emitted_s: float | None = None
    source_time_progress: float | None = None
    wall_elapsed_s: float | None = None
    pass_reconciled: bool | None = None
    loop: bool | None = None
    output_session_dir: str | None = None
    output_anomalies_path: str | None = None
    source_rows_total: int | None = None
    rows_emitted: int | None = None
    rows_skipped: int | None = None
    rows_dropped: int | None = None
    passes: int | None = None
    rederivable: bool | None = None
    rederivable_reason: str | None = None
    skipped_sources: tuple[str, ...] | None = None
    #: R03.12 — the replayed session's capture profile and whether it may be
    #: scored by IF/sequence, read once from its ``capture_profile.json`` at
    #: start. A session without a capture manifest is plain default-profile
    #: collection and is eligible. The inference gate reads this during a
    #: replay, never the daemon's own live profile, so a replayed tuning
    #: capture stays unscored however the daemon is configured.
    source_capture_profile: str | None = None
    source_corpus_eligible: bool = True
    #: D02.9 — live collector rows kept out of the pipeline while this replay
    #: ran, per source (``None``: no ingress wired, so live input was *not*
    #: isolated and the output may contain live rows).
    live_rows_withheld: dict[str, int] | None = None
    #: Whether the pipeline drained before the writer switched to the replay
    #: directory (entry) and back (exit). ``False`` means a boundary timed out
    #: and a few records may sit on the wrong side of it.
    entry_settled: bool | None = None
    exit_settled: bool | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "running": self.running,
            "replay_id": self.replay_id,
            "source_session_id": self.source_session_id,
            "started_at": self.started_at,
            "speed": self.speed,
            "effective_speed": self.effective_speed,
            "source_span_s": self.source_span_s,
            "source_time_emitted_s": self.source_time_emitted_s,
            "source_time_progress": self.source_time_progress,
            "wall_elapsed_s": self.wall_elapsed_s,
            "pass_reconciled": self.pass_reconciled,
            "loop": self.loop,
            "output_session_dir": self.output_session_dir,
            "output_anomalies_path": self.output_anomalies_path,
            "source_rows_total": self.source_rows_total,
            "rows_emitted": self.rows_emitted,
            "rows_skipped": self.rows_skipped,
            "rows_dropped": self.rows_dropped,
            "passes": self.passes,
            "rederivable": self.rederivable,
            "rederivable_reason": self.rederivable_reason,
            "skipped_sources": (
                None if self.skipped_sources is None else list(self.skipped_sources)
            ),
            "live_rows_withheld": (
                None if self.live_rows_withheld is None else dict(self.live_rows_withheld)
            ),
            "entry_settled": self.entry_settled,
            "exit_settled": self.exit_settled,
        }


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _is_live_session_name(name: str) -> bool:
    """True for ``YYYY-MM-DDTHHMMSS`` directory names (real captures)."""
    return session_kind(name) is SessionKind.LIVE


class ReplayController:
    """Manage the lifecycle of one in-flight replay.

    Construct once per daemon (held by :class:`DaemonComponents`). Not
    thread-safe; callers serialize start/stop via the asyncio event loop.
    """

    def __init__(
        self,
        *,
        queue: asyncio.Queue[TelemetryRecord],
        writer: ParquetTelemetryWriter | AsyncParquetTelemetryWriter,
        anomaly_logger: AnomalyEventLogger,
        storage_root: Path,
        live_source: LiveFeatureSource | None = None,
        persistence_gate: PersistenceGate | None = None,
        emitter: AnomalyEventEmitter | None = None,
        live_ingress: LiveInputGate | None = None,
        settle_pipeline: PipelineSettle | None = None,
    ) -> None:
        self._queue = queue
        # D02.9: the live collectors' door, and the pipeline barrier used at
        # both boundaries. Optional for callers without collectors (tests,
        # storage-only checks); ``run_daemon`` always wires both.
        self._live_ingress = live_ingress
        self._settle_pipeline = settle_pipeline
        self._writer = ensure_async_writer(writer)
        self._anomaly_logger = anomaly_logger
        self._storage_root = Path(storage_root)
        # In-memory state shared with live collection (Phase 7.1) — all
        # optional so tests/callers that don't wire live inference (e.g. no
        # model artifacts, storage-only checks) still work unchanged.
        self._live_source = live_source
        self._persistence_gate = persistence_gate
        self._emitter = emitter
        self._task: asyncio.Task[None] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._stats: ReplayEmissionStats | None = None
        self._status: ReplayStatus = ReplayStatus(running=False)

    def _reset_in_memory_state(self) -> None:
        """Clear/segregate live in-memory state at a replay boundary (7.1).

        Called on both entry to and exit from replay mode. Best-effort per
        piece, mirroring the writer/anomaly-logger exit contract below — one
        failing reset shouldn't block the other two.
        """
        if self._live_source is not None:
            try:
                self._live_source.reset()
            except Exception:
                log.exception("live_source.reset failed")
        if self._persistence_gate is not None:
            try:
                self._persistence_gate.reset()
            except Exception:
                log.exception("persistence_gate.reset failed")

    def status(self) -> ReplayStatus:
        """Return the current status snapshot.

        ``running`` reflects the controller's view. Replay task cleanup is
        scheduled on the event loop that started the replay. Row counters are
        read from the in-flight replay's stats on every call, so a poller sees
        emission progress rather than having to infer it from chunk counts.
        """
        return self._with_stats(self._status)

    async def _settle(self, boundary: str) -> bool | None:
        """Let already-queued records reach their sinks before a switch."""
        if self._settle_pipeline is None:
            return None
        settled = await self._settle_pipeline(BOUNDARY_SETTLE_SECONDS)
        if not settled:
            log.warning(
                "replay %s boundary: pipeline did not settle in %.0f s; switching anyway",
                boundary,
                BOUNDARY_SETTLE_SECONDS,
            )
        return settled

    def _with_stats(self, status: ReplayStatus) -> ReplayStatus:
        """Overlay the live emission counters onto a status snapshot."""
        if status.running and self._live_ingress is not None:
            status = replace(status, live_rows_withheld=self._live_ingress.withheld())
        stats = self._stats
        if stats is None:
            return status
        return replace(
            status,
            effective_speed=stats.effective_speed(),
            source_span_s=stats.source_span_s,
            source_time_emitted_s=stats.source_time_emitted_s,
            source_time_progress=stats.source_time_progress(),
            wall_elapsed_s=stats.wall_elapsed_s(),
            pass_reconciled=stats.pass_reconciled(),
            source_rows_total=stats.source_rows_total,
            rows_emitted=stats.rows_emitted,
            rows_skipped=stats.rows_skipped,
            rows_dropped=stats.rows_dropped,
            passes=stats.passes,
            rederivable=stats.rederivable,
            rederivable_reason=stats.rederivable_reason,
            skipped_sources=stats.skipped_sources,
        )

    async def start(
        self,
        source_session_id: str,
        *,
        speed: float = 1.0,
        loop: bool = False,
    ) -> ReplayStatus:
        """Start a replay of ``source_session_id`` into the live queue.

        ``source_session_id`` must be the directory name of an existing
        live session (timestamp form, no leading ``_``) under the
        configured storage root. Replays of other replay sessions are
        rejected to keep the isolation contract one-way.
        """
        if self._task is not None and not self._task.done():
            raise ReplayConflictError(f"replay {self._status.replay_id!r} already running")

        if not _is_live_session_name(source_session_id):
            raise ReplayNotFoundError(
                f"refusing to replay non-live-session name {source_session_id!r}"
            )
        source_dir = self._storage_root / source_session_id
        if not source_dir.is_dir():
            raise ReplayNotFoundError(f"session {source_session_id!r} not found")

        manifest = read_capture_manifest(source_dir)
        replay_id = format_session_timestamp(_utc_now())
        # D02.9: stop live input first, then let what is already queued land
        # in the live session, and only then redirect the writer.
        if self._live_ingress is not None:
            self._live_ingress.close(f"replay-{replay_id}")
        try:
            entry_settled = await self._settle("entry")
            await self._writer.enter_replay_mode(replay_id)
        except BaseException:
            if self._live_ingress is not None:
                self._live_ingress.open()
            raise
        try:
            anomalies_path = self._anomaly_logger.enter_replay_mode(replay_id)
        except Exception:
            await self._writer.exit_replay_mode()
            if self._live_ingress is not None:
                self._live_ingress.open()
            raise

        # 7.1 — reset in-memory state now that storage/JSONL are isolated,
        # so the replay run starts from a clean live buffer / persistence
        # streak / dedupe view instead of inheriting whatever live was doing
        # right before this call.
        self._reset_in_memory_state()
        if self._emitter is not None:
            try:
                self._emitter.enter_replay_mode()
            except Exception:
                log.exception("emitter.enter_replay_mode failed")

        stop_event = asyncio.Event()
        speed = speed if speed > 0 else 1.0
        stats = ReplayEmissionStats()
        task = asyncio.create_task(
            replay_session(
                self._queue,
                source_dir,
                stop_event,
                speed=speed,
                loop=loop,
                stats=stats,
            ),
            name=f"replay-{replay_id}",
        )
        self._task = task
        self._stop_event = stop_event
        self._stats = stats
        task.add_done_callback(self._schedule_cleanup)
        # Live polling and the replayed rows must never share a round (C02.8).
        capture_generation.bump(f"replay-start:{replay_id}")
        self._status = ReplayStatus(
            running=True,
            replay_id=replay_id,
            source_session_id=source_session_id,
            started_at=_utc_now().isoformat(),
            speed=speed,
            loop=loop,
            output_session_dir=str(self._storage_root / f"_replay-{replay_id}"),
            output_anomalies_path=str(anomalies_path),
            source_capture_profile=None if manifest is None else manifest.profile,
            source_corpus_eligible=True if manifest is None else manifest.corpus_eligible,
            live_rows_withheld=None if self._live_ingress is None else {},
            entry_settled=entry_settled,
        )
        log.info(
            "replay started",
            extra={
                "replay_id": replay_id,
                "source": source_session_id,
                "speed": speed,
                "loop": loop,
            },
        )
        return self.status()

    async def stop(self) -> ReplayStatus:
        """Cancel the in-flight replay and roll the writer/logger back live."""
        if self._task is None or self._task.done():
            await self._reap_finished_task()
            if not self._status.running:
                raise ReplayNotRunningError("no replay in flight")

        assert self._task is not None
        assert self._stop_event is not None
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except TimeoutError:
            log.warning(
                "replay task didn't stop in 5s; cancelling",
                extra={"replay_id": self._status.replay_id},
            )
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

        if self._cleanup_task is not None:
            await self._cleanup_task
        else:
            await self._reap_finished_task()
        return self._status

    def _schedule_cleanup(self, _task: asyncio.Task[None]) -> None:
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._reap_finished_task())

    async def _reap_finished_task(self) -> None:
        """Reset state + roll writer/logger back to live mode after replay ends."""
        task = self._task
        if task is None or not task.done():
            return
        self._task = None
        self._stop_event = None
        capture_generation.bump("replay-stop")
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            exc = None
        if exc is not None:
            log.error(
                "replay task raised",
                exc_info=(type(exc), exc, exc.__traceback__),
                extra={"replay_id": self._status.replay_id},
            )
        # D02.9: the replay's own tail belongs in its directory — settle
        # before switching back. Live input stays withheld until after.
        try:
            exit_settled = await self._settle("exit")
        except Exception:
            log.exception("replay exit settle failed")
            exit_settled = False
        try:
            await self._writer.exit_replay_mode()
        except Exception:
            log.exception("writer.exit_replay_mode failed")
        try:
            self._anomaly_logger.exit_replay_mode()
        except Exception:
            log.exception("anomaly_logger.exit_replay_mode failed")
        # 7.1 — clear the replay's own buffer/streak/dedupe state so none of
        # it bleeds back into live once this replay is done; restores the
        # emitter's pre-replay counters/dedupe (see enter_replay_mode).
        self._reset_in_memory_state()
        if self._emitter is not None:
            try:
                self._emitter.exit_replay_mode()
            except Exception:
                log.exception("emitter.exit_replay_mode failed")
        withheld = None if self._live_ingress is None else self._live_ingress.open()
        # Bake the final counters into the idle status (and the log) instead of
        # dropping them: "how many rows did that replay actually emit" is the
        # question D02.9 had to reconstruct from chunk counts.
        stats = self._stats
        if stats is not None:
            log.info(
                "replay finished",
                extra={
                    "replay_id": self._status.replay_id,
                    "rows": stats.rows_emitted,
                    "skipped": stats.rows_skipped,
                    "dropped": stats.rows_dropped,
                    "source_rows": stats.source_rows_total,
                    "passes": stats.passes,
                    "rederivable": stats.rederivable,
                    "reconciled": stats.pass_reconciled(),
                    "source_time_s": round(stats.source_time_emitted_s, 3),
                    "effective_speed": stats.effective_speed(),
                    "requested_speed": self._status.speed,
                    "live_rows_withheld": withheld,
                    "entry_settled": self._status.entry_settled,
                    "exit_settled": exit_settled,
                },
            )
        self._status = self._with_stats(
            ReplayStatus(
                running=False,
                live_rows_withheld=withheld,
                entry_settled=self._status.entry_settled,
                exit_settled=exit_settled,
            )
        )
        self._stats = None


__all__ = [
    "BOUNDARY_SETTLE_SECONDS",
    "LiveInputGate",
    "PipelineSettle",
    "ReplayController",
    "ReplayStatus",
    "ReplayError",
    "ReplayConflictError",
    "ReplayNotFoundError",
    "ReplayNotRunningError",
]
