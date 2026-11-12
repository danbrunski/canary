"""Daemon and inference health monitoring (Task 6.27; moved from
``src.models.health`` by R04.8; the old path no longer imports).

A detector that's silently degrading (feature distribution drifted off the
training set, reconstruction error creeping up, the Pi pegged, the schema
quietly mismatched, half the windows missing features) is worse than no
detector — it looks healthy on the dashboard while missing real events.
This module is the observability layer that makes that visible.

Two parts:

* **Pure functions** — :func:`system_metrics` and the host probes here, and the
  model drift functions (:func:`feature_drift`, :func:`reconstruction_error_drift`)
  in :mod:`src.models.drift`. No state, no daemon coupling; testable in isolation.
* **`HealthMonitor`** — a small rolling accumulator the daemon feeds from the
  paths it already runs (inference tick → latency / missing-feature flag /
  data freshness; model-manager poll → swap outcomes). :meth:`snapshot`
  folds in the system + drift metrics and is what ``/status`` surfaces.

Drift uses the scaler the model already persisted (IF manifest's z-score
``means``/``stds``): live-vs-training mean distance in training-σ units is a
cheap, dependency-free population-stability proxy. No scipy / numpy-heavy
PSI machinery — the Pi budget (NFR-01) rules that out and the σ-distance is
enough to fire a "retrain me" warning.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.cgroup_memory import CGROUP_ROOT as CGROUP_ROOT
from src.cgroup_memory import memory_limit_status as memory_limit_status
from src.collection import capture_lifetime
from src.models.drift import FeatureDriftReport as FeatureDriftReport
from src.models.drift import feature_drift as feature_drift
from src.models.drift import reconstruction_error_drift as reconstruction_error_drift
from src.startup import StartupTimeline
from src.storage.capture_losses import CAN0_LOSS_FIELDS, LOSS_FIELDS, LossEpoch

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_CAN0_STATS_PATH",
    "CaptureQualityInputs",
    "HealthMonitor",
    "can0_counters",
    "feature_drift",
    "memory_limit_status",
    "power_status",
    "reconstruction_error_drift",
    "system_metrics",
]


# ---------------------------------------------------------------------------
# System metrics — best-effort, no psutil dependency (Pi target)
# ---------------------------------------------------------------------------


def system_metrics() -> dict[str, float | None]:
    """Coarse host load/memory/temperature. Linux ``/proc`` + ``getloadavg``
    + thermal ``/sys``; ``None`` for any field the platform doesn't expose
    (never raises). Note: ``cpu_percent`` (a delta over time) lives on
    :class:`HealthMonitor`, not here — these reads are all single-shot."""
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
# Rolling monitor — fed from the daemon's existing paths
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaptureQualityInputs:
    """What a capture verdict is scored on, taken at one instant (C03.5).

    ``scheduler`` is the latest live scheduler payload (request table and
    *trailing* rates), ``lifetime`` the capture-lifetime ledger (arm to now)
    or ``None`` when no capture is active, and ``loss_epoch`` every loss
    counter as it reads right now — to be reconciled against the epoch taken
    at arm time, never quoted on its own.
    """

    scheduler: dict[str, Any]
    lifetime: dict[str, Any] | None
    loss_epoch: LossEpoch
    sinks: dict[str, dict[str, Any]]


@dataclass
class _Rolling:
    latencies_ms: deque[float] = field(default_factory=lambda: deque(maxlen=512))
    scores: deque[float] = field(default_factory=lambda: deque(maxlen=512))
    observed: int = 0
    missing_feature_rows: int = 0


#: Task 2.1 — a permanently-absent trained feature column makes every IF tick
#: for a mode ``missing_features=True`` forever. A short run of misses right
#: after startup (schema warmup) shouldn't immediately declare the mode
#: "blind", so blindness only latches once a full window of *consecutive*
#: ticks for that mode all missed — long enough to rule out a startup blip at
#: the live ~1 Hz tick rate, short enough to catch a real dropout mid-drive.
_MODE_BLIND_WINDOW = 8


class HealthMonitor:
    """Rolling inference-health accumulator.

    Single-writer (the daemon's inference task) like the emitter — not
    independently locked. Cheap: bounded deques, integer counters. The
    daemon calls :meth:`observe_inference` per tick, :meth:`note_swap` from
    the model-manager poll, and exposes :meth:`snapshot` through ``/status``.
    """

    def __init__(self, *, can0_stats_path: Path = DEFAULT_CAN0_STATS_PATH) -> None:
        self._roll = _Rolling()
        self._last_data_ts: float | None = None
        self._swaps: dict[str, int] = {"swapped": 0, "rejected": 0, "unchanged": 0, "error": 0}
        self._last_swap_version: str | None = None
        self._model_version_mismatches: dict[str, dict[str, str]] = {}
        #: A01.5 — the serving sequence engine's suppress list against the one
        #: its artifact was trained with; ``None`` until an engine is built.
        self._sequence_suppression: dict[str, Any] | None = None
        #: A06.6 — why the sequence detector is not served (its A02.3 level was
        #: chosen at another forward cadence); ``None`` while it serves.
        self._sequence_cadence_refusal: str | None = None
        self._detector_roles: dict[str, Any] | None = None
        self._obd_scheduler: dict[str, Any] | None = None
        self._telemetry_sinks: dict[str, dict[str, Any]] = {}
        self._cpu_prev: tuple[float, float] | None = None
        #: (monotonic_ts, value) cache so /status polling doesn't spawn a
        #: vcgencmd subprocess every call — the flags barely move at a glance.
        self._power_cache: tuple[float, dict[str, Any] | None] | None = None
        #: Task 2.1 — per operating-mode rolling missing-feature window (IF)
        #: and the mode's latched blind state, keyed by mode name.
        self._if_mode_missing: dict[str, deque[bool]] = {}
        self._if_mode_blind: dict[str, bool] = {}
        #: 4.7 — latest live tick's segmenter-fallback state (edge-triggered
        #: like ``if_blind_modes``, same pattern).
        self._segmenter_fallback_active: bool = False
        #: R03.12 — the latest tick's capture-profile gate verdict
        #: (``{scoring_allowed, profile, source}``), or ``None`` before the
        #: first scorable row. ``/status`` detectors surface it so a withheld
        #: IF/sequence stream names the profile that withheld it.
        self._capture_profile_gate: dict[str, Any] | None = None
        #: Task 6.8 — the counters as first observed (baseline "since this
        #: monitor started watching"), so a delta is visible without needing
        #: a second daemon to remember the counter's value at boot.
        self._can0_stats_path = can0_stats_path
        self._can0_baseline: dict[str, int] | None = None
        #: The queue objects the sinks report on, so a capture stop can wait
        #: for the ingest and storage queues to drain (C03.8) without a
        #: second plumbing path to the daemon's queues.
        self._queues: dict[str, Any] = {}
        #: S01.1 — when each startup stage was reached, served as ``/status``'s
        #: ``startup`` block. Replaced by ``run_daemon(startup=...)`` with the
        #: timeline ``src.main`` began before the daemon existed.
        self.startup = StartupTimeline()

    #: How long a cached power_status() reading stays fresh (seconds).
    _POWER_TTL_SECONDS = 5.0

    def _power_status_cached(self) -> dict[str, Any] | None:
        """`power_status()` behind a short TTL — snapshot() runs on every
        /status poll, and a subprocess per poll would be wasteful on the Pi."""
        now = time.monotonic()
        if self._power_cache is not None and now - self._power_cache[0] < self._POWER_TTL_SECONDS:
            return self._power_cache[1]
        status = power_status()
        self._power_cache = (now, status)
        return status

    def _cpu_percent(self) -> float | None:
        """Busy fraction (0..1) of total CPU time since the previous
        snapshot. ``None`` on the first call (no baseline yet) or where
        ``/proc/stat`` is absent. Stateful by necessity — utilization is a
        delta between two readings, not an instantaneous level."""
        sample = _read_cpu_times()
        if sample is None:
            return None
        prev, self._cpu_prev = self._cpu_prev, sample
        if prev is None:
            return None
        d_idle = sample[0] - prev[0]
        d_total = sample[1] - prev[1]
        if d_total <= 0:
            return None
        return round(max(0.0, min(1.0, 1.0 - d_idle / d_total)), 4)

    def observe_inference(
        self,
        *,
        latency_ms: float | None = None,
        missing_features: bool = False,
        score: float | None = None,
        data_timestamp_epoch: float | None = None,
        operating_mode: str | None = None,
    ) -> bool:
        """Record one inference tick.

        Returns ``True`` exactly on the tick ``operating_mode`` (2.1)
        transitions from not-blind to blind — edge-triggered, so the caller
        can log a fresh warning instead of the old once-per-process one.
        ``False`` when ``operating_mode`` is ``None`` (no per-mode tracking
        possible) or the mode was already blind / never became so.
        """
        self._roll.observed += 1
        if latency_ms is not None:
            self._roll.latencies_ms.append(float(latency_ms))
        if score is not None:
            self._roll.scores.append(float(score))
        if missing_features:
            self._roll.missing_feature_rows += 1
        if data_timestamp_epoch is not None:
            self._last_data_ts = data_timestamp_epoch
        if operating_mode is None:
            return False
        return self._observe_if_mode_missing(operating_mode, missing_features)

    def _observe_if_mode_missing(self, mode: str, missing: bool) -> bool:
        """Update ``mode``'s rolling missing-feature window; return whether
        this tick is the one that latched the mode into blindness (2.1)."""
        window = self._if_mode_missing.setdefault(mode, deque(maxlen=_MODE_BLIND_WINDOW))
        window.append(missing)
        now_blind = len(window) == window.maxlen and all(window)
        was_blind = self._if_mode_blind.get(mode, False)
        self._if_mode_blind[mode] = now_blind
        return now_blind and not was_blind

    @property
    def if_blind_modes(self) -> tuple[str, ...]:
        """Modes currently latched blind (2.1), sorted for stable output."""
        return tuple(sorted(mode for mode, blind in self._if_mode_blind.items() if blind))

    @property
    def if_missing_feature_rate_by_mode(self) -> dict[str, float]:
        """Per-mode missing-feature rate over each mode's rolling window
        (2.1) — the same signal ``blind_modes`` latches off of, surfaced as a
        gauge so a mode heading toward blindness is visible before it fires."""
        return {
            mode: round(sum(window) / len(window), 4)
            for mode, window in self._if_mode_missing.items()
            if window
        }

    def observe_segmenter_fallback(self, active: bool) -> bool:
        """Record this tick's segmenter-fallback state (4.7).

        Returns ``True`` exactly on the tick that transitions into fallback
        (edge-triggered, mirrors :meth:`_observe_if_mode_missing`) so the
        caller can log a fresh warning instead of spamming one per tick —
        every mode-gated behavior surface silently stops firing for as long
        as this is active.
        """
        was_active = self._segmenter_fallback_active
        self._segmenter_fallback_active = active
        return active and not was_active

    @property
    def segmenter_fallback_active(self) -> bool:
        """Whether the live segmenter is currently constant-mode-fallback
        (4.7) — ``/status`` surfaces this alongside ``if_blind_modes``."""
        return self._segmenter_fallback_active

    def observe_capture_profile_gate(self, verdict: dict[str, Any]) -> None:
        """Record this tick's R03.12 capture-profile gate verdict."""
        self._capture_profile_gate = dict(verdict)

    @property
    def capture_profile_gate(self) -> dict[str, Any] | None:
        """The latest capture-profile gate verdict, or ``None`` before one."""
        return None if self._capture_profile_gate is None else dict(self._capture_profile_gate)

    def note_swap(self, kind: str, *, version: str | None = None) -> None:
        self._swaps[kind] = self._swaps.get(kind, 0) + 1
        if kind == "swapped" and version is not None:
            self._last_swap_version = version

    def observe_model_version_mismatch(
        self, name: str, *, serving: str | None, manifest: str | None
    ) -> None:
        """Track a served-binary vs manifest-sidecar version disagreement.

        Recorded per model manager each poll; clears itself once the pair
        agrees again (i.e. after the missing binary finally lands). Surfaced
        in the snapshot so a partial deploy is visible on ``/status`` instead
        of only in a startup log line."""
        if serving is None or manifest is None or serving == manifest:
            self._model_version_mismatches.pop(name, None)
        else:
            self._model_version_mismatches[name] = {"serving": serving, "manifest": manifest}

    def observe_sequence_suppression(self, comparison: Mapping[str, Any] | None) -> None:
        """Record the loaded sequence engine's training-vs-serving suppress
        lists (A01.5), so a difference is on ``/status`` and not only in the
        load-time log line. ``None`` clears it (no engine loaded)."""
        self._sequence_suppression = None if comparison is None else dict(comparison)

    def observe_sequence_cadence_refusal(self, reason: str | None) -> None:
        """Record why the sequence engine was refused for the configured
        forward cadence (A06.6), so ``/status`` names it and not only the log.
        ``None`` clears it."""
        self._sequence_cadence_refusal = reason

    def observe_detector_roles(self, roles: Mapping[str, Any] | None) -> None:
        """Record each learned detector's configured and effective role and
        why (A06.5, ``DetectorRoleGate``): an ``alert`` role serves only with
        an accepted alert-budget verdict for the artifact's sha256."""
        self._detector_roles = None if roles is None else dict(roles)

    def observe_obd_scheduler(self, metrics: Any) -> None:
        """Record the latest adaptive OBD scheduler snapshot for ``/status``."""
        if hasattr(metrics, "to_dict"):
            self._obd_scheduler = dict(metrics.to_dict())
        else:
            self._obd_scheduler = dict(metrics)

    def capture_quality_inputs(self, *, now: float | None = None) -> CaptureQualityInputs:
        """Everything a capture verdict is scored on, at one instant.

        Separate from :meth:`snapshot` because that one needs model/scaler
        arguments a control route has no business assembling. The loss
        counters come back as an *epoch*, not as totals: the 2026-07-29
        incident's 2491 dropped records stayed in the cumulative counter and
        were described as loss inside every later capture until C03.
        """
        monotonic = time.monotonic() if now is None else now
        lifetime = capture_lifetime.snapshot(monotonic)
        return CaptureQualityInputs(
            scheduler=dict(self._obd_scheduler or {}),
            lifetime=None if lifetime is None else lifetime.to_dict(),
            loss_epoch=self.loss_epoch(now=monotonic),
            sinks={name: dict(state) for name, state in self._telemetry_sinks.items()},
        )

    def loss_counters(self) -> dict[str, int | None]:
        """Every attributable loss counter, cumulative, ``None`` = unavailable.

        Keys are ``<sink>.<field>``; :mod:`src.storage.capture_losses`
        assigns each a scope. The kernel's ``can0`` counters are included so
        a receive overrun during a capture is visible, and reported as
        ``None`` (unknown) on a host without the interface — never as zero.
        """
        counters: dict[str, int | None] = {}
        for sink, state in self._telemetry_sinks.items():
            for field_name in LOSS_FIELDS:
                value = state.get(field_name)
                counters[f"{sink}.{field_name}"] = None if value is None else int(value)
        can0 = can0_counters(self._can0_stats_path)
        for field_name in CAN0_LOSS_FIELDS:
            counters[f"can0.{field_name}"] = None if can0 is None else can0.get(field_name)
        return counters

    def loss_epoch(self, *, now: float | None = None) -> LossEpoch:
        """Snapshot every loss counter (C03.5). Take one at arm, one at stop."""
        return LossEpoch(
            taken_at_monotonic=time.monotonic() if now is None else now,
            taken_at_wall=_now(),
            counters=self.loss_counters(),
        )

    def observe_telemetry_queue(self, name: str, queue: Any) -> None:
        """Record queue depth and its high-water mark for one telemetry sink.

        ``queue_depth`` alone is the depth at the last observation, so a sink
        whose consumer wedged, filled its queue, dropped records and then
        recovered reported depth 0 — the peak that explains the loss was gone
        before anything read it (C10.5; seen in the C10 storage-stall
        scenario, where a 256-deep queue dropped 3,742 records and still
        reported ``queue_depth: 0``). The fanout observes every sink's queue
        after each batch, including while its consumer is blocked, so the
        monotonic max over those samples is the high-water mark.
        """
        state = self._sink_state(name)
        depth = int(queue.qsize())
        state["queue_depth"] = depth
        state["queue_high_water"] = max(int(state.get("queue_high_water") or 0), depth)
        state["queue_max_size"] = int(queue.maxsize)
        self._queues[name] = queue

    async def wait_for_queues_idle(
        self, names: Sequence[str] = ("ingest", "storage"), *, timeout: float
    ) -> dict[str, Any]:
        """Wait, bounded, for the named queues to be fully consumed (C03.8).

        Uses ``asyncio.Queue.join`` — every ``get`` must be matched by
        ``task_done``, which the fanout and the storage sink do. Returns which
        queues drained and which timed out or were never observed; a queue
        that never reported is *unknown*, not drained.
        """
        drained: list[str] = []
        timed_out: list[str] = []
        unobserved: list[str] = []
        deadline = time.monotonic() + max(0.0, timeout)
        for name in names:
            queue = self._queues.get(name)
            if queue is None or not hasattr(queue, "join"):
                unobserved.append(name)
                continue
            remaining = max(0.0, deadline - time.monotonic())
            try:
                await asyncio.wait_for(queue.join(), timeout=remaining)
            except TimeoutError:
                timed_out.append(name)
            else:
                drained.append(name)
        return {"drained": drained, "timed_out": timed_out, "unobserved": unobserved}

    def note_telemetry_drop(self, name: str, *, count: int = 1) -> None:
        """Record explicit data loss at a telemetry sink."""
        state = self._sink_state(name)
        state["dropped_records"] = int(state.get("dropped_records", 0)) + count

    def note_telemetry_error(self, name: str, exc: BaseException) -> None:
        """Record the latest exception from one telemetry sink."""
        state = self._sink_state(name)
        state["errors"] = int(state.get("errors", 0)) + 1
        state["last_error"] = f"{type(exc).__name__}: {exc}"

    def observe_receive_path(
        self,
        name: str,
        *,
        received: int,
        parse_errors: int,
        buffer_dropped: int,
        buffer_high_water: int,
        buffer_capacity: int,
        queue_dropped: int,
        batches: int,
        reconnects: int = 0,
    ) -> None:
        """Record a collector receive path's producer-buffer pressure (C07.5).

        A named entry under ``telemetry_sinks`` (e.g. ``can_receive``) so
        loss *before* the ingest queue is attributable on its own, never
        folded into ``storage``. ``dropped_records`` is the total explicit
        loss at this stage; ``buffer_dropped`` (loop too slow to drain the
        thread handoff) and ``queue_dropped`` (ingest queue full) break it
        down by cause. Values are cumulative counters from the collector,
        so they are assigned, not summed.
        """
        state = self._sink_state(name)
        state["records_processed"] = received
        state["parse_errors"] = parse_errors
        state["buffer_dropped"] = buffer_dropped
        state["buffer_high_water"] = buffer_high_water
        state["buffer_capacity"] = buffer_capacity
        state["queue_dropped"] = queue_dropped
        state["dropped_records"] = buffer_dropped + queue_dropped
        state["batches"] = batches
        state["reconnects"] = reconnects

    def observe_collector_handoff(  # noqa: PLR0913
        self,
        name: str,
        *,
        accepted: int,
        buffer_dropped: int,
        buffer_high_water: int,
        buffer_capacity: int,
        queue_dropped: int,
        batches: int,
        raw_responses: int | None,
        derived_records: int,
        reconnects: int = 0,
    ) -> None:
        """Record a polling collector's thread handoff (C03.6, ``obd_collect``).

        The Canable collector's ``QueueDropCounter`` and ``ThreadHandoff``
        counters used to be logged and never reached ``/status``, so a record
        dropped between the poll thread and the ingest queue vanished from the
        capture verdict. ``derived_records`` counts what the collector pushed
        (a batched Mode 01 response producing N readings is N records);
        ``raw_responses`` counts ECU responses, when the loop reports them, so
        the two are never confused.
        """
        self.observe_receive_path(
            name,
            received=accepted + buffer_dropped,
            parse_errors=0,
            buffer_dropped=buffer_dropped,
            buffer_high_water=buffer_high_water,
            buffer_capacity=buffer_capacity,
            queue_dropped=queue_dropped,
            batches=batches,
            reconnects=reconnects,
        )
        state = self._sink_state(name)
        state["raw_responses"] = raw_responses
        state["derived_records"] = derived_records

    def observe_telemetry_write(
        self,
        name: str,
        *,
        records: int,
        elapsed_seconds: float,
        flushes: int = 0,
    ) -> None:
        """Record durable-writer throughput without retaining every sample."""
        state = self._sink_state(name)
        elapsed_ms = max(0.0, elapsed_seconds) * 1000.0
        state["write_calls"] = int(state.get("write_calls", 0)) + 1
        state["records_processed"] = int(state.get("records_processed", 0)) + records
        state["flushes"] = int(state.get("flushes", 0)) + flushes
        state["last_batch_size"] = records
        state["max_batch_size"] = max(int(state.get("max_batch_size", 0)), records)
        state["last_write_ms"] = round(elapsed_ms, 3)
        state["max_write_ms"] = round(max(float(state.get("max_write_ms", 0.0)), elapsed_ms), 3)
        state["last_write_records_per_second"] = (
            round(records / elapsed_seconds, 1) if elapsed_seconds > 0 else None
        )
        if flushes:
            state["last_flushing_write_ms"] = round(elapsed_ms, 3)
            state["max_flushing_write_ms"] = round(
                max(float(state.get("max_flushing_write_ms", 0.0)), elapsed_ms), 3
            )

    def observe_storage_publish(self, name: str, stats: dict[str, Any]) -> None:
        """The durable writer's chunk publishes (``PublishStats``): with
        background publishing, ``max_publish_ms`` is the disk's stall and the
        write timings above are not."""
        self._sink_state(name).update(stats)

    def _sink_state(self, name: str) -> dict[str, Any]:
        return self._telemetry_sinks.setdefault(
            name,
            {
                "queue_depth": None,
                "queue_high_water": None,
                "queue_max_size": None,
                "dropped_records": 0,
                "errors": 0,
                "last_error": None,
            },
        )

    def _can0_status(self) -> dict[str, Any] | None:
        """`can0` counters + delta since this monitor first saw them (6.8).

        `None` (the field is omitted from the snapshot) when there's no can0
        interface — this laptop's real environment tonight, and any Pi before
        the CAN HAT/socketcan interface is up.
        """
        counters = can0_counters(self._can0_stats_path)
        if counters is None:
            return None
        if self._can0_baseline is None:
            self._can0_baseline = dict(counters)
        return {
            **counters,
            "since_start": {name: counters[name] - self._can0_baseline[name] for name in counters},
        }

    def snapshot(
        self,
        *,
        now_epoch: float | None = None,
        scaler: Mapping[str, Any] | None = None,
        live_means: Mapping[str, float] | None = None,
        lstm_thresholds: Mapping[str, Any] | None = None,
        live_mean_recon_error: float | None = None,
    ) -> dict[str, Any]:
        """JSON-safe health block for ``/status``.

        Optional drift inputs are folded in only when provided — the daemon
        passes the running model's scaler + recent live means when it has
        them, and the block degrades gracefully (``None``/absent) otherwise.
        """
        lat = sorted(self._roll.latencies_ms)
        observed = self._roll.observed
        snap: dict[str, Any] = {
            "observed": observed,
            "inference_latency_ms": {
                "mean": round(sum(lat) / len(lat), 3) if lat else None,
                "p95": _percentile(lat, 0.95),
                "max": max(lat) if lat else None,
            },
            "missing_feature_rate": (
                round(self._roll.missing_feature_rows / observed, 4) if observed else None
            ),
            "data_freshness_seconds": (
                None
                if self._last_data_ts is None
                else round(max(0.0, (now_epoch or _now()) - self._last_data_ts), 2)
            ),
            "model_swaps": dict(self._swaps),
            "active_model_version": self._last_swap_version,
            **(
                {
                    "model_version_mismatches": {
                        name: dict(pair)
                        for name, pair in sorted(self._model_version_mismatches.items())
                    }
                }
                if self._model_version_mismatches
                else {}
            ),
            **(
                {"sequence_suppression": dict(self._sequence_suppression)}
                if self._sequence_suppression is not None
                else {}
            ),
            **(
                {"sequence_cadence_refusal": self._sequence_cadence_refusal}
                if self._sequence_cadence_refusal is not None
                else {}
            ),
            **(
                {"detector_roles": dict(self._detector_roles)}
                if self._detector_roles is not None
                else {}
            ),
            "system": {**system_metrics(), **memory_limit_status()},
            "power": self._power_status_cached(),
        }
        snap["system"]["cpu_percent"] = self._cpu_percent()
        if self._obd_scheduler is not None:
            snap["obd_scheduler"] = dict(self._obd_scheduler)
        lifetime = capture_lifetime.snapshot(time.monotonic())
        if lifetime is not None:
            # Arm-to-now statistics for the active capture (C03.2), beside —
            # never inside — the scheduler's trailing 60 s numbers.
            snap["capture_lifetime"] = lifetime.to_dict(events=False)
        if self._telemetry_sinks:
            snap["telemetry_sinks"] = {
                name: dict(state) for name, state in sorted(self._telemetry_sinks.items())
            }
        if scaler is not None and live_means is not None:
            snap["feature_drift"] = feature_drift(live_means, scaler).to_dict()
        if lstm_thresholds is not None:
            snap["reconstruction_error_drift"] = reconstruction_error_drift(
                live_mean_recon_error, lstm_thresholds
            )
        can0 = self._can0_status()
        if can0 is not None:
            snap["can0"] = can0
        return snap


def _percentile(sorted_values: Sequence[float], q: float) -> float | None:
    if not sorted_values:
        return None
    idx = min(len(sorted_values) - 1, int(round(q * (len(sorted_values) - 1))))
    return round(sorted_values[idx], 3)


def _now() -> float:
    from datetime import UTC, datetime

    return datetime.now(UTC).timestamp()
