"""One analysis child at a time across the digest, the workbench and the brain
(E04.4): ``src.control.child_pool``'s registry holds a brain child alive until
its *process* has exited; the workbench refuses ``analysis_busy`` and asks it
to yield; the digest waits, off the loop and bounded, for it to be gone.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src.control import child_pool
from src.control.child_pool import BRAIN_CHILD, CHILD_EXIT_BOUND_SECONDS
from src.control.trip_end import _in_digest_child
from src.control.workbench_jobs import WorkbenchRefusedError, run_off_loop
from tests.configs import isolated_config


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    yield
    child_pool.release_child(BRAIN_CHILD)
    assert asyncio.run(child_pool.wait_for_child_exit(BRAIN_CHILD, 15))


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
