"""Process-wide CAN transmit gate — "passive mode".

CANary's *broadcast* capture (``can_collector``) has always been listen-only
per FR-02 / NFR-06, but the OBD side is not: ``can_obd_client`` and
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
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

import can

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


def is_passive() -> bool:
    """True when CAN transmission is currently suppressed."""
    with _lock:
        return _passive


def set_passive(enabled: bool, *, source: str = "control") -> bool:
    """Enable/disable passive mode. Returns the *previous* state.

    ``source`` is recorded for the status payload so an operator can tell a
    deliberate toggle from an env-var boot.
    """
    global _passive, _source
    with _lock:
        previous = _passive
        _passive = bool(enabled)
        _source = source
    if previous != bool(enabled):
        log.warning(
            "passive mode %s",
            "ENABLED (CAN TX suppressed)" if enabled else "disabled",
            extra={"passive": bool(enabled), "source": source},
        )
    return previous


def suppressed_count() -> int:
    """Number of frames dropped by the gate since process start."""
    with _lock:
        return _suppressed


def reset_suppressed() -> None:
    """Zero the suppression counter (tests)."""
    global _suppressed
    with _lock:
        _suppressed = 0


def snapshot() -> dict[str, Any]:
    """Status-payload view of the gate."""
    with _lock:
        return {
            "enabled": _passive,
            "source": _source,
            "suppressed_frames": _suppressed,
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
        if _passive:
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
    "is_passive",
    "reset_suppressed",
    "send",
    "set_passive",
    "snapshot",
    "suppressed_count",
]
