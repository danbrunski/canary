"""Tests for src/daemon/polling.py's standalone background poll loops."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from src.collection import tx_gate
from src.daemon.polling import poll_passive_ttl

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_gate() -> Iterator[None]:
    tx_gate.set_passive(False, source="test")
    tx_gate.reset_suppressed()
    yield
    tx_gate.set_passive(False, source="test")
    tx_gate.reset_suppressed()


async def test_poll_passive_ttl_auto_disarms_after_expiry() -> None:
    tx_gate.set_passive(True, source="test", ttl_seconds=0.05)
    assert tx_gate.is_passive() is True

    stop = asyncio.Event()
    task = asyncio.create_task(poll_passive_ttl(stop, interval_seconds=0.01))
    try:
        for _ in range(50):
            if not tx_gate.is_passive():
                break
            await asyncio.sleep(0.01)
    finally:
        stop.set()
        await task

    assert tx_gate.is_passive() is False


async def test_poll_passive_ttl_leaves_a_ttl_less_arm_alone() -> None:
    tx_gate.set_passive(True, source="test")

    stop = asyncio.Event()
    task = asyncio.create_task(poll_passive_ttl(stop, interval_seconds=0.01))
    await asyncio.sleep(0.05)
    stop.set()
    await task

    assert tx_gate.is_passive() is True
