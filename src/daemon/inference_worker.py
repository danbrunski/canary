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
from collections import Counter
from collections.abc import Mapping
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
#: How long to wait for the child's *first* answer. It loads artifacts on that
#: tick (~2-7 s measured), so this cannot be tied to the tick period.
STARTUP_RESULT_TIMEOUT_SECONDS = 30.0
#: Longest a model load in the child (startup or hot-swap) may take before the
#: parent stops calling it a swap and treats the child as wedged (A01.6). A
#: load is 2-7 s measured; this is the startup allowance the first answer
#: always had.
SWAP_TIMEOUT_SECONDS = STARTUP_RESULT_TIMEOUT_SECONDS
#: After the child acknowledges a swap with versions other than the ones the
#: parent serves (a manifest landing between the two processes' polls), wait
#: this long before asking again rather than re-sending every tick.
SWAP_RETRY_SECONDS = 5.0
#: Steady-state ceiling when the caller gives no tick period. 30 s on a 1 s
#: tick is thirty consecutive `compute_busy` misses before the fallback even
#: starts (D02.3) — callers should pass `result_timeout_seconds` derived from
#: their interval instead of relying on this.
RESULT_TIMEOUT_SECONDS = 5.0
#: Steady-state timeout as a multiple of the tick period, floored at
#: `MIN_RESULT_TIMEOUT_SECONDS`. Generous enough that ordinary jitter never
#: trips it, small enough that a wedged child is detected in seconds.
RESULT_TIMEOUT_TICKS = 3.0
MIN_RESULT_TIMEOUT_SECONDS = 2.0

#: Grace period for the child to exit on shutdown before it is terminated.
SHUTDOWN_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class WorkerTick:
    """One scoring request. Carries data, never a model.

    ``if_version`` / ``sequence_version`` are the versions the *parent*
    resolved for this tick. A hot-swap propagates without shipping an
    artifact through the pipe: the parent sends a :class:`WorkerSwap` first
    and holds ticks back until the child acknowledges it (A01.6); the child
    still reloads on a tick whose versions differ from what it holds, as a
    backstop.
    """

    index: int
    row: Any
    want_if: bool
    want_sequence: bool
    if_version: str | None
    sequence_version: str | None
    #: Clear the child's sequence window before appending this row (A01.3):
    #: set when the pipeline's generation changed since the last scored tick,
    #: so a replay, capture or model change never shares a window with what
    #: came before it.
    reset_sequence: bool = False


@dataclass(frozen=True)
class WorkerReply:
    """One scoring result, or the error that replaced it."""

    index: int
    if_computed: Any = None
    sequence_result: Any = None
    durations: dict[str, float] | None = None
    error: str | None = None


@dataclass(frozen=True)
class WorkerSwap:
    """Load these versions before the next tick, then say so (A01.6).

    Sent instead of a tick when the parent serves a model version the child
    has not acknowledged. Loading takes 2-7 s against a reply timeout of about
    3 s, so a tick that carried the new version used to time out, fall back,
    and leave its late reply in the pipe for the next tick (A01.1). The parent
    now scores nothing on the child until :class:`WorkerReady` comes back.
    """

    request_id: int
    if_version: str | None
    sequence_version: str | None


@dataclass(frozen=True)
class WorkerReady:
    """The child holds these versions and can score. ``request_id`` 0 is the
    startup load; ``load_seconds`` is the child's own time spent loading."""

    request_id: int
    if_version: str | None
    sequence_version: str | None
    load_seconds: float
    error: str | None = None


@dataclass
class _PendingSwap:
    request_id: int
    started: float
    startup: bool


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

    started = time.perf_counter()
    state = _WorkerState(_load_model_managers(config), config)
    # Startup is a swap from nothing (A01.6): say so once the models are in
    # memory, so the parent never waits on a tick while the child is loading.
    conn.send(state.ready(0, load_seconds=time.perf_counter() - started))

    while True:
        try:
            msg = conn.recv()
        except EOFError:
            return
        if msg is None:
            return
        if isinstance(msg, WorkerSwap):
            conn.send(state.swap(msg))
        elif isinstance(msg, WorkerTick):
            conn.send(state.score(msg))


