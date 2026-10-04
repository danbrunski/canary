"""The trip-end digest cannot take the daemon down any more (D03.6).

Twice the synchronous, uncapped digest cost the daemon: a 5.86 M-row
``_replay-*`` output OOM-killed it on 2026-09-11, and a ~6.4 M-row
``_capture-tuning-*`` session wedged it at 7.36 GB of 8 GB for 38 minutes on
2026-09-17 with no HTTP and no collection. These tests pin the three
protections independently — the computation is off the loop, the child's
address space is capped, and anything refused leaves a sidecar that says
which kind of empty it is — plus the thing all three must not change: a
session that does digest digests byte-identically.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import resource
import shutil
import signal
from collections.abc import Callable
from concurrent.futures import Executor, Future, ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from src.analysis.digest import (
    DIGEST_CHAIN_BYTES_PER_SECOND,
    DIGEST_CHAIN_FIXED_BYTES,
    DIGEST_PEAK_BYTES_PER_ROW,
    REASON_CHILD_FAILED,
    REASON_CHILD_OUT_OF_MEMORY,
    REASON_CHILD_TIMED_OUT,
    REASON_INTERRUPTED_AT_SHUTDOWN,
    REASON_NOT_A_SESSION,
    REASON_OVER_ROW_BUDGET,
    REASON_OVER_SPAN_BUDGET,
    SKIP_SCHEMA_VERSION,
    SessionDigest,
    compute_digest,
    digest_address_space_bytes,
    digest_preflight,
    digest_row_budget,
    digest_span_budget_seconds,
    get_or_compute_digest,
)
from src.analysis.rolling_circumference import METHOD_VERSION
from src.config import CanaryConfig
from src.control import trip_end
from src.control.trip_end import (
    TripEndDigestUnavailableError,
    compute_digest_off_loop,
    compute_digest_under_limit,
    handle_trip_end_off_loop,
    limit_address_space,
)
from src.storage.session_files import count_session_rows, session_time_span_seconds
from src.storage.session_products import (
    DIGEST_FILENAME,
    DIGEST_SKIPPED_FILENAME,
    read_digest,
    read_digest_skipped,
    read_rolling_circumference,
)
from tests.builders import RICH_SESSION_END, digest_config, write_rich_session
from tests.fakes import slow_digest

_MIB = 1024 * 1024


def _budgeted(config: CanaryConfig, **daemon: Any) -> CanaryConfig:
    return config.model_copy(update={"daemon": config.daemon.model_copy(update=daemon)})


def _session(tmp_path: Path, name: str = "2026-05-20T180000") -> Path:
    return write_rich_session(tmp_path, name=name)


class _RefusingExecutor(Executor):
    """A pool that fails the test if anything is ever submitted to it.

    The point of the row budget is that no child is *started* for an
    over-budget session — counting the rows costs a Parquet footer read, and
    starting a worker that would die costs a worker.
    """

    def __init__(self) -> None:
        self.submissions = 0

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        self.submissions += 1
        raise AssertionError("a child was started for a session that should have been refused")


class _TyresOnlyExecutor(Executor):
    """Refuses a digest child, runs the tyre measurement inline (D03.35).

    A drive refused a digest is still measured for tyre wear: two channels,
    not the session, so the digest's budgets do not apply to it.
    """

    def __init__(self) -> None:
        self.digests = 0
        self.tyres = 0

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        if getattr(fn, "func", None) is not trip_end.measure_tyres_under_limit:
            self.digests += 1
            raise AssertionError("a digest child was started for a refused session")
        self.tyres += 1
        # Inline, so without the child's address-space cap: in this process
        # it would cap the test worker itself.
        call = functools.partial(fn, address_space_bytes=None)
        future: Future[Any] = Future()
        future.set_result(call(*args, **kwargs))
        return future


class _MemoryErrorExecutor(Executor):
    """A child that ran out of address space, as ``RLIMIT_AS`` leaves it."""

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        future: Future[Any] = Future()
        future.set_exception(MemoryError())
        return future


async def _ticks_during(awaitable: Any) -> tuple[Any, int]:
    ticks = 0
    done = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not done.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    task = asyncio.create_task(ticker())
    try:
        result = await awaitable
    finally:
        done.set()
        await task
    return result, ticks


# --- 1. off the event loop -------------------------------------------------


async def test_trip_end_digest_keeps_the_loop_running_while_it_computes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path)
    calls: list[bool] = []
    monkeypatch.setattr(trip_end, "compute_digest_under_limit", slow_digest(1.0, calls))
    with ThreadPoolExecutor(max_workers=1) as pool:
        payload, ticks = await _ticks_during(
            handle_trip_end_off_loop(session, digest_config(tmp_path), executor=pool)
        )
    assert payload is not None
    assert payload["session_id"] == session.name
    assert calls == [False], "the digest ran on the event loop's thread"
    # Inline, the loop would have ticked once or not at all during the 1 s.
    assert ticks >= 20
    assert (session / DIGEST_FILENAME).exists()


async def test_concurrent_trip_end_requests_share_the_running_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path)
    calls: list[bool] = []
    monkeypatch.setattr(trip_end, "compute_digest_under_limit", slow_digest(0.1, calls))
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner, follower = await asyncio.gather(
            handle_trip_end_off_loop(session, digest_config(tmp_path), executor=pool),
            handle_trip_end_off_loop(session, digest_config(tmp_path), executor=pool),
        )
    assert sum(result is not None for result in (owner, follower)) == 1
    assert calls == [False]
    assert read_digest(session) is not None


# --- 2. the budgets refuse before anything is started ---------------------


async def test_a_session_over_the_span_budget_is_refused_and_only_its_tyres_are_measured(
    tmp_path: Path,
) -> None:
    """P02.1: the ``value_number`` chain reads part by part, so every digest is
    budgeted by recorded span; an ordinary drive has no row budget at all."""
    session = _session(tmp_path)
    rows = count_session_rows(session)
    span = session_time_span_seconds(session)
    assert span is not None and span > 10
    config = _budgeted(digest_config(tmp_path), digest_span_budget_seconds=10)
    pool = _TyresOnlyExecutor()

    result = await handle_trip_end_off_loop(session, config, executor=pool)

    assert result is None
    assert pool.digests == 0
    assert pool.tyres == 1
    tyres = read_rolling_circumference(session)
    assert tyres is not None and tyres["method_version"] == METHOD_VERSION
    assert not (session / DIGEST_FILENAME).exists()
    skipped = read_digest_skipped(session)
    assert skipped is not None
    assert skipped["reason"] == REASON_OVER_SPAN_BUDGET
    assert skipped["schema_version"] == SKIP_SCHEMA_VERSION == 2
    assert skipped["span_seconds"] == span
    assert skipped["span_budget_seconds"] == 10
    assert skipped["row_count"] == rows
    assert skipped["row_budget"] is None, "no row budget applies to an ordinary drive"
    assert f"{span / 3600:,.2f} h" in skipped["detail"] and "0.00 h" in skipped["detail"]
    # The daemon carried on: nothing raised, and the refusal cost no await at
    # all — a footer read, then straight back to the loop.
    await asyncio.sleep(0)


async def test_a_capture_over_the_row_budget_is_refused_and_only_its_tyres_are_measured(
    tmp_path: Path,
) -> None:
    """A capture's tuning section still reads the session whole (until P02.2),
    so a capture is budgeted by rows as well as by span."""
    session = _session(tmp_path, name="_capture-tuning-2026-05-20T180000")
    rows = count_session_rows(session)
    assert rows is not None and rows > 10
    config = _budgeted(digest_config(tmp_path), digest_row_budget=10)
    pool = _TyresOnlyExecutor()

    result = await handle_trip_end_off_loop(session, config, executor=pool)

    assert result is None
    assert pool.digests == 0
    assert pool.tyres == 1
    tyres = read_rolling_circumference(session)
    assert tyres is not None and tyres["method_version"] == METHOD_VERSION
    skipped = read_digest_skipped(session)
    assert skipped is not None
    assert skipped["reason"] == REASON_OVER_ROW_BUDGET
    assert skipped["row_count"] == rows
    assert skipped["row_budget"] == 10
    assert f"{rows:,}" in skipped["detail"] and "10-row" in skipped["detail"]
    assert "tuning section" in skipped["detail"]


def test_an_ordinary_drive_is_not_budgeted_by_rows(tmp_path: Path) -> None:
    """The row budget that used to refuse every drive over ~5.4 M rows on the
    Pi (``2026-09-20T152758``: 12.8 M) no longer applies to one: its digest
    peaks at 503 MB where it peaked at 4,265 MB (P02.1)."""
    session = _session(tmp_path)
    config = _budgeted(digest_config(tmp_path), digest_row_budget=10)
    assert get_or_compute_digest(session, config, now=RICH_SESSION_END) is not None
    assert read_digest_skipped(session) is None


def test_a_digest_stores_its_tyre_measurement_as_its_own_sidecar(tmp_path: Path) -> None:
    """D03.35: ``rolling_circumference.json`` is the tyre trend's only input,
    so a digest writes it there and not into ``digest.json``."""
    session = _session(tmp_path)
    payload = get_or_compute_digest(session, digest_config(tmp_path), now=RICH_SESSION_END)
    assert payload is not None
    assert "rolling_circumference" not in payload
    tyres = read_rolling_circumference(session)
    assert tyres is not None and tyres["method_version"] == METHOD_VERSION


async def test_a_replay_directory_is_refused_by_the_shared_function(tmp_path: Path) -> None:
    """The GET path refused ``_replay-*`` via ``resolve_session`` all along;
    the trip-end hook bypassed it and digested 5.9 M replayed rows."""
    session = _session(tmp_path, name="_replay-2026-05-20T180000")
    result = await handle_trip_end_off_loop(
        session, digest_config(tmp_path), executor=_RefusingExecutor()
    )
    assert result is None
    skipped = read_digest_skipped(session)
    assert skipped is not None
    assert skipped["reason"] == REASON_NOT_A_SESSION
    assert "_replay-2026-05-20T180000" in skipped["detail"]
    assert read_rolling_circumference(session) is None, "a replay is not a drive"


def test_the_in_process_path_is_gated_by_the_same_budget(tmp_path: Path) -> None:
    """``get_or_compute_digest`` is the shared function every caller reaches
    (HTTP route, MCP tool, scripts) — the budget lives there, not in the
    daemon wiring, so no caller can forget it."""
    session = _session(tmp_path)
    config = _budgeted(digest_config(tmp_path), digest_span_budget_seconds=10)
    assert get_or_compute_digest(session, config, now=RICH_SESSION_END) is None
    assert not (session / DIGEST_FILENAME).exists()
    assert (read_digest_skipped(session) or {})["reason"] == REASON_OVER_SPAN_BUDGET


def test_a_zero_budget_disables_the_check(tmp_path: Path) -> None:
    session = _session(tmp_path)
    config = _budgeted(digest_config(tmp_path), digest_span_budget_seconds=0)
    assert get_or_compute_digest(session, config, now=RICH_SESSION_END) is not None
    assert read_digest_skipped(session) is None
    capture = _session(tmp_path, name="_capture-tuning-2026-05-20T180000")
    no_rows = _budgeted(digest_config(tmp_path), digest_row_budget=0)
    assert digest_preflight(capture, no_rows) is None


def test_a_tuning_section_out_of_memory_is_the_childs_refusal_not_absent_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under a 4 GiB ``RLIMIT_AS`` the 2.05 M-row
    ``_capture-fuel-target-research-2026-09-21T195844`` ran out of memory inside
    the tuning section, whose reads caught it as an absent channel: the digest
    "completed", saying the required-tuple members were never captured and
    carrying no rates for seven polled channels (P02.1, 2026-09-26). Out of
    memory now leaves the child, which reports ``child_out_of_memory``."""
    from src.analysis import tuning_digest

    def out_of_memory(*_args: Any, **_kwargs: Any) -> Any:
        raise MemoryError("Unable to allocate 21.9 MiB")

    monkeypatch.setattr(tuning_digest, "read_channels", out_of_memory)
    monkeypatch.setattr(tuning_digest, "read_aligned", out_of_memory)
    capture = _session(tmp_path, name="_capture-tuning-2026-05-20T180000")
    with pytest.raises(MemoryError):
        compute_digest(capture, digest_config(tmp_path), now=RICH_SESSION_END)


