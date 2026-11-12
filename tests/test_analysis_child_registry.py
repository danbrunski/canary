"""One analysis child at a time across the digest, the workbench, the capture
recompute and the brain (E04.4, R29.2): ``src.control.child_pool``'s registry
holds a brain child alive until its *process* has exited; the workbench refuses
``analysis_busy`` and asks it to yield; the digest waits, off the loop and
bounded, for it to be gone. One exclusive analysis slot is claimed by the
digest, every workbench job and the capture-quality recompute, so none of them
overlaps another.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from src.control import capture_finalize, child_pool
from src.control.capture_finalize import CaptureQualityUnavailableError, recompute_quality_off_loop
from src.control.child_pool import BRAIN_CHILD, CHILD_EXIT_BOUND_SECONDS
from src.control.trip_end import TripEndDigestUnavailableError, _in_digest_child
from src.control.workbench_jobs import WorkbenchRefusedError, run_off_loop
from tests.configs import isolated_config


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    yield
    child_pool.release_child(BRAIN_CHILD)
    child_pool.release_child(child_pool.ANALYSIS_CHILD)
    assert asyncio.run(child_pool.wait_for_child_exit(BRAIN_CHILD, 15))
    assert child_pool.analysis_holder() is None


def test_a_brain_child_is_alive_until_its_process_has_exited() -> None:
    child_pool.claim_child(BRAIN_CHILD)
    pool = child_pool.spawn_pool()
    pool.submit(time.sleep, 60)
    child_pool.attach_child(BRAIN_CHILD, pool)
    child_pool.release_child(BRAIN_CHILD)  # the computation is abandoned...
    assert child_pool.child_alive(BRAIN_CHILD)  # ...but the process still runs
    child_pool.discard_pool(pool, terminate=True)
    assert asyncio.run(child_pool.wait_for_child_exit(BRAIN_CHILD, CHILD_EXIT_BOUND_SECONDS))
    assert not child_pool.child_alive(BRAIN_CHILD)


def test_the_workbench_refuses_busy_and_asks_the_brain_to_yield(tmp_path: Path) -> None:
    config = isolated_config(tmp_path)
    child_pool.claim_child(BRAIN_CHILD)
    with pytest.raises(WorkbenchRefusedError) as refused:
        asyncio.run(run_off_loop("classify", {"session_id": "x"}, config))
    assert refused.value.status == 409
    assert refused.value.detail["error"] == "analysis_busy"
    assert refused.value.detail["reason"] == "brain_child_yielding"
    assert child_pool.yield_requested(BRAIN_CHILD)


def test_the_digest_waits_off_loop_for_a_yielded_brain_child(tmp_path: Path) -> None:
    child_pool.claim_child(BRAIN_CHILD)
    events: list[tuple[str, float]] = []

    def digest_work() -> str:
        events.append(("digest_started", time.monotonic()))
        return "digest"

    async def scenario() -> str:
        async def brain_yields() -> None:
            while not child_pool.yield_requested(BRAIN_CHILD):
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.3)  # the child takes a while to go
            events.append(("brain_exited", time.monotonic()))
            child_pool.release_child(BRAIN_CHILD)

        gaps: list[float] = []

        async def heartbeat() -> None:
            loop = asyncio.get_running_loop()
            last = loop.time()
            while not events or events[-1][0] != "digest_started":
                await asyncio.sleep(0.01)
                gaps.append(loop.time() - last)
                last = loop.time()

        yielding = asyncio.create_task(brain_yields())
        beat = asyncio.create_task(heartbeat())
        result: str = await _in_digest_child(
            digest_work,
            tmp_path,
            executor=ThreadPoolExecutor(max_workers=1),
            timeout=10,
            what="digest",
        )
        await yielding
        await beat
        assert max(gaps) < 0.2  # the wait never blocked the loop
        return result

    assert asyncio.run(scenario()) == "digest"
    order = [name for name, _t in events]
    assert order == ["brain_exited", "digest_started"]


def test_the_digest_goes_ahead_after_the_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child that will not die cannot stall the digest forever."""
    monkeypatch.setattr(child_pool, "CHILD_EXIT_BOUND_SECONDS", 0.2)
    child_pool.claim_child(BRAIN_CHILD)
    started = time.monotonic()
    result = asyncio.run(
        _in_digest_child(
            lambda: "digest",
            tmp_path,
            executor=ThreadPoolExecutor(max_workers=1),
            timeout=10,
            what="digest",
        )
    )
    assert result == "digest"
    assert 0.2 <= time.monotonic() - started < 2


# --- the analysis slot: digest, workbench and capture recompute (R29.2) ---------


