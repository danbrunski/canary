"""The host's own readings: load, memory, temperature, CPU, power, can0 (R29.10).

``HealthMonitor`` used to carry these 170 lines of readers and the state that
goes with them (the CPU baseline, the ``vcgencmd`` cache, the first can0
counters). They have nothing to do with inference or the sinks, so they live
here and ``HealthMonitor.snapshot`` asks :class:`HostMetrics` for the three
blocks it folds into ``/status`` (``system``, ``power``, ``can0``).

Best-effort throughout, no psutil dependency (Pi target): a reading the
platform doesn't expose is ``None``, never an exception and never a zero.

Tests that script a reader patch the name *here* (``_read_throttled_raw``,
``_run_sampler``, ``system_metrics``): that is where it is looked up.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.cgroup_memory import memory_limit_status

__all__ = [
    "DEFAULT_CAN0_STATS_PATH",
    "HOST_CPU_WINDOW_SECONDS",
    "HOST_SAMPLE_INTERVAL_SECONDS",
    "HostCpuSampler",
    "HostMetrics",
    "can0_counters",
    "power_status",
    "system_metrics",
]


# ---------------------------------------------------------------------------
# System metrics — best-effort, no psutil dependency (Pi target)
# ---------------------------------------------------------------------------


def system_metrics() -> dict[str, float | None]:
    """Coarse host load/memory/temperature. Linux ``/proc`` + ``getloadavg``
    + thermal ``/sys``; ``None`` for any field the platform doesn't expose
    (never raises). Note: ``cpu_percent`` (a delta over time) is
    :class:`HostCpuSampler`'s, not here — these reads are all single-shot."""
    out: dict[str, float | None] = {
        "load_1m": None,
        "cpu_count": None,
        "load_per_cpu": None,
        "mem_used_fraction": None,
        "mem_total_mb": None,
        "cpu_temp_c": _cpu_temp_c(),
    }
    try:
        load1 = os.getloadavg()[0]
        out["load_1m"] = round(load1, 3)
        cpus = os.cpu_count()
        if cpus:
            out["cpu_count"] = float(cpus)
            out["load_per_cpu"] = round(load1 / cpus, 3)
    except (OSError, AttributeError):
        pass
    try:
        meminfo: dict[str, float] = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                meminfo[key.strip()] = float(rest.strip().split()[0])  # kB
        total = meminfo.get("MemTotal")
        available = meminfo.get("MemAvailable")
        if total:
            out["mem_total_mb"] = round(total / 1024, 1)
            if available is not None:
                out["mem_used_fraction"] = round((total - available) / total, 4)
    except (OSError, ValueError, IndexError):
        pass
    return out


