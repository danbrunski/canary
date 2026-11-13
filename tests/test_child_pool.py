"""The single-use spawn pool always ends a child it was told to terminate."""

from __future__ import annotations

import asyncio
import re
import threading
import time
from concurrent.futures import BrokenExecutor, Executor, Future, ThreadPoolExecutor
from typing import Any, cast

import pytest

from src.control import child_pool
from src.paths import REPO_ROOT


class _StubbornProcess:
    """Ignores SIGTERM: alive until killed."""

    pid = 4242

    def __init__(self) -> None:
        self.killed = False
        self.joins: list[float | None] = []

    def join(self, timeout: float | None = None) -> None:
        self.joins.append(timeout)

    def is_alive(self) -> bool:
        return not self.killed

    def kill(self) -> None:
        self.killed = True


class _PoliteProcess(_StubbornProcess):
    def is_alive(self) -> bool:
        return False


def test_a_child_that_ignores_sigterm_is_killed_after_the_grace() -> None:
    proc = _StubbornProcess()
    child_pool._reap(proc, grace=0.01)
    assert proc.killed
    assert proc.joins[0] == 0.01


def test_a_child_that_exits_on_sigterm_is_not_killed() -> None:
    proc = _PoliteProcess()
    child_pool._reap(proc, grace=0.01)
    assert not proc.killed


def test_discard_without_terminate_leaves_the_child_alone(monkeypatch: Any) -> None:
    pool = child_pool.spawn_pool()
    reaped: list[Any] = []
    monkeypatch.setattr(child_pool, "_reap", lambda proc, grace: reaped.append(proc))
    child_pool.discard_pool(pool, terminate=False)
    assert reaped == []


# --- run_in_child: outcomes, the slot, the guard against a fifth copy (R29.2) ---


class _Raises(Executor):
    """An executor whose submission or result fails the named way."""

    def __init__(
        self, *, on_submit: BaseException | None = None, result: BaseException | None = None
    ):
        self.on_submit = on_submit
        self.result = result

    def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Future[Any]:
        if self.on_submit is not None:
            raise self.on_submit
        future: Future[Any] = Future()
        if self.result is not None:
            future.set_exception(self.result)
        return future


@pytest.fixture(autouse=True)
def _free_analysis_slot() -> Any:
    yield
    child_pool.release_child(child_pool.ANALYSIS_CHILD)
    assert child_pool.analysis_holder() is None


async def test_a_result_comes_back_and_the_slot_is_free_again() -> None:
    with ThreadPoolExecutor(1) as pool:
        got = await child_pool.run_in_child(lambda: 7, timeout=5, holder="digest", executor=pool)
    assert got == 7
    assert child_pool.analysis_holder() is None


@pytest.mark.parametrize(
    ("executor", "outcome", "cause"),
    [
        (
            _Raises(on_submit=RuntimeError("no fork")),
            child_pool.ChildOutcome.NOT_STARTED,
            RuntimeError,
        ),
        (_Raises(result=BrokenExecutor("died")), child_pool.ChildOutcome.BROKEN, BrokenExecutor),
        (_Raises(result=MemoryError()), child_pool.ChildOutcome.OOM, MemoryError),
        (_Raises(), child_pool.ChildOutcome.TIMED_OUT, TimeoutError),
    ],
    ids=["not_started", "broken", "oom", "timed_out"],
)
async def test_each_way_a_child_gives_no_result_is_a_named_outcome(
    executor: Executor, outcome: child_pool.ChildOutcome, cause: type[BaseException]
) -> None:
    with pytest.raises(child_pool.ChildFailedError) as failed:
        await child_pool.run_in_child(
            lambda: None, timeout=0.05, holder="digest", executor=executor
        )
    assert failed.value.outcome is outcome
    assert isinstance(failed.value.cause, cause)
    assert child_pool.analysis_holder() is None  # an injected executor holds nothing past the call


async def test_the_works_own_exception_crosses_unchanged() -> None:
    def refuse() -> None:
        raise ValueError("not a capture")

    with ThreadPoolExecutor(1) as pool, pytest.raises(ValueError, match="not a capture"):
        await child_pool.run_in_child(refuse, timeout=5, holder="digest", executor=pool)


async def test_an_owned_pool_is_discarded_and_terminated_only_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discards: list[bool] = []
    monkeypatch.setattr(child_pool, "spawn_pool", lambda: ThreadPoolExecutor(1))
    monkeypatch.setattr(
        child_pool, "discard_pool", lambda pool, *, terminate: discards.append(terminate)
    )
    await child_pool.run_in_child(lambda: 1, timeout=5, holder="digest")
    with pytest.raises(child_pool.ChildFailedError):
        await child_pool.run_in_child(
            lambda: threading.Event().wait(0.5), timeout=0.01, holder="digest"
        )
    assert discards == [False, True]


class _ShutdownRaises(ThreadPoolExecutor):
    """A spawn-pool stand-in whose cleanup fails."""

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        super().shutdown(wait=False, cancel_futures=True)
        raise OSError("shutdown blew up")


