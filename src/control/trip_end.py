"""Trip-end orchestration (Task 5.4/5.5, eval-longitudinal-and-dtc.md).

The pure analysis/IO lives in :mod:`src.analysis.digest`; this is the
control-layer side-effecting half — the same split :mod:`src.control.trends`
draws against :mod:`src.analysis.trends`. Two triggers call
:func:`handle_trip_end_off_loop`, both best-effort from the daemon's point of
view:

* :class:`~src.analysis.gps_tracker.GpsTracker`'s sustained moving ->
  stationary edge (the normal case — the daemon's GPS sink calls this the
  tick the edge fires).
* Daemon shutdown (the fallback — the drive ends because the Pi lost power
  or was shut down mid-trip, with no clean GPS-detected stop first).

Idempotent either way: a session that already has a ``digest.json`` is
treated as already handled, so the fallback firing after the GPS edge
already ran (or vice versa) never double-computes or double-notifies.

**The digest never runs on the daemon's event loop, and never without a
memory ceiling.** Computing one loads the whole session twice
(``classify_session`` and ``compute_fingerprint`` each call
``resample_and_segment``) and peaks at O(rows x channels) because the wide
pivot runs at raw timestamp resolution before the 1 Hz resample — 5,483 MB
measured for 2,622,370 rows, to produce a 1,143-row frame. Inline on the
loop that has cost the daemon twice: a 5.86 M-row ``_replay-*`` output got it
OOM-killed on 2026-09-11, and a ~6.4 M-row ``_capture-tuning-*`` session
wedged it at 7.36 GB of 8 GB for 38 minutes on 2026-09-17, with no HTTP and
no collection, until it was restarted by hand. Three protections, in the
order they engage:

1. :func:`~src.analysis.digest.digest_preflight` refuses up front — the
   directory is not a session, or it holds more rows than
   ``daemon.digest_row_budget``. Cheap: a name match and Parquet footer
   metadata.
2. the computation runs in a single-use ``spawn`` child process
   (:func:`compute_digest_off_loop`), the pattern
   :func:`src.control.capture_finalize.recompute_quality_off_loop`
   established for the capture judge — a worker *thread* is not enough,
   since the pandas work holds the GIL.
3. the child caps its own ``RLIMIT_AS`` before touching data, so an
   over-budget digest raises ``MemoryError`` inside the child and the parent
   survives by construction rather than by the kernel's choice of victim.

Any of the three refusing leaves a ``digest_skipped.json`` naming the reason.
A skipped digest that says why is a finding; an absent one is indistinguishable
from a session nobody got round to.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import multiprocessing
import os
import resource
from concurrent.futures import BrokenExecutor, Executor, ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.alerts.notifier import format_digest_message
from src.analysis.digest import (
    REASON_CHILD_FAILED,
    REASON_CHILD_OUT_OF_MEMORY,
    REASON_CHILD_TIMED_OUT,
    SKIP_SCHEMA_VERSION,
    DigestSkipped,
    SessionDigest,
    compute_digest,
    digest_preflight,
    get_or_compute_digest,
    persist_digest,
    read_digest,
    write_digest_skipped,
)
from src.config import CanaryConfig
from src.preprocessing.loader import count_session_rows
from src.timefmt import format_timestamp

if TYPE_CHECKING:
    from src.alerts.emitter import AnomalyEventEmitter
    from src.alerts.notifier import Notifier

log = logging.getLogger(__name__)

#: Ceiling on one digest in the child. A 2.6 M-row session takes minutes on
#: the Pi; this allows several times that and exists only so a wedged child
#: cannot hold the daemon's shutdown (or a background task) forever.
DIGEST_TIMEOUT_SECONDS = 900.0

#: ``RLIMIT_AS`` derivation when ``daemon.digest_address_space_mb`` is unset:
#: half of physical RAM, clamped. Half leaves the running daemon (~1.5 GB on
#: the Pi) and the page cache room beside the child; the 1 GiB floor keeps a
#: small machine from refusing every digest outright, and the 4 GiB ceiling
#: keeps a big laptop from handing the child a limit so wide it is no limit.
_ADDRESS_SPACE_FRACTION = 0.5
_ADDRESS_SPACE_FLOOR_MB = 1024
_ADDRESS_SPACE_CEILING_MB = 4096
_MIB = 1024 * 1024


class TripEndDigestUnavailableError(RuntimeError):
    """The digest produced no answer: the child failed to start, died, or
    timed out. ``reason`` is the :mod:`~src.analysis.digest` ``REASON_*``
    constant the refusal sidecar records."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _physical_memory_bytes() -> int | None:
    try:
        return int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
    except (OSError, ValueError, AttributeError):  # pragma: no cover - non-POSIX
        return None


