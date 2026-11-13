"""Whether a digest may run, and the refusal that says why not.

The admission half of :mod:`src.analysis.digest` (R26.7): the memory budgets
(``derived_address_space_bytes``, :func:`digest_row_budget`,
:func:`digest_span_budget_seconds`), the pre-flight that applies them
(:func:`digest_preflight`), the ``digest_skipped.json`` refusal record
(:class:`DigestSkipped`, :func:`write_digest_skipped`,
:func:`clear_digest_skipped`) and the lock and ranking rules that decide which
trip's answer is the session's top-level one. It computes nothing about the
drive: :mod:`~src.analysis.digest` imports this module and re-exports every
public name, so ``from src.analysis.digest import digest_preflight`` keeps
working.

Its logger is ``src.analysis.digest``, the name the refusal warning has always
been logged under, so a log filter or ``caplog`` on that name still sees it.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.config import CanaryConfig
from src.jsonable import json_default
from src.storage.catalog import record_session_best_effort
from src.storage.session_files import count_session_rows, session_time_span_seconds
from src.storage.session_products import (
    DIGEST_FILENAME,
    DIGEST_SKIPPED_FILENAME,
    read_digest,
    read_digest_skipped,
    trip_digest_path,
    trip_digest_skipped_path,
    trip_key,
    trip_started_at,
)
from src.storage.sessions import is_session_dir_name
from src.storage.sidecar import read_json_object, write_json_sidecar
from src.timefmt import format_timestamp as _format_timestamp

log = logging.getLogger("src.analysis.digest")


#: 2 (A06.5): ``anomaly_counts_by_source`` counts alert-role events only and
#: ``evidence_counts_by_source`` counts the rest. A schema-1 sidecar is still
#: accurate as written — detector roles did not exist when it was computed,
#: so every event it counted was an alert — and readers treat its missing
#: evidence block as empty. It is deliberately *not* recomputed: the digest
#: window ends at "now" when computed after the fact, so a late recompute
#: would count events from long after the trip.
SCHEMA_VERSION = 2


#: 2 (P02.1): adds ``span_seconds`` / ``span_budget_seconds``, and
#: ``row_budget`` is set only where a row budget applies (a capture's tuning
#: section); an ordinary drive's is ``null``.
SKIP_SCHEMA_VERSION = 2


#: The directory is replay output / quarantine / scratch, not a recording of
#: this vehicle (``src.storage.sessions.is_session_dir_name``).
REASON_NOT_A_SESSION = "not_a_session"


#: A capture whose tuning section would read more rows than
#: ``daemon.digest_row_budget`` allows to be loaded at once.
REASON_OVER_ROW_BUDGET = "over_row_budget"


#: A longer recorded span than ``daemon.digest_span_budget_seconds`` allows the
#: ``value_number`` chain (P02.1).
REASON_OVER_SPAN_BUDGET = "over_span_budget"


#: The child hit its ``RLIMIT_AS`` ceiling (or the machine's own limit).
REASON_CHILD_OUT_OF_MEMORY = "child_out_of_memory"


#: The child was still working when the timeout expired and was terminated.
REASON_CHILD_TIMED_OUT = "child_timed_out"


#: The child could not start, or died without answering.
REASON_CHILD_FAILED = "child_failed"


#: The daemon was stopping and cancelled the child before it finished
#: (D03.6). Recorded so the shutdown fallback does not start the same
#: multi-gigabyte work a second time inside the unit's stop deadline, and so
#: the next start can tell "interrupted" from "never attempted".
REASON_INTERRUPTED_AT_SHUTDOWN = "interrupted_at_shutdown"


#: No usable speed source could see the drive end (D03.29): speed was lost
#: while moving for the whole trip-end window, or the session ended with no
#: trip edge and a blind stretch at least that long. The trip-end hook never
#: fired, so nothing was attempted; this says why. The detail names each
#: source and how many readings it gave. A digest requested later still
#: computes, and clears this.
REASON_NO_SPEED_SOURCE = "no_speed_source"


@dataclass(frozen=True)
class DigestSkipped:
    """Why a digest was *not* produced for a session.

    Written whenever the computation is refused up front or fails in the
    child, so "the digest was skipped: 6,400,000 rows over the 4,000,000-row
    budget" and "the digest was never attempted" are different things on
    disk. ``row_count`` is the Parquet footer count and ``span_seconds`` the
    footer timestamps' span (no data read), ``None`` when nothing readable was
    there to count. ``row_budget`` is ``None`` where no row budget applies:
    since P02.1 only a capture's tuning section still reads a session whole.
    """

    schema_version: int
    session_id: str
    #: Machine-readable kind — one of the ``REASON_*`` constants above.
    reason: str
    #: Human-readable one-liner naming the numbers behind ``reason``.
    detail: str
    recorded_at: str
    row_count: int | None
    row_budget: int | None
    #: Which digest code refused, mirroring ``classification.json``'s
    #: ``classifier_version``: a later schema may compute what this one
    #: declined, and a reader must be able to see the refusal is stale.
    digest_schema_version: int = SCHEMA_VERSION
    span_seconds: float | None = None
    span_budget_seconds: float | None = None
    #: D03.63 — the trip this refusal stands in for (``trip_block``), so a
    #: session that held two trips can say which one went undigested and why.
    #: ``None`` for a refusal that names no trip (on demand, or a session-level
    #: one); it is filed under :data:`~src.storage.session_products.SESSION_TRIP_KEY`.
    trip: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "reason": self.reason,
            "detail": self.detail,
            "recorded_at": self.recorded_at,
            "row_count": self.row_count,
            "row_budget": self.row_budget,
            "span_seconds": self.span_seconds,
            "span_budget_seconds": self.span_budget_seconds,
            "digest_schema_version": self.digest_schema_version,
            "trip": self.trip,
        }


#: ``RLIMIT_AS`` derivation when ``daemon.digest_address_space_mb`` is unset:
#: half of physical RAM, clamped. Half leaves the running daemon (~1.5 GB on
#: the Pi) and the page cache room beside the child, and the 4 GiB ceiling
#: keeps a big laptop from handing the child a limit so wide it is no limit.
#: A floor-sized child (1 GiB, any host with 2 GB of RAM or less) leaves no
#: room above ``DIGEST_CHAIN_FIXED_BYTES``, so its span budget is 1 s and every
#: digest and classify job there is refused by name. That is deliberate: the
#: chain aborts outright under a 1 GiB limit (measured 2026-09-26), and a named
#: refusal beats a child that dies.
_ADDRESS_SPACE_FRACTION = 0.5


_ADDRESS_SPACE_FLOOR_MB = 1024


_ADDRESS_SPACE_CEILING_MB = 4096


_MIB = 1024 * 1024


def _physical_memory_bytes() -> int | None:
    try:
        return int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
    except (OSError, ValueError, AttributeError):  # pragma: no cover - non-POSIX
        return None


def derived_address_space_bytes() -> int:
    """The ``RLIMIT_AS`` ceiling for an analysis child with no configured
    figure: derived from physical RAM (see ``_ADDRESS_SPACE_*``). When the
    machine will not report its RAM, the ceiling constant is used — an unknown
    machine still gets a bounded child, which is the whole point.

    The digest child (:func:`digest_address_space_bytes`) and the
    capture-quality child (``src.control.capture_finalize``, D03.64) both start
    from this, so neither can be handed the whole machine.
    """
    total = _physical_memory_bytes()
    if total is None:  # pragma: no cover - non-POSIX
        return _ADDRESS_SPACE_CEILING_MB * _MIB
    derived_mb = int(total * _ADDRESS_SPACE_FRACTION) // _MIB
    return max(_ADDRESS_SPACE_FLOOR_MB, min(_ADDRESS_SPACE_CEILING_MB, derived_mb)) * _MIB


def digest_address_space_bytes(config: CanaryConfig) -> int:
    """The ``RLIMIT_AS`` ceiling to hand the digest child, in bytes.

    ``daemon.digest_address_space_mb`` wins when set, otherwise
    :func:`derived_address_space_bytes`.
    """
    configured = config.daemon.digest_address_space_mb
    if configured is not None:
        return int(configured) * _MIB
    return derived_address_space_bytes()


#: Peak resident bytes per stored session row of a **whole-session raw read**
#: in the digest child: the tuning section of a capture's digest
#: (``tuning_digest.has_tuning_section``; ``read_channels`` with no row filter
#: until P02.2) and the workbench jobs, which share this budget. Since P02.1 the
#: rest of the digest no longer scales with rows (:data:`DIGEST_CHAIN_BYTES_PER_SECOND`).
#:
#: Measured 2026-09-17 on the raw-timestamp pivot: 2,193 bytes/row. Re-measured
#: 2026-09-22 on the long-form reduction (D03.6): 390 and 280 bytes/row, and
#: 2026-09-26 after P02.8 (decoded rows no longer stored): about 314 MB fixed
#: plus ~313 B per stored row, 354-632 B/row on three sessions.
#:
#: Not re-derived by P02.1, and too loose for the tuning section: that section
#: alone peaks at 1,408-1,595 B per stored row on four new-format captures
#: (1.31-2.83 M rows, 2.0-3.8 GB, laptop, 2026-09-26), and under a 4 GiB
#: ``RLIMIT_AS`` the 2.05 M-row ``_capture-fuel-target-research-2026-09-21T195844``
#: ran out of memory inside it. Tightening it also tightens the workbench, so it
#: is an operator decision recorded in P02.1; P02.2 (filtered reads) is what
#: removes the row term.
DIGEST_PEAK_BYTES_PER_ROW = 800


#: The digest's ``value_number`` chain (P02.1): ``resample_and_segment`` read
#: part by part, then classification and the fingerprint's featurization on
#: the 1 Hz frame. Its peak is a fixed cost plus a cost per second of session:
#: it holds one part of raw rows (~100 k) at a time and, for the whole
#: session, only the reduced cells and 1 Hz frames, whose size follows the
#: session's span and channel count, not its raw rows.
#:
#: ``compute_digest`` peak RSS, laptop, each run in its own process, new-format
#: copies (decoded rows removed), 2026-09-26:
#:
#: - ``2026-09-18T152144``, 805 s: 322 MB;
#: - ``2026-09-18T144215``, 2,359 s: 347 MB (1,544 MB before P02.1);
#: - ``2026-09-20T152758``, 8,571 s, 12.8 M rows: 503 MB (4,265 MB before);
#: - four back-to-back copies of it, 34,458 s, 51.4 M rows: 1,220 MB, and
#:   1,255 MB completing inside a 4 GiB ``RLIMIT_AS``.
#:
#: That is ~290 MB fixed plus ~27.7 KB per second of session at the default
#: profile's ~200 1 Hz columns. The constants below keep ~3.5x on the fixed
#: part and ~2.3x on the slope: ``RLIMIT_AS`` bounds virtual memory, not RSS
#: (the laptop's unconstrained VmPeak was 2.4 GB fixed plus 20.6 KB/s), the
#: Pi's allocator is not the laptop's, and a capture profile with more
#: channels widens the 1 Hz frame. A 4 GiB child gets ~13.7 h.
#: **Change these when the chain changes** -- the span budget follows from them.
DIGEST_CHAIN_FIXED_BYTES = 1024 * _MIB


DIGEST_CHAIN_BYTES_PER_SECOND = 64 * 1024


def digest_row_budget(config: CanaryConfig) -> int:
    """How many stored rows a whole-session raw read in the child may attempt:
    the configured budget, or derived.

    Applies to a digest only when it has a tuning section (a capture,
    :func:`~src.analysis.tuning_digest.has_tuning_section`), the one part of
    the digest that still reads a session whole, and to the workbench jobs.
    Every digest is also bounded by :func:`digest_span_budget_seconds`.

    Derived = the child's address-space ceiling divided by the per-row peak,
    so the budget and the ceiling cannot drift apart: on the 8 GB Pi a 4 GiB
    child / 800 B per row ≈ 5.4 M stored rows. The derivation is the point: a
    hand-picked 4 M budget once sat against a ceiling that covered only
    1.95 M rows, and sessions between the two spawned a child that then died
    of ``MemoryError``.

    ``0`` disables the check (the ceiling still applies, so an over-large
    session fails as a caught ``MemoryError`` rather than taking the daemon
    with it).
    """
    configured = config.daemon.digest_row_budget
    if configured is not None:
        return int(configured)
    return digest_address_space_bytes(config) // DIGEST_PEAK_BYTES_PER_ROW


def digest_span_budget_seconds(config: CanaryConfig) -> float:
    """How long a session the digest's ``value_number`` chain may attempt, in
    seconds of recorded span: the configured budget, or derived.

    Derived = what the child's address-space ceiling leaves after
    :data:`DIGEST_CHAIN_FIXED_BYTES`, divided by
    :data:`DIGEST_CHAIN_BYTES_PER_SECOND` -- ~13.7 h for the Pi's 4 GiB child.
    A ceiling too small for the fixed cost gets a 1 s budget, so every session
    is refused by name rather than attempted. The same rule bounds the
    workbench's ``classify`` job, which runs the same chain.

    ``0`` disables the check (the ceiling still applies).
    """
    configured = config.daemon.digest_span_budget_seconds
    if configured is not None:
        return float(configured)
    room = digest_address_space_bytes(config) - DIGEST_CHAIN_FIXED_BYTES
    return max(1.0, room / DIGEST_CHAIN_BYTES_PER_SECOND)


@dataclass(frozen=True)
class DigestBudgets:
    """A session's size, and the budgets that apply to its digest.

    Footer reads only. The one place that decides which budgets apply, so the
    pre-flight and every refusal sidecar written after it (the child's
    failures, ``no_speed_source``) name the same numbers.
    """

    row_count: int | None
    #: Set only when the digest has a tuning section (a capture), the one part
    #: that still reads the session whole; ``None`` otherwise, or when disabled.
    row_budget: int | None
    span_seconds: float | None
    #: ``None`` when disabled.
    span_budget_seconds: float | None

    @classmethod
    def of(cls, session_dir: Path, config: CanaryConfig) -> DigestBudgets:
        from src.analysis.tuning_digest import has_tuning_section

        rows = digest_row_budget(config) if has_tuning_section(session_dir) else 0
        return cls(
            row_count=count_session_rows(session_dir),
            row_budget=rows or None,
            span_seconds=session_time_span_seconds(session_dir),
            span_budget_seconds=digest_span_budget_seconds(config) or None,
        )


def _skipped(
    session_dir: Path,
    reason: str,
    detail: str,
    *,
    budgets: DigestBudgets,
    now: datetime | None = None,
) -> DigestSkipped:
    return DigestSkipped(
        schema_version=SKIP_SCHEMA_VERSION,
        session_id=session_dir.name,
        reason=reason,
        detail=detail,
        recorded_at=_format_timestamp(datetime.now(UTC) if now is None else now),
        row_count=budgets.row_count,
        row_budget=budgets.row_budget,
        span_seconds=budgets.span_seconds,
        span_budget_seconds=budgets.span_budget_seconds,
    )


def digest_preflight(
    session_dir: Path,
    config: CanaryConfig,
    *,
    now: datetime | None = None,
) -> DigestSkipped | None:
    """The gate every digest computation passes, or ``None`` to go ahead.

    Three refusals, all cheap (a name match, a capture manifest and Parquet
    footer metadata — no data read), and all here rather than at a call site
    because the caller that forgot them is the one that took the daemon down:

    * the directory is not a session (``_replay-*`` and friends) — the HTTP
      read has always refused those via ``resolve_session``; the trip-end
      hook did not, and computed a digest over 5.9 M replayed rows;
    * its recorded span is over ``daemon.digest_span_budget_seconds``. Since
      P02.1 the ``value_number`` chain reads the session part by part, so its
      peak follows the span (and channel count), not the rows: ~290 MB plus
      ~28 KB per second, measured (:data:`DIGEST_CHAIN_BYTES_PER_SECOND`);
    * it is a capture, whose tuning section still reads raw rows in one read
      (P02.2: only its channels' rows and columns), with more rows than
      ``daemon.digest_row_budget``. Before P02.1 every digest was budgeted
      this way: the whole-session pivot peaked at 5,483 MB for
      2,622,370 rows, and the 8 GB Pi's cliff was bracketed between ~4.5 M
      rows (completed) and ~5.9 M rows (OOM-killed).
    """

    if not is_session_dir_name(session_dir.name):
        # Derived from the child's address-space ceiling when unset, so a
        # budget and the ceiling cannot state different limits.
        return _skipped(
            session_dir,
            REASON_NOT_A_SESSION,
            f"{session_dir.name!r} is not a live session or capture directory",
            budgets=DigestBudgets(
                row_count=None,
                row_budget=digest_row_budget(config) or None,
                span_seconds=None,
                span_budget_seconds=digest_span_budget_seconds(config) or None,
            ),
            now=now,
        )
    budgets = DigestBudgets.of(session_dir, config)
    span, span_budget = budgets.span_seconds, budgets.span_budget_seconds
    if span_budget is not None and span is not None and span > span_budget:
        return _skipped(
            session_dir,
            REASON_OVER_SPAN_BUDGET,
            f"{span / 3600:,.2f} h of recorded span over the {span_budget / 3600:,.2f} h "
            "digest budget (its 1 Hz chain grows with the span)",
            budgets=budgets,
            now=now,
        )
    rows, row_budget = budgets.row_count, budgets.row_budget
    if row_budget is not None and rows is not None and rows > row_budget:
        return _skipped(
            session_dir,
            REASON_OVER_ROW_BUDGET,
            f"{rows:,} rows over the {row_budget:,}-row digest budget (a capture's "
            "tuning section reads its channels' raw rows, not part by part)",
            budgets=budgets,
            now=now,
        )
    return None


#: Guards every read-compare-write of the session's top-level answer
#: (``digest.json`` / ``digest_skipped.json``). Writers run on the event loop
#: (``persist_digest``) and in threads (``record_trip_end_unobservable``), and
#: two trips of one session can be in flight at once, so without one lock a
#: refusal and a digest interleave into a directory that claims both, or an
#: older trip overwrites a newer one. Every writer goes through it; there is no
#: unlocked way to touch the mirror.
_TOP_LEVEL_LOCK = threading.RLock()


#: Set on a whole-session (trip-less) digest that took the top level from a
#: real trip's refusal: that trip's ``started_at``, which the digest then ranks
#: as (D03.63). A trip-less record has no start of its own, so without this an
#: older trip's digest landing afterwards (its child was still running) found
#: nothing newer at the top level and took it back, though a newer trip had
#: already been answered. A later whole-session digest inherits the rank.
TOP_LEVEL_RANK_KEY = "ranks_as_trip_started_at"


def _ranked_start(record: Mapping[str, Any] | None) -> datetime | None:
    """A top-level record's place in trip order: its own trip's start, or, for
    a whole-session digest standing in for a refused trip, that trip's."""
    if record is None:
        return None
    return trip_started_at(record.get("trip")) or trip_started_at(
        {"started_at": record.get(TOP_LEVEL_RANK_KEY)}
    )


def _tripless_rank(session_dir: Path) -> str | None:
    """The rank a trip-less digest written now carries if it takes the top
    level: the start of the real trip whose refusal it replaces, or the rank of
    the whole-session digest it replaces. Called under :data:`_TOP_LEVEL_LOCK`."""
    refused = read_digest_skipped(session_dir)
    if refused is not None and trip_started_at(refused.get("trip")) is not None:
        return str(refused["trip"]["started_at"])
    held = read_digest(session_dir)
    if held is not None and trip_started_at(held.get("trip")) is None:
        rank = held.get(TOP_LEVEL_RANK_KEY)
        if trip_started_at({"started_at": rank}) is not None:
            return str(rank)
    return None


def _file_away_current_answer(session_dir: Path) -> None:
    """Keep the session's top-level answer as its trip's own record (D03.63).

    ``digest.json`` / ``digest_skipped.json`` are the newest trip's answer, so
    writing a second trip's replaces the first's. A session digested before
    per-trip records existed has the first trip's answer *only* there; copy it
    into ``trip_digests/`` before it is overwritten, unless that is already done.
    """
    for current, path_for in (
        (read_digest(session_dir), trip_digest_path),
        (read_digest_skipped(session_dir), trip_digest_skipped_path),
    ):
        if current is None:
            continue
        target = path_for(session_dir, trip_key(current.get("trip")))
        if not target.exists():
            write_json_sidecar(target, current, default=json_default, mkdir=True)


def _top_level_may_take(
    session_dir: Path, trip: Mapping[str, Any] | None, *, is_digest: bool
) -> bool:
    """Whether this trip's answer may become the session's top-level answer.

    The top level is the *newest trip's*, by parsed ``started_at`` (never by
    write order, and never by key or file name). A trip that began before the
    one already there is filed under ``trip_digests/`` only. A record naming no
    trip (on demand, or a caller with no tracker edge) never displaces a real
    trip's answer, with one exception: a computed whole-session digest may
    replace a real trip's *refusal*, so raising a budget and asking again still
    serves the session instead of the stale 409. That digest then ranks as the
    refused trip (:data:`TOP_LEVEL_RANK_KEY`), so a real trip older than it is
    filed under ``trip_digests/`` only. Called under :data:`_TOP_LEVEL_LOCK`.
    """
    new_start = trip_started_at(trip)
    held = read_digest(session_dir)
    refused = read_digest_skipped(session_dir)
    digest_start = trip_started_at(held.get("trip")) if held else None
    refusal_start = trip_started_at(refused.get("trip")) if refused else None
    if new_start is None:
        if digest_start is not None:
            return False
        return is_digest or refusal_start is None
    held_rank = _ranked_start(held)
    newest = max((start for start in (held_rank, refusal_start) if start), default=None)
    return newest is None or new_start >= newest


def _clear_top_level_skipped(session_dir: Path) -> None:
    with suppress(OSError):
        (session_dir / DIGEST_SKIPPED_FILENAME).unlink(missing_ok=True)


def write_digest_skipped(
    session_dir: Path, skipped: DigestSkipped, *, catalog_db: Path | None = None
) -> Path:
    """Persist the refusal sidecar (atomic tmp+rename, like every sibling).

    Filed twice (D03.63): under its trip in ``trip_digests/``, which is that
    trip's answer for good, and as the session's top-level
    ``digest_skipped.json``, which is the newest trip's. A ``digest.json``
    left by an *earlier* trip is removed from the top level (it stays in
    ``trip_digests/``), so the top level never answers "computed" for a
    session whose latest trip was refused. A refusal for a trip *older* than
    the one the top level holds is filed under its trip only. ``catalog_db``
    re-indexes the session afterwards, like :func:`persist_digest`: a refusal
    can remove the top-level ``digest.json``, and the catalog row must not keep
    showing it.
    """
    key = trip_key(skipped.trip)
    payload = skipped.to_dict()
    path = session_dir / DIGEST_SKIPPED_FILENAME
    with _TOP_LEVEL_LOCK:
        _file_away_current_answer(session_dir)
        write_json_sidecar(
            trip_digest_skipped_path(session_dir, key), payload, default=json_default, mkdir=True
        )
        if _top_level_may_take(session_dir, skipped.trip, is_digest=False):
            write_json_sidecar(path, payload, default=json_default)
            earlier = read_digest(session_dir)
            if earlier is not None and trip_key(earlier.get("trip")) != key:
                with suppress(OSError):
                    (session_dir / DIGEST_FILENAME).unlink(missing_ok=True)
    log.warning(
        "digest skipped for %s: %s (%s)",
        skipped.session_id,
        skipped.detail,
        skipped.reason,
        extra={"digest_skip_reason": skipped.reason, "digest_row_count": skipped.row_count},
    )
    record_session_best_effort(catalog_db, session_dir)
    return path


def clear_digest_skipped(session_dir: Path, key: str | Mapping[str, Any] | None = None) -> None:
    """Drop a stale refusal once a digest has actually been written.

    A directory must never hold both for one trip: "computed" and "refused"
    are mutually exclusive answers, and a leftover refusal beside a real
    ``digest.json`` is the contradiction a reader would have to arbitrate.
    ``key`` is the trip (its ``trip`` block, or its :func:`trip_key`) whose
    refusal goes from ``trip_digests/``. The top-level refusal goes only if it
    is that trip's own or an older trip's (parsed ``started_at``): a refusal for
    a *newer* trip is still the session's newest answer and stays.
    """
    with _TOP_LEVEL_LOCK:
        trip: Mapping[str, Any] | None
        if isinstance(key, str):
            stored = read_json_object(trip_digest_path(session_dir, key)) or read_json_object(
                trip_digest_skipped_path(session_dir, key)
            )
            trip = stored.get("trip") if stored else None
            key_name: str | None = key
        else:
            trip = key
            key_name = None if key is None else trip_key(key)
        refused = read_digest_skipped(session_dir)
        if refused is not None:
            refused_trip = refused.get("trip")
            own = key_name is not None and trip_key(refused_trip) == key_name
            refused_start, start = trip_started_at(refused_trip), trip_started_at(trip)
            older = refused_start is None or (start is not None and refused_start <= start)
            if key_name is None or own or older:
                _clear_top_level_skipped(session_dir)
        if key_name is not None:
            with suppress(OSError):
                trip_digest_skipped_path(session_dir, key_name).unlink(missing_ok=True)
