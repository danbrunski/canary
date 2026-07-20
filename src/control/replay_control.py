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
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.event_logger import AnomalyEventLogger
from src.alerts.fusion import PersistenceGate
from src.collection.replay import replay_session
from src.preprocessing.live import LiveFeatureSource
from src.storage.async_writer import AsyncParquetTelemetryWriter, ensure_async_writer
from src.storage.parquet_writer import ParquetTelemetryWriter
from src.telemetry import TelemetryRecord

log = logging.getLogger(__name__)

_LIVE_SESSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}$")


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
    """Snapshot of the controller state for ``GET /control/replay``."""

    running: bool
    replay_id: str | None = None
    source_session_id: str | None = None
    started_at: str | None = None
    speed: float | None = None
    loop: bool | None = None
    output_session_dir: str | None = None
    output_anomalies_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "running": self.running,
            "replay_id": self.replay_id,
            "source_session_id": self.source_session_id,
            "started_at": self.started_at,
            "speed": self.speed,
            "loop": self.loop,
            "output_session_dir": self.output_session_dir,
            "output_anomalies_path": self.output_anomalies_path,
        }


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _is_live_session_name(name: str) -> bool:
    """True for ``YYYY-MM-DDTHHMMSS`` directory names (real captures)."""
    return bool(_LIVE_SESSION_RE.match(name))


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
    ) -> None:
        self._queue = queue
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
        scheduled on the event loop that started the replay.
        """
        return self._status

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

        replay_id = _utc_now().strftime("%Y-%m-%dT%H%M%S")
        await self._writer.enter_replay_mode(replay_id)
        try:
            anomalies_path = self._anomaly_logger.enter_replay_mode(replay_id)
        except Exception:
            await self._writer.exit_replay_mode()
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
        task = asyncio.create_task(
            replay_session(
                self._queue,
                source_dir,
                stop_event,
                speed=speed,
                loop=loop,
            ),
            name=f"replay-{replay_id}",
        )
        self._task = task
        self._stop_event = stop_event
        task.add_done_callback(self._schedule_cleanup)
        self._status = ReplayStatus(
            running=True,
            replay_id=replay_id,
            source_session_id=source_session_id,
            started_at=_utc_now().isoformat(),
            speed=speed,
            loop=loop,
            output_session_dir=str(self._storage_root / f"_replay-{replay_id}"),
            output_anomalies_path=str(anomalies_path),
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
        return self._status

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
        self._status = ReplayStatus(running=False)


__all__ = [
    "ReplayController",
    "ReplayStatus",
    "ReplayError",
    "ReplayConflictError",
    "ReplayNotFoundError",
    "ReplayNotRunningError",
]
