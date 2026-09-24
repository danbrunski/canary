"""Process-wide CAN transmit gate — "passive mode".

CANary's *broadcast* capture (``can_collector``) is listen-only by
construction, but the OBD side is not: ``can_obd_client`` and
``can_obd_collector`` transmit tester requests (Mode 01/03/06/07/09/0A polls,
Mode 22 DID reads, session control) and ISO-TP flow-control frames. That is
normally exactly what we want, and it is harmless — the ECU answers diagnostic
requests all day.

It stops being harmless when something *else* owns a diagnostic session with a
module: an ECU reprogramming tool holding a programming session, a bidirectional
actuator test, a scan tool mid-routine. Injecting unrelated requests into that
window can draw negative responses or perturb the other tool's sequencing.

Passive mode is the switch for those situations: the daemon keeps running —
broadcast CAN logging, storage, GPS, the anomaly pipeline, the dashboard — but
transmits nothing.

Two layers use this module:

* The OBD collector loop checks :func:`is_passive` and skips *issuing* requests
  altogether. That is the layer that matters for scheduler hygiene: if we only
  blocked the send, every request would time out, trip the miss-backoff logic,
  and passive mode would leave a degraded scheduler behind when it lifts.
* :func:`send` is the backstop, wrapping every ``bus.send()`` in the codebase.
  It catches paths that don't go through the poll loop — ISO-TP flow control,
  ad-hoc client queries from control routes — including ones added later by
  someone who didn't know passive mode existed.

For a hardware-level guarantee that no software path can defeat, put the
controller itself in listen-only mode (``ip link set can0 type can
listen-only on``); the interface then cannot drive the bus at all, not even
ACK bits. That needs an interface down/up cycle, so it is a deliberate
procedure rather than a runtime toggle — this module is the runtime one.

Arming passive mode is easy to forget to undo — the operator's own retro
tonight ("I armed it twice and had to remember to disarm both times") is
exactly the failure mode ``ttl_seconds`` guards against: pass it to
``set_passive`` and the gate auto-disarms itself once the deadline passes,
even if nobody calls ``set_passive(False, ...)``. A background poll task
(``src.daemon.polling.poll_passive_ttl``) calls :func:`check_expiry`
periodically so a died session or a forgotten disarm doesn't silently log
nothing forever.

**ECM session ownership (H05.6).** The ECM has one diagnostic session, shared
between CAN and K-line: an active K-line stream starved CAN Mode 01 for ~132 s
on 2026-09-20. So besides the operator's passive switch, a *holder* may claim
the session — ``kline`` (CANary's own K-line collector) or ``foreign``
(another tester seen on K-line, e.g. UpRev or a scan tool) — and while any
claim stands, CAN diagnostic transmission is suppressed exactly as in passive
mode. :func:`is_passive` answers "is CAN diagnostic TX suppressed?", which is
what every existing caller means (the OBD loop, record arming and idle
shutdown all treat it as "OBD is stale by design"), so they need no change.
A foreign claim outranks our own; claims may carry a TTL, and
:func:`check_expiry` releases one whose TTL has passed. Broadcast CAN capture
is listen-only and never affected.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Literal

import can

from src.collection import capture_generation, capture_lifetime

log = logging.getLogger(__name__)

#: Environment variable consulted once at import so the daemon can be started
#: already-passive (``CANARY_PASSIVE=1 systemctl start canary``) without a
#: config edit or a race against the first poll.
PASSIVE_ENV_VAR = "CANARY_PASSIVE"

_TRUTHY = {"1", "true", "yes", "on"}

_lock = threading.Lock()
_passive: bool = os.environ.get(PASSIVE_ENV_VAR, "").strip().lower() in _TRUTHY
_suppressed: int = 0
#: Set once at import purely so the status payload can explain *why* passive
#: mode is on when nobody remembers toggling it.
_source: str = "env" if _passive else "default"
#: ``time.monotonic()`` deadline for auto-disarm, or ``None`` for "stays
#: armed until manually disarmed" (the pre-TTL default behavior). Monotonic
#: rather than wall-clock so a system clock step (NTP sync, manual set)
#: can't extend or shorten an already-armed window.
_expires_at: float | None = None

EcmSessionHolder = Literal["kline", "foreign"]
EcmSessionOwner = Literal["can_diagnostics", "kline", "foreign"]
#: Holder -> ``time.monotonic()`` expiry, or ``None`` for "until released".
_claims: dict[str, float | None] = {}


def _owner_locked() -> EcmSessionOwner:
    if "foreign" in _claims:
        return "foreign"
    if "kline" in _claims:
        return "kline"
    return "can_diagnostics"


def is_passive() -> bool:
    """True when CAN diagnostic transmission is currently suppressed: the
    operator's passive switch, or another holder owns the ECM session."""
    with _lock:
        return _passive or bool(_claims)


