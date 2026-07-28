"""Process-wide OBD request-rate override — the "bounded high-rate capture" knob.

The scheduler paces requests at ``BUS_BUDGET_MULTIPLIER x poll_rate_hz``, which
is **8 requests/second** on the dev vehicle. That number is a conservative
heuristic, not a measured limit: measured through ``CanObdClient`` on
2026-07-27, this ECU sustained **667 channel-reads/s single-PID and 1387
batched** — roughly two orders of magnitude of unused headroom.

The cost of that conservatism is concrete. WOT work needs many samples inside a
~5 s pull, and at 8 req/s spread over ~60 requests each channel lands at
0.06-0.5 Hz. On the 2026-07-27 drive CANary caught **23** throttle>=70% samples
across 40 minutes where UpRev's synchronously-sampled K-line log caught **537**.
Three separate attempts at WOT AFR and Load Point Scaler work have been blocked
on borrowing that external logger.

Narrowing the channel set (``tuning_profile``) is one lever and is already
built. This is the other one the docstring there names but nothing exposed:
raise the budget itself, for a bounded window.

Shaped like ``tx_gate`` and ``tuning_profile`` deliberately — process-wide
runtime state, no config churn, visible in ``/status``, reversible without a
restart. It is something you arm for one pull and drop again, not a deployment
posture.

**Why the TTL defaults to on here when passive mode's does not.** A forgotten
passive mode logs nothing, which is loud and self-announcing. A forgotten rate
override is quiet: it just keeps hammering the bus and burning Pi CPU at 25x
the intended rate for as long as the daemon runs. So ``set_override`` schedules
an auto-revert by default rather than on request; pass ``ttl_seconds=None``
explicitly if you genuinely want it to persist.

**Why the cap is 200 and not 667.** The 667 figure came from a probe loop with
the bus effectively to itself. In service this bus already carries ~29
broadcast IDs on 9-18 ms cadences. Each diagnostic read adds a request and a
response frame, so 200 req/s is ~400 extra frames/s — order 20% additional
load at 500 kbps, which leaves the broadcast traffic room. Going near the
measured ceiling would starve it. Raising the budget also does not help past
the point where responses stop keeping up: watch ``response_rate`` in
``/status``, and if it falls, the extra requests are pure bus noise.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

#: Hard ceiling for an override, in requests/second. See the module docstring
#: for why this sits well below the measured 667/s single-PID capability.
MAX_BUDGET_REQUESTS_PER_SECOND = 200.0

#: Floor — below the 2.0 req/s scheduler minimum an override is meaningless.
MIN_BUDGET_REQUESTS_PER_SECOND = 2.0

#: Default auto-revert window. Long enough for a few pulls plus the drive to
#: and from them; short enough that a forgotten override costs one trip, not
#: the life of the process.
DEFAULT_TTL_SECONDS = 600.0

_lock = threading.Lock()
_override: float | None = None
_source = "default"
_expires_at: float | None = None


class BudgetOutOfRangeError(ValueError):
    """Requested override is outside ``[MIN, MAX]`` requests/second."""


def override() -> float | None:
    """Current override in requests/second, or ``None`` when unset."""
    with _lock:
        return _override


def effective(default_rps: float) -> float:
    """The rate the scheduler should actually pace at right now.

    Callers pass the configured default so this module never needs to know
    about ``CanObdSpec``; the override simply wins when present. Read this
    *inside* the poll loop rather than once at startup, or an override armed
    mid-drive will not take effect until the daemon restarts.
    """
    with _lock:
        return default_rps if _override is None else _override


def set_override(
    rps: float | None,
    *,
    source: str = "control",
    ttl_seconds: float | None = DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> float | None:
    """Set (or clear, with ``None``) the request-rate override.

    Returns the *previous* override. ``ttl_seconds`` schedules an auto-revert
    — see the module docstring for why it defaults to on; pass ``None`` to opt
    out deliberately. Clearing always cancels any pending TTL. ``now``
    overrides ``time.monotonic()`` (tests).

    Raises :class:`BudgetOutOfRangeError` outside ``[MIN, MAX]`` rather than
    silently clamping: an operator who asked for 500 req/s has a mistaken
    model of the bus, and quietly giving them 200 hides that.
    """
    global _override, _source, _expires_at
    if rps is not None and not (
        MIN_BUDGET_REQUESTS_PER_SECOND <= rps <= MAX_BUDGET_REQUESTS_PER_SECOND
    ):
        raise BudgetOutOfRangeError(
            f"{rps} req/s outside [{MIN_BUDGET_REQUESTS_PER_SECOND}, "
            f"{MAX_BUDGET_REQUESTS_PER_SECOND}]"
        )
    effective_now = time.monotonic() if now is None else now
    with _lock:
        previous = _override
        _override = None if rps is None else float(rps)
        _source = source
        _expires_at = (
            effective_now + ttl_seconds if rps is not None and ttl_seconds is not None else None
        )
    if previous != _override:
        log.warning(
            "obd bus budget override %s",
            f"SET to {rps} req/s" if rps is not None else "cleared",
            extra={"bus_budget_rps": rps, "source": source, "ttl_seconds": ttl_seconds},
        )
    return previous


def check_expiry(*, now: float | None = None) -> bool:
    """Revert to the configured default if the TTL has passed.

    Returns whether it fired. Cheap no-op unless both armed and expired, so
    it is safe at whatever cadence ``src.daemon.polling.poll_bus_budget_ttl``
    picks.
    """
    global _override, _expires_at
    effective_now = time.monotonic() if now is None else now
    with _lock:
        if _override is None or _expires_at is None or effective_now < _expires_at:
            return False
        _override = None
        _expires_at = None
    log.warning("obd bus budget override TTL expired; reverted to configured default")
    return True


def reset() -> None:
    """Drop all state — for tests, which share this module across cases."""
    global _override, _source, _expires_at
    with _lock:
        _override = None
        _source = "default"
        _expires_at = None


def snapshot(*, now: float | None = None) -> dict[str, Any]:
    """Status-payload view, mirroring ``tx_gate.snapshot``."""
    effective_now = time.monotonic() if now is None else now
    with _lock:
        remaining = None if _expires_at is None else max(0.0, _expires_at - effective_now)
        return {
            "override_requests_per_second": _override,
            "source": _source,
            "ttl_remaining_seconds": None if remaining is None else round(remaining, 1),
            "max_requests_per_second": MAX_BUDGET_REQUESTS_PER_SECOND,
        }


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "MAX_BUDGET_REQUESTS_PER_SECOND",
    "MIN_BUDGET_REQUESTS_PER_SECOND",
    "BudgetOutOfRangeError",
    "check_expiry",
    "effective",
    "override",
    "reset",
    "set_override",
    "snapshot",
]
