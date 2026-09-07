"""Process-wide capture generation counter (roadmap task C02.8).

A *generation* is one uninterrupted stretch of polling under one set of
conditions. Anything that changes what "the same round" could mean starts a
new one: a capture profile activation, pause/resume, the passive-mode gate,
a collector (re)start after a bus failure, and a replay starting or stopping.

The collector loop reads :func:`current` once per tick and, when it has
moved, discards its round state and restarts round numbering. A request
issued under generation *g* is tagged *g* even when its response lands after
the bump — the raw evidence is kept, but it can never complete a tuple in the
capture that started afterwards.

Generations are integers, monotonic within one daemon process and scoped to
that process: a restart begins again at one. Round identity is therefore
``(session, generation, round_id)``; the session boundary is what separates
two processes' generation ``1``.

This module deliberately imports nothing from the rest of ``src`` so the
hooks (``tuning_profile``, ``tx_gate``, the replay controller, the collector)
can all call it without an import cycle.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger(__name__)

_lock = threading.Lock()
_generation = 0
_reason = "startup"


def current() -> int:
    """The generation in force right now (``0`` before any collector started)."""
    with _lock:
        return _generation


def bump(reason: str) -> int:
    """Start a new generation and return it. ``reason`` lands in the log."""
    global _generation, _reason
    with _lock:
        _generation += 1
        _reason = reason
        generation = _generation
    log.info("capture generation %d: %s", generation, reason, extra={"generation": generation})
    return generation


def snapshot() -> dict[str, Any]:
    with _lock:
        return {"generation": _generation, "reason": _reason}


def _reset_for_tests() -> None:
    global _generation, _reason
    with _lock:
        _generation = 0
        _reason = "startup"


__all__ = ["bump", "current", "snapshot"]