def digest_address_space_bytes(config: CanaryConfig) -> int:
    """The ``RLIMIT_AS`` ceiling to hand the digest child, in bytes.

    ``daemon.digest_address_space_mb`` wins when set. Otherwise derived from
    physical RAM (see ``_ADDRESS_SPACE_*``); when the machine will not report
    its RAM, the ceiling constant is used — an unknown machine still gets a
    bounded child, which is the whole point.
    """
    configured = config.daemon.digest_address_space_mb
    if configured is not None:
        return int(configured) * _MIB
    total = _physical_memory_bytes()
    if total is None:  # pragma: no cover - non-POSIX
        return _ADDRESS_SPACE_CEILING_MB * _MIB
    derived_mb = int(total * _ADDRESS_SPACE_FRACTION) // _MIB
    return max(_ADDRESS_SPACE_FLOOR_MB, min(_ADDRESS_SPACE_CEILING_MB, derived_mb)) * _MIB


#: Peak resident bytes per session row for the digest chain, measured
#: 2026-09-17: ``resample_and_segment`` over a 2,622,370-row session peaked at
#: 5,483 MB (``read_session`` 1,879 MB, ``to_wide`` 3,440 MB) to emit 1,143
#: rows. 5,483 MiB / 2,622,370 rows = 2,193 bytes/row; rounded up, since
#: under-estimating turns a clean refusal into a child that dies.
#: **This is the one number to change when the pivot is moved after the
#: resample** — the row budget follows from it.
DIGEST_PEAK_BYTES_PER_ROW = 2_200


def digest_row_budget(config: CanaryConfig) -> int:
    """How many rows a digest may attempt: the configured budget, or derived.

    Derived = the child's address-space ceiling divided by the measured
    per-row peak, so the budget and the ceiling cannot drift apart. On the
    8 GB Pi that is a 4 GiB child / 2,200 B per row ≈ 1.95 M rows, which is
    the honest figure: a 4 M budget against the same ceiling let sessions
    between the two spawn a child that then died of ``MemoryError``.

    ``0`` disables the check (the ceiling still applies, so an over-large
    session fails as a caught ``MemoryError`` rather than taking the daemon
    with it).
    """
    configured = config.daemon.digest_row_budget
    if configured is not None:
        return int(configured)
    return digest_address_space_bytes(config) // DIGEST_PEAK_BYTES_PER_ROW


