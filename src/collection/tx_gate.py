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

**An armed passive survives a daemon restart (D03.30).** On 2026-09-24 the
operator had a scan tool connected with passive armed (TTL 1800 s); a deploy
restart came up with passive off and CANary sent Mode 01 for ~13 s before it
was re-armed by hand. The switch was process memory only. Now, once
:func:`restore_persisted` has named a state file (``src.main`` does it before
the startup VIN probe transmits; ``run_daemon`` does it before any collector
starts), every arm is written there — wall-clock ``armed_at``, ``ttl_seconds``
and ``source`` — and every disarm or TTL expiry removes it. The next process
restores an arm with TTL remaining before it transmits anything; one that
expired while the daemon was down is dropped, and the log says which. ECM
session *claims* are not persisted: they describe a live K-line exchange that
the next process re-detects.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import can

from src.atomic_write import atomic_write_text
from src.collection import capture_generation, capture_lifetime

if TYPE_CHECKING:
    from src.config import CanaryConfig

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

#: Where an armed passive is persisted so it survives a restart (D03.30), or
#: ``None`` before :func:`restore_persisted` names one (a bare import, a CLI
#: tool, a unit test): then arming stays process memory only, as it always was.
_state_path: Path | None = None
#: Whether the state file on disk describes the current arm — set only by a
#: successful write (or a restore from a file that is still there), cleared
#: by a removal or a failed write. ``survives_restart`` reads this, never
#: merely "a path is configured": a swallowed write error must not let
#: ``restart_canary.sh`` promise the arm will be restored.
_persisted: bool = False
#: Schema version of the persisted state file.
PASSIVE_STATE_VERSION = 1
#: File name of the persisted state, beside the daemon's other control state.
PASSIVE_STATE_FILENAME = "passive_state.json"


def passive_state_path(config: CanaryConfig) -> Path:
    """Where the daemon persists an armed passive: the control-state
    directory (``trip_state``'s), beside the trip odometer.

    ``src.main`` asks with the *unscoped* config, before the VIN probe has
    picked a profile, because the probe itself transmits; so on a real start
    the file is not per-vehicle, which is right — passive describes the bus
    and whatever else is plugged into it, not the vehicle. It passes that
    path to ``run_daemon``; a caller that does not (tests, the validation
    harness) gets this function over its own config.
    """
    return config.daemon.trip_state.parent / PASSIVE_STATE_FILENAME


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


def passive_armed() -> bool:
    """The operator's passive switch alone, without session claims.

    :func:`is_passive` is also true while CANary's own K-line reader holds the
    ECM, so it cannot answer "may the K-line reader start?" — this can. Armed
    passive means *nothing* of CANary's transmits, on any bus: it is armed for
    another tool (a scan tool, a reflash), not for CAN alone.
    """
    with _lock:
        return _passive


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


def _persist_locked(enabled: bool, source: str, ttl_seconds: float | None) -> None:
    """Write (armed) or remove (disarmed) the state file. Caller holds ``_lock``.

    Never raises: a state file that cannot be written must not stop the
    operator arming passive — the arm itself holds in memory, and the error
    is logged so a restart that loses it is explained.
    """
    global _persisted
    path = _state_path
    if path is None:
        _persisted = False
        return
    try:
        if enabled:
            payload = {
                "version": PASSIVE_STATE_VERSION,
                "enabled": True,
                "armed_at": time.time(),
                "ttl_seconds": ttl_seconds,
                "source": source,
            }
            atomic_write_text(path, json.dumps(payload, sort_keys=True) + "\n", mkdir=True)
            _persisted = True
        else:
            _persisted = False
            path.unlink(missing_ok=True)
    except OSError:
        _persisted = False
        log.exception("could not persist passive state to %s; it will not survive a restart", path)