# --- 3. the address-space ceiling -----------------------------------------


def _child_observed_limit(nbytes: int) -> tuple[int, int]:
    """Run in a real child: apply the cap and report what the OS says."""
    limit_address_space(nbytes)
    return resource.getrlimit(resource.RLIMIT_AS)


async def test_the_ceiling_is_applied_inside_the_child_not_the_parent() -> None:
    before = resource.getrlimit(resource.RLIMIT_AS)
    ceiling = 700 * _MIB
    loop = asyncio.get_running_loop()
    with ProcessPoolExecutor(
        max_workers=1, mp_context=__import__("multiprocessing").get_context("spawn")
    ) as pool:
        soft, _hard = await loop.run_in_executor(pool, _child_observed_limit, ceiling)
    assert soft == ceiling, "the child did not observe the RLIMIT_AS ceiling"
    assert resource.getrlimit(resource.RLIMIT_AS) == before, "the parent's own limit moved"


def test_compute_digest_under_limit_caps_before_reading_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap must go on before the data does — a limit applied after the
    pivot has already allocated protects nothing."""
    session = _session(tmp_path)
    order: list[str] = []
    monkeypatch.setattr(trip_end, "limit_address_space", lambda n: order.append(f"limit:{n}"))
    real = compute_digest

    def spy(*args: Any, **kwargs: Any) -> SessionDigest:
        order.append("compute")
        return real(*args, **kwargs)

    monkeypatch.setattr(trip_end, "compute_digest", spy)
    digest = compute_digest_under_limit(
        session, digest_config(tmp_path), now=RICH_SESSION_END, address_space_bytes=1234 * _MIB
    )
    assert order == [f"limit:{1234 * _MIB}", "compute"]
    assert digest.session_id == session.name


def test_the_ceiling_comes_from_config_when_set(tmp_path: Path) -> None:
    config = _budgeted(digest_config(tmp_path), digest_address_space_mb=1500)
    assert digest_address_space_bytes(config) == 1500 * _MIB


def test_the_derived_ceiling_is_bounded_and_below_total_ram(tmp_path: Path) -> None:
    config = digest_config(tmp_path)
    assert config.daemon.digest_address_space_mb is None
    derived = digest_address_space_bytes(config)
    total = int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
    assert 1024 * _MIB <= derived <= 4096 * _MIB
    # The whole point: the child can never be handed the whole machine.
    assert derived < total or total <= 2048 * _MIB


def test_limit_address_space_never_widens_an_existing_hard_limit() -> None:
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    assert (soft, hard) == resource.getrlimit(resource.RLIMIT_AS)
    # Asking for more than the current hard limit must clamp, not raise; on a
    # machine with no limit at all there is nothing to clamp against.
    if hard != resource.RLIM_INFINITY:  # pragma: no cover - depends on the host
        assert limit_address_space(hard * 4)[0] <= hard


# --- 4. a child that fails says so, and the parent survives ---------------


async def test_a_child_memory_error_is_recorded_and_never_raised(tmp_path: Path) -> None:
    session = _session(tmp_path)
    config = digest_config(tmp_path)

    result = await handle_trip_end_off_loop(session, config, executor=_MemoryErrorExecutor())

    assert result is None
    assert not (session / DIGEST_FILENAME).exists()
    skipped = read_digest_skipped(session)
    assert skipped is not None
    assert skipped["reason"] == REASON_CHILD_OUT_OF_MEMORY
    assert skipped["row_count"] == count_session_rows(session)
    assert "address-space ceiling" in skipped["detail"]
    # The parent is still here and its loop still works.
    await asyncio.sleep(0)


async def test_a_wedged_child_times_out_is_terminated_and_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real digest in a real child, against a timeout far shorter than it
    # can possibly take: a fake would have to be picklable for ``spawn``, and
    # what is under test here is the spawned child's fate, not the compute.
    session = _session(tmp_path)
    pools: list[ProcessPoolExecutor] = []
    spawn = trip_end._spawn_pool

    def tracked() -> ProcessPoolExecutor:
        pool = spawn()
        pools.append(pool)
        return pool

    monkeypatch.setattr(trip_end, "_spawn_pool", tracked)
    processes: list[Any] = []

    async def watch() -> None:
        while not processes:
            if pools and pools[0]._processes:
                processes.extend(pools[0]._processes.values())
            await asyncio.sleep(0.001)

    watcher = asyncio.create_task(watch())
    result = await handle_trip_end_off_loop(session, digest_config(tmp_path), timeout=0.05)
    watcher.cancel()

    assert result is None
    skipped = read_digest_skipped(session)
    assert skipped is not None
    assert skipped["reason"] == REASON_CHILD_TIMED_OUT
    assert processes, "no child was spawned"
    for proc in processes:
        # SIGTERM lands while the child is still importing pandas, so reaping
        # can take a few seconds; what matters is that it dies rather than
        # being left to finish on its own holding a core and its frames.
        for _ in range(120):
            if not proc.is_alive():
                break
            await asyncio.sleep(0.25)
        assert not proc.is_alive()
        assert proc.exitcode in (-signal.SIGTERM, -signal.SIGKILL), (
            "the child exited on its own, not terminated"
        )


async def test_a_dead_child_is_reported_not_hung(tmp_path: Path) -> None:
    class _Broken(Executor):
        def submit(self, fn: Callable[..., Any], /, *a: Any, **k: Any) -> Future[Any]:
            from concurrent.futures.process import BrokenProcessPool

            future: Future[Any] = Future()
            future.set_exception(BrokenProcessPool("child terminated abruptly"))
            return future

    session = _session(tmp_path)
    with pytest.raises(TripEndDigestUnavailableError, match="failed"):
        await compute_digest_off_loop(session, digest_config(tmp_path), executor=_Broken())
    # The hook turns it into a recorded refusal, never an exception.
    assert (
        await handle_trip_end_off_loop(session, digest_config(tmp_path), executor=_Broken()) is None
    )
    assert (read_digest_skipped(session) or {})["reason"] == REASON_CHILD_FAILED


# --- 5. a session that does digest digests identically --------------------


async def test_the_real_child_digest_is_byte_identical_to_the_in_process_one(
    tmp_path: Path,
) -> None:
    """Nothing about running off-loop under a cap may change the output."""
    inline_root = tmp_path / "inline"
    child_root = tmp_path / "child"
    inline_root.mkdir()
    inline = _session(inline_root)
    shutil.copytree(inline, child_root / inline.name)
    child = child_root / inline.name

    get_or_compute_digest(inline, digest_config(tmp_path), now=RICH_SESSION_END)
    await handle_trip_end_off_loop(child, digest_config(tmp_path), now=RICH_SESSION_END)

    assert (child / DIGEST_FILENAME).read_text() == (inline / DIGEST_FILENAME).read_text()
    assert read_digest_skipped(child) is None


class _HangingExecutor(Executor):
    """A child that never answers — a several-GB digest still running when
    the daemon is told to stop. Counts submissions so a restart is visible."""

    def __init__(self) -> None:
        self.submissions = 0

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        self.submissions += 1
        return Future()


async def test_a_digest_cancelled_at_shutdown_is_recorded_and_not_restarted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D03.6 — shutdown cancels the in-flight child; the fallback must not
    start it again inside the unit's stop deadline.

    Before: cancellation removed the in-flight marker and wrote nothing, so
    ``run_daemon``'s fallback trigger re-entered ``handle_trip_end_off_loop``
    for the same session and immediately spawned a second child with the
    full 900 s timeout, under a 30 s ``TimeoutStopSec``. Now the interrupted
    computation is a sidecar on disk and a process-local fact, so the second
    call is a no-op — and a later process (the next daemon start) may still
    compute the digest, because the sidecar is not a permanent refusal.
    """
    session = _session(tmp_path)
    config = digest_config(tmp_path)
    pool = _HangingExecutor()
    trip_end._INTERRUPTED.discard(session)
    # The closing thread-pool step would otherwise apply the child's
    # RLIMIT_AS to this very process (a thread is not a child), and with the
    # heavier test modules collected that ceiling is close enough to bite
    # the tests that follow. The limit is not what this test is about.
    monkeypatch.setattr(trip_end, "limit_address_space", lambda n: (n, n))

    task = asyncio.create_task(handle_trip_end_off_loop(session, config, executor=pool))
    for _ in range(100):
        if trip_end.digest_in_flight(session):
            break
        await asyncio.sleep(0.01)
    assert trip_end.digest_in_flight(session), "the child should be running"
    assert pool.submissions == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    skipped = read_digest_skipped(session)
    assert skipped is not None
    assert skipped["reason"] == REASON_INTERRUPTED_AT_SHUTDOWN
    assert skipped["row_count"] == count_session_rows(session)
    assert not trip_end.digest_in_flight(session)
    assert trip_end.digest_interrupted(session)

    # The shutdown fallback: same session, same process — no second child.
    assert await handle_trip_end_off_loop(session, config, executor=pool) is None
    assert pool.submissions == 1
    assert read_digest(session) is None

    # A fresh process has no memory of the interruption and computes normally.
    trip_end._INTERRUPTED.discard(session)
    with ThreadPoolExecutor(max_workers=1) as fresh:
        digest = await handle_trip_end_off_loop(
            session, config, now=RICH_SESSION_END, executor=fresh
        )
    assert digest is not None
    assert read_digest_skipped(session) is None, "a landed digest clears the sidecar"


