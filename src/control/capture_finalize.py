"""Capture finalization: boundary, drain, flush, then judge (roadmap C03.8).

The stop route used to score the trailing scheduler window and write the
sidecar *before* the writer rotated — so the verdict never knew whether the
last records made it to disk, and a flush failure at stop left a passing
sidecar beside a truncated session. The order here is the point:

1. **Boundary** — bump the capture generation. Readings issued afterwards
   carry the next generation and cannot complete a tuple in this capture.
2. **Drain** — wait, bounded, for the ingest and storage queues to be fully
   consumed, so every record the collector handed off has met the writer.
3. **Flush** — push the writer's buffer to Parquet. A failure here is
   recorded, not swallowed: the sidecar then says ``persistence_loss``.
4. **Judge** — read the rows back, count tuples per round, reconcile the
   loss-counter epochs, and only then write ``capture_quality.json``.

``live_capture_quality`` is the same arithmetic without the boundary: it
flushes and reads what is on disk *so far*, which is what the arm-time
verification and the MCP tool report. Both go through
``recompute_capture_quality`` so a live, a stop-time and a recomputed
verdict cannot disagree about what a complete tuple is.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from src.collection import capture_generation
from src.collection.capture_losses import (
    LossEpoch,
    reconcile_losses,
    summarize_losses,
)
from src.collection.capture_quality import SCOPE_LIVE, SCOPE_STOP, CaptureQuality
from src.control.capture_manifest import (
    read_capture_epoch,
    write_capture_epoch,
    write_capture_quality,
)

log = logging.getLogger(__name__)

#: How long a stop waits for the ingest and storage queues to empty. A
#: healthy daemon drains a few hundred records in well under a second; a
#: wedged sink is reported as ``drain_timed_out``, never waited on forever.
DRAIN_TIMEOUT_SECONDS = 5.0


class _Health(Protocol):
    def capture_quality_inputs(self, *, now: float | None = None) -> Any: ...

    def loss_epoch(self, *, now: float | None = None) -> LossEpoch: ...

    async def wait_for_queues_idle(self, names: Any = ..., *, timeout: float) -> dict[str, Any]: ...


class _Writer(Protocol):
    @property
    def rows_written(self) -> int: ...

    async def flush(self) -> Path | None: ...


@dataclass(frozen=True)
class StartEpoch:
    """What the arm wrote down for the stop to reconcile against."""

    loss_epoch: LossEpoch
    generation: int
    started_wall: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "generation": self.generation,
            "started_wall": round(self.started_wall, 3),
            "loss_epoch": self.loss_epoch.to_dict(),
        }


def record_capture_start(session_dir: Path, health: _Health | None) -> StartEpoch | None:
    """Snapshot the loss counters at arm and persist them beside the manifest."""
    if health is None:
        return None
    epoch = StartEpoch(
        loss_epoch=health.loss_epoch(),
        generation=capture_generation.current(),
        started_wall=time.time(),
    )
    try:
        write_capture_epoch(session_dir, epoch.to_dict())
    except OSError:
        log.exception("failed to write capture epoch sidecar for %s", session_dir)
    return epoch


def _start_epoch_from_disk(session_dir: Path) -> LossEpoch | None:
    payload = read_capture_epoch(session_dir)
    if payload is None:
        return None
    raw = payload.get("loss_epoch")
    return LossEpoch.from_dict(raw) if isinstance(raw, Mapping) else None


def losses_since(
    session_dir: Path, start: StartEpoch | None, stop: LossEpoch | None
) -> dict[str, Any]:
    """Reconcile arm-time and stop-time epochs; a missing side reads as unknown.

    The arm epoch comes from memory when this process armed the capture, else
    from ``capture_epoch.json`` — so a daemon restarted mid-capture still
    reconciles against the counters as they were at arm, or says it cannot.
    """
    start_epoch = start.loss_epoch if start is not None else _start_epoch_from_disk(session_dir)
    return summarize_losses(reconcile_losses(start_epoch, stop))


async def live_capture_quality(
    session_dir: Path,
    *,
    health: _Health,
    writer: _Writer | None,
    start: StartEpoch | None,
) -> CaptureQuality:
    """Score the running capture on what has been persisted so far."""
    from src.analysis.capture_coverage import recompute_capture_quality

    flush_error = await _flush(writer)
    inputs = health.capture_quality_inputs()
    losses = losses_since(session_dir, start, inputs.loss_epoch)
    persistence = {
        "flushed": flush_error is None,
        "flush_error": flush_error,
        "rows_written": None if writer is None else writer.rows_written,
        "scope": SCOPE_LIVE,
    }
    return recompute_capture_quality(
        session_dir,
        lifetime=inputs.lifetime,
        losses=losses,
        persistence=persistence,
        scheduler=inputs.scheduler,
        scope=SCOPE_LIVE,
    )


async def finalize_capture(
    session_dir: Path,
    *,
    health: _Health | None,
    writer: _Writer | None,
    start: StartEpoch | None,
    drain_timeout: float = DRAIN_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    """Boundary, drain, flush, then write the stop-time sidecar.

    Never raises: a capture that recorded good data must not fail to stop
    because its report card could not be written. Returns the sidecar payload
    or ``None`` when nothing could be scored (no health monitor) — which the
    digest reads as unknown, never as a pass.
    """
    boundary = capture_generation.bump("capture-stop")
    if health is None:
        return None
    try:
        return await _finalize(
            session_dir,
            health=health,
            writer=writer,
            start=start,
            boundary=boundary,
            drain_timeout=drain_timeout,
        )
    except Exception:
        log.exception("failed to finalize capture quality for %s", session_dir)
        return None


async def _finalize(  # noqa: PLR0913
    session_dir: Path,
    *,
    health: _Health,
    writer: _Writer | None,
    start: StartEpoch | None,
    boundary: int,
    drain_timeout: float,
) -> dict[str, Any]:
    from src.analysis.capture_coverage import recompute_capture_quality

    # Lifetime is read before the drain so the interval ends at the boundary,
    # not after however long the queues took.
    inputs = health.capture_quality_inputs()
    drain = await health.wait_for_queues_idle(timeout=drain_timeout)
    flush_error = await _flush(writer)
    stop_epoch = health.loss_epoch()
    losses = losses_since(session_dir, start, stop_epoch)
    lifetime = (
        None if inputs.lifetime is None else {**inputs.lifetime, "generation_boundary": boundary}
    )
    persistence = {
        "scope": SCOPE_STOP,
        "generation_boundary": boundary,
        "drained": drain["drained"],
        "drain_timed_out": drain["timed_out"],
        "drain_unobserved": drain["unobserved"],
        "flushed": flush_error is None,
        "flush_error": flush_error,
        "rows_written": None if writer is None else writer.rows_written,
        "loss_epoch_start": None if start is None else start.loss_epoch.to_dict(),
        "loss_epoch_stop": stop_epoch.to_dict(),
    }
    quality = recompute_capture_quality(
        session_dir,
        lifetime=lifetime,
        losses=losses,
        persistence=persistence,
        scheduler=inputs.scheduler,
        scope=SCOPE_STOP,
    )
    payload = quality.to_dict()
    write_capture_quality(session_dir, payload)
    if not quality.passed:
        log.warning(
            "capture %s ended with verdict %s",
            session_dir.name,
            quality.verdict,
            extra={"capture_verdict": quality.verdict, "failures": quality.failures},
        )
    return payload


async def _flush(writer: _Writer | None) -> str | None:
    if writer is None:
        return None
    try:
        await writer.flush()
    except Exception as exc:  # noqa: BLE001 — the failure is the finding
        log.exception("capture flush failed at stop")
        return f"{type(exc).__name__}: {exc}"
    return None


__all__ = [
    "DRAIN_TIMEOUT_SECONDS",
    "StartEpoch",
    "finalize_capture",
    "live_capture_quality",
    "losses_since",
    "record_capture_start",
]
