"""Daemon and inference health monitoring (Task 6.27; moved from
``src.models.health`` by R04.8; the old path no longer imports).

A detector that's silently degrading (feature distribution drifted off the
training set, reconstruction error creeping up, the Pi pegged, the schema
quietly mismatched, half the windows missing features) is worse than no
detector — it looks healthy on the dashboard while missing real events.
This module is the observability layer that makes that visible.

:class:`HealthMonitor` is a facade (R29.10) over three owners and keeps the one
job that is its own:

* **Inference accounting** — here. A small rolling accumulator the daemon feeds
  from the paths it already runs (inference tick → latency / missing-feature
  flag / data freshness; model-manager poll → swap outcomes).
* **Detector-state blocks** — here, one :meth:`HealthMonitor.set_detector_block`
  for the values something else decides and ``/status`` echoes (the OBD
  scheduler's snapshot, detector roles, the capture-profile gate, …).
* **The host** — :mod:`src.daemon.host_metrics`: load, memory, temperature, the
  CPU sampler, ``vcgencmd`` power flags and the can0 counters.
* **Sinks, queues and loss epochs** — :mod:`src.daemon.sink_health`.

:meth:`HealthMonitor.snapshot` folds those and the model drift metrics
(:func:`feature_drift`, :func:`reconstruction_error_drift` in
:mod:`src.models.drift`) into what ``/status`` surfaces. The names importers
took from this module before the split are all still here, except the three host
readers (``system_metrics``, ``power_status``, ``can0_counters``): their one
importer now takes them from :mod:`src.daemon.host_metrics`, and a name
re-exported here would let a test patch it where nothing looks it up.

Drift uses the scaler the model already persisted (IF manifest's z-score
``means``/``stds``): live-vs-training mean distance in training-σ units is a
cheap, dependency-free population-stability proxy. No scipy / numpy-heavy
PSI machinery — the Pi budget (NFR-01) rules that out and the σ-distance is
enough to fire a "retrain me" warning.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from src.cgroup_memory import CGROUP_ROOT as CGROUP_ROOT
from src.cgroup_memory import memory_limit_status as memory_limit_status
from src.collection import capture_lifetime
from src.daemon.host_metrics import DEFAULT_CAN0_STATS_PATH as DEFAULT_CAN0_STATS_PATH
from src.daemon.host_metrics import HostCpuSampler as HostCpuSampler
from src.daemon.host_metrics import HostMetrics
from src.daemon.sink_health import CaptureQualityInputs as CaptureQualityInputs
from src.daemon.sink_health import SinkHealth
from src.models.drift import FeatureDriftReport as FeatureDriftReport
from src.models.drift import feature_drift as feature_drift
from src.models.drift import reconstruction_error_drift as reconstruction_error_drift
from src.startup import StartupTimeline
from src.storage.capture_losses import LossEpoch

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_CAN0_STATS_PATH",
    "DETECTOR_BLOCKS",
    "CaptureQualityInputs",
    "DetectorBlock",
    "HealthMonitor",
    "HostCpuSampler",
    "VersionPair",
    "feature_drift",
    "memory_limit_status",
    "reconstruction_error_drift",
]


# ---------------------------------------------------------------------------
# Detector-state blocks — values something else decides, echoed by /status
# ---------------------------------------------------------------------------

#: The blocks :meth:`HealthMonitor.set_detector_block` takes. A closed set, so a
#: misspelt name is an error at the call (and a type error before it runs)
#: rather than a block that is stored and never shown.
DetectorBlock = Literal[
    "model_version_mismatch",
    "sequence_suppression",
    "sequence_cadence_refusal",
    "detector_roles",
    "obd_scheduler",
    "capture_profile_gate",
    "segmenter_fallback",
]


def _copy_out(stored: Any) -> Any:
    """What ``snapshot()`` returns for a stored value: a copy of a mapping, else the value."""
    return dict(stored) if isinstance(stored, Mapping) else stored


@dataclass(frozen=True)
class _BlockSpec:
    """How one block is stored and where ``snapshot()`` shows it."""

    #: Value given to ``set_detector_block`` → what is stored (a copy), or
    #: ``None`` to clear the block.
    coerce: Callable[[Any], Any]
    #: The key ``snapshot()`` carries it under; ``None`` for a block read
    #: through a property (``capture_profile_gate``, ``segmenter_fallback_active``).
    snapshot_key: str | None = None
    #: Stored value → the independent copy ``snapshot()`` returns.
    render: Callable[[Any], Any] = _copy_out
    #: One entry per ``key`` (the model manager), not one value.
    keyed: bool = False
    #: Placed after ``system`` / ``power`` rather than before them, which is
    #: where ``/status`` has always carried the OBD scheduler.
    tail: bool = False


def _as_is(value: Any) -> Any:
    return value


def _copy_mapping(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    return None if value is None else dict(value)


def _scheduler_metrics(value: Any) -> dict[str, Any] | None:
    """The collector hands over its metrics object; a test or the harness a mapping."""
    if value is None:
        return None
    return dict(value.to_dict()) if hasattr(value, "to_dict") else dict(value)


@dataclass(frozen=True)
class VersionPair:
    """What one model manager serves against what its manifest sidecar claims.

    A typed value, not a mapping: ``set_detector_block`` reads a *missing or
    misspelt* key as "unknown" and an unknown side clears the entry, so a
    ``{"servng": ..., "manifest": ...}`` would have switched the partial-deploy
    detector off without a word. Here a wrong field name is a ``TypeError`` at
    the call, and a mypy error before it runs."""

    serving: str | None
    manifest: str | None


def _version_pair(value: VersionPair | None) -> dict[str, str] | None:
    """A served-binary vs manifest-sidecar disagreement, or ``None`` once they agree.

    Recorded per model manager each poll; clears itself when the pair agrees
    again (the missing binary finally landed), and records nothing while
    either side is unknown. Surfaced so a partial deploy is visible on
    ``/status`` instead of only in a startup log line."""
    if value is None:
        return None
    if not isinstance(value, VersionPair):
        raise TypeError(
            f"model_version_mismatch takes a VersionPair or None, not {type(value).__name__}"
        )
    if value.serving is None or value.manifest is None or value.serving == value.manifest:
        return None
    return {"serving": value.serving, "manifest": value.manifest}


def _sorted_pairs(entries: Mapping[str, Mapping[str, str]]) -> dict[str, dict[str, str]]:
    return {name: dict(pair) for name, pair in sorted(entries.items())}


DETECTOR_BLOCKS: dict[str, _BlockSpec] = {
    # The order is the order ``snapshot()`` emits the head blocks in.
    "model_version_mismatch": _BlockSpec(
        _version_pair,
        snapshot_key="model_version_mismatches",
        render=_sorted_pairs,
        keyed=True,
    ),
    #: A01.5 — the serving sequence engine's suppress list against the one its
    #: artifact was trained with; cleared (``None``) while no engine is built.
    "sequence_suppression": _BlockSpec(_copy_mapping, snapshot_key="sequence_suppression"),
    #: A06.6 — why the sequence detector is not served (its A02.3 level was
    #: chosen at another forward cadence); ``None`` while it serves.
    "sequence_cadence_refusal": _BlockSpec(_as_is, snapshot_key="sequence_cadence_refusal"),
    #: A06.5 — each learned detector's configured and effective role and why
    #: (``DetectorRoleGate``): an ``alert`` role serves only with an accepted
    #: alert-budget verdict for the artifact's sha256.
    "detector_roles": _BlockSpec(_copy_mapping, snapshot_key="detector_roles"),
    #: The latest adaptive OBD scheduler snapshot.
    "obd_scheduler": _BlockSpec(_scheduler_metrics, snapshot_key="obd_scheduler", tail=True),
    #: R03.12 — the latest tick's capture-profile gate verdict
    #: (``{scoring_allowed, profile, source, …}``), ``None`` before the first
    #: scorable row. ``/status`` detectors surface it so a withheld IF/sequence
    #: stream names the profile that withheld it.
    "capture_profile_gate": _BlockSpec(_copy_mapping),
    #: 4.7 — whether the live segmenter is on its constant-mode fallback, which
    #: silences every mode-gated behavior surface for as long as it lasts.
    "segmenter_fallback": _BlockSpec(bool),
}


# ---------------------------------------------------------------------------
# Rolling monitor — fed from the daemon's existing paths
# ---------------------------------------------------------------------------


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
    """Rolling inference-health accumulator, and the facade over the host and
    sink accounting (R29.10).

    Single-writer (the daemon's inference task) like the emitter — not
    independently locked. Cheap: bounded deques, integer counters. The
    daemon calls :meth:`observe_inference` per tick, :meth:`note_swap` from
    the model-manager poll, :meth:`set_detector_block` for the values other
    code decides, and exposes :meth:`snapshot` through ``/status``.
    """

    def __init__(
        self,
        *,
        can0_stats_path: Path = DEFAULT_CAN0_STATS_PATH,
        host_cpu: HostCpuSampler | None = None,
    ) -> None:
        self._roll = _Rolling()
        self._last_data_ts: float | None = None
        self._swaps: dict[str, int] = {"swapped": 0, "rejected": 0, "unchanged": 0, "error": 0}
        self._last_swap_version: str | None = None
        #: Stored values of the :data:`DETECTOR_BLOCKS`, by block name. A keyed
        #: block holds ``{key: value}``; a cleared one is absent.
        self._blocks: dict[str, Any] = {}
        #: Load, memory, temperature, the CPU baseline (sampled on its own
        #: clock, R29.9, so no poll of ``snapshot()`` moves it), ``vcgencmd``
        #: behind a TTL and the can0 counters as first observed (6.8).
        self._host = HostMetrics(can0_stats_path=can0_stats_path, cpu=host_cpu)
        #: Sink state, the queues a capture stop waits on, loss epochs.
        self._sinks = SinkHealth()
        #: Task 2.1 — per operating-mode rolling missing-feature window (IF)
        #: and the mode's latched blind state, keyed by mode name.
        self._if_mode_missing: dict[str, deque[bool]] = {}
        self._if_mode_blind: dict[str, bool] = {}
        #: S01.1 — when each startup stage was reached, served as ``/status``'s
        #: ``startup`` block. Replaced by ``run_daemon(startup=...)`` with the
        #: timeline ``src.main`` began before the daemon existed.
        self.startup = StartupTimeline()

    # -- host -------------------------------------------------------------

    @property
    def host_cpu(self) -> HostCpuSampler:
        """The CPU sampler, for the daemon and for tests that drive it by hand.

        Read-only: it is the one :class:`~src.daemon.host_metrics.HostMetrics`
        reads, so a sampler is handed to the constructor. Assigning here would
        replace a name nothing reads and change nothing."""
        return self._host.cpu

    def start_host_sampler(self) -> None:
        """Start the host sampler (idempotent). The daemon calls it at boot so the
        first ``/status`` already has a value; ``snapshot()`` starts it otherwise."""
        self._host.start_sampler()

    def _cpu_percent(self) -> float | None:
        """Busy fraction (0..1) of total CPU time over the sampler's trailing
        window (:class:`HostCpuSampler`), or ``None`` until it has two samples.
        A sampler the constructor was handed is the caller's to start."""
        return self._host.cpu_percent()

    # -- inference --------------------------------------------------------

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

    def note_swap(self, kind: str, *, version: str | None = None) -> None:
        self._swaps[kind] = self._swaps.get(kind, 0) + 1
        if kind == "swapped" and version is not None:
            self._last_swap_version = version

    # -- detector-state blocks ---------------------------------------------

    def set_detector_block(
        self, name: DetectorBlock, value: Any, *, key: str | None = None
    ) -> None:
        """Store ``value`` as the ``name`` block, replacing the last one; ``None`` clears it.

        What each block holds is :data:`DETECTOR_BLOCKS`. ``model_version_mismatch``
        is per model manager and takes the manager as ``key`` with a
        :class:`VersionPair`: a pair that agrees (or has an unknown side) clears
        that manager's entry, so the caller passes what it read and never
        decides what counts as a mismatch. Every other block
        takes no key. Values are copied in and copied out, so neither side can
        change the other's.

        ``segmenter_fallback`` is a flag, and its rising edge is the caller's
        to find (read :attr:`segmenter_fallback_active` before setting), since
        only the caller knows what to log.
        """
        spec = DETECTOR_BLOCKS.get(name)
        if spec is None:
            raise ValueError(f"unknown detector block {name!r}; known: {sorted(DETECTOR_BLOCKS)}")
        if spec.keyed and key is None:
            raise ValueError(f"detector block {name!r} needs a key (the model manager)")
        if not spec.keyed and key is not None:
            raise ValueError(f"detector block {name!r} takes no key")
        stored = spec.coerce(value)
        if key is not None:
            entries: dict[str, Any] = self._blocks.setdefault(name, {})
            if stored is None:
                entries.pop(key, None)
            else:
                entries[key] = stored
        elif stored is None:
            self._blocks.pop(name, None)
        else:
            self._blocks[name] = stored

    def _block_entries(self, *, tail: bool) -> dict[str, Any]:
        """The set blocks ``snapshot()`` carries, in registry order; head or tail group."""
        entries: dict[str, Any] = {}
        for name, spec in DETECTOR_BLOCKS.items():
            if spec.snapshot_key is None or spec.tail != tail:
                continue
            stored = self._blocks.get(name)
            if stored is None or (spec.keyed and not stored):
                continue
            entries[spec.snapshot_key] = spec.render(stored)
        return entries

    @property
    def segmenter_fallback_active(self) -> bool:
        """Whether the live segmenter is currently constant-mode-fallback
        (4.7) — ``/status`` surfaces this alongside ``if_blind_modes``."""
        return bool(self._blocks.get("segmenter_fallback", False))

    @property
    def capture_profile_gate(self) -> dict[str, Any] | None:
        """The latest capture-profile gate verdict, or ``None`` before one."""
        gate = self._blocks.get("capture_profile_gate")
        return None if gate is None else dict(gate)

    # -- sinks, queues and loss epochs --------------------------------------

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
            scheduler=dict(self._blocks.get("obd_scheduler") or {}),
            lifetime=None if lifetime is None else lifetime.to_dict(),
            loss_epoch=self.loss_epoch(now=monotonic),
            sinks=self._sinks.states(),
        )

    def loss_counters(self) -> dict[str, int | None]:
        """Every attributable loss counter, cumulative, ``None`` = unavailable
        (:meth:`SinkHealth.loss_counters`, with the kernel's can0 counters)."""
        return self._sinks.loss_counters(self._host.can0_raw())

    def loss_epoch(self, *, now: float | None = None) -> LossEpoch:
        """Snapshot every loss counter (C03.5). Take one at arm, one at stop."""
        return self._sinks.loss_epoch(
            monotonic=time.monotonic() if now is None else now,
            wall=_now(),
            can0=self._host.can0_raw(),
        )

    def observe_telemetry_queue(self, name: str, queue: Any) -> None:
        """Record queue depth and its high-water mark for one telemetry sink."""
        self._sinks.observe_telemetry_queue(name, queue)

    async def wait_for_queues_idle(
        self, names: Sequence[str] = ("ingest", "storage"), *, timeout: float
    ) -> dict[str, Any]:
        """Wait, bounded, for the named queues to be fully consumed (C03.8)."""
        return await self._sinks.wait_for_queues_idle(names, timeout=timeout)

    def note_telemetry_drop(self, name: str, *, count: int = 1) -> None:
        """Record explicit data loss at a telemetry sink."""
        self._sinks.note_telemetry_drop(name, count=count)

    def note_telemetry_error(self, name: str, exc: BaseException) -> None:
        """Record the latest exception from one telemetry sink."""
        self._sinks.note_telemetry_error(name, exc)

    def observe_receive_path(  # noqa: PLR0913
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
        """Record a collector receive path's producer-buffer pressure (C07.5)."""
        self._sinks.observe_receive_path(
            name,
            received=received,
            parse_errors=parse_errors,
            buffer_dropped=buffer_dropped,
            buffer_high_water=buffer_high_water,
            buffer_capacity=buffer_capacity,
            queue_dropped=queue_dropped,
            batches=batches,
            reconnects=reconnects,
        )

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
        """Record a polling collector's thread handoff (C03.6, ``obd_collect``)."""
        self._sinks.observe_collector_handoff(
            name,
            accepted=accepted,
            buffer_dropped=buffer_dropped,
            buffer_high_water=buffer_high_water,
            buffer_capacity=buffer_capacity,
            queue_dropped=queue_dropped,
            batches=batches,
            raw_responses=raw_responses,
            derived_records=derived_records,
            reconnects=reconnects,
        )

    def observe_telemetry_write(
        self,
        name: str,
        *,
        records: int,
        elapsed_seconds: float,
        flushes: int = 0,
    ) -> None:
        """Record durable-writer throughput without retaining every sample."""
        self._sinks.observe_telemetry_write(
            name, records=records, elapsed_seconds=elapsed_seconds, flushes=flushes
        )

    def observe_storage_publish(self, name: str, stats: dict[str, Any]) -> None:
        """The durable writer's chunk publishes (``PublishStats``)."""
        self._sinks.observe_storage_publish(name, stats)

    # -- /status ------------------------------------------------------------

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
            **self._block_entries(tail=False),
            "system": self._host.system(),
            "power": self._host.power(),
        }
        snap.update(self._block_entries(tail=True))
        lifetime = capture_lifetime.snapshot(time.monotonic())
        if lifetime is not None:
            # Arm-to-now statistics for the active capture (C03.2), beside —
            # never inside — the scheduler's trailing 60 s numbers.
            snap["capture_lifetime"] = lifetime.to_dict(events=False)
        sinks = self._sinks.snapshot()
        if sinks:
            snap["telemetry_sinks"] = sinks
        if scaler is not None and live_means is not None:
            snap["feature_drift"] = feature_drift(live_means, scaler).to_dict()
        if lstm_thresholds is not None:
            snap["reconstruction_error_drift"] = reconstruction_error_drift(
                live_mean_recon_error, lstm_thresholds
            )
        can0 = self._host.can0_status()
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