class _Holder:
    """A digest child that runs until released, holding the analysis slot."""

    def __init__(self, tmp_path: Path) -> None:
        self.release = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.tmp_path = tmp_path
        self.task: asyncio.Task[Any] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(
            _in_digest_child(
                lambda: self.release.wait(10) and "digest",
                self.tmp_path,
                executor=self.pool,
                timeout=30,
                what="digest",
            )
        )
        while child_pool.analysis_holder() is None:
            await asyncio.sleep(0.01)

    async def finish(self) -> Any:
        self.release.set()
        assert self.task is not None
        result = await self.task
        self.pool.shutdown()
        return result


@pytest.fixture
def fake_recompute(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The recompute's child entry replaced by a recorder, so these tests are
    about who may run it, not the scoring."""
    ran: list[str] = []

    def fake(session_dir: Path, kwargs: Any, *, address_space_bytes: int | None = None) -> str:
        ran.append(session_dir.name)
        return "scored"

    monkeypatch.setattr(capture_finalize, "recompute_under_limit", fake)
    return ran


def test_a_recompute_requested_during_a_digest_is_refused_by_name_past_its_wait(
    tmp_path: Path, fake_recompute: list[str]
) -> None:
    """R29.2: a capture-stop recompute used to overlap a trip-end digest. It now
    finds the analysis child taken, and past its bounded wait says who has it."""

    async def scenario() -> None:
        digest = _Holder(tmp_path)
        await digest.start()
        with ThreadPoolExecutor(1) as pool, pytest.raises(CaptureQualityUnavailableError) as err:
            await recompute_quality_off_loop(tmp_path, executor=pool, slot_wait=0.2)
        assert "held by digest" in str(err.value)
        assert "could not start" in str(err.value)
        assert fake_recompute == []  # it never ran beside the digest
        assert await digest.finish() == "digest"

    asyncio.run(scenario())


def test_a_recompute_requested_during_a_digest_waits_for_it_to_finish(
    tmp_path: Path, fake_recompute: list[str]
) -> None:
    """The choice made for ``capture_finalize`` (see ``RECOMPUTE_SLOT_WAIT_SECONDS``):
    the stop-time verdict cannot be retried, so within the bound it waits."""

    async def scenario() -> list[str]:
        digest = _Holder(tmp_path)
        await digest.start()
        with ThreadPoolExecutor(1) as pool:
            recompute = asyncio.create_task(
                recompute_quality_off_loop(tmp_path, executor=pool, slot_wait=10)
            )
            await asyncio.sleep(0.3)
            assert not recompute.done() and fake_recompute == []  # waiting, not running beside it
            await digest.finish()
            assert str(await recompute) == "scored"
        return fake_recompute

    assert asyncio.run(scenario()) == [tmp_path.name]


def test_a_deadline_ends_the_wait_for_a_digest_however_long_the_slot_wait_is(
    tmp_path: Path, fake_recompute: list[str]
) -> None:
    """The daemon's shutdown stop passes the unit's stop deadline: the default
    120 s wait for a digest becomes whatever that leaves, and the digest is not
    disturbed (it is cut by the shutdown's own settle, not by the judge)."""

    async def scenario() -> float:
        digest = _Holder(tmp_path)
        await digest.start()
        loop = asyncio.get_running_loop()
        started = loop.time()
        with ThreadPoolExecutor(1) as pool, pytest.raises(CaptureQualityUnavailableError) as err:
            await recompute_quality_off_loop(tmp_path, executor=pool, deadline=started + 0.3)
        elapsed = loop.time() - started
        assert "held by digest" in str(err.value)
        assert fake_recompute == []
        assert child_pool.analysis_holder() == "digest"
        assert await digest.finish() == "digest"
        return elapsed

    assert asyncio.run(scenario()) < capture_finalize.RECOMPUTE_SLOT_WAIT_SECONDS / 4


def test_a_deadline_cancels_a_recompute_that_is_still_computing_and_frees_the_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()

    def hangs(session_dir: Path, kwargs: Any, *, address_space_bytes: int | None = None) -> str:
        release.wait(10)
        return "late"

    monkeypatch.setattr(capture_finalize, "recompute_under_limit", hangs)

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(1) as pool:
            with pytest.raises(CaptureQualityUnavailableError, match="before its deadline"):
                await recompute_quality_off_loop(
                    tmp_path, executor=pool, deadline=loop.time() + 0.3
                )
            assert child_pool.analysis_holder() is None  # the slot did not outlive the call
            release.set()

    asyncio.run(scenario())


def test_a_deadline_also_bounds_the_queue_behind_another_recompute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One recompute runs per loop at a time; a stop queued behind one (an HTTP
    ``/control/capture/quality`` read, say) is still held to its deadline."""
    release = threading.Event()
    started: list[str] = []

    def slow(session_dir: Path, kwargs: Any, *, address_space_bytes: int | None = None) -> str:
        started.append(session_dir.name)
        release.wait(10)
        return "scored"

    monkeypatch.setattr(capture_finalize, "recompute_under_limit", slow)
    other = tmp_path / "other"
    other.mkdir()

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(2) as pool:
            first = asyncio.create_task(recompute_quality_off_loop(other, executor=pool))
            while not started:
                await asyncio.sleep(0.01)
            with pytest.raises(CaptureQualityUnavailableError, match="before its deadline"):
                await recompute_quality_off_loop(
                    tmp_path, executor=pool, deadline=loop.time() + 0.3
                )
            assert not first.done()  # the other recompute carried on untouched
            assert started == ["other"]
            release.set()
            assert str(await first) == "scored"

    asyncio.run(scenario())


def test_a_digest_waits_for_a_recompute_and_is_refused_by_name_past_its_timeout(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        release = threading.Event()
        with ThreadPoolExecutor(2) as pool:
            recompute = asyncio.create_task(
                child_pool.run_in_child(
                    lambda: release.wait(10),
                    timeout=30,
                    holder="capture_recompute",
                    executor=pool,
                )
            )
            while child_pool.analysis_holder() is None:
                await asyncio.sleep(0.01)
            with pytest.raises(TripEndDigestUnavailableError) as err:
                await _in_digest_child(
                    lambda: "digest", tmp_path, executor=pool, timeout=0.2, what="digest"
                )
            assert err.value.reason == "child_failed"
            assert "held by capture_recompute" in str(err.value)
            release.set()
            await recompute

    asyncio.run(scenario())


def test_the_workbench_refuses_busy_naming_the_holder_while_a_recompute_runs(
    tmp_path: Path,
) -> None:
    config = isolated_config(tmp_path)

    async def scenario() -> dict[str, Any]:
        release = threading.Event()
        with ThreadPoolExecutor(2) as pool:
            recompute = asyncio.create_task(
                child_pool.run_in_child(
                    lambda: release.wait(10),
                    timeout=30,
                    holder="capture_recompute",
                    executor=pool,
                )
            )
            while child_pool.analysis_holder() is None:
                await asyncio.sleep(0.01)
            try:
                with pytest.raises(WorkbenchRefusedError) as refused:
                    await run_off_loop("classify", {"session_id": "x"}, config, executor=pool)
            finally:
                release.set()
                await recompute
        assert refused.value.status == 409
        return refused.value.detail

    detail = asyncio.run(scenario())
    assert (detail["error"], detail["reason"]) == ("analysis_busy", "child_busy")
    assert detail["holder"] == "capture_recompute"


def test_the_brain_yields_to_whoever_holds_the_analysis_slot(tmp_path: Path) -> None:
    from src.control.brain_jobs import BrainScheduler
    from src.vehicle.profiles import VehicleProfile, scope_config

    # The brain is per vehicle: it refuses a config that is not scoped to a profile.
    config = scope_config(
        isolated_config(tmp_path), VehicleProfile(slug="test-vehicle", vin="", name="Test")
    )
    scheduler = BrainScheduler(config, activity=lambda: None)
    assert scheduler.blocked() is None

    async def scenario() -> tuple[str | None, str | None]:
        release = threading.Event()
        with ThreadPoolExecutor(1) as pool:
            held = asyncio.create_task(
                child_pool.run_in_child(
                    lambda: release.wait(10), timeout=30, holder="capture_recompute", executor=pool
                )
            )
            while child_pool.analysis_holder() is None:
                await asyncio.sleep(0.01)
            seen = scheduler.blocked(), scheduler._yield_reason()
            release.set()
            await held
        return seen

    assert asyncio.run(scenario()) == ("capture_recompute_running", "capture_recompute_running")
    assert scheduler.blocked() is None


def test_a_request_serving_caller_is_refused_at_once_not_queued(tmp_path: Path) -> None:
    """The tuning refresh serves an HTTP request, so it passes ``slot_wait=0``:
    refused by name (a ``failed`` note on the stored digest), not held for the
    holder's whole run."""

    async def scenario() -> float:
        digest = _Holder(tmp_path)
        await digest.start()
        started = time.monotonic()
        with ThreadPoolExecutor(1) as pool, pytest.raises(TripEndDigestUnavailableError) as err:
            await _in_digest_child(
                lambda: None,
                tmp_path,
                executor=pool,
                timeout=30,
                what="tuning refresh",
                slot_wait=0.0,
            )
        assert "held by digest" in str(err.value)
        elapsed = time.monotonic() - started
        await digest.finish()
        return elapsed

    assert asyncio.run(scenario()) < 1.0