def ecm_session_owner() -> EcmSessionOwner:
    """Who owns the ECM's diagnostic session right now."""
    with _lock:
        return _owner_locked()


def _owner_changed(previous: EcmSessionOwner, current: EcmSessionOwner, now: float) -> None:
    if previous == current:
        return
    capture_generation.bump(f"ecm-session-{current}")
    if previous != "can_diagnostics":
        capture_lifetime.mark_paused(False, reason=f"ecm-session-{previous}", now=now)
    if current != "can_diagnostics":
        capture_lifetime.mark_paused(True, reason=f"ecm-session-{current}", now=now)
    log.warning(
        "ECM diagnostic session owner: %s -> %s",
        previous,
        current,
        extra={"ecm_session_owner": current, "previous_owner": previous},
    )


def claim_ecm_session(
    holder: EcmSessionHolder, *, ttl_seconds: float | None = None, now: float | None = None
) -> EcmSessionOwner:
    """Claim (or renew) the ECM session for ``holder``. Returns the owner
    before the claim. Renewing with a TTL pushes the expiry out."""
    effective_now = time.monotonic() if now is None else now
    with _lock:
        previous = _owner_locked()
        _claims[holder] = effective_now + ttl_seconds if ttl_seconds is not None else None
        current = _owner_locked()
    _owner_changed(previous, current, effective_now)
    return previous


def release_ecm_session(holder: EcmSessionHolder, *, now: float | None = None) -> None:
    effective_now = time.monotonic() if now is None else now
    with _lock:
        previous = _owner_locked()
        _claims.pop(holder, None)
        current = _owner_locked()
    _owner_changed(previous, current, effective_now)


def reset_ecm_session_claims(*, now: float | None = None) -> None:
    """Drop every claim (test and validation-harness setup)."""
    for holder in ("foreign", "kline"):
        release_ecm_session(holder, now=now)


def set_passive(
    enabled: bool,
    *,
    source: str = "control",
    ttl_seconds: float | None = None,
    now: float | None = None,
) -> bool:
    """Enable/disable passive mode. Returns the *previous* state.

    ``source`` is recorded for the status payload so an operator can tell a
    deliberate toggle from an env-var boot. ``ttl_seconds`` (only meaningful
    when arming) schedules an auto-disarm after that many seconds —
    :func:`check_expiry` (polled by
    ``src.daemon.polling.poll_passive_ttl``) is what actually flips it back
    off; omit for the old "stays armed until manually disarmed" behavior.
    Disarming (``enabled=False``) always clears any pending TTL. ``now``
    overrides ``time.monotonic()`` (tests).
    """
    global _passive, _source, _expires_at
    effective_now = time.monotonic() if now is None else now
    with _lock:
        previous = _passive
        _passive = bool(enabled)
        _source = source
        _expires_at = effective_now + ttl_seconds if enabled and ttl_seconds is not None else None
    if previous != bool(enabled):
        # Polling stops or resumes: a new capture generation (C02.8), and a
        # pause window in the capture-lifetime ledger (C03.4).
        capture_generation.bump("passive-on" if enabled else "passive-off")
        capture_lifetime.mark_paused(bool(enabled), reason="passive", now=effective_now)
        log.warning(
            "passive mode %s",
            "ENABLED (CAN TX suppressed)" if enabled else "disabled",
            extra={"passive": bool(enabled), "source": source, "ttl_seconds": ttl_seconds},
        )
    return previous


