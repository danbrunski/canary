"""R29.9 -- the host CPU baseline has an owner that samples on its own clock.

``HealthMonitor._cpu_percent`` used to be the delta since the previous
``snapshot()``, and every ``/status`` and MCP ``get_status`` calls
``snapshot()``: the reading depended on who polled last, and two pollers on
different cadences each saw a window set by the other.
"""

from __future__ import annotations

import gc
import threading
import time
from collections.abc import Callable
from itertools import count

import pytest

import src.daemon.health as health_mod
from src.daemon.health import HealthMonitor, HostCpuSampler


class Host:
    """A scripted ``/proc/stat`` and a clock, advanced together by :meth:`tick`.

    ``busy`` is the fraction of the next interval's jiffies spent not idle.
    """

    def __init__(self, interval: float = 1.0) -> None:
        self.now = 1000.0
        self.idle = 0.0
        self.total = 0.0
        self.interval = interval
        self.reads = 0

    def read(self) -> tuple[float, float]:
        self.reads += 1
        return self.idle, self.total

    def clock(self) -> float:
        return self.now

    def tick(self, busy: float, sampler: HostCpuSampler) -> None:
        """One interval at ``busy`` utilization, then the sampler's own sample."""
        jiffies = 400.0 * self.interval  # 4 cpus at 100 Hz
        self.now += self.interval
        self.total += jiffies
        self.idle += jiffies * (1.0 - busy)
        sampler.sample()

    def sampler(self, window: float = 5.0) -> HostCpuSampler:
        sampler = HostCpuSampler(
            interval_seconds=self.interval,
            window_seconds=window,
            read=self.read,
            clock=self.clock,
        )
        sampler.sample()  # the baseline `start()` takes
        return sampler


def test_no_value_until_the_sampler_has_two_readings() -> None:
    host = Host()
    sampler = host.sampler()
    assert sampler.cpu_percent() is None
    host.tick(0.5, sampler)
    assert sampler.cpu_percent() == 0.5


def test_the_value_is_the_busy_fraction_over_the_sampled_interval() -> None:
    host = Host()
    sampler = host.sampler()
    host.tick(0.25, sampler)
    assert sampler.cpu_percent() == 0.25
    host.tick(1.0, sampler)
    # Two intervals now sit inside the window: 25 % then 100 % is 62.5 %.
    assert sampler.cpu_percent() == 0.625


def test_the_window_forgets_load_older_than_it_reaches() -> None:
    host = Host()
    sampler = host.sampler(window=3.0)
    for _ in range(3):
        host.tick(1.0, sampler)  # a burst
    assert sampler.cpu_percent() == 1.0
    for _ in range(3):
        host.tick(0.0, sampler)  # then idle for the whole window
    assert sampler.cpu_percent() == 0.0


def test_a_snapshot_never_moves_the_baseline() -> None:
    """The defect: a second poller used to shorten the first one's window."""
    host = Host()
    monitor = HealthMonitor(host_cpu=host.sampler())
    host.tick(0.4, monitor.host_cpu)
    reads = host.reads

    values = [monitor.snapshot()["system"]["cpu_percent"] for _ in range(50)]

    assert values == [0.4] * 50
    assert host.reads == reads  # snapshot() reads the ring; it takes no sample


def test_two_back_to_back_snapshots_return_the_same_cpu_value() -> None:
    host = Host()
    monitor = HealthMonitor(host_cpu=host.sampler())
    host.tick(0.3, monitor.host_cpu)
    host.tick(0.7, monitor.host_cpu)
    first = monitor.snapshot()["system"]["cpu_percent"]
    second = monitor.snapshot()["system"]["cpu_percent"]
    assert first is not None and first == second


def test_pollers_on_different_cadences_read_the_same_number() -> None:
    host = Host()
    monitor = HealthMonitor(host_cpu=host.sampler())
    host.tick(0.6, monitor.host_cpu)
    fast = [monitor._cpu_percent() for _ in range(20)]
    slow = monitor._cpu_percent()
    assert set(fast) == {slow} == {0.6}


def test_the_value_moves_only_when_the_sampler_samples() -> None:
    host = Host()
    monitor = HealthMonitor(host_cpu=host.sampler(window=1.0))
    host.tick(0.2, monitor.host_cpu)
    assert monitor._cpu_percent() == 0.2
    host.tick(0.9, monitor.host_cpu)
    assert monitor._cpu_percent() == 0.9


