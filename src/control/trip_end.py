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

The tracker's edge reads vehicle speed before GPS (D03.29). When no usable
speed source could see a drive end at all, :func:`record_trip_end_unobservable`
writes a ``no_speed_source`` refusal instead, so the session says why it has
no digest rather than looking like one nobody got round to.

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
import dataclasses
import functools
import logging
import os
import resource
from concurrent.futures import BrokenExecutor, Executor
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.alerts.notifier import format_digest_message
from src.analysis.digest import (
    REASON_CHILD_FAILED,
    REASON_CHILD_OUT_OF_MEMORY,
    REASON_CHILD_TIMED_OUT,
    REASON_INTERRUPTED_AT_SHUTDOWN,
    REASON_NO_SPEED_SOURCE,
    SKIP_SCHEMA_VERSION,
    DigestSkipped,
    SessionDigest,
    compute_digest,
    digest_preflight,
    get_or_compute_digest,
    persist_digest,
    read_digest,
    read_digest_skipped,
    write_digest_skipped,
)
from src.config import CanaryConfig
from src.control import child_pool
from src.preprocessing.loader import count_session_rows
from src.timefmt import format_timestamp

if TYPE_CHECKING:
    from src.alerts.emitter import AnomalyEventEmitter
    from src.alerts.notifier import Notifier
    from src.analysis.gps_tracker import TripEnd

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


#: Peak resident bytes per session row for the digest chain.
#:
#: Measured 2026-09-17 on the raw-timestamp pivot: ``resample_and_segment``
#: over the 2,622,370-row ``2026-08-08T214218`` peaked at 5,483 MB to emit
#: 1,143 rows — 2,193 bytes/row, recorded here as 2,200.
#:
#: Re-measured 2026-09-22 on the long-form reduction that replaced the pivot
#: (D03.6, ``data/analyses/2026-09-22-d03-d04-desk-pass.md``), same session,
#: same laptop, each stage in its own process: ``resample_and_segment``
#: 859 MB, ``compute_digest`` 959 MB, ``compute_digest_under_limit`` under
#: the Pi's 4 GiB ``RLIMIT_AS`` 976 MB — **390 bytes/row**. A 5,570,352-row
#: session (``2026-09-18T144215``) peaked at 1,490 MB, 280 bytes/row. The
#: figure here is twice the larger measurement, because the Pi's allocator
#: is not the laptop's and under-estimating turns a clean refusal into a
#: child that dies (a caught ``MemoryError`` under the ceiling, never the
#: daemon — but still a session without a digest).
#: **This is the one number to change when the reduction changes** — the
#: row budget follows from it.
DIGEST_PEAK_BYTES_PER_ROW = 800