class _WorkerState:
    """What the scoring child holds: its model managers and one sequence engine.

    Split out of :func:`_serve` so the protocol — score a tick, load on a swap,
    acknowledge when ready — can be exercised in-process with fake managers;
    the pipe loop around it only dispatches.
    """

    def __init__(self, managers: Mapping[str, Any], config: Any) -> None:
        from src.daemon.detectors.sequence import (
            _build_sequence_engine,
            _forward_cadence_for,
            _suppress_channels_for,
        )

        self._build_engine = _build_sequence_engine
        self._suppress_for = _suppress_channels_for
        self._cadence_for = _forward_cadence_for
        self.if_manager = managers.get("isolation_forest")
        sequence_type = next((name for name in managers if name != "isolation_forest"), None)
        self.sequence_manager = managers.get(sequence_type) if sequence_type else None
        # A stand-in for the ``components`` argument ``_suppress_channels_for``
        # takes: it reads nothing but ``.config``.
        self._config_holder = type("_ConfigHolder", (), {"config": config})()
        self.engine: Any = None
        self.engine_version: str | None = None
        self._ensure_engine()

    @property
    def if_version(self) -> str | None:
        loaded = self.if_manager.current if self.if_manager is not None else None
        return loaded.serving_version if loaded is not None else None

    @property
    def sequence_version(self) -> str | None:
        manager = self.sequence_manager
        loaded = manager.current if manager is not None else None
        return loaded.serving_version if loaded is not None else None

    def _ensure_engine(self) -> None:
        """Build the sequence engine for whatever version is loaded now."""
        manager = self.sequence_manager
        loaded = manager.current if manager is not None else None
        if loaded is None:
            self.engine, self.engine_version = None, None
            return
        version = loaded.serving_version
        if self.engine_version == version and self.engine is not None:
            return
        # A06.6: the same forward cadence as the parent, from the same config
        # — the child's window is the one that advances every tick.
        self.engine = self._build_engine(
            loaded,
            suppress_channels=self._suppress_for(self._config_holder, loaded),
            forward_every_n_ticks=self._cadence_for(self._config_holder),
        )
        self.engine_version = version if self.engine is not None else None

    def ready(
        self, request_id: int, *, load_seconds: float, error: str | None = None
    ) -> WorkerReady:
        return WorkerReady(
            request_id=request_id,
            if_version=self.if_version,
            sequence_version=self.sequence_version,
            load_seconds=load_seconds,
            error=error,
        )

    def swap(self, msg: WorkerSwap) -> WorkerReady:
        """Load the versions the parent now serves, then acknowledge (A01.6).

        The parent sent no sequence rows while this ran, so the window is
        cleared even when the sequence model itself did not change: a window
        must never span the rows it missed.
        """
        started = time.perf_counter()
        error: str | None = None
        try:
            if self.if_manager is not None and self.if_version != msg.if_version:
                self.if_manager.poll()
            if self.sequence_manager is not None and self.sequence_version != msg.sequence_version:
                self.sequence_manager.poll()
            self._ensure_engine()
            if self.engine is not None:
                self.engine.reset()
        except Exception as exc:  # noqa: BLE001 - report, never take the worker down
            error = f"{type(exc).__name__}: {exc}"
            log.exception("inference worker failed loading for swap %d", msg.request_id)
        return self.ready(msg.request_id, load_seconds=time.perf_counter() - started, error=error)

    def score(self, msg: WorkerTick) -> WorkerReply:
        """Score one tick against the models this child holds."""
        from src.daemon.detectors.isolation_forest import _if_compute
        from src.daemon.detectors.sequence import _sequence_observe

        durations: dict[str, float] = {}
        if_computed: Any = None
        sequence_result: Any = None
        error: str | None = None
        started = time.perf_counter()

        try:
            if msg.want_if and self.if_manager is not None:
                if self.if_version != msg.if_version:
                    # The parent swaps before it sends a tick for a new
                    # version; this is the backstop for one that did not.
                    self.if_manager.poll()
                loaded = self.if_manager.current
                if loaded is not None:
                    stage = time.perf_counter()
                    if_computed = _if_compute(loaded, msg.row)
                    durations["isolation_forest"] = time.perf_counter() - stage

            if msg.want_sequence and self.sequence_manager is not None:
                if self.sequence_version != msg.sequence_version:
                    self.sequence_manager.poll()
                self._ensure_engine()
                if self.engine is not None and msg.reset_sequence:
                    self.engine.reset()
                if self.engine is not None:
                    stage = time.perf_counter()
                    sequence_result = _sequence_observe(self.engine, msg.row)
                    durations["sequence"] = time.perf_counter() - stage
        except Exception as exc:  # noqa: BLE001 - report, never take the worker down
            error = f"{type(exc).__name__}: {exc}"
            log.exception("inference worker failed scoring tick %d", msg.index)

        durations["compute"] = time.perf_counter() - started
        return WorkerReply(
            index=msg.index,
            if_computed=if_computed,
            sequence_result=sequence_result,
            durations=durations,
            error=error,
        )