def _cpu_temp_c() -> float | None:
    """SoC temperature in °C from the Linux thermal sysfs — the Raspberry Pi
    exposes its CPU zone at ``thermal_zone0``. ``None`` when no thermal zone
    is present (e.g. a dev laptop / container)."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as fh:
            milli = float(fh.read().strip())
    except (OSError, ValueError):
        return None
    return round(milli / 1000.0, 1)


def _read_cpu_times() -> tuple[float, float] | None:
    """``(idle_jiffies, total_jiffies)`` from ``/proc/stat``'s aggregate
    ``cpu`` line, or ``None`` when unavailable. A single reading is
    meaningless — the caller diffs two to get utilization over the interval."""
    try:
        with open("/proc/stat") as fh:
            fields = fh.readline().split()
    except OSError:
        return None
    if len(fields) < 5 or fields[0] != "cpu":
        return None
    try:
        vals = [float(x) for x in fields[1:]]
    except ValueError:
        return None
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0.0)  # idle + iowait
    return idle, sum(vals)


#: How often :class:`HostCpuSampler` reads ``/proc/stat``, and how far back the
#: busy fraction it reports reaches. A read is ~30 µs, so the interval is about
#: resolution, not cost; the window is about what the number means.
HOST_SAMPLE_INTERVAL_SECONDS = 1.0
HOST_CPU_WINDOW_SECONDS = 5.0


class HostCpuSampler:
    """The host's CPU utilization, sampled on this object's own clock (R29.9).

    Utilization is a *delta* between two ``/proc/stat`` readings, and the delta
    has to be over a span someone chose. ``HealthMonitor`` used to keep the
    previous reading and diff against it on every ``snapshot()``, so the span
    was "since whoever last polled": every ``/status`` and MCP ``get_status``
    moved the baseline for the next one, and two pollers on different cadences
    each saw a window set by the other (down to a few milliseconds, where one
    jiffy of noise reads as 0 % or 100 %).

    Here the readings are taken by :meth:`sample` on a fixed interval (a small
    daemon thread after :meth:`start`) into a ring, and :meth:`cpu_percent` only
    *reads* the ring: the busy fraction between the newest sample and the oldest
    one still inside the window. Reading never writes, so any number of
    pollers, at any rate, get the same value until the next sample lands.

    ``None`` means "no measurement", never zero: fewer than two samples, a
    platform without ``/proc/stat``, or a newest sample older than three
    intervals (the sampler stopped, or its thread died). A stale number
    presented as current is the failure this refuses.

    The thread holds only a weak reference to the sampler and ends when the
    sampler is collected, so a monitor built by a test and dropped leaves
    nothing running.
    """

    def __init__(
        self,
        *,
        interval_seconds: float = HOST_SAMPLE_INTERVAL_SECONDS,
        window_seconds: float = HOST_CPU_WINDOW_SECONDS,
        read: Callable[[], tuple[float, float] | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_seconds <= 0 or window_seconds < interval_seconds:
            raise ValueError("need 0 < interval_seconds <= window_seconds")
        self._interval = interval_seconds
        self._window = window_seconds
        self._read = read
        self._clock = clock
        #: (clock seconds, idle jiffies, total jiffies), oldest first. Long
        #: enough for the window plus the baseline just outside it.
        self._ring: deque[tuple[float, float, float]] = deque(
            maxlen=int(window_seconds / interval_seconds) + 2
        )
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def sample(self) -> None:
        """Take one reading now. The thread calls this; so can a test."""
        reading = (self._read or _read_cpu_times)()
        if reading is None:
            return
        with self._lock:
            self._append(reading)

    def _append(self, reading: tuple[float, float]) -> None:
        """Add a reading to the ring; the caller holds the lock."""
        self._ring.append((self._clock(), reading[0], reading[1]))

    def cpu_percent(self) -> float | None:
        """Busy fraction (0..1) of total CPU time over the trailing window.

        Pure read: never samples and never moves a baseline.
        """
        with self._lock:
            samples = list(self._ring)
        if len(samples) < 2:
            return None
        newest = samples[-1]
        if self._clock() - newest[0] > 3 * self._interval:
            return None
        baseline = next(item for item in samples if newest[0] - item[0] <= self._window)
        d_idle = newest[1] - baseline[1]
        d_total = newest[2] - baseline[2]
        if baseline is newest or d_total <= 0:
            return None
        return round(max(0.0, min(1.0, 1.0 - d_idle / d_total)), 4)

    def start(self) -> None:
        """Take a baseline reading and sample every ``interval`` until :meth:`stop`.

        Idempotent and safe to call from any number of threads at once (sixteen
        concurrent ``/status`` requests used to start seven samplers, and
        :meth:`stop` could reach only the last): the check, the baseline and
        the thread start are one step under the lock. A sampler that was
        stopped but whose thread has not yet noticed starts a new one.
        Not started by the constructor: a monitor that is built and never read
        (most of the test suite) must not start a thread.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive() and not self._stop.is_set():
                return
            self._stop = threading.Event()
            reading = (self._read or _read_cpu_times)()
            if reading is not None:
                self._append(reading)  # a baseline now: the first value needs one interval
            thread = threading.Thread(
                target=_run_sampler,
                args=(weakref.ref(self), self._stop, self._interval),
                name="canary-host-sampler",
                daemon=True,
            )
            thread.start()
            self._thread = thread

    def stop(self) -> None:
        """End the thread. The ring stays, and ages out as stale."""
        self._stop.set()

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()