def digest_row_budget(config: CanaryConfig) -> int:
    """How many rows a digest may attempt: the configured budget, or derived.

    Derived = the child's address-space ceiling divided by the measured
    per-row peak, so the budget and the ceiling cannot drift apart. On the
    8 GB Pi that is a 4 GiB child / 800 B per row ≈ 5.4 M rows (about 38
    minutes of ``default`` collection at ~2,355 rows/s). The derivation is
    the point: a hand-picked 4 M budget once sat against a ceiling that
    covered only 1.95 M rows, and sessions between the two spawned a child
    that then died of ``MemoryError``.

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


_spawn_pool = child_pool.spawn_pool
_discard_pool = child_pool.discard_pool


#: Sessions with a digest computation in flight on this loop. The GPS edge
#: fires once per drive and the shutdown fallback is idempotent via
#: ``digest.json``, but that check only covers a *finished* computation —
#: without this, a shutdown landing mid-digest would start a second child
#: over the same multi-gigabyte session.
_IN_FLIGHT: dict[Path, asyncio.Task[dict[str, Any] | None]] = {}

#: Sessions whose digest child this process cancelled at shutdown (D03.6).
#: ``_settle_trip_end_tasks`` cancels what will not finish inside the unit's
#: stop deadline, and the fallback trigger then runs
#: :func:`handle_trip_end_off_loop` again for the active session. Without
#: this, cancellation cleared ``_IN_FLIGHT`` and the fallback immediately
#: restarted the same multi-gigabyte child with the full digest timeout —
#: inside a 30 s ``TimeoutStopSec``. Process-local on purpose: the next
#: daemon start may legitimately compute the digest; this one may not.
_INTERRUPTED: set[Path] = set()


def digest_interrupted(session_dir: Path) -> bool:
    """Whether this process already cancelled a digest for ``session_dir``."""
    return session_dir in _INTERRUPTED


def digest_in_flight(session_dir: Path) -> bool:
    """Whether a digest for this session is already being computed."""
    return session_dir in _IN_FLIGHT


def any_digest_in_flight() -> bool:
    """Whether any digest child is running on this loop.

    The workbench's whole-session analyses (:mod:`src.control.workbench_jobs`)
    share this child's memory budget, so they wait for a digest rather than
    run a second multi-gigabyte child beside it.
    """
    return bool(_IN_FLIGHT)


def trip_block(trip: TripEnd, session_dir: Path) -> dict[str, Any]:
    """The digest's ``trip`` block (D03.29): what the trip spanned.

    Decision: a trip that spans several sessions (a capture started or
    stopped mid-drive, a daemon restart) is digested **on the session it
    ended in** — where the stop was seen — and this block names every session
    it touched, captures included, ordered by
    :func:`src.control.sessions.session_started_at` (never by directory name:
    ``_capture-…`` sorts before every live session by string). The digest's
    statistics (modes, anomalies, fingerprint) cover that one session;
    ``coverage`` says so, so no reader mistakes them for the whole drive.
    """
    from src.control.sessions import session_started_at

    never = datetime.max.replace(tzinfo=UTC)
    spanned = sorted(
        {*trip.sessions, session_dir},
        key=lambda path: (session_started_at(path) or never, path.name),
    )

    def _stamp(epoch: float | None) -> str | None:
        return None if epoch is None else format_timestamp(datetime.fromtimestamp(epoch, tz=UTC))

    return {
        "started_at": _stamp(trip.started_epoch),
        "stopped_at": _stamp(trip.stopped_epoch),
        "speed_source": trip.speed_source,
        "end_session": session_dir.name,
        "sessions": [path.name for path in spanned],
        "coverage": "end_session_only" if len(spanned) > 1 else "whole_trip",
        "detail": trip.detail,
    }


async def handle_trip_end_off_loop(  # noqa: PLR0913
    session_dir: Path,
    config: CanaryConfig,
    *,
    trip: TripEnd | None = None,
    emitter: AnomalyEventEmitter | None = None,
    notifier: Notifier | None = None,
    now: datetime | None = None,
    catalog_db: Path | None = None,
    executor: Executor | None = None,
    timeout: float = DIGEST_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    """Assemble + persist the digest for one completed trip, off the loop.

    Never raises — a drive that recorded good data must not take the daemon
    down because its report card could not be written. ``trip`` (the tracker's
    :class:`~src.analysis.gps_tracker.TripEnd`) adds the :func:`trip_block`. Returns the digest
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
    if session_dir in _INTERRUPTED:
        # Cancelled at shutdown earlier in this process; the refusal sidecar
        # is already on disk. Starting it again here is exactly the duplicate
        # child the shutdown deadline cannot afford.
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
    except asyncio.CancelledError:
        # Shutdown cancelled the child. Record the outcome before
        # re-raising so the interruption is a fact on disk, not a session
        # that silently has no digest — and remember it so the shutdown
        # fallback cannot start the same work again in this process.
        _INTERRUPTED.add(session_dir)
        write_digest_skipped(
            session_dir,
            _child_failure(
                session_dir,
                config,
                REASON_INTERRUPTED_AT_SHUTDOWN,
                "digest child cancelled before it finished (daemon shutdown)",
                now=now,
            ),
        )
        raise
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

    if trip is not None:
        digest = dataclasses.replace(digest, trip=trip_block(trip, session_dir))
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


def record_trip_end_unobservable(
    session_dir: Path,
    config: CanaryConfig,
    detail: str,
    *,
    now: datetime | None = None,
) -> DigestSkipped | None:
    """Record that no usable speed source could see this session's drive end.

    The trip-end hook runs on the tracker's edge; with no speed source there
    is no edge, and before D03.29 that left a session with neither
    ``digest.json`` nor ``digest_skipped.json`` — indistinguishable from one
    nobody got round to. This writes the ``no_speed_source`` refusal naming
    the sources (``detail`` is the tracker's inventory). Cheap: no data read
    beyond Parquet footers. Blocking file I/O, so the daemon calls it in a
    thread.

    Never replaces an answer already on disk — a computed digest, or a more
    specific refusal (a row budget, a failed child) — and never writes into a
    directory that is not a session (replay output describes no drive).
    Returns what it wrote, or ``None``.
    """
    from src.control.sessions import is_session_dir_name

    if not is_session_dir_name(session_dir.name) or not session_dir.is_dir():
        return None
    if read_digest(session_dir) is not None or read_digest_skipped(session_dir) is not None:
        return None
    skipped = DigestSkipped(
        schema_version=SKIP_SCHEMA_VERSION,
        session_id=session_dir.name,
        reason=REASON_NO_SPEED_SOURCE,
        detail=detail,
        recorded_at=format_timestamp(datetime.now(UTC) if now is None else now),
        row_count=count_session_rows(session_dir),
        row_budget=digest_row_budget(config) or None,
    )
    write_digest_skipped(session_dir, skipped)
    return skipped


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
    "record_trip_end_unobservable",
    "trip_block",
]