class InferenceWorker:
    """Parent-side handle on the scoring child.

    Blocking by design: it is called from ``run_in_executor``, so the wait
    happens on the pipeline's existing worker thread rather than the event
    loop. What changes versus the old thread-only path is *where the model
    runs* — the wait itself is a pipe read, which releases the GIL.
    """

    def __init__(self, config: Any, *, result_timeout_seconds: float | None = None) -> None:
        #: The parent's live, profile-scoped config — not a path. See
        #: :func:`_worker_main`.
        self._config = config
        self._proc: mp.process.BaseProcess | None = None
        self._conn: Connection | None = None
        self._failures = 0
        self._answered_once = False
        self._result_timeout_seconds = (
            RESULT_TIMEOUT_SECONDS
            if result_timeout_seconds is None
            else max(MIN_RESULT_TIMEOUT_SECONDS, float(result_timeout_seconds))
        )
        #: Why the parent fell back to in-process scoring, cumulative (D02.3).
        #: `/status` shows these: before them a wedged child was visible only
        #: as a large `compute_wait` max, which is how a 63 s stall on
        #: 2026-09-10 recorded `compute_failures: 0`.
        self._fallbacks: Counter[str] = Counter()
        #: Replies that arrived for an earlier tick and were discarded (A01.1).
        #: A reply to a tick that timed out stays in the pipe; before this
        #: count existed it was handed to the *next* tick, and every tick after
        #: a timeout silently applied its predecessor's result.
        self._stale_replies = 0
        #: Model versions the child last acknowledged holding (A01.6), as
        #: ``(if_version, sequence_version)``. ``None`` until the first
        #: acknowledgement — only a handle wired by hand (tests) stays there,
        #: and it then sends every tick straight through, as before.
        self._child_versions: tuple[str | None, str | None] | None = None
        #: The load the child is doing now: startup, or a hot-swap. While set,
        #: no tick goes to the child and the sequence detector is not ready.
        self._swap: _PendingSwap | None = None
        self._next_swap_id = 1
        #: After a mismatched acknowledgement, no new swap before this time.
        self._swap_retry_after = 0.0
        self._swaps_completed = 0
        self._swap_mismatches = 0
        self._not_ready_ticks = 0
        self._startup_seconds: float | None = None
        self._last_swap_seconds: float | None = None
        self._max_swap_seconds: float | None = None
        self._last_load_seconds: float | None = None

    @property
    def fallbacks(self) -> dict[str, int]:
        """Cumulative fallback reasons; empty while the child is answering."""
        return dict(sorted(self._fallbacks.items()))

    @property
    def fallback_count(self) -> int:
        return sum(self._fallbacks.values())

    @property
    def stale_replies(self) -> int:
        """Late replies to earlier ticks, discarded rather than applied."""
        return self._stale_replies

    @property
    def swaps(self) -> dict[str, Any]:
        """Model loads in the child and the ticks they cost (A01.6).

        ``last_swap_seconds`` / ``max_swap_seconds`` are the parent's wall time
        from asking for a hot-swap to the child acknowledging it;
        ``last_load_seconds`` is the child's own load time for the latest
        acknowledgement, startup included. ``not_ready_ticks`` counts ticks the
        sequence detector skipped because a load was in progress — they are not
        timeouts and do not appear in :attr:`fallbacks`.
        """
        return {
            "in_progress": self._swap is not None,
            "startup_seconds": self._startup_seconds,
            "completed": self._swaps_completed,
            "last_swap_seconds": self._last_swap_seconds,
            "max_swap_seconds": self._max_swap_seconds,
            "last_load_seconds": self._last_load_seconds,
            "not_ready_ticks": self._not_ready_ticks,
            "version_mismatches": self._swap_mismatches,
        }

    @property
    def result_timeout_seconds(self) -> float:
        """The wait in force now — the startup allowance until first answer."""
        return (
            self._result_timeout_seconds
            if self._answered_once
            else max(self._result_timeout_seconds, STARTUP_RESULT_TIMEOUT_SECONDS)
        )

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
        # The child loads every model before it can score. It says when it is
        # done (WorkerReady 0); until then the parent treats the sequence
        # detector as not ready instead of blocking a tick on the load.
        self._swap = _PendingSwap(request_id=0, started=time.monotonic(), startup=True)
        log.info("inference worker started (pid %s)", proc.pid)
        return True

    def ready_for(self, tick: WorkerTick) -> bool:
        """Whether ``tick`` may go to the child now (A01.6).

        ``False`` means the child is loading models — at startup, or because
        the parent now serves a version the child has not acknowledged. The
        caller then scores the Isolation Forest in-process (one row, exact) and
        treats the sequence detector as not ready for this tick. That is not a
        timeout: nothing was sent, so no late reply can follow it into the pipe.

        The first tick that needs a version the child does not hold sends the
        :class:`WorkerSwap`; the child answers with :class:`WorkerReady` once
        loaded. A load longer than :data:`SWAP_TIMEOUT_SECONDS` is no longer a
        swap: it is counted as ``swap_timeout`` in :attr:`fallbacks`. A child
        that never finished starting then gets ticks again, where the ordinary
        reply timeout handles it; one that never finished a hot-swap is asked
        again after :data:`SWAP_RETRY_SECONDS`, and the sequence detector stays
        not ready meanwhile.
        """
        conn = self._conn
        if conn is None or not self.alive:
            return True  # compute() counts it as worker_down
        try:
            acknowledged = self._drain_acknowledgements(conn)
        except (EOFError, BrokenPipeError, OSError):
            return True  # compute() finds the broken pipe and counts it
        now = time.monotonic()
        swap = self._swap
        if swap is not None:
            if now - swap.started <= SWAP_TIMEOUT_SECONDS:
                self._not_ready_ticks += 1
                return False
            log.warning(
                "inference worker has not acknowledged its model load after %.0fs; "
                "treating it as wedged",
                now - swap.started,
            )
            self._fallbacks["swap_timeout"] += 1
            self._swap = None
            self._swap_retry_after = now + SWAP_RETRY_SECONDS
        if self._child_versions is None or self._holds(tick):
            return True
        if acknowledged:
            # The child loaded, but not what this tick needs: a manifest landed
            # between the two processes' polls. Ask again shortly, not per tick.
            self._swap_mismatches += 1
            self._swap_retry_after = now + SWAP_RETRY_SECONDS
            log.warning(
                "inference worker acknowledged if=%s seq=%s; parent serves if=%s seq=%s",
                self._child_versions[0],
                self._child_versions[1],
                tick.if_version,
                tick.sequence_version,
            )
        self._not_ready_ticks += 1
        if now < self._swap_retry_after:
            return False
        request_id = self._next_swap_id
        self._next_swap_id += 1
        try:
            conn.send(
                WorkerSwap(
                    request_id=request_id,
                    if_version=tick.if_version,
                    sequence_version=tick.sequence_version,
                )
            )
        except (EOFError, BrokenPipeError, OSError):
            return True  # compute() finds the broken pipe and counts it
        self._swap = _PendingSwap(request_id=request_id, started=now, startup=False)
        log.info(
            "inference worker loading if=%s seq=%s; sequence detector not ready until it "
            "acknowledges",
            tick.if_version,
            tick.sequence_version,
        )
        return False

    def _holds(self, tick: WorkerTick) -> bool:
        """The child holds every version ``tick`` asks it to score with."""
        versions = self._child_versions
        if versions is None:
            return True
        if_version, sequence_version = versions
        return (not tick.want_if or tick.if_version == if_version) and (
            not tick.want_sequence or tick.sequence_version == sequence_version
        )

    def _drain_acknowledgements(self, conn: Connection) -> bool:
        """Read whatever the child has already sent, without waiting.

        Returns whether a load acknowledgement arrived. A reply to an earlier
        tick found here is late by definition and is discarded (A01.1).
        """
        acknowledged = False
        while conn.poll(0):
            msg = conn.recv()
            if isinstance(msg, WorkerReady):
                self._note_ready(msg)
                acknowledged = True
            elif isinstance(msg, WorkerReply):
                self._stale_replies += 1
        return acknowledged

    def _note_ready(self, msg: WorkerReady) -> None:
        """Record what the child now holds and how long loading it took."""
        self._child_versions = (msg.if_version, msg.sequence_version)
        self._last_load_seconds = msg.load_seconds
        if msg.error is not None:
            log.warning("inference worker reported a load error: %s", msg.error)
        swap = self._swap
        if swap is None or msg.request_id != swap.request_id:
            return  # late: the swap had already been given up on
        self._swap = None
        elapsed = time.monotonic() - swap.started
        if swap.startup:
            self._startup_seconds = elapsed
        else:
            self._swaps_completed += 1
            self._last_swap_seconds = elapsed
            self._max_swap_seconds = max(self._max_swap_seconds or 0.0, elapsed)
        log.info(
            "inference worker ready after %.2fs (child load %.2fs): if=%s seq=%s",
            elapsed,
            msg.load_seconds,
            msg.if_version,
            msg.sequence_version,
        )

    def compute(self, tick: WorkerTick) -> _TickResult | None:
        """Score one tick in the child. ``None`` means "fall back this tick"."""
        from src.daemon.inference import _TickResult

        conn = self._conn
        if conn is None or not self.alive:
            self._fallbacks["worker_down"] += 1
            return None
        timeout = self.result_timeout_seconds
        deadline = time.monotonic() + timeout
        try:
            conn.send(tick)
            # The child answers in order, so anything ahead of this tick's reply
            # is a late answer to a tick that already timed out. Discard those
            # and keep waiting on the same deadline; never apply another tick's
            # result (A01.1).
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not conn.poll(remaining):
                    log.warning(
                        "inference worker did not answer tick %d within %.1fs; "
                        "falling back in-process",
                        tick.index,
                        timeout,
                    )
                    self._failures += 1
                    self._fallbacks["timeout"] += 1
                    return None
                reply = conn.recv()
                if isinstance(reply, WorkerReady):
                    # A load acknowledgement that arrived after its swap was
                    # given up on: record what the child holds, keep waiting.
                    self._note_ready(reply)
                    continue
                if isinstance(reply, WorkerReply) and reply.index != tick.index:
                    self._stale_replies += 1
                    log.debug(
                        "discarding late inference reply for tick %d while waiting on tick %d",
                        reply.index,
                        tick.index,
                    )
                    continue
                break
        except (EOFError, BrokenPipeError, OSError):
            log.warning("inference worker pipe broke; falling back in-process")
            self._failures += 1
            self._fallbacks["pipe_error"] += 1
            return None

        if not isinstance(reply, WorkerReply):
            self._fallbacks["bad_reply"] += 1
            return None
        if reply.error is not None:
            log.warning("inference worker reported an error: %s", reply.error)
            self._fallbacks["worker_error"] += 1
        self._failures = 0
        self._answered_once = True
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
    "WorkerReady",
    "WorkerReply",
    "WorkerSwap",
    "WorkerTick",
    "RESULT_TIMEOUT_SECONDS",
    "SWAP_TIMEOUT_SECONDS",
]