@pytest.mark.parametrize("fails", [False, True], ids=["result", "timeout"])
async def test_a_pool_whose_cleanup_raises_still_frees_the_slot(
    monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    """The slot is released in a ``finally`` of its own: a raising
    ``pool.shutdown`` used to leave it held until the daemon restarted, and every
    later analysis was refused naming a holder that was long gone."""
    monkeypatch.setattr(child_pool, "spawn_pool", lambda: _ShutdownRaises(1))
    release = threading.Event()
    call = (lambda: release.wait(0.5)) if fails else (lambda: 1)
    with pytest.raises((OSError, child_pool.ChildFailedError)):
        await child_pool.run_in_child(call, timeout=0.01 if fails else 5, holder="digest")
    release.set()
    assert child_pool.analysis_holder() is None


def test_a_shutdown_that_raises_still_terminates_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung child that is attached to the slot would hold it until the process
    ended on its own; the terminate does not depend on the shutdown succeeding."""

    class _Proc:
        terminated = False

        def terminate(self) -> None:
            self.terminated = True

    proc = _Proc()
    pool = child_pool.spawn_pool()
    cast(Any, pool)._processes = {1: proc}
    reaped: list[Any] = []
    monkeypatch.setattr(child_pool, "_reap", lambda p, grace: reaped.append(p))
    monkeypatch.setattr(
        pool, "shutdown", lambda *a, **k: (_ for _ in ()).throw(OSError("shutdown blew up"))
    )
    with pytest.raises(OSError, match="blew up"):
        child_pool.discard_pool(pool, terminate=True)
    assert proc.terminated
    deadline = time.monotonic() + 2
    while not reaped and time.monotonic() < deadline:
        time.sleep(0.01)
    assert reaped == [proc]


async def test_a_second_claim_is_refused_by_the_holders_name_or_waits_its_turn() -> None:
    release = threading.Event()
    with ThreadPoolExecutor(2) as pool:
        first = asyncio.create_task(
            child_pool.run_in_child(
                lambda: release.wait(5) and "first", timeout=10, holder="digest", executor=pool
            )
        )
        while child_pool.analysis_holder() is None:
            await asyncio.sleep(0.01)
        with pytest.raises(child_pool.AnalysisSlotBusyError) as busy:
            await child_pool.run_in_child(
                lambda: "second", timeout=5, holder="workbench", executor=pool
            )
        assert busy.value.holder == "digest"
        assert "held by digest" in str(busy.value)
        waiting = asyncio.create_task(
            child_pool.run_in_child(
                lambda: "second", timeout=5, holder="capture_recompute", executor=pool, slot_wait=5
            )
        )
        await asyncio.sleep(0.2)
        assert not waiting.done()  # it is waiting, not refused
        release.set()
        assert await first == "first"
        assert await waiting == "second"


async def test_cancelling_a_claim_that_is_waiting_leaves_the_slot_with_its_holder() -> None:
    release = threading.Event()
    with ThreadPoolExecutor(2) as pool:
        first = asyncio.create_task(
            child_pool.run_in_child(
                lambda: release.wait(5), timeout=10, holder="digest", executor=pool
            )
        )
        while child_pool.analysis_holder() is None:
            await asyncio.sleep(0.01)
        waiting = asyncio.create_task(
            child_pool.run_in_child(
                lambda: None, timeout=5, holder="workbench", executor=pool, slot_wait=5
            )
        )
        await asyncio.sleep(0.1)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert child_pool.analysis_holder() == "digest"
        release.set()
        await first


def test_the_failure_types_survive_a_pickle_round_trip() -> None:
    import pickle

    failed = pickle.loads(
        pickle.dumps(child_pool.ChildFailedError(child_pool.ChildOutcome.OOM, "out of memory"))
    )
    assert (failed.outcome, str(failed)) == (child_pool.ChildOutcome.OOM, "out of memory")
    busy = pickle.loads(pickle.dumps(child_pool.AnalysisSlotBusyError("digest", "held")))
    assert (busy.holder, str(busy)) == ("digest", "held")


#: Modules that may build a pool themselves. The brain keeps its own lifecycle
#: (it polls its child so it can yield); the list may only shrink.
_MAY_SPAWN = {"src/control/child_pool.py", "src/control/brain_jobs_scheduler.py"}


def test_no_fifth_copy_of_the_bounded_child_runner() -> None:
    """R29.2 closed by a guard, not a count: an analysis child is spawned by
    ``child_pool.run_in_child``, so the slot cannot be forgotten. A module that
    builds its own pool, reaches for the old private names, or (under
    ``src/control/``, where the analysis children live) constructs a
    ``ProcessPoolExecutor`` directly, fails here. The model, feature and inference
    packages have pools of their own with other lifecycles and are not scanned
    for that."""
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "src").rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        if rel not in _MAY_SPAWN and re.search(r"\bspawn_pool\(", text):
            offenders.append(f"{rel}: builds its own spawn pool; use child_pool.run_in_child")
        if re.search(r"\b_(spawn|discard)_pool\b", text):
            offenders.append(f"{rel}: private pool helper; use child_pool.run_in_child")
        if (
            rel.startswith("src/control/")
            and rel != "src/control/child_pool.py"
            and re.search(r"\bProcessPoolExecutor\(", text)
        ):
            offenders.append(f"{rel}: builds a ProcessPoolExecutor; use child_pool.run_in_child")
    assert offenders == []
