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
import json
import os
import resource
import shutil
import signal
import time
from collections.abc import Callable
from concurrent.futures import Executor, Future, ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from src.analysis.digest import (
    DIGEST_FILENAME,
    DIGEST_SKIPPED_FILENAME,
    REASON_CHILD_FAILED,
    REASON_CHILD_OUT_OF_MEMORY,
    REASON_CHILD_TIMED_OUT,
    REASON_NOT_A_SESSION,
    REASON_OVER_ROW_BUDGET,
    SessionDigest,
    compute_digest,
    get_or_compute_digest,
    read_digest,
    read_digest_skipped,
)
from src.config import CanaryConfig
from src.control import trip_end
from src.control.trip_end import (
    DIGEST_PEAK_BYTES_PER_ROW,
    TripEndDigestUnavailableError,
    compute_digest_off_loop,
    compute_digest_under_limit,
    digest_address_space_bytes,
    digest_row_budget,
    handle_trip_end_off_loop,
    limit_address_space,
)
from src.preprocessing.loader import count_session_rows
from tests.test_digest import SESSION_END, _config
from tests.test_fingerprint import _write_rich_session

_MIB = 1024 * 1024


def _budgeted(config: CanaryConfig, **daemon: Any) -> CanaryConfig:
    return config.model_copy(update={"daemon": config.daemon.model_copy(update=daemon)})


def _session(tmp_path: Path, name: str = "2026-05-20T180000") -> Path:
    return _write_rich_session(tmp_path, name=name)


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


class _MemoryErrorExecutor(Executor):
    """A child that ran out of address space, as ``RLIMIT_AS`` leaves it."""

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        future: Future[Any] = Future()
        future.set_exception(MemoryError())
        return future


