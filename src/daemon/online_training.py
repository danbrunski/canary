"""Config-gated scheduled retrain (Task 3.2, online-training-strategy.md).

Mirrors :func:`src.daemon.retention.run_retention_sweeper`'s shape: a loop
bounded by ``stop_event``, ticking on ``interval_seconds``. Each tick submits
a ``retrain_all`` job through the existing :class:`~src.control.jobs.JobController`
— the same allow-listed subprocess mechanism the dashboard's manual "Retrain"
button already uses — so a scheduled retrain never runs on the daemon's own
event loop or blocks real-time capture; the daemon just watches the job's
status in its in-memory ledger cache (a cheap dict lookup, not additional
training).

The job is windowed to the trailing ``window_days`` of sessions
(``--since``, Task 3.1) — computed fresh every tick, so the window genuinely
slides rather than being a fixed corpus pinned at daemon startup. The
scheduler never has a code path that can request
``--no-calibration-holdout``: :meth:`JobController._session_args` only adds
``--since`` from a ``since`` param, nothing here ever sets the flag that
disables the holdout, and if the selected window is too small to spare one,
``retrain.py`` itself refuses (:data:`WINDOW_TOO_SMALL_FOR_HOLDOUT`) rather
than degrading to in-sample calibration — this loop treats that refusal as a
routine skip, not an error.

A scheduled run lands ``unpublished`` in the training ledger (the default
``TrainingRunRecord.publish_status``, unchanged by this module) — promotion
to ``active`` is a separate, explicit action (Task 3.3's promotion gate);
this scheduler never calls it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from src.control.jobs import JobController, JobRecord
from src.models.retrain_exit_codes import WINDOW_TOO_SMALL_FOR_HOLDOUT

log = logging.getLogger(__name__)

__all__ = ["run_online_training_scheduler"]

_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


async def run_online_training_scheduler(
    job_controller: JobController,
    stop_event: asyncio.Event,
    *,
    interval_seconds: float,
    window_days: int,
    poll_interval_seconds: float = 5.0,
) -> None:
    """Submit a windowed ``retrain_all`` job every ``interval_seconds``.

    Never raises out of the loop: a scheduling bug on one tick is logged and
    the loop keeps running — a broken scheduler must not take down capture,
    same posture as the retention sweeper.
    """
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")

    while not stop_event.is_set():
        since = (datetime.now(UTC) - timedelta(days=window_days)).strftime("%Y-%m-%d")
        try:
            job = job_controller.submit("retrain_all", {"since": since})
            log.info(
                "online-training: scheduled retrain submitted",
                extra={"job_id": job.job_id, "since": since},
            )
            record = await _await_terminal(
                job_controller, job.job_id, poll_interval_seconds, stop_event
            )
            if record is None:
                log.info(
                    "online-training: daemon stopping, scheduled retrain left running",
                    extra={"job_id": job.job_id},
                )
            elif record.status == "succeeded":
                log.info(
                    "online-training: scheduled retrain succeeded",
                    extra={"job_id": record.job_id, "since": since},
                )
            elif record.returncode == WINDOW_TOO_SMALL_FOR_HOLDOUT:
                log.info(
                    "online-training: scheduled retrain skipped — window too small "
                    "for a calibration holdout (never degrades to in-sample)",
                    extra={"job_id": record.job_id, "since": since},
                )
            else:
                log.error(
                    "online-training: scheduled retrain failed",
                    extra={
                        "job_id": record.job_id,
                        "status": record.status,
                        "returncode": record.returncode,
                    },
                )
        except Exception:  # noqa: BLE001 — a scheduling bug must not kill capture
            log.exception("online-training scheduler tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


async def _await_terminal(
    job_controller: JobController,
    job_id: str,
    poll_interval_seconds: float,
    stop_event: asyncio.Event,
) -> JobRecord | None:
    while not stop_event.is_set():
        record = job_controller.get(job_id)
        if record is None or record.status in _TERMINAL_STATUSES:
            return record
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_seconds)
        except TimeoutError:
            continue
    return job_controller.get(job_id)
