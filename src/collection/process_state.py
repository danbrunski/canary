"""One reset for every process-global in ``src.collection`` (R07.4).

The collection layer keeps process-wide state on purpose -- the passive gate,
the active capture profile, the bus-budget override, the capture ledger, the
remembered discovery results -- because the daemon has one bus and one
collector. Tests share a process (and an xdist worker) across cases, so that
state leaked: twelve test files reset ``tx_gate`` and ``tuning_profile`` by
hand, each a different subset, only one reset the ECM session claims and only
one reset the bus budget and the capture ledger. Which test saw a stale
passive gate depended on the order the worker happened to run them in.

:func:`reset_process_state_for_tests` puts every one of them back to its
start-up value, through each module's own public (or ``_reset_for_tests``)
function; ``tests/conftest.py`` calls it around every test.
:data:`COVERED_MODULES` names the modules it covers, and
``tests/test_process_state_reset.py`` fails when a collection module gains a
``global`` statement without appearing there.

Deliberately not reset (derived caches, keyed so a stale entry cannot be
wrong): ``capture_profiles._SERVING_DID_CACHE`` and ``_SERVING_MODE01_CACHE``
(path + mtime) and the
``lru_cache`` loaders in ``obd_scheduler.spec`` (static catalogue files).
"""

from __future__ import annotations

from src.collection import (
    bus_budget,
    capture_generation,
    capture_lifetime,
    capture_profiles,
    discovery,
    queueing,
    tuning_profile,
    tx_gate,
)
from src.collection.kline.sessions import MODULE_SESSIONS

#: Every collection module holding process-global state this function resets,
#: as ``src/collection``-relative paths.
COVERED_MODULES: frozenset[str] = frozenset(
    {
        "bus_budget.py",
        "capture_generation.py",
        "capture_lifetime.py",
        "capture_profiles.py",
        "discovery.py",
        "kline/sessions.py",
        "queueing.py",
        "tuning_profile.py",
        "tx_gate.py",
    }
)

#: The module-level mutable containers and instances among that state, as
#: ``(path, name)``. ``tests/test_process_state_reset.py`` fails on any
#: module-level mutable assignment in ``src/collection`` that is neither here
#: nor in its reasoned allowlist of read-only tables.
COVERED_GLOBALS: frozenset[tuple[str, str]] = frozenset(
    {
        ("capture_lifetime.py", "_ledger"),
        ("discovery.py", "_remembered"),
        ("kline/sessions.py", "MODULE_SESSIONS"),
        ("queueing.py", "_CAN_FRAME_DROP_LOGS"),
        ("queueing.py", "_UNATTRIBUTED_QUEUE_DROPS"),
        ("tuning_profile.py", "_did_channels"),
        ("tuning_profile.py", "_profile"),
        ("tx_gate.py", "_claims"),
    }
)


def reset_process_state_for_tests() -> None:
    """Return every collection process-global to its start-up value.

    Order matters: disarming the gate bumps the capture generation and marks
    the capture ledger, so those two are reset last.
    """
    # Persistence off first, so the disarm below does not delete a state
    # file another test (or the daemon it started) wrote (D03.30).
    tx_gate.configure_persistence(None)
    tx_gate.set_passive(False, source="default")
    tx_gate.reset_ecm_session_claims()
    tx_gate.reset_suppressed()
    bus_budget.reset()
    tuning_profile._reset_for_tests()
    discovery.reset_discovery()
    queueing._reset_for_tests()
    MODULE_SESSIONS.reset()
    capture_lifetime._reset_for_tests()
    capture_generation._reset_for_tests()
    capture_profiles._reset_for_tests()


__all__ = ["COVERED_GLOBALS", "COVERED_MODULES", "reset_process_state_for_tests"]
