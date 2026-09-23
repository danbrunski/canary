"""Tests for the ntfy notifier (Task 5.2, eval-longitudinal-and-dtc.md)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest

from src.alerts.events import AnomalyEvent
from src.alerts.notifier import Notifier, format_digest_message, format_event_message
from src.config import NotificationsConfig


def _event(**overrides: Any) -> AnomalyEvent:
    fields: dict[str, Any] = {
        "timestamp": "2026-07-19T12:00:00+00:00",
        "source": "fused",
        "model_version": "if=v1|lstm=v1",
        "score": 0.9,
        "threshold": 0.5,
        "affected_channels": ("obd:RPM",),
        "session": "2026-07-19T115900",
    }
    fields.update(overrides)
    return AnomalyEvent(**fields)


def _config(**overrides: Any) -> NotificationsConfig:
    fields: dict[str, Any] = {
        "enabled": True,
        "ntfy_url": "https://ntfy.example",
        "topic": "canary-test",
    }
    fields.update(overrides)
    return NotificationsConfig(**fields)


# ---------------------------------------------------------------------------
# Formatting — pure, no transport involved
# ---------------------------------------------------------------------------


def test_format_event_message_includes_score_and_channels() -> None:
    title, body = format_event_message(_event())
    assert "fused" in title
    assert "0.9" in body
    assert "obd:RPM" in body
    assert "2026-07-19T115900" in body


def test_format_event_message_prefers_behavior_cause() -> None:
    event = _event(source="behavior", detail={"cause": "afr_bank_balance critical: lean"})
    _, body = format_event_message(event)
    assert body.startswith("afr_bank_balance critical: lean")


def test_format_digest_message_compact_rendering() -> None:
    digest = {
        "session_id": "2026-07-19T120000",
        "duration_seconds": 1800.0,
        "anomaly_counts_by_source": {"fused": 2, "behavior": 1},
        "new_dtcs": [{"code": "P0171"}],
        "new_ledger_events": [{"id": "abc"}],
        "fingerprint_top_movers": [{"stat": "idle_rpm_mean", "z_score": 3.2}],
    }
    title, body = format_digest_message(digest)
    assert "2026-07-19T120000" in title
    assert "30.0 min" in body
    assert "3 gated anomaly event" in body
    assert "fused=2" in body
    assert "P0171" in body
    assert "new ledger events: 1" in body
    assert "idle_rpm_mean" in body


def test_format_digest_message_omits_evidence_line_when_zero() -> None:
    digest = {
        "session_id": "s",
        "duration_seconds": 60.0,
        "anomaly_counts_by_source": {"fused": 1},
        "evidence_counts_by_source": {},
    }
    _, body = format_digest_message(digest)
    assert "evidence" not in body


def test_format_digest_message_reads_a_pre_a06_sidecar() -> None:
    """A schema-1 digest has no evidence key; every event it counted was an
    alert, because roles did not exist yet."""
    _, body = format_digest_message(
        {"session_id": "s", "duration_seconds": 60.0, "anomaly_counts_by_source": {"fused": 2}}
    )
    assert "2 gated anomaly event(s)" in body
    assert "evidence" not in body


def test_format_digest_message_never_counts_evidence_as_gated() -> None:
    digest = {
        "session_id": "s",
        "duration_seconds": 60.0,
        "anomaly_counts_by_source": {},
        "evidence_counts_by_source": {"isolation_forest": 3, "lstm_autoencoder": 2},
    }
    _, body = format_digest_message(digest)
    assert "0 gated anomaly event(s)" in body
    assert "by source" not in body
    assert "5 evidence event(s) (not alerts): isolation_forest=3, lstm_autoencoder=2" in body


# ---------------------------------------------------------------------------
# Transport — mocked, fire-and-forget
# ---------------------------------------------------------------------------


async def test_notify_posts_to_configured_topic() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["title"] = request.headers.get("Title")
        seen["body"] = request.content.decode("utf-8")
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = Notifier(_config(), client=client)
    notifier.notify("Trip digest", "30 min, 0 anomalies")
    # notify() only schedules the task; let the event loop run it.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await client.aclose()

    assert seen["url"] == "https://ntfy.example/canary-test"
    assert seen["title"] == "Trip digest"
    assert seen["body"] == "30 min, 0 anomalies"


async def test_notify_disabled_never_sends() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = Notifier(_config(enabled=False), client=client)
    notifier.notify("title", "body")
    await asyncio.sleep(0)
    await client.aclose()

    assert calls == []


async def test_notify_blank_topic_never_sends() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = Notifier(_config(topic=""), client=client)
    notifier.notify("title", "body")
    await asyncio.sleep(0)
    await client.aclose()

    assert calls == []


async def test_notify_failure_is_isolated_and_logged_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raising/broken transport must not propagate — this is the hard
    correctness requirement: a notification failure can never disrupt the
    anomaly-detection pipeline."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = Notifier(_config(), client=client)
    with caplog.at_level(logging.WARNING, logger="src.alerts.notifier"):
        notifier.notify("title", "body")  # must not raise
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    await client.aclose()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "ntfy push failed" in warnings[0].message


def test_notify_without_running_loop_does_not_raise() -> None:
    """A sync caller (no event loop) must get a silent no-op, not a crash."""
    notifier = Notifier(_config())
    notifier.notify("title", "body")  # no running loop; must not raise


async def test_notify_timeout_does_not_stall_caller() -> None:
    """``notify`` schedules and returns immediately regardless of how long
    the send takes — the emit path is never blocked on the network."""

    class _SlowTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(10)
            return httpx.Response(200)  # pragma: no cover — never reached in test

    client = httpx.AsyncClient(transport=_SlowTransport())
    notifier = Notifier(_config(), client=client)

    loop = asyncio.get_running_loop()
    before = {t for t in asyncio.all_tasks(loop) if not t.done()}
    start = loop.time()
    notifier.notify("title", "body")
    elapsed = loop.time() - start
    assert elapsed < 0.5  # returned immediately, did not await the send

    # Cleanup: cancel the background send so it doesn't outlive the test.
    for task in asyncio.all_tasks(loop) - before:
        task.cancel()
    await client.aclose()
