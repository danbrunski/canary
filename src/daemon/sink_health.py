"""Telemetry sink, queue and loss-epoch accounting (R29.10; C03.5, C03.6, C03.8, C07.5, C10.5).

What ``/status`` serves as ``telemetry_sinks``, and what a capture verdict is
scored on: one state dict per named sink (``ingest``, ``storage``,
``can_receive``, ``obd_collect``), the queue objects the sinks report on so a
capture stop can wait for them to drain, and the loss counters as an *epoch*.

``HealthMonitor`` keeps the public ``observe_telemetry_*`` / ``note_telemetry_*``
methods the daemon calls and forwards them here, so no call site moved. Apart
from the drain deadline in :meth:`SinkHealth.wait_for_queues_idle`, this class
reads no clock and no sysfs: the facade hands it the instant and the kernel's
can0 counters, so a test that fixes the monitor's clock fixes the epochs too.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from src.storage.capture_losses import CAN0_LOSS_FIELDS, LOSS_FIELDS, LossEpoch

__all__ = ["CaptureQualityInputs", "LossEpoch", "SinkHealth"]


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


class SinkHealth:
    """Per-sink counters and queue depths, single-writer like the monitor it serves."""

    def __init__(self) -> None:
        self._sinks: dict[str, dict[str, Any]] = {}
        #: The queue objects the sinks report on, so a capture stop can wait
        #: for the ingest and storage queues to drain (C03.8) without a
        #: second plumbing path to the daemon's queues.
        self._queues: dict[str, Any] = {}

    def states(self) -> dict[str, dict[str, Any]]:
        """Every sink's state, copied, in the order the sinks first reported."""
        return {name: dict(state) for name, state in self._sinks.items()}

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """The ``telemetry_sinks`` block: copied, sorted by name; empty with no sink."""
        return {name: dict(state) for name, state in sorted(self._sinks.items())}

    def loss_counters(self, can0: Mapping[str, int] | None) -> dict[str, int | None]:
        """Every attributable loss counter, cumulative, ``None`` = unavailable.

        Keys are ``<sink>.<field>``; :mod:`src.storage.capture_losses`
        assigns each a scope. ``can0`` is the kernel's counters, read by the
        caller: included so a receive overrun during a capture is visible, and
        reported as ``None`` (unknown) on a host without the interface — never
        as zero.
        """
        counters: dict[str, int | None] = {}
        for sink, state in self._sinks.items():
            for field_name in LOSS_FIELDS:
                value = state.get(field_name)
                counters[f"{sink}.{field_name}"] = None if value is None else int(value)
        for field_name in CAN0_LOSS_FIELDS:
            counters[f"can0.{field_name}"] = None if can0 is None else can0.get(field_name)
        return counters

    def loss_epoch(
        self, *, monotonic: float, wall: float, can0: Mapping[str, int] | None
    ) -> LossEpoch:
        """Snapshot every loss counter (C03.5). Take one at arm, one at stop."""
        return LossEpoch(
            taken_at_monotonic=monotonic,
            taken_at_wall=wall,
            counters=self.loss_counters(can0),
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
        return self._sinks.setdefault(
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
