"""Push notifications via ntfy (Task 5.2, eval-longitudinal-and-dtc.md).

Best-effort, fire-and-forget: :meth:`Notifier.notify` schedules the POST as
its own asyncio task and returns immediately, and every failure inside that
task is caught and logged — never raised, never retried. A dead/misconfigured
ntfy endpoint (or, overnight, the fact that ``topic`` is deliberately blank
by default) must never slow down or crash the anomaly-detection pipeline
this hangs off of (:class:`~src.alerts.emitter.AnomalyEventEmitter`).

Formatting (:func:`format_event_message` / :func:`format_digest_message`) is
pure and separately testable from the HTTP send.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import httpx

from src.config import NotificationsConfig

if TYPE_CHECKING:
    from src.alerts.events import AnomalyEvent

log = logging.getLogger(__name__)

#: Short — a stalled/unreachable ntfy server must never hold up the caller
#: for long even in the worst case; the send already runs off to the side of
#: the emit path, but a wedged connection would otherwise pile up tasks.
DEFAULT_TIMEOUT_SECONDS = 5.0


class Notifier:
    """Fire-and-forget ntfy pusher.

    Construct one per daemon (or per test) from :class:`NotificationsConfig`.
    ``client`` is a test seam — pass an ``httpx.AsyncClient`` wired to
    ``httpx.MockTransport`` to assert on the request without a network call;
    production callers leave it ``None`` and a client is built lazily and
    closed after each push (pushes are rare — end-of-drive / critical alerts
    — so there's no benefit to holding a connection open between them).
    """

    def __init__(
        self,
        config: NotificationsConfig,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._config = config
        self._client = client
        self._timeout = timeout

    def notify(self, title: str, message: str, *, priority: str = "default") -> None:
        """Schedule a push; never blocks, never raises.

        A no-op when notifications are disabled/unconfigured, or when
        there's no running event loop to schedule the send on (e.g. a sync
        test calling this directly) — logged at debug, not a failure.
        """
        if not self._config.enabled or not self._config.topic:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.debug("notifier.notify: no running event loop; dropping push")
            return
        loop.create_task(self._send(title, message, priority=priority))

    async def _send(self, title: str, message: str, *, priority: str) -> None:
        client = self._client
        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)
        try:
            url = f"{self._config.ntfy_url.rstrip('/')}/{self._config.topic}"
            await client.post(
                url,
                content=message.encode("utf-8"),
                headers={"Title": title, "Priority": priority},
            )
        except Exception as exc:  # noqa: BLE001 — a notification must never propagate
            log.warning("ntfy push failed: %s", exc)
        finally:
            if owns_client:
                await client.aclose()


def format_event_message(event: AnomalyEvent) -> tuple[str, str]:
    """``(title, body)`` for a critical-alert push (5.3).

    Mirrors the compact one-liner :func:`~src.alerts.events.behavior_cause`
    renders for behavior events, generalized to every source: score vs.
    threshold plus whatever channels the source attributed the anomaly to.
    """
    title = f"CANary: {event.source} anomaly"
    channels = ", ".join(event.affected_channels) or "n/a"
    threshold = "n/a" if event.threshold is None else f"{event.threshold:.3g}"
    cause = event.detail.get("cause") if isinstance(event.detail, dict) else None
    body = (
        str(cause)
        if cause
        else f"score {event.score:.3g} (threshold {threshold}); channels: {channels}"
    )
    if event.session:
        body += f" [session {event.session}]"
    return title, body


def format_digest_message(digest: dict[str, Any]) -> tuple[str, str]:
    """``(title, body)`` compact rendering of a ``digest.json`` payload (5.5)."""
    session_id = digest.get("session_id", "?")
    duration_s = float(digest.get("duration_seconds") or 0.0)
    duration_min = duration_s / 60.0
    counts = digest.get("anomaly_counts_by_source") or {}
    total_anomalies = sum(int(v) for v in counts.values())
    new_dtcs = digest.get("new_dtcs") or []
    new_ledger_events = digest.get("new_ledger_events") or []

    title = f"CANary trip digest: {session_id}"
    lines = [f"{duration_min:.1f} min drive, {total_anomalies} gated anomaly event(s)"]
    if counts:
        by_source = ", ".join(f"{source}={count}" for source, count in sorted(counts.items()))
        lines.append(f"by source: {by_source}")
    if new_dtcs:
        codes = ", ".join(str(entry.get("code", "?")) for entry in new_dtcs)
        lines.append(f"new DTCs: {codes}")
    if new_ledger_events:
        lines.append(f"new ledger events: {len(new_ledger_events)}")
    top_movers = digest.get("fingerprint_top_movers") or []
    if top_movers:
        mover = top_movers[0]
        lines.append(f"top mover: {mover.get('stat')} z={mover.get('z_score', 'n/a')}")
    return title, "\n".join(lines)


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "Notifier",
    "format_digest_message",
    "format_event_message",
]