def limit_address_space(nbytes: int) -> tuple[int, int]:
    """Cap this process's address space at ``nbytes`` and return the limit.

    Never *raises* the limit: an existing hard limit (a cgroup, a systemd
    ``LimitAS=``) stays the ceiling. Returns what ``getrlimit`` reports
    afterwards so a caller — or a test running this in a real child — can
    assert the limit is actually in force rather than trusting the call.

    ``RLIMIT_AS`` bounds *virtual* address space, which is deliberately
    stricter than RSS: pandas/pyarrow map more than they resident, so this
    refuses somewhat before the kernel would have killed anything. Refusing
    early with a named reason is the trade being made.
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    target = nbytes if hard == resource.RLIM_INFINITY else min(nbytes, hard)
    if soft != resource.RLIM_INFINITY:
        target = min(target, soft)
    resource.setrlimit(resource.RLIMIT_AS, (target, hard))
    return resource.getrlimit(resource.RLIMIT_AS)


def compute_digest_under_limit(
    session_dir: Path,
    config: CanaryConfig,
    *,
    now: datetime | None = None,
    address_space_bytes: int | None = None,
) -> SessionDigest:
    """The child-process entry point: cap, then compute.

    Module-level and picklable on purpose — ``spawn`` has to import it by
    name. The limit goes on *before* any data is read, so the first oversized
    allocation raises ``MemoryError`` here instead of growing until the
    kernel intervenes in the parent's process tree.
    """
    if address_space_bytes is not None:
        limit_address_space(address_space_bytes)
    return compute_digest(session_dir, config, now=now)


async def compute_digest_off_loop(
    session_dir: Path,
    config: CanaryConfig,
    *,
    now: datetime | None = None,
    executor: Executor | None = None,
    timeout: float = DIGEST_TIMEOUT_SECONDS,
    address_space_bytes: int | None = None,
) -> SessionDigest:
    """:func:`compute_digest_under_limit` in a child process, awaited without
    holding the loop — the only way an async caller may compute a digest.

    With no ``executor`` a single-use ``spawn`` pool is created and torn down
    per call, exactly as
    :func:`src.control.capture_finalize.recompute_quality_off_loop` does and
    for the same reasons (``spawn`` because forking a process running asyncio
    plus threads inherits their locks; single-use so the session's frames go
    back to the OS with the child instead of staying resident in a pool).
    Tests inject an executor. A child that cannot start, dies, times out or
    is cancelled is terminated and reported as
    :class:`TripEndDigestUnavailableError`; ``MemoryError`` and the compute's
    own ``FileNotFoundError`` cross the boundary unchanged.
    """
    ceiling = (
        digest_address_space_bytes(config) if address_space_bytes is None else address_space_bytes
    )
    call = functools.partial(
        compute_digest_under_limit,
        session_dir,
        config,
        now=now,
        address_space_bytes=ceiling,
    )
    owned = executor is None
    pool: Executor = _spawn_pool() if executor is None else executor
    finished = False
    try:
        try:
            future = asyncio.get_running_loop().run_in_executor(pool, call)
        except Exception as exc:
            raise TripEndDigestUnavailableError(
                REASON_CHILD_FAILED,
                f"digest for {session_dir.name} could not start: {type(exc).__name__}: {exc}",
            ) from exc
        try:
            digest = await asyncio.wait_for(future, timeout)
        except TimeoutError as exc:
            raise TripEndDigestUnavailableError(
                REASON_CHILD_TIMED_OUT,
                f"digest for {session_dir.name} did not finish within {timeout:.0f}s",
            ) from exc
        except BrokenExecutor as exc:
            raise TripEndDigestUnavailableError(
                REASON_CHILD_FAILED,
                f"digest process for {session_dir.name} failed: {type(exc).__name__}: {exc}",
            ) from exc
        finished = True
        return digest
    finally:
        if owned:
            _discard_pool(pool, terminate=not finished)


def _spawn_pool() -> ProcessPoolExecutor:
    return ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))


def _discard_pool(pool: Executor, *, terminate: bool) -> None:
    """Release a single-use pool without waiting on the loop for its child.

    On any path that did not produce a digest the child is terminated: a
    timed-out or cancelled computation would otherwise keep a core and up to
    its whole address-space ceiling until it finished on its own.
    """
    processes = []
    if terminate and isinstance(pool, ProcessPoolExecutor):
        # No public way to reach the workers before 3.14's terminate_workers().
        processes = list((pool._processes or {}).values())
    pool.shutdown(wait=False, cancel_futures=True)
    for proc in processes:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001 — already gone is the goal
            log.debug("digest child already exited", exc_info=True)


#: Sessions with a digest computation in flight on this loop. The GPS edge
#: fires once per drive and the shutdown fallback is idempotent via
#: ``digest.json``, but that check only covers a *finished* computation —
#: without this, a shutdown landing mid-digest would start a second child
#: over the same multi-gigabyte session.
_IN_FLIGHT: dict[Path, asyncio.Task[dict[str, Any] | None]] = {}


def digest_in_flight(session_dir: Path) -> bool:
    """Whether a digest for this session is already being computed."""
    return session_dir in _IN_FLIGHT


async def handle_trip_end_off_loop(  # noqa: PLR0913
    session_dir: Path,
    config: CanaryConfig,
    *,
    emitter: AnomalyEventEmitter | None = None,
    notifier: Notifier | None = None,
    now: datetime | None = None,
    catalog_db: Path | None = None,
    executor: Executor | None = None,
    timeout: float = DIGEST_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    """Assemble + persist the digest for one completed trip, off the loop.

    Never raises — a drive that recorded good data must not take the daemon
    down because its report card could not be written. Returns the digest
    dict on a freshly-computed trip end, or ``None`` when the trip has no
    telemetry, was already handled, was refused by
    :func:`~src.analysis.digest.digest_preflight`, or failed in the child.
    The last two cases write ``digest_skipped.json`` naming the reason, so
    ``None`` is never the whole story on disk.
    """
    if emitter is not None:
        # The next trip gets its own push budget regardless of which path
        # (fresh, refused or idempotent re-fire) got us here.
        emitter.reset_notification_count()
    if read_digest(session_dir) is not None:
        return None
    current_task = asyncio.current_task()
    existing = _IN_FLIGHT.get(session_dir)
    if existing is not None and existing is not current_task:
        log.debug("digest for %s already in flight; awaiting shared result", session_dir.name)
        await asyncio.shield(existing)
        return None
    skipped = digest_preflight(session_dir, config, now=now)
    if skipped is not None:
        write_digest_skipped(session_dir, skipped)
        return None

    if current_task is not None:
        _IN_FLIGHT[session_dir] = current_task
    try:
        digest = await compute_digest_off_loop(
            session_dir,
            config,
            now=now,
            executor=executor,
            timeout=timeout,
            address_space_bytes=None,
        )
    except FileNotFoundError:
        # No telemetry parquet — "skip", not "quarantine", same as every
        # other post-hoc sidecar. Nothing to record: there was no session.
        return None
    except MemoryError:
        write_digest_skipped(
            session_dir,
            _child_failure(
                session_dir,
                config,
                REASON_CHILD_OUT_OF_MEMORY,
                f"digest child exceeded its {digest_address_space_bytes(config) // _MIB} MiB "
                "address-space ceiling",
                now=now,
            ),
        )
        return None
    except TripEndDigestUnavailableError as exc:
        write_digest_skipped(
            session_dir,
            _child_failure(session_dir, config, exc.reason, str(exc), now=now),
        )
        return None
    except Exception as exc:  # noqa: BLE001 — a digest is a report, not a gate
        log.exception("digest for %s failed", session_dir.name)
        write_digest_skipped(
            session_dir,
            _child_failure(
                session_dir,
                config,
                REASON_CHILD_FAILED,
                f"{type(exc).__name__}: {exc}",
                now=now,
            ),
        )
        return None
    finally:
        if _IN_FLIGHT.get(session_dir) is current_task:
            _IN_FLIGHT.pop(session_dir, None)

    payload = persist_digest(session_dir, digest, catalog_db=catalog_db)
    if payload is None:
        return None
    if notifier is not None and config.notifications.digest_enabled:
        title, body = format_digest_message(payload)
        notifier.notify(title, body)
    return payload


def _child_failure(
    session_dir: Path,
    config: CanaryConfig,
    reason: str,
    detail: str,
    *,
    now: datetime | None,
) -> DigestSkipped:
    """A refusal sidecar for a failure that happened *after* the pre-flight.

    Re-counts the rows so the sidecar always names the size of the session
    that could not be digested, whichever of the three protections tripped.
    """
    return DigestSkipped(
        schema_version=SKIP_SCHEMA_VERSION,
        session_id=session_dir.name,
        reason=reason,
        detail=detail,
        recorded_at=format_timestamp(datetime.now(UTC) if now is None else now),
        row_count=count_session_rows(session_dir),
        row_budget=digest_row_budget(config) or None,
    )


def handle_trip_end(
    session_dir: Path,
    config: CanaryConfig,
    *,
    emitter: AnomalyEventEmitter | None = None,
    notifier: Notifier | None = None,
    now: datetime | None = None,
    catalog_db: Path | None = None,
) -> dict[str, Any] | None:
    """In-process trip end, for callers that are not on an event loop.

    Offline tools and scripts (``scripts/catalog.py``, a hand-run digest)
    keep this; the daemon uses :func:`handle_trip_end_off_loop`, which is the
    only variant with the address-space ceiling. Still pre-flighted, so the
    row budget applies here too.

    Returns the digest dict on a freshly-computed trip end, or ``None``
    when the trip has no telemetry, no session directory, was refused by
    the pre-flight, or was already handled (idempotent re-fire — see module
    docstring). ``catalog_db`` (Task 6.3), when given, re-indexes the session
    into the catalog right after the digest is written; ``None`` (the
    default) skips it.
    """
    if emitter is not None:
        emitter.reset_notification_count()
    if read_digest(session_dir) is not None:
        return None
    digest = get_or_compute_digest(session_dir, config, now=now, catalog_db=catalog_db)
    if digest is None:
        return None
    if notifier is not None and config.notifications.digest_enabled:
        title, body = format_digest_message(digest)
        notifier.notify(title, body)
    return digest


__all__ = [
    "DIGEST_TIMEOUT_SECONDS",
    "TripEndDigestUnavailableError",
    "compute_digest_off_loop",
    "compute_digest_under_limit",
    "digest_address_space_bytes",
    "digest_in_flight",
    "handle_trip_end",
    "handle_trip_end_off_loop",
    "limit_address_space",
]