def _run_sampler(
    ref: weakref.ReferenceType[HostCpuSampler], stop: threading.Event, interval: float
) -> None:
    """The sampler thread's body: sample, wait, until stopped or the sampler is gone."""
    while not stop.wait(interval):
        sampler = ref()
        if sampler is None:
            return
        sampler.sample()
        del sampler  # do not hold it across the wait: that would keep it alive


# ---------------------------------------------------------------------------
# Power / throttling — Raspberry Pi firmware status (vcgencmd get_throttled)
# ---------------------------------------------------------------------------

#: ``vcgencmd get_throttled`` returns a bitmask. The low nibble is *live*
#: conditions; bits 16-19 latch "has occurred since boot" (sticky until reboot).
#: Under-voltage is a power-supply problem, not heat — so we surface it
#: distinctly from the thermal soft-limit. See [[project_daemon_shutdown_hang]]
#: (separate issue) and the 2026-05-24 under-voltage incident.
_THROTTLE_BITS_NOW: dict[int, str] = {
    0: "under_voltage",
    1: "arm_frequency_capped",
    2: "currently_throttled",
    3: "soft_temp_limit",
}
_THROTTLE_BITS_SINCE_BOOT: dict[int, str] = {
    16: "under_voltage",
    17: "arm_frequency_capped",
    18: "throttling",
    19: "soft_temp_limit",
}


def power_status() -> dict[str, Any] | None:
    """Decode ``vcgencmd get_throttled`` into current + since-boot power flags.

    Returns ``{"raw": int, "now": {...}, "since_boot": {...}}`` where each inner
    dict maps a condition name to a bool. ``None`` when ``vcgencmd`` is absent
    or fails (a dev laptop / container) — never raises. The since-boot flags are
    the "past-detected" anomalies: once the firmware throttles or browns out,
    the bit stays set until the next reboot.
    """
    raw = _read_throttled_raw()
    if raw is None:
        return None
    return {
        "raw": raw,
        "now": {name: bool(raw & (1 << bit)) for bit, name in _THROTTLE_BITS_NOW.items()},
        "since_boot": {
            name: bool(raw & (1 << bit)) for bit, name in _THROTTLE_BITS_SINCE_BOOT.items()
        },
    }


