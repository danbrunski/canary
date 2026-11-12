"""Refuse local whole-corpus reads while the daemon is capturing (R29.1).

``query_telemetry``, ``get_behavior_deviations``, ``search_anomaly_log`` and the
two tuning tools (``get_delivery_error``, ``compare_tuning_sessions``) run pandas
or DuckDB over stored Parquet *inside canary-mcp*, which ``systemd/canary-mcp.service``
runs on the Pi beside the collector. A capture is the one time the collector must
not lose memory or CPU to a report (AGENTS.md, "No analysis on the Pi while it
collects": a ``nice -19`` pandas run once cost a 3-minute outage and 150k records),
and the daemon's own routes already refuse with ``analysis_deferred``. These tools
do not go through the daemon, so they ask it.

The question is "is the daemon this server is configured against capturing?",
answered from ``GET /status?view=compact`` (``capture_profile.active``, the same
fact ``workbench_jobs.capture_deferral`` reads in the daemon). The answer when
the daemon cannot be asked is a decision, made once here:

* **The connection cannot be made** (refused, no route, name not resolved, the
  connect itself timing out): **fail open**. ``canary-mcp`` also runs on the
  laptop against a pulled corpus with no daemon, which is exactly where this
  analysis is meant to go (the deferral's ``laptop_path``); and nothing that
  accepts no connection is collecting, so there is nothing to protect. Refusing
  would make the laptop path unusable.
* **Anything else**: **fail closed**, refusing ``capture_state_unknown``. That
  is a connection that was made and then did not answer in time (a stalled
  daemon, which is the one that is busiest collecting), an HTTP error, a body
  that is not JSON, a body without ``capture_profile.active``. Something is
  listening and it will not say whether it is recording, which is the case the
  rule exists for; the refusal names it rather than guessing. A daemon that
  stalls past :data:`CAPTURE_PROBE_TIMEOUT_SECONDS` is refused, not waved through.

A ``canary-mcp`` that reads a corpus on one machine but is pointed at a daemon on
another (``CANARY_DAEMON_URL``) is refused while *that* daemon captures, even
though the work would not compete with it. Point it at a daemon on the same
machine as the corpus, or leave none reachable.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from src.mcp_server.client import DaemonClient, DaemonError

log = logging.getLogger(__name__)

#: Longest the probe waits for the daemon. The compact status answers in ~4 ms;
#: this only bounds a daemon that accepts the connection and then stalls.
CAPTURE_PROBE_TIMEOUT_SECONDS = 2.0


def _laptop_path(storage_path: Path | None) -> str:
    where = str(storage_path) if storage_path is not None else "<storage>"
    return (
        f"ssh canary@canary.local 'tar -cf - -C {where} <session>' | tar -xf - "
        "then run this tool on the laptop against the pulled copy (canary-mcp with "
        "CANARY_STORAGE_PATH pointing at it and no daemon reachable). Never per-file rsync."
    )


def _refusal(error: str, reason: str, tool: str, message: str, **extra: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "error": error,
        "reason": reason,
        "tool": tool,
        "message": message,
        **extra,
    }


def _no_daemon_listening(exc: DaemonError) -> bool:
    """True only when the connection itself could not be made.

    ``DaemonClient`` wraps every ``httpx.HTTPError`` as a status-less
    ``DaemonError``, so the cause is what tells a missing daemon from a stalled
    one: a read timeout is a connection that was made."""
    return exc.status is None and isinstance(
        exc.__cause__, httpx.ConnectError | httpx.ConnectTimeout
    )


def _unknown(tool: str, why: str) -> dict[str, Any]:
    return _refusal(
        "capture_state_unknown",
        "daemon_status_unreadable",
        tool,
        f"{tool} is refused: {why}, so whether a capture is recording is unknown, "
        "and a capture is never competed with.",
    )


async def capture_refusal(
    client: DaemonClient, *, tool: str, storage_path: Path | None = None
) -> dict[str, Any] | None:
    """``None`` when ``tool`` may run; otherwise the refusal to return instead.

    ``analysis_deferred`` (``capture_active``) names the profile and carries the
    laptop path, the daemon's own 409 shape. ``capture_state_unknown`` is the
    fail-closed case in the module docstring.
    """
    try:
        status = await client.get_json(
            "/status", {"view": "compact"}, timeout=CAPTURE_PROBE_TIMEOUT_SECONDS
        )
    except DaemonError as exc:
        if _no_daemon_listening(exc):
            log.debug("capture state not asked of %s (no daemon to connect to): %s", tool, exc)
            return None
        return _unknown(
            tool,
            f"the daemon answered its status with HTTP {exc.status}"
            if exc.status is not None
            else f"the daemon did not answer its status ({exc})",
        )
    except ValueError as exc:
        # ``DaemonClient`` parses the body itself; a 200 that is not JSON
        # (a proxy's error page, a half-written reply) raises here.
        return _unknown(tool, f"the daemon's status was not readable JSON ({exc})")
    profile = status.get("capture_profile") if isinstance(status, dict) else None
    active = profile.get("active") if isinstance(profile, dict) else None
    if not isinstance(active, bool):
        return _unknown(tool, "the daemon's status does not say whether a capture is recording")
    if not active:
        return None
    name = str(profile.get("name")) if isinstance(profile, dict) else "capture"
    return _refusal(
        "analysis_deferred",
        "capture_active",
        tool,
        f"{tool} is deferred while {name!r} is recording: it reads stored telemetry on "
        "the machine that is collecting.",
        profile=name,
        laptop_path=_laptop_path(storage_path),
    )


__all__ = ["CAPTURE_PROBE_TIMEOUT_SECONDS", "capture_refusal"]