def set_passive(
    enabled: bool,
    *,
    source: str = "control",
    ttl_seconds: float | None = None,
    now: float | None = None,
    persist: bool = True,
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

    Once a state file is configured (:func:`restore_persisted`), an arm is
    written to it and a disarm removes it, so the switch survives a daemon
    restart (D03.30). ``persist=False`` is for the restore itself, which must
    keep the original ``armed_at``/``source`` rather than restamp them.
    """
    global _passive, _source, _expires_at
    effective_now = time.monotonic() if now is None else now
    with _lock:
        previous = _passive
        _passive = bool(enabled)
        _source = source
        _expires_at = effective_now + ttl_seconds if enabled and ttl_seconds is not None else None
        if persist:
            _persist_locked(bool(enabled), source, ttl_seconds)
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
        _persist_locked(False, "ttl-expired", None)
    log.warning("passive mode TTL expired; auto-disarmed", extra={"passive": False})
    return True


def state_path() -> Path | None:
    """The configured persistence file, or ``None`` (process memory only)."""
    with _lock:
        return _state_path


def configure_persistence(path: Path | None) -> None:
    """Name the state file without restoring from it (``None`` switches
    persistence off — test reset). :func:`restore_persisted` is what the
    daemon calls; this exists for the reset and for tests."""
    global _state_path, _persisted
    with _lock:
        _state_path = None if path is None else Path(path)
        _persisted = False


#: Source recorded when a state file exists but cannot be trusted (D03.30).
UNTRUSTED_STATE_SOURCE = "restored:untrusted-state"


def _arm_fail_safe(path: Path, cause: str) -> str:
    """A state file exists but cannot be trusted: arm passive with no TTL.

    Failing open here is the 2026-09-24 incident again — the daemon comes up
    transmitting beside a tool the operator armed passive for. The file is
    kept (a disarm through ``/control/passive`` removes it), so every restart
    until then fails safe the same way, and the log names the cause.
    """
    global _persisted
    set_passive(True, source=UNTRUSTED_STATE_SOURCE, persist=False)
    with _lock:
        # The file stays, and the next process arms from it again.
        _persisted = True
    log.error(
        "persisted passive state %s cannot be trusted (%s); passive ARMED with no TTL "
        "until disarmed through /control/passive",
        path,
        cause,
        extra={"passive": True, "source": UNTRUSTED_STATE_SOURCE, "cause": cause},
    )
    return "untrusted"


def restore_persisted(path: Path, *, now_wall: float | None = None) -> str:
    """Adopt ``path`` as the state file and restore an arm persisted there.

    Must run before anything transmits: ``src.main`` calls it before the
    startup VIN probe, ``run_daemon`` before any collector starts. Returns
    what happened, for the caller's log and the tests:

    * ``"absent"`` — no persisted arm; passive is whatever it already was;
    * ``"restored"`` — armed again with the TTL that was left (none if the
      arm had none), source ``restored:<original source>``;
    * ``"expired"`` — a readable file says the TTL ran out while the daemon
      was down; the file is removed and passive stays off;
    * ``"untrusted"`` — a file exists but cannot be read, is not JSON, has an
      unknown version or lacks ``armed_at``: **fails safe**, armed with no
      TTL (:data:`UNTRUSTED_STATE_SOURCE`), the file kept, an ERROR logged.
      Nothing says the other tool has left the bus.

    Wall clock, because monotonic time does not survive a process. A clock
    that stepped *backwards* across the restart cannot lengthen the arm past
    its original TTL (the remainder is clamped to it); one that stepped
    forwards shortens it, which is the one direction this cannot detect.
    Idempotent: ``src.main`` and ``run_daemon`` both call it with the same
    path in production, and the second call re-arms to the same deadline.
    """
    global _persisted
    configure_persistence(path)
    wall = time.time() if now_wall is None else now_wall
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        return _arm_fail_safe(path, f"unreadable: {type(exc).__name__}: {exc}")
    try:
        payload = json.loads(text)
    except ValueError as exc:
        return _arm_fail_safe(path, f"not JSON: {exc}")
    if not isinstance(payload, dict) or payload.get("version") != PASSIVE_STATE_VERSION:
        version = payload.get("version") if isinstance(payload, dict) else None
        return _arm_fail_safe(path, f"unknown state version {version!r}")
    try:
        armed_at = float(payload["armed_at"])
        raw_ttl = payload.get("ttl_seconds")
        ttl = None if raw_ttl is None else float(raw_ttl)
    except (ValueError, KeyError, TypeError) as exc:
        return _arm_fail_safe(path, f"missing or malformed armed_at/ttl_seconds: {exc!r}")
    source = str(payload.get("source") or "unknown")
    restored_source = source if source.startswith("restored:") else f"restored:{source}"
    with _lock:
        armed_by_env = _passive and _source == "env"
    if armed_by_env:
        # ``CANARY_PASSIVE=1`` armed this process with no TTL; a persisted
        # TTL must not shorten that. The file stays for the next start.
        log.info("passive already armed by %s; persisted arm by %s not applied", "env", source)
        return "restored"
    if ttl is None:
        set_passive(True, source=restored_source, persist=False)
        with _lock:
            _persisted = True
        log.warning(
            "passive mode RESTORED after restart (armed by %s with no TTL; stays armed until "
            "disarmed)",
            source,
            extra={"passive": True, "source": restored_source, "ttl_seconds": None},
        )
        return "restored"
    remaining = min(ttl, armed_at + ttl - wall)
    if remaining <= 0:
        with _lock:
            _persist_locked(False, "expired-at-boot", None)
        log.warning(
            "persisted passive arm by %s expired %.0f s before this start; passive stays off",
            source,
            -remaining,
            extra={"passive": False, "source": source, "expired_seconds_ago": round(-remaining, 1)},
        )
        return "expired"
    set_passive(True, source=restored_source, ttl_seconds=remaining, persist=False)
    with _lock:
        _persisted = True
    log.warning(
        "passive mode RESTORED after restart (armed by %s, %.0f s of its %.0f s TTL left)",
        source,
        remaining,
        ttl,
        extra={"passive": True, "source": restored_source, "ttl_seconds": round(remaining, 1)},
    )
    return "restored"


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
            # D03.30: whether an arm is written where the next process will
            # restore it. False while armed means a restart drops it.
            "survives_restart": _passive and _persisted,
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
    "PASSIVE_STATE_FILENAME",
    "PASSIVE_STATE_VERSION",
    "UNTRUSTED_STATE_SOURCE",
    "check_expiry",
    "claim_ecm_session",
    "configure_persistence",
    "passive_state_path",
    "ecm_session_owner",
    "is_passive",
    "passive_armed",
    "release_ecm_session",
    "reset_ecm_session_claims",
    "reset_suppressed",
    "restore_persisted",
    "send",
    "set_passive",
    "snapshot",
    "state_path",
    "suppressed_count",
]