def test_a_sampler_that_stopped_reads_as_unknown_not_as_its_last_value() -> None:
    host = Host()
    sampler = host.sampler()
    host.tick(0.5, sampler)
    assert sampler.cpu_percent() == 0.5
    host.now += 3.5 * host.interval  # no sample for more than three intervals
    assert sampler.cpu_percent() is None


def test_a_platform_without_proc_stat_reads_as_unknown() -> None:
    sampler = HostCpuSampler(read=lambda: None)
    sampler.sample()
    sampler.sample()
    assert sampler.cpu_percent() is None


def test_a_counter_that_did_not_advance_reads_as_unknown() -> None:
    host = Host()
    sampler = host.sampler()
    host.now += 1.0
    sampler.sample()  # /proc/stat unchanged: no jiffies elapsed
    assert sampler.cpu_percent() is None


def test_the_interval_must_fit_the_window() -> None:
    with pytest.raises(ValueError):
        HostCpuSampler(interval_seconds=2.0, window_seconds=1.0)
    with pytest.raises(ValueError):
        HostCpuSampler(interval_seconds=0.0)


# ---------------------------------------------------------------------------
# The thread
# ---------------------------------------------------------------------------


def _wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return condition()


def _sampler_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "canary-host-sampler" and t.is_alive()]


def test_the_thread_samples_on_its_own_clock_until_stopped() -> None:
    reads = count()
    sampler = HostCpuSampler(
        interval_seconds=0.01,
        window_seconds=0.05,
        read=lambda: (float(next(reads)), float(next(reads) + 1_000_000)),
    )
    assert not sampler.running
    sampler.start()
    sampler.start()  # idempotent: one thread
    try:
        assert sampler.running
        assert len([t for t in _sampler_threads() if t is sampler._thread]) == 1
        assert _wait_for(lambda: sampler.cpu_percent() is not None)
    finally:
        sampler.stop()
    assert _wait_for(lambda: not sampler.running)


def test_a_dropped_sampler_leaves_no_thread_behind() -> None:
    sampler = HostCpuSampler(interval_seconds=0.01, window_seconds=0.05, read=lambda: (1.0, 2.0))
    sampler.start()
    thread = sampler._thread
    assert thread is not None and thread.is_alive()
    del sampler
    gc.collect()
    assert _wait_for(lambda: not thread.is_alive())


def test_concurrent_starts_make_exactly_one_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sixteen /status requests used to start seven samplers, of which stop()
    could reach one."""
    started: list[threading.Event] = []

    def parked(ref: object, stop: threading.Event, interval: float) -> None:
        started.append(stop)
        stop.wait(10.0)

    monkeypatch.setattr(health_mod, "_run_sampler", parked)

    def slow_read() -> tuple[float, float]:
        time.sleep(0.02)  # /proc/stat on a busy Pi; widens the check-then-start window
        return (1.0, 2.0)

    sampler = HostCpuSampler(read=slow_read)
    barrier = threading.Barrier(16)

    def start() -> None:
        barrier.wait()
        sampler.start()

    callers = [threading.Thread(target=start) for _ in range(16)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join()
    try:
        assert _wait_for(lambda: len(started) >= 1)
        time.sleep(0.05)  # a duplicate would have announced itself by now
        assert len(started) == 1
        assert sampler.running
    finally:
        sampler.stop()
    assert _wait_for(lambda: not sampler.running)


def test_start_after_stop_starts_a_new_thread_even_before_the_old_one_noticed() -> None:
    sampler = HostCpuSampler(interval_seconds=0.5, window_seconds=1.0, read=lambda: (1.0, 2.0))
    sampler.start()
    first = sampler._thread
    sampler.stop()
    sampler.start()  # the first thread is still in its wait
    second = sampler._thread
    try:
        assert second is not first and second is not None and second.is_alive()
        assert _wait_for(lambda: first is not None and not first.is_alive())
        assert sampler.running
    finally:
        sampler.stop()


def test_a_monitor_starts_its_own_sampler_on_the_first_read_and_only_one() -> None:
    monitor = HealthMonitor()
    assert not monitor.host_cpu.running  # constructing a monitor starts nothing
    try:
        monitor.snapshot()
        monitor.snapshot()
        monitor.start_host_sampler()
        assert monitor.host_cpu.running
        assert len([t for t in _sampler_threads() if t is monitor.host_cpu._thread]) == 1
    finally:
        monitor.host_cpu.stop()


def test_a_sampler_handed_to_a_monitor_is_not_started_behind_the_callers_back() -> None:
    host = Host()
    sampler = host.sampler()
    monitor = HealthMonitor(host_cpu=sampler)
    monitor.snapshot()
    assert not sampler.running