def _slow_digest(seconds: float, calls: list[bool]) -> Callable[..., SessionDigest]:
    def fake(session_dir: Path, config: CanaryConfig, **_: Any) -> SessionDigest:
        try:
            asyncio.get_running_loop()
            calls.append(True)
        except RuntimeError:
            calls.append(False)
        time.sleep(seconds)
        return SessionDigest(
            schema_version=1,
            computed_at="2026-05-20T18:05:00+00:00",
            session_id=session_dir.name,
            duration_seconds=1.0,
            mode_seconds={},
            anomaly_counts_by_source={},
            top_behavior_deviations=[],
            fingerprint_top_movers=[],
            new_dtcs=[],
            new_ledger_events=[],
        )

    return fake


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
    monkeypatch.setattr(trip_end, "compute_digest_under_limit", _slow_digest(1.0, calls))
    with ThreadPoolExecutor(max_workers=1) as pool:
        payload, ticks = await _ticks_during(
            handle_trip_end_off_loop(session, _config(tmp_path), executor=pool)
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
    monkeypatch.setattr(trip_end, "compute_digest_under_limit", _slow_digest(0.1, calls))
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner, follower = await asyncio.gather(
            handle_trip_end_off_loop(session, _config(tmp_path), executor=pool),
            handle_trip_end_off_loop(session, _config(tmp_path), executor=pool),
        )
    assert sum(result is not None for result in (owner, follower)) == 1
    assert calls == [False]
    assert read_digest(session) is not None


# --- 2. the row budget refuses before anything is started ------------------


async def test_a_session_over_the_row_budget_is_refused_and_no_child_starts(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    rows = count_session_rows(session)
    assert rows is not None and rows > 10
    config = _budgeted(_config(tmp_path), digest_row_budget=10)
    pool = _RefusingExecutor()

    result = await handle_trip_end_off_loop(session, config, executor=pool)

    assert result is None
    assert pool.submissions == 0
    assert not (session / DIGEST_FILENAME).exists()
    skipped = read_digest_skipped(session)
    assert skipped is not None
    assert skipped["reason"] == REASON_OVER_ROW_BUDGET
    assert skipped["row_count"] == rows
    assert skipped["row_budget"] == 10
    assert f"{rows:,}" in skipped["detail"] and "10-row" in skipped["detail"]
    # The daemon carried on: nothing raised, and the refusal cost no await at
    # all — a footer read, then straight back to the loop.
    await asyncio.sleep(0)


async def test_a_replay_directory_is_refused_by_the_shared_function(tmp_path: Path) -> None:
    """The GET path refused ``_replay-*`` via ``resolve_session`` all along;
    the trip-end hook bypassed it and digested 5.9 M replayed rows."""
    session = _session(tmp_path, name="_replay-2026-05-20T180000")
    result = await handle_trip_end_off_loop(
        session, _config(tmp_path), executor=_RefusingExecutor()
    )
    assert result is None
    skipped = read_digest_skipped(session)
    assert skipped is not None
    assert skipped["reason"] == REASON_NOT_A_SESSION
    assert "_replay-2026-05-20T180000" in skipped["detail"]


def test_the_in_process_path_is_gated_by_the_same_budget(tmp_path: Path) -> None:
    """``get_or_compute_digest`` is the shared function every caller reaches
    (HTTP route, MCP tool, scripts) — the budget lives there, not in the
    daemon wiring, so no caller can forget it."""
    session = _session(tmp_path)
    config = _budgeted(_config(tmp_path), digest_row_budget=10)
    assert get_or_compute_digest(session, config, now=SESSION_END) is None
    assert not (session / DIGEST_FILENAME).exists()
    assert (read_digest_skipped(session) or {})["reason"] == REASON_OVER_ROW_BUDGET


def test_a_zero_budget_disables_the_row_check(tmp_path: Path) -> None:
    session = _session(tmp_path)
    config = _budgeted(_config(tmp_path), digest_row_budget=0)
    assert get_or_compute_digest(session, config, now=SESSION_END) is not None
    assert read_digest_skipped(session) is None


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
        session, _config(tmp_path), now=SESSION_END, address_space_bytes=1234 * _MIB
    )
    assert order == [f"limit:{1234 * _MIB}", "compute"]
    assert digest.session_id == session.name


def test_the_ceiling_comes_from_config_when_set(tmp_path: Path) -> None:
    config = _budgeted(_config(tmp_path), digest_address_space_mb=1500)
    assert digest_address_space_bytes(config) == 1500 * _MIB


def test_the_derived_ceiling_is_bounded_and_below_total_ram(tmp_path: Path) -> None:
    config = _config(tmp_path)
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
    config = _config(tmp_path)

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
    result = await handle_trip_end_off_loop(session, _config(tmp_path), timeout=0.05)
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
        assert proc.exitcode == -signal.SIGTERM, "the child exited on its own, not terminated"


async def test_a_dead_child_is_reported_not_hung(tmp_path: Path) -> None:
    class _Broken(Executor):
        def submit(self, fn: Callable[..., Any], /, *a: Any, **k: Any) -> Future[Any]:
            from concurrent.futures.process import BrokenProcessPool

            future: Future[Any] = Future()
            future.set_exception(BrokenProcessPool("child terminated abruptly"))
            return future

    session = _session(tmp_path)
    with pytest.raises(TripEndDigestUnavailableError, match="failed"):
        await compute_digest_off_loop(session, _config(tmp_path), executor=_Broken())
    # The hook turns it into a recorded refusal, never an exception.
    assert await handle_trip_end_off_loop(session, _config(tmp_path), executor=_Broken()) is None
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

    get_or_compute_digest(inline, _config(tmp_path), now=SESSION_END)
    await handle_trip_end_off_loop(child, _config(tmp_path), now=SESSION_END)

    assert (child / DIGEST_FILENAME).read_text() == (inline / DIGEST_FILENAME).read_text()
    assert read_digest_skipped(child) is None


async def test_an_earlier_refusal_is_cleared_once_a_digest_lands(tmp_path: Path) -> None:
    session = _session(tmp_path)
    tight = _budgeted(_config(tmp_path), digest_row_budget=10)
    assert await handle_trip_end_off_loop(session, tight) is None
    assert (session / DIGEST_SKIPPED_FILENAME).exists()

    assert await handle_trip_end_off_loop(session, _config(tmp_path), now=SESSION_END) is not None
    assert read_digest(session) is not None
    assert not (session / DIGEST_SKIPPED_FILENAME).exists(), (
        "a session cannot be both computed and refused"
    )


async def test_an_already_digested_session_is_not_recomputed(tmp_path: Path) -> None:
    session = _session(tmp_path)
    assert await handle_trip_end_off_loop(session, _config(tmp_path), now=SESSION_END) is not None
    assert (
        await handle_trip_end_off_loop(
            session, _config(tmp_path), now=SESSION_END, executor=_RefusingExecutor()
        )
        is None
    )


# --- 6. a refusal reads as a refusal downstream ---------------------------


def test_the_catalog_tells_refused_apart_from_never_attempted(tmp_path: Path) -> None:
    from src.storage.catalog import SessionCatalog

    refused = _session(tmp_path, name="2026-05-20T180000")
    untouched = _session(tmp_path, name="2026-05-21T180000")
    tight = _budgeted(_config(tmp_path), digest_row_budget=10)
    get_or_compute_digest(refused, tight, now=SESSION_END)

    with SessionCatalog(tmp_path / "catalog.db") as catalog:
        refused_row = catalog.record_session(refused)
        untouched_row = catalog.record_session(untouched)

    assert refused_row.digest is None
    assert refused_row.digest_skipped is not None
    assert refused_row.digest_skipped["reason"] == REASON_OVER_ROW_BUDGET
    assert untouched_row.digest is None
    assert untouched_row.digest_skipped is None


def test_the_catalog_drops_the_refusal_once_a_digest_exists(tmp_path: Path) -> None:
    from src.storage.catalog import SessionCatalog

    session = _session(tmp_path)
    get_or_compute_digest(session, _budgeted(_config(tmp_path), digest_row_budget=10))
    # Simulate a refusal sidecar left behind by an older build.
    get_or_compute_digest(session, _config(tmp_path), now=SESSION_END)
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
    capped = _budgeted(_config(tmp_path), digest_row_budget=None, digest_address_space_mb=4096)
    expected = 4096 * 1024 * 1024 // DIGEST_PEAK_BYTES_PER_ROW
    assert digest_row_budget(capped) == expected
    # Halving the ceiling halves the budget: they move together, by construction.
    halved = _budgeted(capped, digest_address_space_mb=2048)
    assert digest_row_budget(halved) == expected // 2


def test_an_explicit_row_budget_still_wins(tmp_path: Path) -> None:
    """The derivation is a default, not a policy: an operator who has measured
    their own machine can say so."""
    config = _config(tmp_path)
    explicit = _budgeted(config, digest_row_budget=123_456)
    assert digest_row_budget(explicit) == 123_456
    disabled = _budgeted(config, digest_row_budget=0)
    assert digest_row_budget(disabled) == 0
