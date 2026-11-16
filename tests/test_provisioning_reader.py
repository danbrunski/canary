"""The cached network reader behind /control/network and /status.network (P2.3)."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence

from src.provisioning.network import RunResult
from src.provisioning.reader import NetworkStatusReader
from tests.provisioning_builders import HEALTHY, IP_JSON, ReaderFakeRunner


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _reader(
    runner: ReaderFakeRunner,
    clock: Clock | None = None,
    spawn: Callable[[Callable[[], None]], None] | None = None,
) -> NetworkStatusReader:
    clock = clock or Clock()
    return NetworkStatusReader(
        runner, ttl_s=5.0, clock=clock, wall_clock=lambda: 1_700_000_000.0, spawn=spawn
    )


def test_reading_carries_the_status_characteristic_facts() -> None:
    reading = _reader(ReaderFakeRunner(HEALTHY)).read()
    payload = reading.to_dict(dashboard_port=8000, now=1_700_000_002.0)
    assert payload["mode"] == "client"
    assert payload["ssid"] == "Starlink"
    assert payload["wifi_interface"] == "wlan0"
    assert payload["addresses"] == {
        "eth0": ["192.168.137.2"],
        "wlan0": ["192.168.1.23", "2001:db8::23"],
    }
    # By IP, never canary.local: Android does not resolve mDNS.
    assert payload["dashboard_url"] == "http://192.168.1.23:8000/"
    assert payload["error"] is None
    assert payload["age_s"] == 2.0


def test_read_is_cached_for_the_ttl() -> None:
    runner, clock = ReaderFakeRunner(HEALTHY), Clock()
    reader = _reader(runner, clock)
    reader.read()
    forks = len(runner.calls)
    assert forks == 4  # ip + device status + mode + ssid
    for _ in range(20):
        reader.read()
    assert len(runner.calls) == forks
    clock.now += 5.1
    reader.read()
    assert len(runner.calls) == 2 * forks


def test_concurrent_reads_share_one_refresh() -> None:
    gate = threading.Event()
    runner = ReaderFakeRunner(HEALTHY)

    def slow(argv: Sequence[str], *, timeout: float) -> RunResult:
        gate.wait(2)
        return runner(argv, timeout=timeout)

    reader = NetworkStatusReader(slow, ttl_s=5.0)
    threads = [threading.Thread(target=reader.read) for _ in range(5)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join(5)
    assert len(runner.calls) == 4


def test_missing_tools_are_an_error_not_no_wifi() -> None:
    missing = RunResult(127, "", "[Errno 2] No such file or directory: 'nmcli'")
    runner = ReaderFakeRunner({("ip",): RunResult(0, IP_JSON, ""), ("nmcli",): missing})
    payload = _reader(runner).read().to_dict(dashboard_port=8000)
    assert payload["mode"] == "unknown"
    assert payload["ssid"] is None
    assert "nmcli is not installed" in payload["error"]
    # ip still answered, so the addresses are real.
    assert payload["addresses"]["eth0"] == ["192.168.137.2"]


def test_networkmanager_not_running_is_named() -> None:
    down = RunResult(8, "", "Error: NetworkManager is not running.")
    runner = ReaderFakeRunner({("ip",): RunResult(127, "", "no ip"), ("nmcli",): down})
    payload = _reader(runner).read().to_dict(dashboard_port=8000)
    assert payload["mode"] == "unknown"
    assert "ip is not installed" in payload["error"]
    assert "NetworkManager is not running" in payload["error"]
    assert payload["addresses"] == {}
    assert payload["dashboard_url"] is None


def test_an_unexpected_exception_is_reported_not_raised() -> None:
    def boom(argv: Sequence[str], *, timeout: float) -> RunResult:
        raise PermissionError("denied")

    payload = NetworkStatusReader(boom).read().to_dict(dashboard_port=8000)
    assert payload["hostname"] is None
    assert payload["error"] == "network status failed: denied"


def test_peek_never_runs_nmcli_on_the_caller() -> None:
    runner, clock = ReaderFakeRunner(HEALTHY), Clock()
    spawned: list[Callable[[], None]] = []
    reader = _reader(runner, clock, spawn=spawned.append)
    assert reader.peek() is None
    assert runner.calls == []
    # A second peek while the refresh is pending does not queue another.
    assert reader.peek() is None
    assert len(spawned) == 1
    spawned.pop()()
    assert len(runner.calls) == 4
    first = reader.peek()
    assert first is not None and first.status is not None
    # Within max age: served from the cache, nothing spawned.
    clock.now += 29
    assert reader.peek() is first
    assert spawned == []
    # Stale: the old copy is served while one refresh starts.
    clock.now += 2
    assert reader.peek() is first
    assert len(spawned) == 1


def test_peek_reuses_a_fresh_read() -> None:
    runner = ReaderFakeRunner(HEALTHY)
    spawned: list[Callable[[], None]] = []
    reader = _reader(runner, spawn=spawned.append)
    reading = reader.read()
    assert reader.peek() is reading
    assert spawned == []
