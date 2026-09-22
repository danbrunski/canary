"""The single-use spawn pool always ends a child it was told to terminate."""

from __future__ import annotations

from typing import Any

from src.control import child_pool


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
