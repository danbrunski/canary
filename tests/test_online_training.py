"""Tests for the online-training scheduler (Task 3.2, online-training-strategy.md)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from src.control.jobs import JobRecord
from src.daemon.online_training import run_online_training_scheduler
from src.models.retrain_exit_codes import WINDOW_TOO_SMALL_FOR_HOLDOUT


class _FakeJobController:
    """Duck-typed ``JobController`` stand-in.

    ``get`` always returns the terminal state the test configured, so the
    scheduler's ``_await_terminal`` poll resolves on its very first check —
    no real subprocess, no timing races to fight in the test.
    """

    def __init__(self, *, status: str, returncode: int | None = 0) -> None:
        self.status = status
        self.returncode = returncode
        self.submissions: list[tuple[str, dict[str, Any]]] = []
        self._job_id = "job-1"

    def submit(self, kind: str, params: dict[str, Any]) -> JobRecord:
        self.submissions.append((kind, dict(params)))
        return JobRecord(
            job_id=self._job_id,
            kind=kind,
            status="queued",
            created_at="2026-07-19T00:00:00Z",
            log_path=Path("/dev/null"),
        )

    def get(self, job_id: str) -> JobRecord | None:
        return JobRecord(
            job_id=job_id,
            kind="retrain_all",
            status=self.status,  # type: ignore[arg-type]
            created_at="2026-07-19T00:00:00Z",
            log_path=Path("/dev/null"),
            returncode=self.returncode,
        )


async def _run_one_tick(
    controller: _FakeJobController, *, interval_seconds: float = 1000.0
) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_online_training_scheduler(
            controller,  # type: ignore[arg-type]
            stop,
            interval_seconds=interval_seconds,
            window_days=7,
            poll_interval_seconds=0.01,
        )
    )
    for _ in range(100):
        if controller.submissions:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)  # let the tick's post-submit logging land
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)


async def test_scheduler_submits_retrain_all_with_since_window_no_holdout_override() -> None:
    controller = _FakeJobController(status="succeeded", returncode=0)
    await _run_one_tick(controller)
    assert len(controller.submissions) == 1
    kind, params = controller.submissions[0]
    assert kind == "retrain_all"
    assert "since" in params
    # Structurally cannot request in-sample calibration — no params key exists
    # for it anywhere in this module.
    assert "no_calibration_holdout" not in params


async def test_scheduler_logs_success_message(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="src.daemon.online_training")
    controller = _FakeJobController(status="succeeded", returncode=0)
    await _run_one_tick(controller)
    assert any("succeeded" in record.message for record in caplog.records)


async def test_scheduler_treats_window_too_small_as_skip_not_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="src.daemon.online_training")
    controller = _FakeJobController(status="failed", returncode=WINDOW_TOO_SMALL_FOR_HOLDOUT)
    await _run_one_tick(controller)
    assert any(
        "skipped" in record.message and record.levelname == "INFO" for record in caplog.records
    )
    assert not any(record.levelname == "ERROR" for record in caplog.records)


async def test_scheduler_logs_error_on_genuine_failure(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="src.daemon.online_training")
    controller = _FakeJobController(status="failed", returncode=1)
    await _run_one_tick(controller)
    assert any(
        record.levelname == "ERROR" and "failed" in record.message for record in caplog.records
    )


async def test_scheduler_honors_interval() -> None:
    controller = _FakeJobController(status="succeeded", returncode=0)
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_online_training_scheduler(
            controller,  # type: ignore[arg-type]
            stop,
            interval_seconds=30.0,
            window_days=7,
            poll_interval_seconds=0.01,
        )
    )
    try:
        for _ in range(100):
            if controller.submissions:
                break
            await asyncio.sleep(0.01)
        assert len(controller.submissions) == 1
        await asyncio.sleep(0.3)
        assert len(controller.submissions) == 1  # a second tick would resubmit
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)


async def test_scheduler_rejects_non_positive_interval() -> None:
    with pytest.raises(ValueError):
        await run_online_training_scheduler(
            _FakeJobController(status="succeeded"),  # type: ignore[arg-type]
            asyncio.Event(),
            interval_seconds=0,
            window_days=7,
        )


async def test_scheduler_stops_promptly_when_stop_event_set_mid_wait() -> None:
    """Never busy-loops: `stop_event.set()` during the inter-tick sleep
    returns quickly rather than waiting out the full interval."""
    controller = _FakeJobController(status="succeeded", returncode=0)
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_online_training_scheduler(
            controller,  # type: ignore[arg-type]
            stop,
            interval_seconds=1000.0,
            window_days=7,
            poll_interval_seconds=0.01,
        )
    )
    for _ in range(100):
        if controller.submissions:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)  # would hang ~1000s if not honored