def check_expiry(*, now: float | None = None) -> bool:
    """Auto-disarm if a TTL was set and it has passed. Returns whether it fired.

    Cheap and safe to call at whatever cadence
    ``src.daemon.polling.poll_passive_ttl`` chooses (seconds-scale is plenty
    — TTLs are minutes-scale) since it's a no-op unless both armed and
    expired.
    """
    global _passive, _expires_at
    effective_now = time.monotonic() if now is None else now
    with _lock:
        expired_claims = [
            holder
            for holder, deadline in _claims.items()
            if deadline is not None and effective_now >= deadline
        ]
    for holder in expired_claims:
        log.warning("ECM session claim by %s expired; released", holder)
        release_ecm_session(holder, now=effective_now)  # type: ignore[arg-type]
    with _lock:
        if not _passive or _expires_at is None or effective_now < _expires_at:
            return bool(expired_claims)
        _passive = False
        _expires_at = None
    log.warning("passive mode TTL expired; auto-disarmed", extra={"passive": False})
    return True


def suppressed_count() -> int:
    """Number of frames dropped by the gate since process start."""
    with _lock:
        return _suppressed


def reset_suppressed() -> None:
    """Zero the suppression counter (tests)."""
    global _suppressed
    with _lock:
        _suppressed = 0


def snapshot(*, now: float | None = None) -> dict[str, Any]:
    """Status-payload view of the gate."""
    effective_now = time.monotonic() if now is None else now
    with _lock:
        ttl_remaining = (
            round(max(0.0, _expires_at - effective_now), 1)
            if _passive and _expires_at is not None
            else None
        )
        return {
            "enabled": _passive,
            "source": _source,
            "suppressed_frames": _suppressed,
            "ttl_remaining_seconds": ttl_remaining,
            "ecm_session_owner": _owner_locked(),
            "ecm_session_claims": {
                holder: (
                    round(max(0.0, deadline - effective_now), 1) if deadline is not None else None
                )
                for holder, deadline in sorted(_claims.items())
            },
            "tx_suppressed": _passive or bool(_claims),
        }


def send(bus: can.BusABC, message: can.Message, *, what: str) -> bool:
    """Transmit ``message`` unless passive mode is on.

    Returns True if the frame went out, False if it was suppressed. Callers
    must treat False as "no response is coming" — it is not an error, and it
    must not be counted as a bus fault or a request timeout.

    ``can.CanError`` is deliberately NOT caught here: existing call sites have
    their own logging and recovery for a genuinely failed send, and swallowing
    it would change their behaviour.
    """
    global _suppressed
    with _lock:
        if _passive or _claims:
            _suppressed += 1
            suppressed = _suppressed
        else:
            suppressed = None
    if suppressed is not None:
        # Every suppressed frame at INFO would flood the log at poll rate;
        # the counter in the status payload is the real signal.
        log.debug("passive mode suppressed CAN TX", extra={"what": what, "count": suppressed})
        return False
    bus.send(message)
    return True


__all__ = [
    "PASSIVE_ENV_VAR",
    "EcmSessionHolder",
    "EcmSessionOwner",
    "check_expiry",
    "claim_ecm_session",
    "ecm_session_owner",
    "is_passive",
    "release_ecm_session",
    "reset_ecm_session_claims",
    "reset_suppressed",
    "send",
    "set_passive",
    "snapshot",
    "suppressed_count",
]