async def test_an_earlier_refusal_is_cleared_once_a_digest_lands(tmp_path: Path) -> None:
    session = _session(tmp_path)
    tight = _budgeted(digest_config(tmp_path), digest_span_budget_seconds=10)
    assert await handle_trip_end_off_loop(session, tight) is None
    assert (session / DIGEST_SKIPPED_FILENAME).exists()

    assert (
        await handle_trip_end_off_loop(session, digest_config(tmp_path), now=RICH_SESSION_END)
        is not None
    )
    assert read_digest(session) is not None
    assert not (session / DIGEST_SKIPPED_FILENAME).exists(), (
        "a session cannot be both computed and refused"
    )


async def test_an_already_digested_session_is_not_recomputed(tmp_path: Path) -> None:
    session = _session(tmp_path)
    assert (
        await handle_trip_end_off_loop(session, digest_config(tmp_path), now=RICH_SESSION_END)
        is not None
    )
    assert (
        await handle_trip_end_off_loop(
            session, digest_config(tmp_path), now=RICH_SESSION_END, executor=_RefusingExecutor()
        )
        is None
    )


# --- 6. a refusal reads as a refusal downstream ---------------------------


def test_the_catalog_tells_refused_apart_from_never_attempted(tmp_path: Path) -> None:
    from src.storage.catalog import SessionCatalog

    refused = _session(tmp_path, name="2026-05-20T180000")
    untouched = _session(tmp_path, name="2026-05-21T180000")
    tight = _budgeted(digest_config(tmp_path), digest_span_budget_seconds=10)
    get_or_compute_digest(refused, tight, now=RICH_SESSION_END)

    with SessionCatalog(tmp_path / "catalog.db") as catalog:
        refused_row = catalog.record_session(refused)
        untouched_row = catalog.record_session(untouched)

    assert refused_row.digest is None
    assert refused_row.digest_skipped is not None
    assert refused_row.digest_skipped["reason"] == REASON_OVER_SPAN_BUDGET
    assert untouched_row.digest is None
    assert untouched_row.digest_skipped is None


