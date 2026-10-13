"""The network facts the dashboard shows, read cheaply (roadmap P2.3).

The same facts as the BLE status characteristic (:mod:`.status`) -- hostname,
mode, SSID, addresses, the dashboard URL by IP -- for the daemon's
``GET /control/network`` and ``GET /status``. A reading forks ``ip`` and up to
three ``nmcli`` processes, so it is cached: a phone polling the page, or every
open dashboard polling ``/status``, must not fork nmcli several times a second.

An empty reading says which kind of empty it is. NetworkManager missing (a
laptop dev install) or not running is an ``error``, and the mode reads
``unknown`` rather than ``no-wifi``: "this Pi has no WiFi" and "nobody asked
NetworkManager" demand opposite responses.

Reads only, unprivileged: ``nmcli device status``/``connection show`` and
``ip -j addr`` need no root. Nothing here imports D-Bus.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from src.provisioning.network import (
    NetworkManagerCli,
    NetworkStatus,
    Runner,
    RunResult,
    run_command,
)
from src.provisioning.status import dashboard_url

#: How long ``GET /control/network`` reuses a reading.
DEFAULT_TTL_S = 5.0
#: How stale ``/status`` lets its copy get before refreshing it in the
#: background. ``/status`` never waits on nmcli.
STATUS_MAX_AGE_S = 30.0
#: Exit status :func:`run_command` reports for a program that is not installed.
_NOT_FOUND = 127


@dataclass(frozen=True)
class NetworkReading:
    """One read of the Pi's network, or why there is none."""

    status: NetworkStatus | None
    #: ``None`` when every command answered; otherwise what failed, by name.
    error: str | None
    #: Wall-clock epoch of the read, for the age a client shows.
    read_at: float
    #: An nmcli call failed, so mode and SSID are unknown (addresses from
    #: ``ip`` may still be good).
    nmcli_failed: bool = False

    def to_dict(self, *, dashboard_port: int, now: float | None = None) -> dict[str, Any]:
        status = self.status
        now = time.time() if now is None else now
        mode = None if status is None else status.mode
        if self.nmcli_failed:
            mode = "unknown"
        return {
            "hostname": None if status is None else status.hostname,
            "mode": mode,
            "ssid": None if status is None or self.nmcli_failed else status.ssid,
            "wifi_interface": None if status is None else status.wifi_interface,
            "addresses": {} if status is None else status.addresses,
            "dashboard_url": None if status is None else dashboard_url(status, dashboard_port),
            "read_at": self.read_at,
            "age_s": round(max(0.0, now - self.read_at), 1),
            "error": self.error,
        }


def _describe_failure(argv: Sequence[str], result: RunResult) -> str:
    if result.returncode == _NOT_FOUND:
        return f"{argv[0]} is not installed (NetworkManager and iproute2 are Pi-side tools)"
    detail = result.stderr.strip() or "no output"
    return f"{' '.join(argv[:3])} failed ({result.returncode}): {detail}"


class NetworkStatusReader:
    """Single-flight, cached :meth:`NetworkManagerCli.status`.

    ``read`` blocks (route threadpool); ``peek`` never does, refreshing a
    stale copy on a background thread (``/status``).
    """

    def __init__(
        self,
        runner: Runner = run_command,
        *,
        interface: str | None = None,
        ttl_s: float = DEFAULT_TTL_S,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        spawn: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._runner = runner
        self._failures: list[tuple[Sequence[str], RunResult]] = []
        self._network = NetworkManagerCli(self._recording_runner, interface=interface)
        self._ttl_s = ttl_s
        self._clock = clock
        self._wall_clock = wall_clock
        self._spawn = spawn or _spawn_daemon_thread
        #: Held for the whole of a refresh, so concurrent callers share one.
        self._lock = threading.Lock()
        self._cached: tuple[NetworkReading, float] | None = None

    def _recording_runner(self, argv: Sequence[str], *, timeout: float) -> RunResult:
        result = self._runner(argv, timeout=timeout)
        if result.returncode != 0:
            self._failures.append((tuple(argv), result))
        return result

    def _fresh(self, max_age_s: float) -> NetworkReading | None:
        cached = self._cached
        if cached is not None and self._clock() - cached[1] < max_age_s:
            return cached[0]
        return None

    def _refresh_locked(self) -> NetworkReading:
        self._failures = []
        status: NetworkStatus | None
        try:
            status = self._network.status()
            errors = [_describe_failure(argv, result) for argv, result in self._failures]
        except Exception as exc:  # noqa: BLE001 -- a read must report, never raise
            status, errors = None, [f"network status failed: {exc}"]
        reading = NetworkReading(
            status=status,
            error="; ".join(dict.fromkeys(errors)) or None,
            read_at=self._wall_clock(),
            nmcli_failed=any(argv[0] == "nmcli" for argv, _ in self._failures),
        )
        self._cached = (reading, self._clock())
        return reading

    def read(self) -> NetworkReading:
        """A reading at most ``ttl_s`` old, refreshing it here if needed."""
        with self._lock:
            return self._fresh(self._ttl_s) or self._refresh_locked()

    def peek(self, max_age_s: float = STATUS_MAX_AGE_S) -> NetworkReading | None:
        """The cached reading without waiting; ``None`` before the first read.

        A copy older than ``max_age_s`` is still returned (its ``age_s`` says
        so) while a background refresh replaces it, unless one is running.
        """
        fresh = self._fresh(max_age_s)
        if fresh is not None:
            return fresh
        stale = self._cached
        if self._lock.acquire(blocking=False):
            try:
                self._spawn(self._refresh_in_background)
            except BaseException:
                self._lock.release()
                raise
        return None if stale is None else stale[0]

    def _refresh_in_background(self) -> None:
        # The lock was taken by ``peek``; a ``threading.Lock`` may be released
        # by another thread, which is what hands the refresh over.
        try:
            self._refresh_locked()
        finally:
            self._lock.release()


def _spawn_daemon_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="network-status", daemon=True).start()


__all__ = [
    "DEFAULT_TTL_S",
    "STATUS_MAX_AGE_S",
    "NetworkReading",
    "NetworkStatusReader",
]