def _read_throttled_raw() -> int | None:
    """Raw integer from ``vcgencmd get_throttled`` (format ``throttled=0x...``),
    or ``None`` when vcgencmd is missing/errors/times out."""
    try:
        completed = subprocess.run(
            ["vcgencmd", "get_throttled"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    _, _, value = completed.stdout.strip().partition("=")
    try:
        return int(value, 16)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# can0 rx-overrun counter (Task 6.8) — a passive capture-health tripwire
# ---------------------------------------------------------------------------

#: SocketCAN interfaces expose the standard Linux net-device statistics under
#: sysfs. ``rx_over_errors``/``rx_dropped`` are what actually matter for the
#: capture-architecture question (frames the kernel couldn't hand off in
#: time); ``rx_errors`` is the broader bucket they roll up into.
DEFAULT_CAN0_STATS_PATH = Path("/sys/class/net/can0/statistics")

_CAN0_COUNTER_NAMES: tuple[str, ...] = ("rx_over_errors", "rx_dropped", "rx_errors")


def can0_counters(stats_path: Path = DEFAULT_CAN0_STATS_PATH) -> dict[str, int] | None:
    """Read can0's rx overrun/drop/error counters straight from sysfs.

    Passive read only — per the roadmap doc, nonzero *growth* during a drive
    is the only intended trigger to revisit the capture architecture; this
    function just surfaces the raw numbers for a human to notice that growth,
    it does not alert on its own. Returns ``None`` when the interface isn't
    present (a dev laptop, or a Pi before the CAN HAT/can0 comes up) — the
    caller must treat that as "no data available", never a crash.
    """
    if not stats_path.is_dir():
        return None
    counters: dict[str, int] = {}
    for name in _CAN0_COUNTER_NAMES:
        try:
            counters[name] = int((stats_path / name).read_text().strip())
        except (OSError, ValueError):
            return None
    return counters


# ---------------------------------------------------------------------------
# The stateful part: what one monitor remembers between /status polls
# ---------------------------------------------------------------------------


class HostMetrics:
    """The host readings one ``HealthMonitor`` serves, and what it remembers of them.

    Three things are stateful and were fields of the monitor: the CPU baseline
    (the :class:`HostCpuSampler`, which samples on its own clock so no poll
    moves it), a short cache of ``vcgencmd`` (``snapshot()`` runs on every
    ``/status`` poll and a subprocess per poll is wasteful on the Pi), and the
    can0 counters as first observed, so growth during a drive is visible
    without a second daemon remembering the value at boot.
    """

    #: How long a cached :func:`power_status` reading stays fresh (seconds); the
    #: flags barely move at a glance.
    POWER_TTL_SECONDS = 5.0

    def __init__(
        self,
        *,
        can0_stats_path: Path = DEFAULT_CAN0_STATS_PATH,
        cpu: HostCpuSampler | None = None,
    ) -> None:
        self.cpu = HostCpuSampler() if cpu is None else cpu
        #: A sampler the caller handed in is the caller's to start (a test
        #: drives it by hand); the one built here starts with the first read.
        self._start_cpu_on_read = cpu is None
        self._can0_stats_path = can0_stats_path
        self._can0_baseline: dict[str, int] | None = None
        #: (monotonic_ts, value).
        self._power_cache: tuple[float, dict[str, Any] | None] | None = None

    def start_sampler(self) -> None:
        """Start the CPU sampler (idempotent)."""
        self.cpu.start()

    def cpu_percent(self) -> float | None:
        """Busy fraction (0..1) of total CPU time over the sampler's trailing
        window, or ``None`` until it has two samples.

        A read of the sampler's ring, not a delta against the previous call:
        the value does not depend on who polled last, and polling it twice
        returns the same number."""
        if self._start_cpu_on_read:
            self.cpu.start()
        return self.cpu.cpu_percent()

    def system(self) -> dict[str, Any]:
        """The ``system`` block: single-shot reads, the cgroup ceiling, then CPU."""
        return {**system_metrics(), **memory_limit_status(), "cpu_percent": self.cpu_percent()}

    def power(self) -> dict[str, Any] | None:
        """:func:`power_status` behind a short TTL cache."""
        now = time.monotonic()
        if self._power_cache is not None and now - self._power_cache[0] < self.POWER_TTL_SECONDS:
            return self._power_cache[1]
        status = power_status()
        self._power_cache = (now, status)
        return status

    def can0_raw(self) -> dict[str, int] | None:
        """can0's counters as they read right now, or ``None`` with no interface."""
        return can0_counters(self._can0_stats_path)

    def can0_status(self) -> dict[str, Any] | None:
        """``can0`` counters + delta since this object first saw them (6.8).

        ``None`` (the field is omitted from the snapshot) when there's no can0
        interface — a dev laptop, or a Pi before the CAN HAT/socketcan
        interface is up.
        """
        counters = self.can0_raw()
        if counters is None:
            return None
        if self._can0_baseline is None:
            self._can0_baseline = dict(counters)
        return {
            **counters,
            "since_start": {name: counters[name] - self._can0_baseline[name] for name in counters},
        }
