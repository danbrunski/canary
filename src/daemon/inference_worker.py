"""Out-of-process model scoring — the follow-up C08 could not measure.

C08 moved the IF and sequence forward passes off the event loop into a worker
*thread*. On the Pi that turned out not to be enough, because the GIL makes
"off the loop" and "off the CPU" different things.

Measured on the truck 2026-09-09 at ``engine_running``
(``data/analyses/2026-09-09-pending-hardware.md``): the daemon missed **306 of
633** 1 Hz ticks while ``load_1m`` was 1.52 on **four** cores and CPU sat at
24 % — two and a half cores idle. One Python process is bound to roughly one
core, so a 30 s compute spike starved the collector thread and the HTTP server
sharing that process: collection stopped for ~100 s at a stretch and a
concurrent ``fuel-map`` capture scored **0 of 468** complete tuples with a
perfectly healthy 3.957 Hz in-sample rate. Nothing overflowed — the collector
thread simply never got scheduled.

So the scoring moves to a child process, where a slow model costs a core
instead of the daemon. Only the one-row feature frame and a version tag cross
the boundary; the models are loaded child-side from the same artifact paths
the parent watches.

**Exactly one persistent worker, never a pool.** The sequence engine owns a
rolling window that is only ever advanced by a scoring pass, so the window is
*state*: a second worker would score against a different history, and a
worker restarted mid-drive resumes with an empty window (it refills, and
``_sequence_observe`` already returns ``None`` until it is full).

The parent keeps every side effect. The child touches no broadcaster, no
emitter, no persistence gate and no storage — it returns the same
``_TickResult`` the in-process path returned, and ``LiveInferencePipeline._apply``
publishes it on the loop exactly as before.

Failure is never fatal: if the child cannot start, dies, or answers late, the
pipeline falls back to the in-process executor. A degraded detector is worth
less than a stalled collector, which is the trade this whole module exists to
make.

**The one real constraint: the child only ever sees models that came from
disk.** It rebuilds its managers from the config's artifact paths, so a parent
holding a model that did *not* come from there — an injected fake, anything
that populates ``model_managers`` itself — would have the child scoring
against something else, or against nothing at all, and saying so nowhere.
That is why ``models.out_of_process_scoring`` ships **off**: a deployment
whose models do come from disk opts in, and nothing else changes behaviour.
Found by turning it on by default and watching every ``run_live_inference``
end-to-end test complete zero ticks — the parent's stub models were invisible
to the child, so tick 0 blocked and every later tick skipped as
``compute_busy``.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import time
from dataclasses import dataclass
from multiprocessing.connection import Connection
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.daemon.inference import _TickResult

log = logging.getLogger(__name__)

#: How long the parent waits for a child result before giving up on the
#: worker for this tick. Deliberately generous — the whole point is to
#: tolerate a slow model — but bounded, so a wedged child cannot hold a tick
#: forever. The parent skips (and counts) rather than blocking the loop.
RESULT_TIMEOUT_SECONDS = 30.0

#: Grace period for the child to exit on shutdown before it is terminated.
SHUTDOWN_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class WorkerTick:
    """One scoring request. Carries data, never a model.

    ``if_version`` / ``sequence_version`` are the versions the *parent*
    resolved for this tick. The child reloads from disk when they differ from
    what it holds, so a hot-swap propagates without shipping an artifact
    through the pipe.
    """

    index: int
    row: Any
    want_if: bool
    want_sequence: bool
    if_version: str | None
    sequence_version: str | None


@dataclass(frozen=True)
class WorkerReply:
    """One scoring result, or the error that replaced it."""

    index: int
    if_computed: Any = None
    sequence_result: Any = None
    durations: dict[str, float] | None = None
    error: str | None = None


def _worker_main(conn: Connection, config: Any) -> None:  # pragma: no cover - child
    """Child entry point: own the models, score what arrives, send it back.

    Runs under ``spawn``, so nothing is inherited — the parent's *live*
    config object is pickled across instead of a path. That distinction
    matters: ``main`` runs ``scope_config`` to partition every vehicle-
    specific path under the active profile slug, so a child that re-read the
    YAML would look for artifacts in an unscoped ``models/`` and score with
    nothing. Forking would inherit the loaded models for free, but forking a
    process running asyncio plus a thread pool inherits whatever locks those
    threads hold, and torch is unforgiving about it.
    """
    try:
        _serve(conn, config)
    except (KeyboardInterrupt, EOFError):
        pass
    except Exception:
        log.exception("inference worker died")
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - shutting down anyway
            pass


def _serve(conn: Connection, config: Any) -> None:  # pragma: no cover - child
    from src.daemon.components import _load_model_managers
    from src.daemon.inference import (
        _build_sequence_engine,
        _if_compute,
        _sequence_observe,
        _suppress_channels_for,
    )

    managers = _load_model_managers(config)
    if_manager = managers.get("isolation_forest")

    sequence_type: str | None = None
    for name in managers:
        if name != "isolation_forest":
            sequence_type = name
            break
    sequence_manager = managers.get(sequence_type) if sequence_type else None

    # A stand-in for the ``components`` argument ``_suppress_channels_for``
    # takes: it reads nothing but ``.config``.
    config_holder = type("_ConfigHolder", (), {"config": config})()

    engine: Any = None
    engine_version: str | None = None

    while True:
        try:
            msg = conn.recv()
        except EOFError:
            return
        if msg is None:
            return
        if not isinstance(msg, WorkerTick):
            continue

        durations: dict[str, float] = {}
        if_computed: Any = None
        sequence_result: Any = None
        error: str | None = None
        started = time.perf_counter()

        try:
            if msg.want_if and if_manager is not None:
                loaded = if_manager.current
                if loaded is None or loaded.serving_version != msg.if_version:
                    if_manager.poll()
                    loaded = if_manager.current
                if loaded is not None:
                    stage = time.perf_counter()
                    if_computed = _if_compute(loaded, msg.row)
                    durations["isolation_forest"] = time.perf_counter() - stage

            if msg.want_sequence and sequence_manager is not None:
                loaded_seq = sequence_manager.current
                if loaded_seq is None or loaded_seq.serving_version != msg.sequence_version:
                    sequence_manager.poll()
                    loaded_seq = sequence_manager.current
                if loaded_seq is not None:
                    version = loaded_seq.serving_version
                    if engine_version != version:
                        engine = _build_sequence_engine(
                            loaded_seq,
                            suppress_channels=_suppress_channels_for(config_holder, loaded_seq),
                        )
                        engine_version = version if engine is not None else None
                    if engine is not None:
                        stage = time.perf_counter()
                        sequence_result = _sequence_observe(engine, msg.row)
                        durations["sequence"] = time.perf_counter() - stage
        except Exception as exc:  # noqa: BLE001 - report, never take the worker down
            error = f"{type(exc).__name__}: {exc}"
            log.exception("inference worker failed scoring tick %d", msg.index)

        durations["compute"] = time.perf_counter() - started
        conn.send(
            WorkerReply(
                index=msg.index,
                if_computed=if_computed,
                sequence_result=sequence_result,
                durations=durations,
                error=error,
            )
        )


class InferenceWorker:
    """Parent-side handle on the scoring child.

    Blocking by design: it is called from ``run_in_executor``, so the wait
    happens on the pipeline's existing worker thread rather than the event
    loop. What changes versus the old thread-only path is *where the model
    runs* — the wait itself is a pipe read, which releases the GIL.
    """

    def __init__(self, config: Any) -> None:
        #: The parent's live, profile-scoped config — not a path. See
        #: :func:`_worker_main`.
        self._config = config
        self._proc: mp.process.BaseProcess | None = None
        self._conn: Connection | None = None
        self._failures = 0

    @property
    def alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.is_alive()

    def start(self) -> bool:
        """Spawn the child. ``False`` means the caller should stay in-process."""
        try:
            ctx = mp.get_context("spawn")
            parent_conn, child_conn = ctx.Pipe()
            proc = ctx.Process(
                target=_worker_main,
                args=(child_conn, self._config),
                name="canary-inference",
                daemon=True,
            )
            proc.start()
            child_conn.close()
        except Exception:  # noqa: BLE001 - a missing worker is not fatal
            log.exception("could not start the inference worker; scoring stays in-process")
            return False
        self._proc = proc
        self._conn = parent_conn
        log.info("inference worker started (pid %s)", proc.pid)
        return True

    def compute(self, tick: WorkerTick) -> _TickResult | None:
        """Score one tick in the child. ``None`` means "fall back this tick"."""
        from src.daemon.inference import _TickResult

        conn = self._conn
        if conn is None or not self.alive:
            return None
        try:
            conn.send(tick)
            if not conn.poll(RESULT_TIMEOUT_SECONDS):
                log.warning(
                    "inference worker did not answer tick %d within %.0fs; falling back in-process",
                    tick.index,
                    RESULT_TIMEOUT_SECONDS,
                )
                self._failures += 1
                return None
            reply = conn.recv()
        except (EOFError, BrokenPipeError, OSError):
            log.warning("inference worker pipe broke; falling back in-process")
            self._failures += 1
            return None

        if not isinstance(reply, WorkerReply):
            return None
        if reply.error is not None:
            log.warning("inference worker reported an error: %s", reply.error)
        self._failures = 0
        return _TickResult(
            if_computed=reply.if_computed,
            sequence_result=reply.sequence_result,
            durations=reply.durations or {},
        )

    def stop(self) -> None:
        """Ask the child to exit, then make sure it did."""
        conn, proc = self._conn, self._proc
        self._conn, self._proc = None, None
        if conn is not None:
            try:
                conn.send(None)
            except Exception:  # noqa: BLE001 - already gone
                pass
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        if proc is None:
            return
        proc.join(SHUTDOWN_TIMEOUT_SECONDS)
        if proc.is_alive():
            log.warning("inference worker did not exit; terminating")
            proc.terminate()
            proc.join(SHUTDOWN_TIMEOUT_SECONDS)


__all__ = [
    "InferenceWorker",
    "WorkerReply",
    "WorkerTick",
    "RESULT_TIMEOUT_SECONDS",
]