def test_the_catalog_drops_the_refusal_once_a_digest_exists(tmp_path: Path) -> None:
    from src.storage.catalog import SessionCatalog

    session = _session(tmp_path)
    get_or_compute_digest(
        session, _budgeted(digest_config(tmp_path), digest_span_budget_seconds=10)
    )
    # Simulate a refusal sidecar left behind by an older build.
    get_or_compute_digest(session, digest_config(tmp_path), now=RICH_SESSION_END)
    (session / DIGEST_SKIPPED_FILENAME).write_text(json.dumps({"reason": "stale"}) + "\n")

    with SessionCatalog(tmp_path / "catalog.db") as catalog:
        row = catalog.record_session(session)
    assert row.digest is not None
    assert row.digest_skipped is None


def test_the_row_budget_is_derived_from_the_address_space_ceiling(tmp_path: Path) -> None:
    """Budget and ceiling must not be able to state different limits.

    A hand-picked 4 M-row budget sat against a 4 GiB ceiling that covers only
    ~1.95 M rows at the measured 2,200 bytes/row, so a session in between
    passed the budget, spawned a child and died of MemoryError — safe, but it
    reported a memory anomaly where the honest answer was "larger than what
    fits". Deriving one from the other leaves a single number to change when
    the pivot moves after the resample.
    """
    capped = _budgeted(
        digest_config(tmp_path), digest_row_budget=None, digest_address_space_mb=4096
    )
    expected = 4096 * 1024 * 1024 // DIGEST_PEAK_BYTES_PER_ROW
    assert digest_row_budget(capped) == expected
    # Halving the ceiling halves the budget: they move together, by construction.
    halved = _budgeted(capped, digest_address_space_mb=2048)
    assert digest_row_budget(halved) == expected // 2


def test_the_span_budget_is_derived_from_the_address_space_ceiling(tmp_path: Path) -> None:
    """P02.1: the ``value_number`` chain's peak is a fixed cost plus a cost per
    second of session, so its budget is what the ceiling leaves after the
    fixed cost, per second -- ~13.7 h for the Pi's 4 GiB child."""
    capped = _budgeted(digest_config(tmp_path), digest_address_space_mb=4096)
    expected = (4096 * _MIB - DIGEST_CHAIN_FIXED_BYTES) / DIGEST_CHAIN_BYTES_PER_SECOND
    assert digest_span_budget_seconds(capped) == expected
    assert 13 * 3600 < expected < 14 * 3600
    halved = _budgeted(capped, digest_address_space_mb=2048)
    assert digest_span_budget_seconds(halved) < expected / 2
    # A ceiling that cannot hold the fixed cost refuses every session by name
    # rather than disabling the check (0 means disabled).
    tiny = _budgeted(capped, digest_address_space_mb=1024)
    assert digest_span_budget_seconds(tiny) == 1.0
    assert digest_span_budget_seconds(_budgeted(capped, digest_span_budget_seconds=600)) == 600
    assert digest_span_budget_seconds(_budgeted(capped, digest_span_budget_seconds=0)) == 0


def test_an_explicit_row_budget_still_wins(tmp_path: Path) -> None:
    """The derivation is a default, not a policy: an operator who has measured
    their own machine can say so."""
    config = digest_config(tmp_path)
    explicit = _budgeted(config, digest_row_budget=123_456)
    assert digest_row_budget(explicit) == 123_456
    disabled = _budgeted(config, digest_row_budget=0)
    assert digest_row_budget(disabled) == 0


def test_delivery_inputs_out_of_memory_is_not_inputs_never_captured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``read_delivery_inputs`` counted any failed read as an absent channel, so
    out of memory became ``InputsNotCapturedError`` and the tuning digest noted
    "its inputs were never captured" (P02.1 verification, 2026-09-26). The
    section-level test above passes as long as *some* site re-raises; this one
    pins this site."""
    import pandas as pd

    from src.analysis import tuning
    from src.analysis.lambda_delivery import load_lambda_delivery

    def out_of_memory(*_args: Any, **_kwargs: Any) -> Any:
        raise MemoryError("Unable to allocate 21.9 MiB")

    monkeypatch.setattr(tuning, "read_channels", out_of_memory)
    declaration = load_lambda_delivery(Path("data/nissan-xterra-2008/lambda_delivery.yaml"))
    route = tuning.DeliveryRoute(
        target_source="mode01_pid_0x44",
        measured_source="ecm_lambda_pid_0x24",
        rpm_spec="obd:RPM",
        measured_label="obd:O2_WR_LAMBDA_S1",
    )
    with pytest.raises(MemoryError):
        tuning.read_delivery_inputs(
            pd.DataFrame(), route, declaration, calibration=None, afr_stoich=14.7
        )
